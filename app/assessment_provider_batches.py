import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifact_gateway import get_preferred_text_source
from app.assessment_execution import (
    _records,
    _skill_version,
    _step,
    persist_document_analysis,
)
from app.config import Settings
from app.document_evidence import (
    build_document_map_plan,
    render_model_document,
    segment_paragraphs,
    validate_document_analysis_citations,
)
from app.execution_accounting import ProviderUsageContext, persist_model_invocations, refresh_skill_run_usage
from app.model_batching import BatchModelRequest, get_batch_adapter, resolve_batch_target
from app.model_execution import (
    InstructionLayer,
    InvocationTelemetry,
    StructuredModelRequest,
    assemble_structured_prompt,
    validate_structured_output,
)
from app.models import (
    MatterDefinitionAssessmentProviderBatch,
    MatterDocument,
    ReviewBatchRunDocument,
    SkillRun,
)
from app.skill_execution import (
    PLATFORM_SKILL_INSTRUCTIONS,
    complete_skill_run,
    create_skill_run,
    fail_skill_run,
)

TERMINAL_PROVIDER_STATES = {
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_PARTIALLY_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
}
SUCCESS_PROVIDER_STATES = {"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"}
# Gemini inline batches are limited to 20 MB. Leave ample room for the
# instructions, JSON schema, and request envelope added during submission.
PROVIDER_BATCH_MAX_ESTIMATED_BYTES = 12_000_000
PROVIDER_BATCH_REQUEST_OVERHEAD_BYTES = 50_000
logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def plan_provider_batches(
    db: Session,
    assessment_id: uuid.UUID,
    document_ids: list[uuid.UUID],
    settings: Settings,
) -> dict[str, Any]:
    assessment, _, _, matter = _records(db, assessment_id)
    if not assessment.use_batching:
        return {
            "provider_batch_ids": [],
            "realtime_document_ids": [str(value) for value in document_ids],
            "poll_seconds": settings.definition_assessment_provider_batch_poll_seconds,
        }
    if assessment.review_batch_run_id is None:
        raise ValueError("Assessment batch run has not been created")
    document_ids = [
        document_id
        for document_id in document_ids
        if (
            run_document := db.get(
                ReviewBatchRunDocument,
                (assessment.review_batch_run_id, document_id),
            )
        )
        is not None
        and run_document.status not in {"COMPLETED", "SKIPPED"}
    ]
    resolved_models = assessment.configuration_snapshot.get("resolved_models")
    model_key = resolved_models.get("document_analysis") if isinstance(resolved_models, dict) else None
    if not isinstance(model_key, str):
        return {
            "provider_batch_ids": [],
            "realtime_document_ids": [str(value) for value in document_ids],
            "poll_seconds": settings.definition_assessment_provider_batch_poll_seconds,
        }
    target_resolution = resolve_batch_target(model_key)
    if target_resolution is None:
        # Providers without a registered Batch API adapter remain fully
        # supported through the existing real-time structured-model path.
        return {
            "provider_batch_ids": [],
            "realtime_document_ids": [str(value) for value in document_ids],
            "poll_seconds": settings.definition_assessment_provider_batch_poll_seconds,
        }
    _, target = target_resolution
    all_existing = list(
        db.scalars(
            select(MatterDefinitionAssessmentProviderBatch)
            .where(
                MatterDefinitionAssessmentProviderBatch.assessment_run_id == assessment.id,
                MatterDefinitionAssessmentProviderBatch.review_batch_run_id == assessment.review_batch_run_id,
            )
            .order_by(MatterDefinitionAssessmentProviderBatch.ordinal)
        )
    )
    existing = [batch for batch in all_existing if batch.status in {"QUEUED", "SUBMITTED", "RUNNING"}]
    if existing:
        batched = {uuid.UUID(value) for batch in existing for value in batch.document_ids}
        return {
            "provider_batch_ids": [str(batch.id) for batch in existing],
            "realtime_document_ids": [str(value) for value in document_ids if value not in batched],
            "poll_seconds": settings.definition_assessment_provider_batch_poll_seconds,
        }

    eligible: list[tuple[uuid.UUID, int]] = []
    realtime: list[uuid.UUID] = []
    for document_id in document_ids:
        document = db.get(MatterDocument, document_id)
        if document is None:
            realtime.append(document_id)
            continue
        source = get_preferred_text_source(
            collection_item_id=document.collection_item_id,
            actor_user_id=assessment.initiated_by_user_id,
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
        )
        if source is None:
            realtime.append(document_id)
            continue
        paragraph_map = segment_paragraphs(source.text)
        plan = build_document_map_plan(
            paragraph_map,
            max_characters=settings.definition_assessment_map_max_characters,
        )
        if paragraph_map.paragraphs and len(plan.windows) == 1:
            estimated_bytes = len(source.text.encode("utf-8")) + PROVIDER_BATCH_REQUEST_OVERHEAD_BYTES
            eligible.append((document_id, estimated_bytes))
        else:
            realtime.append(document_id)

    provider_batches: list[MatterDefinitionAssessmentProviderBatch] = []
    batch_size = settings.definition_assessment_provider_batch_size
    grouped: list[list[uuid.UUID]] = []
    current_group: list[uuid.UUID] = []
    current_bytes = 0
    for document_id, estimated_bytes in eligible:
        if current_group and (
            len(current_group) >= batch_size
            or current_bytes + estimated_bytes > PROVIDER_BATCH_MAX_ESTIMATED_BYTES
        ):
            grouped.append(current_group)
            current_group = []
            current_bytes = 0
        if estimated_bytes > PROVIDER_BATCH_MAX_ESTIMATED_BYTES:
            realtime.append(document_id)
            continue
        current_group.append(document_id)
        current_bytes += estimated_bytes
    if current_group:
        grouped.append(current_group)

    for document_group in grouped:
        batch = MatterDefinitionAssessmentProviderBatch(
            assessment_run_id=assessment.id,
            review_batch_run_id=assessment.review_batch_run_id,
            ordinal=max((batch.ordinal for batch in all_existing), default=0) + len(provider_batches) + 1,
            document_ids=[str(value) for value in document_group],
            status="QUEUED",
            provider=target.provider,
            model=target.model,
            request_count=0,
        )
        db.add(batch)
        provider_batches.append(batch)
    db.commit()
    return {
        "provider_batch_ids": [str(batch.id) for batch in provider_batches],
        "realtime_document_ids": [str(value) for value in realtime],
        "poll_seconds": settings.definition_assessment_provider_batch_poll_seconds,
    }


