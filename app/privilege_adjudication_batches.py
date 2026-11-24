"""Provider Batch transport for family-level privilege criterion adjudication."""

from __future__ import annotations

import asyncio
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
from app.evidence_adjudication_artifacts import CriterionAdjudicationAttempt
from app.evidence_adjudication_launcher import (
    _workflow,
    execute_prepared_adjudication_shadow,
    prepare_adjudication_shadow_launch,
)
from app.evidence_adjudication_managed import ManagedCriterionAdjudicator
from app.evidence_adjudication_shadow import WorkflowAdjudicationShadowArtifactRepository
from app.execution_accounting import ProviderUsageContext, persist_model_invocations, refresh_skill_run_usage
from app.model_batching import BatchModelRequest, get_batch_adapter, resolve_batch_target
from app.model_execution import (
    InstructionLayer,
    InvocationTelemetry,
    StructuredModelRequest,
    assemble_structured_prompt,
    provider_compatible_model_settings,
)
from app.models import (
    MatterDefinitionAssessmentProviderBatch,
    ModelInvocation,
    ReviewBatchRun,
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

logger = logging.getLogger(__name__)

PRIVILEGE_ADJUDICATION_BATCH_MODE = "PRIVILEGE_FAMILY_ADJUDICATION_PACKETS_V1"
FINALIZATION_CHECKPOINT_SIZE = 10


def _review_run(db: Session, review_batch_run_id: uuid.UUID) -> ReviewBatchRun:
    value = db.get(ReviewBatchRun, review_batch_run_id)
    if value is None or value.workflow_run_record_id is None:
        raise ValueError("Privilege adjudication run is unavailable")
    return value


def _source_assessment_id(review_run: ReviewBatchRun) -> uuid.UUID:
    value = review_run.configuration_snapshot.get("source_evidence_assessment_id")
    if value is None:
        raise ValueError("Privilege adjudication source assessment is unavailable")
    return uuid.UUID(str(value))


def _prepare(db: Session, *, review_run: ReviewBatchRun, document: dict[str, Any], actor_user_id: uuid.UUID):
    return prepare_adjudication_shadow_launch(
        db,
        actor_user_id=actor_user_id,
        assessment_id=_source_assessment_id(review_run),
        observations_artifact_id=uuid.UUID(str(document["observations_artifact_id"])),
        target_matter_document_id=uuid.UUID(str(document["matter_document_id"])),
    )


def _model_packet_estimate(prepared) -> tuple[int, int]:
    by_id = {value.observation_id: value for value in prepared.observations.observations}
    request_count = 0
    estimated_bytes = 0
    for packet in prepared.plan.packets:
        if packet.deterministic_without_model:
            continue
        criterion = next(value for value in prepared.catalog.criteria if value.criterion_key == packet.criterion_key)
        request_count += 1
        estimated_bytes += (
            len(prepared.revision.content_markdown.encode("utf-8"))
            + len(json.dumps(criterion.model_dump(mode="json"), separators=(",", ":")).encode("utf-8"))
            + len(
                json.dumps(
                    [by_id[value].model_dump(mode="json") for value in packet.observation_ids],
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            + PROVIDER_BATCH_REQUEST_OVERHEAD_BYTES
        )
    return request_count, estimated_bytes


def _refresh_transport_progress(db: Session, review_run: ReviewBatchRun) -> None:
    batches = list(
        db.scalars(
            select(MatterDefinitionAssessmentProviderBatch).where(
                MatterDefinitionAssessmentProviderBatch.review_batch_run_id == review_run.id
            )
        )
    )
    counts: dict[str, int] = defaultdict(int)
    for batch in batches:
        if isinstance(batch.manifest, dict) and batch.manifest.get("mode") == PRIVILEGE_ADJUDICATION_BATCH_MODE:
            counts[batch.status] += 1
    total = sum(counts.values())
    terminal = sum(counts.get(status, 0) for status in ("COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"))
    active = sum(counts.get(status, 0) for status in ("QUEUED", "SUBMITTED", "RUNNING"))
    state = "WAITING_FOR_PROVIDER_BATCH" if active else "PROVIDER_BATCHES_FINALIZED" if total else "REALTIME_ONLY"
    workflow = db.get(WorkflowRun, review_run.workflow_run_record_id)
    if workflow is not None:
        workflow.current_stage = state if active else "PRIVILEGE_ADJUDICATION"
        workflow.progress = {
            **(workflow.progress or {}),
            "transport": "PROVIDER_BATCH",
            "transport_state": state,
            "provider_batch_count": total,
            "provider_batch_completed_count": terminal,
            "provider_batch_active_count": active,
        }
    raw_program_run_id = review_run.configuration_snapshot.get("review_program_run_id")
    if raw_program_run_id:
        program_run = db.get(ReviewProgramRun, uuid.UUID(str(raw_program_run_id)))
        if program_run is not None:
            progress = dict((program_run.progress_summary or {}).get("privilege_adjudication") or {})
            program_run.progress_summary = {
                **(program_run.progress_summary or {}),
                "privilege_adjudication": {
                    **progress,
                    "transport": "PROVIDER_BATCH",
                    "transport_state": state,
                    "provider_batch_count": total,
                    "provider_batch_completed_count": terminal,
                    "provider_batch_active_count": active,
                },
            }


def plan_privilege_adjudication_batches(
    db: Session,
    *,
    review_batch_run_id: uuid.UUID,
    documents: list[dict[str, Any]],
    actor_user_id: uuid.UUID,
    settings: Settings,
) -> dict[str, Any]:
    review_run = _review_run(db, review_batch_run_id)
    existing = list(
        db.scalars(
            select(MatterDefinitionAssessmentProviderBatch)
            .where(MatterDefinitionAssessmentProviderBatch.review_batch_run_id == review_run.id)
            .order_by(MatterDefinitionAssessmentProviderBatch.ordinal)
        )
    )
    active = [
        value
        for value in existing
        if isinstance(value.manifest, dict)
        and value.manifest.get("mode") == PRIVILEGE_ADJUDICATION_BATCH_MODE
        and value.status in {"QUEUED", "SUBMITTED", "RUNNING"}
    ]
    if active:
        batched = {value for batch in active for value in batch.document_ids}
        return {
            "provider_batch_ids": [str(value.id) for value in active],
            "realtime_anchor_document_ids": [
                str(value["matter_document_id"])
                for value in documents
                if str(value["matter_document_id"]) not in batched
            ],
        }

    request_limit = settings.definition_assessment_provider_batch_size
    groups: list[list[dict[str, Any]]] = []
    realtime: list[str] = []
    current: list[dict[str, Any]] = []
    current_requests = 0
    current_bytes = 0
    target = None
    for document in documents:
        anchor_id = str(document["matter_document_id"])
        try:
            prepared = _prepare(
                db,
                review_run=review_run,
                document=document,
                actor_user_id=actor_user_id,
            )
            resolved = resolve_batch_target(prepared.resolved_model)
            request_count, estimated_bytes = _model_packet_estimate(prepared)
        except Exception as exc:  # noqa: BLE001 - durable real-time path records preparation failures.
            logger.warning("Privilege family adjudication is not batch eligible anchor=%s: %s", anchor_id, exc)
            realtime.append(anchor_id)
            continue
        if resolved is None or request_count < 1:
            realtime.append(anchor_id)
            continue
        _model_key, candidate_target = resolved
        if target is None:
            target = candidate_target
        if (
            candidate_target != target
            or request_count > request_limit
            or estimated_bytes > PROVIDER_BATCH_MAX_ESTIMATED_BYTES
        ):
            realtime.append(anchor_id)
            continue
        if current and (
            current_requests + request_count > request_limit
            or current_bytes + estimated_bytes > PROVIDER_BATCH_MAX_ESTIMATED_BYTES
        ):
            groups.append(current)
            current = []
            current_requests = 0
            current_bytes = 0
        current.append(document)
        current_requests += request_count
        current_bytes += estimated_bytes
    if current:
        groups.append(current)

    provider_batches: list[MatterDefinitionAssessmentProviderBatch] = []
    next_ordinal = max((value.ordinal for value in existing), default=0)
    for offset, group in enumerate(groups, start=1):
        if target is None:
            break
        batch = MatterDefinitionAssessmentProviderBatch(
            assessment_run_id=_source_assessment_id(review_run),
            review_batch_run_id=review_run.id,
            ordinal=next_ordinal + offset,
            document_ids=[str(value["matter_document_id"]) for value in group],
            status="QUEUED",
            provider=target.provider,
            model=target.model,
            request_count=0,
            manifest={
                "mode": PRIVILEGE_ADJUDICATION_BATCH_MODE,
                "families": group,
            },
        )
        db.add(batch)
        provider_batches.append(batch)
    db.flush()
    _refresh_transport_progress(db, review_run)
    db.commit()
    return {
        "provider_batch_ids": [str(value.id) for value in provider_batches],
        "realtime_anchor_document_ids": realtime,
    }


def _managed_adjudicator(db: Session, prepared, workflow_run_id: uuid.UUID):
    workflow, step, _decision_step, _projection_step = _workflow(
        db,
        prepared,
        retry_failed=False,
        workflow_run_id=workflow_run_id,
    )
    return (
        workflow,
        step,
        ManagedCriterionAdjudicator(
            db=db,
            workflow=workflow,
            step=step,
            skill_version=prepared.skill_version,
            review_guidance=prepared.revision.content_markdown,
            catalog=prepared.catalog,
            observations=prepared.observations,
            resolved_model=prepared.resolved_model,
        ),
    )


def _structured_request(prepared, adjudicator, packet, *, run_id: str) -> StructuredModelRequest:
    model_call = adjudicator.prepare_model_call(prepared.plan, packet)
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
        model_settings=prepared.skill_version.model_policy,
        limits=prepared.skill_version.limits,
        cache_policy=prepared.skill_version.cache_policy,
        cache_identity=model_call.cache_identity,
        request_type="criterion-adjudication",
        run_id=run_id,
    )


def _prepare_requests(
    db: Session,
    *,
    batch: MatterDefinitionAssessmentProviderBatch,
    actor_user_id: uuid.UUID,
) -> tuple[list[BatchModelRequest], list[dict[str, Any]]]:
    review_run = _review_run(db, batch.review_batch_run_id)
    current_manifest = batch.manifest if isinstance(batch.manifest, dict) else {}
    existing_entries = {
        (str(value.get("observations_artifact_id")), str(value.get("unit_id"))): value
        for value in current_manifest.get("requests", [])
        if isinstance(value, dict)
    }
    requests: list[BatchModelRequest] = []
    manifest: list[dict[str, Any]] = []
    for document in current_manifest.get("families", []):
        prepared = _prepare(db, review_run=review_run, document=document, actor_user_id=actor_user_id)
        prior_workflow_ids = {
            str(value["workflow_run_id"])
            for (artifact_id, _unit), value in existing_entries.items()
            if artifact_id == str(document["observations_artifact_id"])
        }
        if len(prior_workflow_ids) > 1:
            raise ValueError("Privilege adjudication family has conflicting workflow IDs")
        if prior_workflow_ids:
            workflow_run_id = uuid.UUID(next(iter(prior_workflow_ids)))
            workflow, step, adjudicator = _managed_adjudicator(db, prepared, workflow_run_id)
        else:
            workflow, step, _decision_step, _projection_step = _workflow(db, prepared, retry_failed=False)
            workflow_run_id = workflow.id
            adjudicator = ManagedCriterionAdjudicator(
                db=db,
                workflow=workflow,
                step=step,
                skill_version=prepared.skill_version,
                review_guidance=prepared.revision.content_markdown,
                catalog=prepared.catalog,
                observations=prepared.observations,
                resolved_model=prepared.resolved_model,
            )
        for packet in prepared.plan.packets:
            if packet.deterministic_without_model:
                continue
            key = (str(document["observations_artifact_id"]), packet.unit_id)
            prior = existing_entries.get(key)
            if prior is None:
                model_call = adjudicator.prepare_model_call(prepared.plan, packet)
                skill_run = create_skill_run(
                    db,
                    workflow=workflow,
                    step=step,
                    skill_version=prepared.skill_version,
                    scope_type="MATTER_DOCUMENT",
                    scope_id=prepared.plan.matter_document_id,
                    request_input={
                        "stable_context": model_call.stable_context,
                        "dynamic_input": model_call.dynamic_input,
                    },
                    output_schema=model_call.application_schema,
                    limits=prepared.skill_version.limits,
                    model_settings=provider_compatible_model_settings(
                        prepared.skill_version.model_policy,
                        resolved_model=prepared.resolved_model,
                    ),
                )
                custom_id = str(skill_run.id)
            else:
                custom_id = str(prior["custom_id"])
                skill_run = db.get(SkillRun, uuid.UUID(str(prior["skill_run_id"])))
                if skill_run is None:
                    raise ValueError("Privilege adjudication batch skill run is unavailable")
            structured = _structured_request(prepared, adjudicator, packet, run_id=custom_id)
            assembly = assemble_structured_prompt(structured, resolved_model=prepared.resolved_model)
            skill_run.cache_fingerprint = assembly.cache_fingerprint
            requests.append(
                BatchModelRequest(
                    custom_id=custom_id,
                    prompt=f"{assembly.stable_prefix}\n{assembly.dynamic_payload}",
                    instructions=assembly.instructions,
                    output_schema=structured.output_schema,
                    model_settings=assembly.model_settings,
                    limits=structured.limits,
                )
            )
            manifest.append(
                {
                    "custom_id": custom_id,
                    "matter_document_id": str(document["matter_document_id"]),
                    "matter_document_ids": list(document.get("matter_document_ids") or []),
                    "observations_artifact_id": str(document["observations_artifact_id"]),
                    "workflow_run_id": str(workflow_run_id),
                    "unit_id": packet.unit_id,
                    "plan_hash": prepared.plan.plan_hash,
                    "skill_run_id": str(skill_run.id),
                    "model_configuration_hash": assembly.model_configuration_hash,
                }
            )
        for raw_document_id in document.get("matter_document_ids") or [document["matter_document_id"]]:
            row = db.get(ReviewBatchRunDocument, (review_run.id, uuid.UUID(str(raw_document_id))))
            if row is not None and row.status not in {"COMPLETED", "SKIPPED"}:
                row.status = "IN_PROGRESS"
        batch.manifest = {**current_manifest, "requests": list(manifest)}
        batch.request_count = len(manifest)
        db.commit()
    return requests, manifest


def submit_privilege_adjudication_batch(
    db: Session,
    *,
    batch_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> str:
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    if batch is None:
        raise ValueError("Privilege adjudication provider batch is unavailable")
    if batch.provider_batch_id:
        return batch.provider_status or "JOB_STATE_QUEUED"
    display_name = f"privilege-adjudication-{batch.id}"
    adapter = get_batch_adapter(batch.provider)
    manifest = batch.manifest if isinstance(batch.manifest, dict) else {}
    if manifest.get("display_name") == display_name:
        found = adapter.find(display_name)
        if found is not None:
            batch.provider_batch_id = found.batch_id
            batch.provider_status = found.status
            batch.status = "SUBMITTED"
            batch.submitted_at = batch.submitted_at or utcnow()
            _refresh_transport_progress(db, _review_run(db, batch.review_batch_run_id))
            db.commit()
            return found.status
    batch.manifest = {**manifest, "display_name": display_name}
    db.commit()
    requests, request_manifest = _prepare_requests(db, batch=batch, actor_user_id=actor_user_id)
    if not requests:
        raise ValueError("Privilege adjudication provider batch contains no model packets")
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    batch.status = "SUBMITTED"
    batch.request_count = len(requests)
    batch.manifest = {**(batch.manifest or {}), "requests": request_manifest}
    db.commit()
    try:
        submitted = adapter.submit(model=batch.model, requests=requests, display_name=display_name)
    except Exception:
        found = adapter.find(display_name)
        if found is None:
            raise
        submitted = found
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    batch.provider_batch_id = submitted.batch_id
    batch.provider_status = submitted.status
    batch.status = "SUBMITTED"
    batch.submitted_at = batch.submitted_at or utcnow()
    _refresh_transport_progress(db, _review_run(db, batch.review_batch_run_id))
    db.commit()
    return submitted.status


def poll_privilege_adjudication_batch(db: Session, batch_id: uuid.UUID) -> str:
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    if batch is None or not batch.provider_batch_id:
        raise ValueError("Privilege adjudication provider batch has not been submitted")
    result = get_batch_adapter(batch.provider).poll(batch.provider_batch_id)
    batch.provider_status = result.status
    batch.last_polled_at = utcnow()
    if result.status in {"JOB_STATE_QUEUED", "JOB_STATE_PENDING"}:
        batch.status = "SUBMITTED"
    elif result.status not in TERMINAL_PROVIDER_STATES:
        batch.status = "RUNNING"
    elif result.status not in SUCCESS_PROVIDER_STATES:
        batch.status = "FAILED"
        batch.error_message = str(result.error or f"Provider batch ended in {result.status}")[:4_000]
        batch.completed_at = utcnow()
    _refresh_transport_progress(db, _review_run(db, batch.review_batch_run_id))
    db.commit()
    return result.status


def _usage_context(workflow: WorkflowRun, provider_batch_id: str) -> ProviderUsageContext:
    return ProviderUsageContext(
        tenant_id=workflow.tenant_id,
        client_id=workflow.client_id,
        matter_id=workflow.matter_id,
        started_by_user_id=workflow.initiated_by_user_id,
        job_type="PRIVILEGE_REVIEW_ADJUDICATION",
        job_id=workflow.id,
        job_created_at=workflow.created_at,
        details={"role_key": "criterion_adjudication", "provider_batch_id": provider_batch_id},
    )


def _invocation_ids(db: Session, skill_run_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
    return tuple(
        db.scalars(
            select(ModelInvocation.id)
            .where(ModelInvocation.skill_run_id == skill_run_id)
            .order_by(ModelInvocation.attempt, ModelInvocation.request_sequence)
        )
    )


def _fail_skill_run(db: Session, item: dict[str, Any], exc: Exception) -> None:
    skill_run = db.get(SkillRun, uuid.UUID(str(item["skill_run_id"])))
    if skill_run is not None and skill_run.status == "RUNNING":
        fail_skill_run(skill_run, exc)
        refresh_skill_run_usage(db, skill_run, refresh_workflow_usage=False)


def _apply_packet(
    db: Session,
    *,
    batch: MatterDefinitionAssessmentProviderBatch,
    item: dict[str, Any],
    response: Any,
    actor_user_id: uuid.UUID,
) -> None:
    review_run = _review_run(db, batch.review_batch_run_id)
    prepared = _prepare(db, review_run=review_run, document=item, actor_user_id=actor_user_id)
    workflow_run_id = uuid.UUID(str(item["workflow_run_id"]))
    workflow, _step, adjudicator = _managed_adjudicator(db, prepared, workflow_run_id)
    packet = next((value for value in prepared.plan.packets if value.unit_id == item["unit_id"]), None)
    if packet is None or prepared.plan.plan_hash != item["plan_hash"]:
        raise ValueError("Privilege adjudication batch packet no longer matches its immutable plan")
    repository = WorkflowAdjudicationShadowArtifactRepository(
        actor_user_id=workflow.initiated_by_user_id,
        producer_run_id=workflow.id,
    )
    existing = repository.load_attempt(prepared.plan, packet, 1)
    if existing is not None and existing[0].status in {"COMPLETED", "COMPLETED_WITH_ADJUSTMENTS"}:
        return
    if response is None:
        raise ValueError("Provider Batch API returned no criterion result")
    if response.error:
        raise ValueError(f"Provider Batch API reported a criterion error: {response.error}")
    if not response.output_text:
        raise ValueError("Provider Batch API returned an empty criterion result")
    raw_output = json.loads(response.output_text)
    if not isinstance(raw_output, dict):
        raise TypeError("Provider Batch API criterion result is not a JSON object")
    normalized = adjudicator.normalize_model_output(prepared.plan, packet, raw_output)
    skill_run = db.get(SkillRun, uuid.UUID(str(item["skill_run_id"])))
    if skill_run is None:
        raise ValueError("Privilege adjudication batch skill run is unavailable")
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
    result = CriterionAdjudicationAttempt(
        plan_hash=prepared.plan.plan_hash,
        unit_id=packet.unit_id,
        unit_ordinal=packet.ordinal,
        criterion_key=packet.criterion_key,
        attempt=1,
        status="COMPLETED_WITH_ADJUSTMENTS" if normalized.adjustments else "COMPLETED",
        adjudication=normalized.adjudication,
        decision=normalized.decision,
        adjustments=normalized.adjustments,
        invocation_ids=_invocation_ids(db, skill_run.id),
        started_at=skill_run.started_at or batch.submitted_at or now,
        completed_at=skill_run.completed_at or now,
    )
    plan_artifact = repository.persist_plan(prepared.plan)
    repository.persist_attempt(prepared.plan, plan_artifact.artifact_id, result)


def finalize_privilege_adjudication_batch(
    db: Session,
    *,
    batch_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> dict[str, Any]:
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    if batch is None or not batch.provider_batch_id:
        raise ValueError("Privilege adjudication provider batch has not been submitted")
    state, responses = get_batch_adapter(batch.provider).results(batch.provider_batch_id)
    if state not in SUCCESS_PROVIDER_STATES:
        raise ValueError(f"Privilege adjudication provider batch is not ready: {state}")
    response_by_id = {value.custom_id: value for value in responses if value.custom_id}
    manifest = list((batch.manifest or {}).get("requests") or [])
    failed_anchors: set[str] = set()
    changed = 0
    for item in manifest:
        try:
            _apply_packet(
                db,
                batch=batch,
                item=item,
                response=response_by_id.get(str(item["custom_id"])),
                actor_user_id=actor_user_id,
            )
        except SQLAlchemyError:
            db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - this packet falls back to real-time adjudication.
            logger.warning(
                "Privilege adjudication packet will fall back to real time batch=%s anchor=%s unit=%s: %s",
                batch.id,
                item.get("matter_document_id"),
                item.get("unit_id"),
                exc,
            )
            _fail_skill_run(db, item, exc)
            failed_anchors.add(str(item["matter_document_id"]))
        changed += 1
        if changed >= FINALIZATION_CHECKPOINT_SIZE:
            db.commit()
            changed = 0
    if changed:
        db.commit()

    review_run = _review_run(db, batch.review_batch_run_id)
    families_by_anchor = {
        str(value["matter_document_id"]): value for value in (batch.manifest or {}).get("families", [])
    }
    completed_families: list[dict[str, Any]] = []
    fallback_anchors: set[str] = set()
    for anchor_id in batch.document_ids:
        document = families_by_anchor.get(anchor_id)
        if document is None:
            fallback_anchors.add(anchor_id)
            continue
        entries = [value for value in manifest if str(value["matter_document_id"]) == anchor_id]
        workflow_ids = {str(value["workflow_run_id"]) for value in entries}
        if len(workflow_ids) != 1:
            fallback_anchors.add(anchor_id)
            continue
        try:
            prepared = _prepare(db, review_run=review_run, document=document, actor_user_id=actor_user_id)
            result = asyncio.run(
                execute_prepared_adjudication_shadow(
                    db,
                    prepared,
                    retry_failed=True,
                    workflow_run_id=uuid.UUID(next(iter(workflow_ids))),
                )
            )
            completed_families.append({"document": document, "result": result})
        except Exception:
            logger.exception("Privilege family adjudication finalization failed anchor=%s", anchor_id)
            fallback_anchors.add(anchor_id)
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    fallback_anchors.update(
        failed_anchors - {str(value["document"]["matter_document_id"]) for value in completed_families}
    )
    batch.provider_status = state
    batch.status = "COMPLETED_WITH_ERRORS" if fallback_anchors else "COMPLETED"
    batch.error_message = (
        f"{len(fallback_anchors)} families require real-time adjudication fallback" if fallback_anchors else None
    )
    batch.completed_at = utcnow()
    _refresh_transport_progress(db, review_run)
    db.commit()
    return {
        "completed_families": completed_families,
        "fallback_anchor_document_ids": sorted(fallback_anchors),
    }


def fail_privilege_adjudication_batch_to_fallback(
    db: Session,
    *,
    batch_id: uuid.UUID,
    message: str,
) -> list[str]:
    batch = db.get(MatterDefinitionAssessmentProviderBatch, batch_id)
    if batch is None:
        return []
    manifest = batch.manifest if isinstance(batch.manifest, dict) else {}
    display_name = manifest.get("display_name")
    if not batch.provider_batch_id and isinstance(display_name, str):
        try:
            found = get_batch_adapter(batch.provider).find(display_name)
            if found is not None:
                batch.provider_batch_id = found.batch_id
                batch.provider_status = found.status
                get_batch_adapter(batch.provider).cancel(found.batch_id)
        except Exception:
            logger.warning("Could not reconcile interrupted privilege adjudication batch", exc_info=True)
    for item in manifest.get("requests", []):
        if isinstance(item, dict) and item.get("skill_run_id"):
            _fail_skill_run(db, item, RuntimeError(message))
    batch.status = "FAILED"
    batch.error_message = message[:4_000]
    batch.completed_at = utcnow()
    review_run = _review_run(db, batch.review_batch_run_id)
    for raw_document_id in [
        value
        for family in manifest.get("families", [])
        for value in (family.get("matter_document_ids") or [family.get("matter_document_id")])
        if value
    ]:
        row = db.get(ReviewBatchRunDocument, (review_run.id, uuid.UUID(str(raw_document_id))))
        if row is not None and row.status not in {"COMPLETED", "SKIPPED"}:
            row.status = "QUEUED"
            row.completed_at = None
    _refresh_transport_progress(db, review_run)
    db.commit()
    return list(batch.document_ids)
