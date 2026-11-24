"""Provider Batch API transport for Privilege Review evidence-extraction units."""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.assessment_provider_batches import (
    PROVIDER_BATCH_MAX_ESTIMATED_BYTES,
    PROVIDER_BATCH_REQUEST_OVERHEAD_BYTES,
    SUCCESS_PROVIDER_STATES,
    TERMINAL_PROVIDER_STATES,
)
from app.config import Settings
from app.evidence_first_artifacts import EvidenceExtractionUnitResult, EvidenceUnitAdjustment
from app.evidence_first_launcher import (
    _shadow_workflow,
    finalize_prepared_evidence_shadow,
    initialize_prepared_evidence_shadow,
)
from app.evidence_first_managed import ManagedEvidenceUnitExtractor
from app.evidence_first_shadow import WorkflowEvidenceShadowArtifactRepository
from app.execution_accounting import ProviderUsageContext, persist_model_invocations, refresh_skill_run_usage
from app.model_batching import BatchModelRequest, get_batch_adapter, resolve_batch_target
from app.model_execution import (
    InstructionLayer,
    InvocationTelemetry,
    StructuredModelRequest,
    assemble_structured_prompt,
)
from app.models import (
    MatterDefinitionAssessmentProviderBatch,
    MatterDefinitionAssessmentRun,
    ModelInvocation,
    ReviewBatchRunDocument,
    ReviewProgramRun,
    SkillRun,
    WorkflowRun,
    utcnow,
)
from app.skill_execution import (
    PLATFORM_SKILL_INSTRUCTIONS,
    complete_skill_run,
    create_skill_run,
    fail_skill_run,
)
from app.workflows.evidence_assessments import _prepared_extraction

logger = logging.getLogger(__name__)

PRIVILEGE_BATCH_MANIFEST_MODE = "PRIVILEGE_FAMILY_EVIDENCE_UNITS_V1"
PRIVILEGE_BATCH_FINALIZATION_CHECKPOINT_SIZE = 10


def _refresh_transport_progress(db: Session, assessment: MatterDefinitionAssessmentRun) -> None:
    batches = list(
        db.scalars(
            select(MatterDefinitionAssessmentProviderBatch).where(
                MatterDefinitionAssessmentProviderBatch.assessment_run_id == assessment.id
            )
        )
    )
    counts: dict[str, int] = defaultdict(int)
    for batch in batches:
        if isinstance(batch.manifest, dict) and batch.manifest.get("mode") == PRIVILEGE_BATCH_MANIFEST_MODE:
            counts[batch.status] += 1
    total = sum(counts.values())
    terminal = sum(
        counts.get(status, 0)
        for status in ("COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED")
    )
    active = sum(counts.get(status, 0) for status in ("QUEUED", "SUBMITTED", "RUNNING"))
    transport_state = (
        "WAITING_FOR_PROVIDER_BATCH"
        if active
        else "PROVIDER_BATCHES_FINALIZED"
        if total
        else "REALTIME_ONLY"
    )
    workflow = db.get(WorkflowRun, assessment.workflow_run_id)
    if workflow is not None:
        workflow.current_stage = transport_state if active else "EVIDENCE_EXTRACTION"
        workflow.progress = {
            **(workflow.progress or {}),
            "transport_state": transport_state,
            "provider_batch_count": total,
            "provider_batch_completed_count": terminal,
            "provider_batch_active_count": active,
        }
    raw_program_run_id = assessment.configuration_snapshot.get("privilege_review_program_run_id")
    if raw_program_run_id:
        program_run = db.get(ReviewProgramRun, uuid.UUID(str(raw_program_run_id)))
        if program_run is not None:
            evidence = dict((program_run.progress_summary or {}).get("privilege_evidence") or {})
            program_run.progress_summary = {
                **(program_run.progress_summary or {}),
                "privilege_evidence": {
                    **evidence,
                    "transport": "PROVIDER_BATCH",
                    "provider": next(
                        (batch.provider for batch in batches if batch.provider),
                        None,
                    ),
                    "transport_state": transport_state,
                    "provider_batch_count": total,
                    "provider_batch_completed_count": terminal,
                    "provider_batch_active_count": active,
                },
            }