def _structured_request(assessment, workflow, revision, version, document, source, paragraph_map):
    stable_context = {"matter_definition": revision.content_markdown}
    dynamic_input = {
        "analysis_stage": "DOCUMENT",
        "document": {
            "matter_document_id": str(document.id),
            "collection_item_id": str(document.collection_item_id),
            "source_artifact_id": str(source.artifact_id),
            "source_hash": source.content_hash,
            "text": render_model_document(paragraph_map),
        },
        "paragraph_map": paragraph_map.as_dict()["paragraphs"],
    }
    cache_identity = {
        "tenant_id": str(workflow.tenant_id),
        "matter_id": str(assessment.matter_id),
        "revision_hash": assessment.definition_content_hash,
        "skill_version_id": str(version.id),
        "schema_version": version.output_schema_key,
        "paragraph_map_version": paragraph_map.version,
    }
    return StructuredModelRequest(
        instruction_layers=(
            InstructionLayer("platform_security", PLATFORM_SKILL_INSTRUCTIONS),
            InstructionLayer("managed_skill", version.instructions),
        ),
        stable_context=stable_context,
        dynamic_input=dynamic_input,
        output_schema=version.output_schema,
        model_key=version.model_key,
        model_settings=version.model_policy,
        limits=version.limits,
        cache_policy=version.cache_policy,
        cache_identity=cache_identity,
        request_type="document-analysis",
    )