def _resolved_extraction_model(assessment: MatterDefinitionAssessmentRun) -> str | None:
    orchestration = assessment.configuration_snapshot.get("evidence_first_orchestration")
    stages = orchestration.get("stages") if isinstance(orchestration, dict) else None
    extraction = stages.get("EXTRACTION") if isinstance(stages, dict) else None
    model = extraction.get("resolved_model") if isinstance(extraction, dict) else None
    return model if isinstance(model, str) else None


def _estimate_prepared_document(prepared) -> tuple[int, int]:
    value = prepared.documents[0]
    context_bytes = len(
        json.dumps(
            value.document_context.as_model_input() if value.document_context is not None else [],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    stable_bytes = len(prepared.revision.content_markdown.encode("utf-8")) + len(
        json.dumps(prepared.catalog.model_dump(mode="json"), separators=(",", ":")).encode("utf-8")
    )
    source_bytes = len(value.source.text.encode("utf-8"))
    unit_count = len(value.plan.extraction_units)
    # Source text is partitioned across units while the stable/context portions are
    # repeated in provider batch requests. The fixed overhead covers schemas and
    # the request envelope.
    estimated_bytes = source_bytes + unit_count * (
        context_bytes + stable_bytes + PROVIDER_BATCH_REQUEST_OVERHEAD_BYTES
    )
    return unit_count, estimated_bytes


def plan_privilege_evidence_batches(
    db: Session,
    *,
    assessment_id: uuid.UUID,
    document_ids: list[uuid.UUID],
    actor_user_id: uuid.UUID,
    settings: Settings,
) -> dict[str, Any]:
    assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
    if assessment is None or assessment.review_batch_run_id is None:
        raise ValueError("Privilege evidence assessment is unavailable")
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
    model_key = _resolved_extraction_model(assessment)
    target_resolution = resolve_batch_target(model_key) if assessment.use_batching and model_key else None
    if target_resolution is None:
        return {"provider_batch_ids": [], "realtime_document_ids": [str(value) for value in document_ids]}
    _, target = target_resolution

    existing = list(
        db.scalars(
            select(MatterDefinitionAssessmentProviderBatch)
            .where(
                MatterDefinitionAssessmentProviderBatch.assessment_run_id == assessment.id,
                MatterDefinitionAssessmentProviderBatch.review_batch_run_id == assessment.review_batch_run_id,
            )
            .order_by(MatterDefinitionAssessmentProviderBatch.ordinal)
        )
    )
    privilege_existing = [
        batch
        for batch in existing
        if isinstance(batch.manifest, dict)
        and batch.manifest.get("mode") == PRIVILEGE_BATCH_MANIFEST_MODE
        and batch.status in {"QUEUED", "SUBMITTED", "RUNNING"}
    ]
    if privilege_existing:
        batched = {value for batch in privilege_existing for value in batch.document_ids}
        return {
            "provider_batch_ids": [str(batch.id) for batch in privilege_existing],
            "realtime_document_ids": [str(value) for value in document_ids if str(value) not in batched],
        }

    request_limit = settings.definition_assessment_provider_batch_size
    grouped: list[list[uuid.UUID]] = []
    realtime: list[uuid.UUID] = []
    current: list[uuid.UUID] = []
    current_requests = 0
    current_bytes = 0
    for document_id in document_ids:
        try:
            prepared = _prepared_extraction(
                db,
                assessment_id=str(assessment_id),
                document_id=str(document_id),
                actor_user_id=str(actor_user_id),
            )
            unit_count, estimated_bytes = _estimate_prepared_document(prepared)
        except Exception as exc:  # noqa: BLE001 - the durable document path records preparation failures.
            logger.warning(
                "Privilege evidence document is not provider-batch eligible assessment_id=%s document_id=%s: %s",
                assessment_id,
                document_id,
                exc,
            )
            realtime.append(document_id)
            continue
        if (
            unit_count < 1
            or unit_count > request_limit
            or estimated_bytes > PROVIDER_BATCH_MAX_ESTIMATED_BYTES
        ):
            realtime.append(document_id)
            continue
        if current and (
            current_requests + unit_count > request_limit
            or current_bytes + estimated_bytes > PROVIDER_BATCH_MAX_ESTIMATED_BYTES
        ):
            grouped.append(current)
            current = []
            current_requests = 0
            current_bytes = 0
        current.append(document_id)
        current_requests += unit_count
        current_bytes += estimated_bytes
    if current:
        grouped.append(current)

    next_ordinal = max((batch.ordinal for batch in existing), default=0)
    provider_batches: list[MatterDefinitionAssessmentProviderBatch] = []
    for index, group in enumerate(grouped, start=1):
        batch = MatterDefinitionAssessmentProviderBatch(
            assessment_run_id=assessment.id,
            review_batch_run_id=assessment.review_batch_run_id,
            ordinal=next_ordinal + index,
            document_ids=[str(value) for value in group],
            status="QUEUED",
            provider=target.provider,
            model=target.model,
            request_count=0,
            manifest={"mode": PRIVILEGE_BATCH_MANIFEST_MODE},
        )
        db.add(batch)
        provider_batches.append(batch)
    db.flush()
    _refresh_transport_progress(db, assessment)
    db.commit()
    return {
        "provider_batch_ids": [str(batch.id) for batch in provider_batches],
        "realtime_document_ids": [str(value) for value in realtime],
    }


def _extractor(db: Session, prepared, workflow_run_id: uuid.UUID) -> tuple[Any, Any, Any, ManagedEvidenceUnitExtractor]:
    workflow, step = _shadow_workflow(
        db,
        prepared,
        retry_failed=False,
        workflow_run_id=workflow_run_id,
    )
    value = prepared.documents[0]
    extractor = ManagedEvidenceUnitExtractor(
        db=db,
        workflow=workflow,
        step=step,
        skill_version=prepared.skill_version,
        review_guidance=prepared.revision.content_markdown,
        catalog=prepared.catalog,
        source_text=value.source.text,
        source_artifact_id=value.source.artifact_id,
        source_content_hash=value.source.content_hash,
        document_context=value.document_context,
        resolved_model=prepared.resolved_model,
        model_settings=prepared.model_settings,
        refresh_workflow_usage=False,
        checkpoint_before_model_call=True,
    )
    return workflow, step, value, extractor


def _structured_request(prepared, extractor: ManagedEvidenceUnitExtractor, plan, unit, *, run_id: str):
    model_call = extractor.prepare_model_call(plan, unit)
    return StructuredModelRequest(
        instruction_layers=(
            InstructionLayer("platform_security", PLATFORM_SKILL_INSTRUCTIONS),
            InstructionLayer("managed_skill", prepared.skill_version.instructions),
        ),
        stable_context=model_call.stable_context,
        dynamic_input=model_call.dynamic_input,
        output_schema=model_call.application_schema,
        provider_output_schema=model_call.provider_schema,
        model_key=prepared.skill_version.model_key,
        model_settings=prepared.model_settings,
        limits=prepared.skill_version.limits,
        cache_policy=prepared.skill_version.cache_policy,
        cache_identity=model_call.cache_identity,
        request_type="evidence-extraction",
        run_id=run_id,
    )


def _prepare_batch_requests(
    db: Session,
    *,
    batch: MatterDefinitionAssessmentProviderBatch,
    actor_user_id: uuid.UUID,
) -> tuple[list[BatchModelRequest], list[dict[str, str]]]:
    existing_manifest = batch.manifest if isinstance(batch.manifest, dict) else {}
    existing_entries = {
        (str(item.get("document_id")), str(item.get("unit_id"))): item
        for item in existing_manifest.get("requests", [])
        if isinstance(item, dict)
    }
    requests: list[BatchModelRequest] = []
    manifest: list[dict[str, str]] = []
    for raw_document_id in batch.document_ids:
        prepared = _prepared_extraction(
            db,
            assessment_id=str(batch.assessment_run_id),
            document_id=raw_document_id,
            actor_user_id=str(actor_user_id),
        )
        existing_workflow_ids = {
            str(item.get("workflow_run_id"))
            for (document_id, _), item in existing_entries.items()
            if document_id == raw_document_id and item.get("workflow_run_id")
        }
        if len(existing_workflow_ids) > 1:
            raise ValueError("Privilege provider batch has conflicting document workflow IDs")
        if existing_workflow_ids:
            workflow_run_id = uuid.UUID(next(iter(existing_workflow_ids)))
        else:
            initialized = initialize_prepared_evidence_shadow(db, prepared)
            workflow_run_id = uuid.UUID(initialized["workflow_run_id"])
        workflow, step, value, extractor = _extractor(db, prepared, workflow_run_id)
        for unit in value.plan.extraction_units:
            key = (raw_document_id, unit.unit_id)
            prior = existing_entries.get(key)
            if prior is None:
                model_call = extractor.prepare_model_call(value.plan, unit)
                skill_run = create_skill_run(
                    db,
                    workflow=workflow,
                    step=step,
                    skill_version=prepared.skill_version,
                    scope_type="MATTER_DOCUMENT",
                    scope_id=value.document.id,
                    request_input={
                        "stable_context": model_call.stable_context,
                        "dynamic_input": model_call.dynamic_input,
                    },
                    output_schema=model_call.application_schema,
                    limits=prepared.skill_version.limits,
                    model_settings=prepared.model_settings,
                )
                custom_id = str(skill_run.id)
            else:
                custom_id = str(prior["custom_id"])
                skill_run = db.get(SkillRun, uuid.UUID(str(prior["skill_run_id"])))
                if skill_run is None:
                    raise ValueError("Privilege provider batch skill run is missing")
            request = _structured_request(
                prepared,
                extractor,
                value.plan,
                unit,
                run_id=custom_id,
            )
            assembly = assemble_structured_prompt(request, resolved_model=prepared.resolved_model)
            skill_run.cache_fingerprint = assembly.cache_fingerprint
            requests.append(
                BatchModelRequest(
                    custom_id=custom_id,
                    prompt=f"{assembly.stable_prefix}\n{assembly.dynamic_payload}",
                    instructions=assembly.instructions,
                    output_schema=request.output_schema,
                    model_settings=assembly.model_settings,
                    limits=request.limits,
                )
            )
            manifest.append(
                {
                    "custom_id": custom_id,
                    "document_id": raw_document_id,
                    "workflow_run_id": str(workflow_run_id),
                    "unit_id": unit.unit_id,
                    "plan_hash": value.plan.plan_hash,
                    "skill_run_id": str(skill_run.id),
                    "model_configuration_hash": assembly.model_configuration_hash,
                }
            )
        run_document = db.get(
            ReviewBatchRunDocument,
            (batch.review_batch_run_id, uuid.UUID(raw_document_id)),
        )
        if run_document is not None and run_document.status not in {"COMPLETED", "SKIPPED"}:
            run_document.status = "IN_PROGRESS"
        batch.manifest = {
            **existing_manifest,
            "mode": PRIVILEGE_BATCH_MANIFEST_MODE,
            "requests": list(manifest),
        }
        batch.request_count = len(manifest)
        db.commit()
    return requests, manifest


def submit_privilege_evidence_batch(
    db: Session,
    *,
    batch_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> str:
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    if batch is None:
        raise ValueError("Privilege evidence provider batch is unavailable")
    if batch.provider_batch_id:
        return batch.provider_status or "JOB_STATE_QUEUED"
    display_name = f"privilege-evidence-{batch.id}"
    adapter = get_batch_adapter(batch.provider)
    current_manifest = batch.manifest if isinstance(batch.manifest, dict) else {}
    if current_manifest.get("display_name") == display_name:
        found = adapter.find(display_name)
        if found is not None:
            batch.provider_batch_id = found.batch_id
            batch.provider_status = found.status
            batch.status = "SUBMITTED"
            batch.submitted_at = batch.submitted_at or utcnow()
            assessment = db.get(MatterDefinitionAssessmentRun, batch.assessment_run_id)
            if assessment is not None:
                _refresh_transport_progress(db, assessment)
            db.commit()
            return found.status

    batch.manifest = {
        **current_manifest,
        "mode": PRIVILEGE_BATCH_MANIFEST_MODE,
        "display_name": display_name,
    }
    db.commit()
    requests, manifest = _prepare_batch_requests(db, batch=batch, actor_user_id=actor_user_id)
    if not requests:
        raise ValueError("Privilege evidence provider batch contains no extraction units")
    batch.status = "SUBMITTED"
    batch.request_count = len(requests)
    batch.manifest = {
        "mode": PRIVILEGE_BATCH_MANIFEST_MODE,
        "display_name": display_name,
        "requests": manifest,
    }
    db.commit()
    try:
        submission = adapter.submit(model=batch.model, requests=requests, display_name=display_name)
    except Exception:
        found = adapter.find(display_name)
        if found is None:
            raise
        submission = found
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    batch.provider_batch_id = submission.batch_id
    batch.provider_status = submission.status
    batch.status = "SUBMITTED"
    batch.submitted_at = batch.submitted_at or utcnow()
    assessment = db.get(MatterDefinitionAssessmentRun, batch.assessment_run_id)
    if assessment is not None:
        _refresh_transport_progress(db, assessment)
    db.commit()
    return submission.status


def poll_privilege_evidence_batch(db: Session, batch_id: uuid.UUID) -> str:
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    if batch is None or not batch.provider_batch_id:
        raise ValueError("Privilege evidence provider batch has not been submitted")
    result = get_batch_adapter(batch.provider).poll(batch.provider_batch_id)
    batch.provider_status = result.status
    batch.last_polled_at = utcnow()
    if result.status in {"JOB_STATE_QUEUED", "JOB_STATE_PENDING"}:
        batch.status = "SUBMITTED"
    elif result.status not in TERMINAL_PROVIDER_STATES:
        batch.status = "RUNNING"
    elif result.status not in SUCCESS_PROVIDER_STATES:
        batch.status = "FAILED"
        batch.error_message = str(result.error or f"Provider batch ended in {result.status}")[:4000]
        batch.completed_at = utcnow()
    assessment = db.get(MatterDefinitionAssessmentRun, batch.assessment_run_id)
    if assessment is not None:
        _refresh_transport_progress(db, assessment)
    db.commit()
    return result.status


def _usage_context(workflow, provider_batch_id: str) -> ProviderUsageContext:
    return ProviderUsageContext(
        tenant_id=workflow.tenant_id,
        client_id=workflow.client_id,
        matter_id=workflow.matter_id,
        started_by_user_id=workflow.initiated_by_user_id,
        job_type="PRIVILEGE_REVIEW_EVIDENCE",
        job_id=workflow.id,
        job_created_at=workflow.created_at,
        details={
            "workflow_key": workflow.workflow_key,
            "role_key": "evidence_extraction",
            "provider_batch_id": provider_batch_id,
        },
    )


def _invocation_ids(db: Session, skill_run_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
    return tuple(
        db.scalars(
            select(ModelInvocation.id)
            .where(ModelInvocation.skill_run_id == skill_run_id)
            .order_by(ModelInvocation.attempt, ModelInvocation.request_sequence)
        )
    )


def _fail_batch_skill_run(db: Session, item: dict[str, Any], exc: Exception) -> None:
    skill_run = db.get(SkillRun, uuid.UUID(str(item["skill_run_id"])))
    if skill_run is not None and skill_run.status == "RUNNING":
        fail_skill_run(skill_run, exc)
        refresh_skill_run_usage(db, skill_run, refresh_workflow_usage=False)


def _apply_batch_unit(
    db: Session,
    *,
    batch: MatterDefinitionAssessmentProviderBatch,
    item: dict[str, Any],
    response: Any,
    actor_user_id: uuid.UUID,
) -> None:
    prepared = _prepared_extraction(
        db,
        assessment_id=str(batch.assessment_run_id),
        document_id=str(item["document_id"]),
        actor_user_id=str(actor_user_id),
    )
    workflow_run_id = uuid.UUID(str(item["workflow_run_id"]))
    workflow, _step, value, extractor = _extractor(db, prepared, workflow_run_id)
    unit = next((candidate for candidate in value.plan.extraction_units if candidate.unit_id == item["unit_id"]), None)
    if unit is None or value.plan.plan_hash != item["plan_hash"]:
        raise ValueError("Privilege provider batch unit no longer matches its immutable plan")
    repository = WorkflowEvidenceShadowArtifactRepository(
        actor_user_id=workflow.initiated_by_user_id,
        producer_run_id=workflow.id,
    )
    existing = repository.load_attempt(value.plan, unit, 1)
    if existing is not None and existing[0].status in {"COMPLETED", "COMPLETED_WITH_ADJUSTMENTS"}:
        return
    skill_run = db.get(SkillRun, uuid.UUID(str(item["skill_run_id"])))
    if skill_run is None:
        raise ValueError("Privilege provider batch skill run is unavailable")
    if response is None:
        raise ValueError("Provider Batch API returned no result for the extraction unit")
    if response.error:
        raise ValueError(f"Provider Batch API reported an extraction error: {response.error}")
    if not response.output_text:
        raise ValueError("Provider Batch API returned an empty extraction result")
    raw_output = json.loads(response.output_text)
    if not isinstance(raw_output, dict):
        raise TypeError("Provider Batch API extraction result is not a JSON object")
    capture = extractor.normalize_model_output(value.plan, unit, raw_output)
    now = datetime.now(timezone.utc)
    if skill_run.status == "RUNNING":
        persist_model_invocations(
            db,
            (
                InvocationTelemetry(
                    request_sequence=1,
                    provider_request_id=response.provider_request_id,
                    provider=batch.provider,
                    model=batch.model,
                    model_configuration_hash=str(item["model_configuration_hash"]),
                    request_count=1,
                    input_tokens=response.input_tokens,
                    cached_input_tokens=response.cached_input_tokens,
                    cache_write_tokens=0,
                    output_tokens=response.output_tokens,
                    latency_ms=0,
                    started_at=batch.submitted_at or now,
                    completed_at=now,
                ),
            ),
            skill_run_id=skill_run.id,
            usage_context=_usage_context(workflow, str(batch.provider_batch_id)),
        )
        complete_skill_run(db, skill_run, refresh_workflow_usage=False)
    adjustments = tuple(
        EvidenceUnitAdjustment(
            item_index=adjustment.item_index,
            code=adjustment.code,
            policy=adjustment.policy,
            path=adjustment.path,
            detail=adjustment.detail,
        )
        for adjustment in capture.result.adjustments
    )
    result = EvidenceExtractionUnitResult(
        plan_hash=value.plan.plan_hash,
        source_content_hash=value.plan.source_content_hash,
        unit_id=unit.unit_id,
        unit_ordinal=unit.ordinal,
        attempt=1,
        status="COMPLETED_WITH_ADJUSTMENTS" if adjustments else "COMPLETED",
        observations=capture.result.observations,
        adjustments=adjustments,
        invocation_ids=_invocation_ids(db, skill_run.id),
        started_at=skill_run.started_at or batch.submitted_at or now,
        completed_at=skill_run.completed_at or now,
    )
    plan_artifact = repository.persist_plan(value.plan)
    repository.persist_attempt(value.plan, plan_artifact.artifact_id, result)


def _document_has_complete_units(db: Session, prepared, workflow_run_id: uuid.UUID) -> bool:
    workflow, _step, value, _extractor_value = _extractor(db, prepared, workflow_run_id)
    repository = WorkflowEvidenceShadowArtifactRepository(
        actor_user_id=workflow.initiated_by_user_id,
        producer_run_id=workflow.id,
    )
    for unit in value.plan.extraction_units:
        latest = None
        for attempt in range(1, 4):
            loaded = repository.load_attempt(value.plan, unit, attempt)
            if loaded is None:
                break
            latest = loaded[0]
        if latest is None or latest.status not in {"COMPLETED", "COMPLETED_WITH_ADJUSTMENTS"}:
            return False
    return True


def _record_completed_document(
    db: Session,
    *,
    batch: MatterDefinitionAssessmentProviderBatch,
    document_id: uuid.UUID,
    workflow_run_id: uuid.UUID,
) -> None:
    row = db.get(ReviewBatchRunDocument, (batch.review_batch_run_id, document_id))
    if row is None:
        raise ValueError("Privilege provider batch document is unavailable")
    row.status = "COMPLETED"
    row.completed_at = utcnow()
    row.failure_code = None
    row.failure_message = None
    row.failure_retriable = False
    row.last_workflow_run_id = workflow_run_id


def finalize_privilege_evidence_batch(
    db: Session,
    *,
    batch_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> dict[str, Any]:
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    if batch is None or not batch.provider_batch_id:
        raise ValueError("Privilege evidence provider batch has not been submitted")
    state, responses = get_batch_adapter(batch.provider).results(batch.provider_batch_id)
    if state not in SUCCESS_PROVIDER_STATES:
        raise ValueError(f"Privilege evidence provider batch is not ready: {state}")
    response_by_id = {item.custom_id: item for item in responses if item.custom_id}
    manifest = batch.manifest.get("requests", []) if isinstance(batch.manifest, dict) else []
    fallback_documents: set[str] = set()
    changed = 0
    for item in manifest:
        try:
            _apply_batch_unit(
                db,
                batch=batch,
                item=item,
                response=response_by_id.get(str(item["custom_id"])),
                actor_user_id=actor_user_id,
            )
        except SQLAlchemyError:
            db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - one bad item falls back to the durable real-time unit path.
            logger.warning(
                "Privilege provider batch unit will fall back to real time batch_id=%s document_id=%s unit_id=%s: %s",
                batch.id,
                item.get("document_id"),
                item.get("unit_id"),
                exc,
            )
            _fail_batch_skill_run(db, item, exc)
            fallback_documents.add(str(item["document_id"]))
        changed += 1
        if changed >= PRIVILEGE_BATCH_FINALIZATION_CHECKPOINT_SIZE:
            db.commit()
            changed = 0
    if changed:
        db.commit()

    entries_by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in manifest:
        entries_by_document[str(item["document_id"])].append(item)
    completed = 0
    for raw_document_id in batch.document_ids:
        if raw_document_id in fallback_documents:
            continue
        entries = entries_by_document.get(raw_document_id) or []
        if not entries:
            fallback_documents.add(raw_document_id)
            continue
        workflow_run_ids = {str(item["workflow_run_id"]) for item in entries}
        if len(workflow_run_ids) != 1:
            fallback_documents.add(raw_document_id)
            continue
        workflow_run_id = uuid.UUID(next(iter(workflow_run_ids)))
        prepared = _prepared_extraction(
            db,
            assessment_id=str(batch.assessment_run_id),
            document_id=raw_document_id,
            actor_user_id=str(actor_user_id),
        )
        if not _document_has_complete_units(db, prepared, workflow_run_id):
            fallback_documents.add(raw_document_id)
            continue
        extraction = finalize_prepared_evidence_shadow(
            db,
            prepared,
            workflow_run_id=workflow_run_id,
            unit_results=[{"executed": True, "reused": False} for _item in entries],
        )
        report = (extraction.get("documents") or [{}])[0]
        if not report.get("evidence_complete") or not report.get("observations_artifact_id"):
            fallback_documents.add(raw_document_id)
            continue
        _record_completed_document(
            db,
            batch=batch,
            document_id=uuid.UUID(raw_document_id),
            workflow_run_id=workflow_run_id,
        )
        completed += 1
        db.commit()

    for raw_document_id in fallback_documents:
        row = db.get(ReviewBatchRunDocument, (batch.review_batch_run_id, uuid.UUID(raw_document_id)))
        if row is not None and row.status not in {"COMPLETED", "SKIPPED"}:
            row.status = "QUEUED"
            row.completed_at = None
            row.failure_code = None
            row.failure_message = None
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    batch.provider_status = state
    batch.status = "COMPLETED_WITH_ERRORS" if fallback_documents else "COMPLETED"
    batch.error_message = (
        f"{len(fallback_documents)} documents require real-time extraction fallback"
        if fallback_documents
        else None
    )
    batch.completed_at = utcnow()
    assessment = db.get(MatterDefinitionAssessmentRun, batch.assessment_run_id)
    if assessment is not None:
        _refresh_transport_progress(db, assessment)
    db.commit()
    return {
        "completed": completed,
        "fallback_document_ids": sorted(fallback_documents),
    }


def fail_privilege_evidence_batch_to_fallback(
    db: Session,
    *,
    batch_id: uuid.UUID,
    message: str,
) -> list[str]:
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    if batch is None:
        return []
    manifest_value = batch.manifest if isinstance(batch.manifest, dict) else {}
    display_name = manifest_value.get("display_name")
    if not batch.provider_batch_id and isinstance(display_name, str):
        try:
            found = get_batch_adapter(batch.provider).find(display_name)
            if found is not None:
                batch.provider_batch_id = found.batch_id
                batch.provider_status = found.status
                get_batch_adapter(batch.provider).cancel(found.batch_id)
        except Exception:
            logger.warning(
                "Could not reconcile or cancel an interrupted privilege provider batch batch_id=%s",
                batch.id,
                exc_info=True,
            )
    manifest = manifest_value.get("requests", [])
    for item in manifest:
        if isinstance(item, dict) and item.get("skill_run_id"):
            _fail_batch_skill_run(db, item, RuntimeError(message))
    batch.status = "FAILED"
    batch.error_message = message[:4000]
    batch.completed_at = utcnow()
    for raw_document_id in batch.document_ids:
        row = db.get(ReviewBatchRunDocument, (batch.review_batch_run_id, uuid.UUID(raw_document_id)))
        if row is not None and row.status not in {"COMPLETED", "SKIPPED"}:
            row.status = "QUEUED"
            row.completed_at = None
    assessment = db.get(MatterDefinitionAssessmentRun, batch.assessment_run_id)
    if assessment is not None:
        _refresh_transport_progress(db, assessment)
    db.commit()
    return list(batch.document_ids)