def submit_provider_batch(db: Session, batch_id: uuid.UUID, settings: Settings) -> str:
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    if batch is None:
        raise ValueError("Assessment provider batch not found")
    if batch.provider_batch_id:
        return batch.provider_status or "JOB_STATE_QUEUED"
    assessment, workflow, revision, _ = _records(db, batch.assessment_run_id)
    step = _step(db, workflow, ordinal=3, role_key="document_analysis", total_count=assessment.selected_count)
    step.status = "RUNNING"
    step.started_at = step.started_at or utcnow()
    version = _skill_version(db, assessment, "document_analysis")
    resolved_models = assessment.configuration_snapshot.get("resolved_models")
    selected_model = resolved_models.get("document_analysis") if isinstance(resolved_models, dict) else None
    if not isinstance(selected_model, str):
        raise TypeError("Assessment does not have a pinned document-analysis model")
    requests: list[BatchModelRequest] = []
    manifest: list[dict[str, str]] = []
    for raw_document_id in batch.document_ids:
        document_id = uuid.UUID(raw_document_id)
        document = db.get(MatterDocument, document_id)
        run_document = db.get(ReviewBatchRunDocument, (batch.review_batch_run_id, document_id))
        if document is None or run_document is None:
            raise ValueError("Assessment provider batch references a missing document")
        source = get_preferred_text_source(
            collection_item_id=document.collection_item_id,
            actor_user_id=assessment.initiated_by_user_id,
            tenant_id=workflow.tenant_id,
            client_id=workflow.client_id,
        )
        if source is None:
            raise ValueError("An eligible batch document no longer has a text source")
        paragraph_map = segment_paragraphs(source.text)
        request = _structured_request(assessment, workflow, revision, version, document, source, paragraph_map)
        assembly = assemble_structured_prompt(request, resolved_model=selected_model)
        skill_run = create_skill_run(
            db,
            workflow=workflow,
            step=step,
            skill_version=version,
            scope_type="MATTER_DOCUMENT",
            scope_id=document.id,
            request_input={"stable_context": request.stable_context, "dynamic_input": request.dynamic_input},
        )
        skill_run.cache_fingerprint = assembly.cache_fingerprint
        run_document.status = "IN_PROGRESS"
        custom_id = str(document.id)
        requests.append(
            BatchModelRequest(
                custom_id=custom_id,
                prompt=f"{assembly.stable_prefix}\n{assembly.dynamic_payload}",
                instructions=assembly.instructions,
                output_schema=version.output_schema,
                model_settings=version.model_policy,
                limits=version.limits,
            )
        )
        manifest.append(
            {
                "custom_id": custom_id,
                "document_id": str(document.id),
                "skill_run_id": str(skill_run.id),
                "model_configuration_hash": assembly.model_configuration_hash,
            }
        )
    db.flush()
    submission = get_batch_adapter(batch.provider).submit(
        model=batch.model,
        requests=requests,
        display_name=f"assessment-{assessment.id}-{batch.review_batch_run_id}-{batch.ordinal}",
    )
    batch.provider_batch_id = submission.batch_id
    batch.provider_status = submission.status
    batch.status = "SUBMITTED"
    batch.request_count = len(requests)
    batch.manifest = {"requests": manifest}
    batch.submitted_at = utcnow()
    db.commit()
    return batch.provider_status


def poll_provider_batch(db: Session, batch_id: uuid.UUID) -> str:
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    if batch is None or not batch.provider_batch_id:
        raise ValueError("Assessment provider batch has not been submitted")
    result = get_batch_adapter(batch.provider).poll(batch.provider_batch_id)
    state = result.status
    batch.provider_status = state
    batch.last_polled_at = utcnow()
    if state in {"JOB_STATE_QUEUED", "JOB_STATE_PENDING"}:
        batch.status = "SUBMITTED"
    elif state not in TERMINAL_PROVIDER_STATES:
        batch.status = "RUNNING"
    elif state not in SUCCESS_PROVIDER_STATES:
        batch.status = "FAILED"
        batch.error_message = str(result.error or f"Provider batch ended in {state}")[:4000]
        batch.completed_at = utcnow()
    db.commit()
    return state


def finalize_provider_batch(db: Session, batch_id: uuid.UUID) -> dict[str, int]:
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    if batch is None or not batch.provider_batch_id:
        raise ValueError("Assessment provider batch has not been submitted")
    assessment, workflow, revision, matter = _records(db, batch.assessment_run_id)
    version = _skill_version(db, assessment, "document_analysis")
    state, responses = get_batch_adapter(batch.provider).results(batch.provider_batch_id)
    if state not in SUCCESS_PROVIDER_STATES:
        raise ValueError(f"Assessment provider batch is not ready: {state}")
    responses_by_id = {
        item.custom_id: item
        for item in responses
        if item.custom_id
    }
    manifest = batch.manifest.get("requests", []) if isinstance(batch.manifest, dict) else []
    completed = 0
    failed = 0
    for item in manifest:
        document_id = uuid.UUID(item["document_id"])
        skill_run = db.get(SkillRun, uuid.UUID(item["skill_run_id"]))
        run_document = db.get(ReviewBatchRunDocument, (batch.review_batch_run_id, document_id))
        document = db.get(MatterDocument, document_id)
        response_item = responses_by_id.get(item["custom_id"])
        try:
            if skill_run is None or run_document is None or document is None:
                raise ValueError("Assessment batch result references a missing record")
            if response_item is None:
                raise ValueError("Gemini Batch API returned no result for the document")
            if response_item.error:
                raise ValueError(response_item.error)
            if not response_item.output_text:
                raise ValueError("Batch API returned an empty document analysis")
            output = json.loads(response_item.output_text)
            validate_structured_output(output, version.output_schema)
            source = get_preferred_text_source(
                collection_item_id=document.collection_item_id,
                actor_user_id=assessment.initiated_by_user_id,
                tenant_id=workflow.tenant_id,
                client_id=workflow.client_id,
            )
            if source is None:
                raise ValueError("Document text source is unavailable while finalizing its batch result")
            paragraph_map = segment_paragraphs(source.text)
            validate_document_analysis_citations(output, paragraph_map)
            now = utcnow()
            persist_model_invocations(
                db,
                (
                    InvocationTelemetry(
                        request_sequence=1,
                        provider_request_id=response_item.provider_request_id,
                        provider=batch.provider,
                        model=batch.model,
                        model_configuration_hash=item["model_configuration_hash"],
                        request_count=1,
                        input_tokens=response_item.input_tokens,
                        cached_input_tokens=response_item.cached_input_tokens,
                        cache_write_tokens=0,
                        output_tokens=response_item.output_tokens,
                        latency_ms=0,
                        started_at=batch.submitted_at or now,
                        completed_at=now,
                    ),
                ),
                skill_run_id=skill_run.id,
                usage_context=ProviderUsageContext(
                    tenant_id=workflow.tenant_id,
                    client_id=workflow.client_id,
                    matter_id=workflow.matter_id,
                    started_by_user_id=workflow.initiated_by_user_id,
                    job_type="MATTER_DEFINITION_ASSESSMENT",
                    job_id=workflow.id,
                    job_created_at=workflow.created_at,
                    details={
                        "workflow_key": workflow.workflow_key,
                        "role_key": "document_analysis",
                        "provider_batch_id": batch.provider_batch_id,
                    },
                ),
            )
            complete_skill_run(db, skill_run)
            persist_document_analysis(
                db,
                assessment=assessment,
                matter=matter,
                revision=revision,
                document=document,
                run_document=run_document,
                version=version,
                source=source,
                paragraph_map=paragraph_map,
                output=output,
                skill_run=skill_run,
            )
            completed += 1
        except Exception as exc:  # noqa: BLE001 - one invalid provider result must not discard the batch
            if skill_run is not None:
                fail_skill_run(skill_run, exc)
                refresh_skill_run_usage(db, skill_run)
            if run_document is not None:
                run_document.status = "FAILED"
                run_document.completed_at = utcnow()
            failed += 1
    batch.provider_status = state
    batch.status = "COMPLETED_WITH_ERRORS" if failed else "COMPLETED"
    batch.completed_at = utcnow()
    batch.error_message = f"{failed} document analyses failed" if failed else None
    db.commit()
    return {"completed": completed, "failed": failed}


def fail_provider_batch(db: Session, batch_id: uuid.UUID, message: str) -> None:
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    if batch is None:
        return
    batch.status = "FAILED"
    batch.error_message = message[:4000]
    batch.completed_at = utcnow()
    manifest = batch.manifest.get("requests", []) if isinstance(batch.manifest, dict) else []
    for item in manifest:
        skill_run_id = item.get("skill_run_id") if isinstance(item, dict) else None
        if not skill_run_id:
            continue
        skill_run = db.get(SkillRun, uuid.UUID(skill_run_id))
        if skill_run is not None and skill_run.status == "RUNNING":
            fail_skill_run(skill_run, RuntimeError(message))
            refresh_skill_run_usage(db, skill_run)
    for document_id in (uuid.UUID(value) for value in batch.document_ids):
        run_document = db.get(ReviewBatchRunDocument, (batch.review_batch_run_id, document_id))
        if run_document is not None and run_document.status not in {"COMPLETED", "SKIPPED"}:
            run_document.status = "FAILED"
            run_document.completed_at = utcnow()
    db.commit()


def cancel_provider_batches(db: Session, assessment_id: uuid.UUID) -> None:
    batches = list(
        db.scalars(
            select(MatterDefinitionAssessmentProviderBatch).where(
                MatterDefinitionAssessmentProviderBatch.assessment_run_id == assessment_id,
                MatterDefinitionAssessmentProviderBatch.status.in_(("QUEUED", "SUBMITTED", "RUNNING")),
            )
        )
    )
    for batch in batches:
        if batch.provider_batch_id:
            try:
                get_batch_adapter(batch.provider).cancel(batch.provider_batch_id)
            except Exception:
                logger.warning(
                    "Could not cancel assessment provider batch batch_id=%s provider_batch_id=%s",
                    batch.id,
                    batch.provider_batch_id,
                    exc_info=True,
                )
        batch.status = "CANCELED"
        batch.completed_at = utcnow()
    db.commit()
