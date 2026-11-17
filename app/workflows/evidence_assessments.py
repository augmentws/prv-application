"""Durable evidence-first Matter Definition assessment orchestration."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from dbos import DBOS, Queue, SetWorkflowID
from sqlalchemy import select

from app.agent_models import resolve_agent_model
from app.assessment_execution import refresh_progress
from app.audit import record_audit
from app.config import get_settings
from app.database import SessionLocal
from app.evidence_adjudication_launcher import (
    execute_prepared_adjudication_shadow,
    prepare_adjudication_shadow_launch,
)
from app.evidence_first_launcher import (
    _require_shadow_operator,
    execute_prepared_evidence_shadow,
    execute_prepared_evidence_unit,
    finalize_prepared_evidence_shadow,
    initialize_prepared_evidence_shadow,
    prepare_evidence_shadow_launch,
)
from app.evidence_narrative_launcher import (
    execute_prepared_narrative_shadow,
    prepare_narrative_shadow_launch,
)
from app.evidence_outcome_launcher import prepare_evidence_outcome_manifest
from app.evidence_routing import (
    EVIDENCE_FIRST_SYNTHESIS_SOURCE,
    SYNTHESIS_DOCUMENT_SOURCE_KEY,
)
from app.model_execution import content_hash
from app.models import (
    Matter,
    MatterDefinitionAssessmentRun,
    ReviewBatchRunDocument,
    User,
    utcnow,
)
from app.workflow_specs import (
    MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW_SPEC,
    MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW_SPEC,
    MATTER_DEFINITION_EVIDENCE_SHADOW_SPEC,
    binding_snapshot,
    resolve_workflow_skill_bindings,
)
from app.workflows.matter_definition_assessments import (
    complete,
    fail,
    plan,
    retrieve,
    sync_batch_search,
    synthesize,
)

logger = logging.getLogger(__name__)

EVIDENCE_ASSESSMENT_ORCHESTRATION_VERSION = "matter-definition-evidence-assessment-v3"
_COMPATIBLE_EVIDENCE_ASSESSMENT_VERSIONS = {
    "matter-definition-evidence-assessment-v1",
    "matter-definition-evidence-assessment-v2",
    EVIDENCE_ASSESSMENT_ORCHESTRATION_VERSION,
}
EVIDENCE_ASSESSMENT_QUEUE = Queue("evidence-assessments", global_concurrency=2)
EVIDENCE_DOCUMENT_QUEUE = Queue(
    "evidence-assessment-documents",
    global_concurrency=get_settings().definition_assessment_evidence_document_concurrency,
)
EVIDENCE_EXTRACTION_UNIT_QUEUE = Queue(
    "evidence-extraction-units",
    global_concurrency=get_settings().definition_assessment_evidence_extraction_concurrency,
)
EVIDENCE_ADJUDICATION_QUEUE = Queue(
    "evidence-adjudication",
    global_concurrency=get_settings().definition_assessment_evidence_adjudication_concurrency,
)
EVIDENCE_NARRATIVE_QUEUE = Queue(
    "evidence-narrative",
    global_concurrency=get_settings().definition_assessment_evidence_narrative_concurrency,
)

_STAGE_SPECS = {
    "EXTRACTION": MATTER_DEFINITION_EVIDENCE_SHADOW_SPEC,
    "ADJUDICATION": MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW_SPEC,
    "NARRATIVE": MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW_SPEC,
}


def _frozen_stage_configuration(db, *, tenant_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    stages: dict[str, dict[str, Any]] = {}
    settings = get_settings()
    for stage, spec in _STAGE_SPECS.items():
        resolved = resolve_workflow_skill_bindings(
            db,
            workflow_key=spec.key,
            tenant_id=tenant_id,
        )
        snapshot = binding_snapshot(resolved)
        role_key = spec.roles[0].key
        _binding, _definition, version = resolved[role_key]
        resolved_model = resolve_agent_model(version.model_key, settings)
        stages[stage] = {
            "workflow_key": spec.key,
            "code_version": spec.code_version,
            "binding_snapshot": snapshot,
            "resolved_model": resolved_model,
            "configuration_hash": content_hash({"binding_snapshot": snapshot, "resolved_model": resolved_model}),
        }
    return stages


@DBOS.step(name="activate_evidence_first_assessment", retries_allowed=True, max_attempts=3)
def activate_evidence_first_assessment(assessment_id: str, actor_user_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        assessment = db.scalar(
            select(MatterDefinitionAssessmentRun)
            .where(MatterDefinitionAssessmentRun.id == uuid.UUID(assessment_id))
            .with_for_update()
        )
        if assessment is None:
            raise ValueError("Matter Definition assessment not found")
        if assessment.synthesis_result is not None:
            raise ValueError("A completed assessment cannot be switched to evidence-first execution")
        matter = db.get(Matter, assessment.matter_id)
        if matter is None:
            raise ValueError("Assessment matter is unavailable")
        actor = _require_shadow_operator(
            db.get(User, uuid.UUID(actor_user_id)),
            tenant_id=matter.client.tenant_id,
        )
        existing = assessment.configuration_snapshot.get("evidence_first_orchestration")
        if existing is not None:
            if (
                not isinstance(existing, dict)
                or existing.get("version") not in _COMPATIBLE_EVIDENCE_ASSESSMENT_VERSIONS
            ):
                raise ValueError("Assessment has an incompatible evidence-first configuration")
            if existing.get("actor_user_id") != str(actor.id):
                raise ValueError("Assessment evidence-first operator does not match the frozen run")
            if existing.get("version") != EVIDENCE_ASSESSMENT_ORCHESTRATION_VERSION:
                upgraded = {
                    **existing,
                    "version": EVIDENCE_ASSESSMENT_ORCHESTRATION_VERSION,
                    "upgraded_from_version": existing.get("version"),
                    "upgraded_at": datetime.now(timezone.utc).isoformat(),
                }
                assessment.configuration_snapshot = {
                    **assessment.configuration_snapshot,
                    "evidence_first_orchestration": upgraded,
                }
                record_audit(
                    db,
                    tenant_id=matter.client.tenant_id,
                    actor_user_id=actor.id,
                    action="matter_definition.evidence_assessment.orchestration_upgraded",
                    target_type="matter_definition_assessment_run",
                    target_id=assessment.id,
                    details={
                        "from_version": existing.get("version"),
                        "to_version": EVIDENCE_ASSESSMENT_ORCHESTRATION_VERSION,
                    },
                )
                db.commit()
                return upgraded
            return existing
        current_source = assessment.configuration_snapshot.get(SYNTHESIS_DOCUMENT_SOURCE_KEY)
        if current_source not in {None, EVIDENCE_FIRST_SYNTHESIS_SOURCE}:
            raise ValueError("Assessment is frozen to a different synthesis document source")
        stage_configuration = _frozen_stage_configuration(
            db,
            tenant_id=matter.client.tenant_id,
        )
        frozen = {
            "version": EVIDENCE_ASSESSMENT_ORCHESTRATION_VERSION,
            "actor_user_id": str(actor.id),
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "narrative_enabled": True,
            "stages": stage_configuration,
            "configuration_hash": content_hash(stage_configuration),
        }
        assessment.configuration_snapshot = {
            **assessment.configuration_snapshot,
            SYNTHESIS_DOCUMENT_SOURCE_KEY: EVIDENCE_FIRST_SYNTHESIS_SOURCE,
            "evidence_first_orchestration": frozen,
        }
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=actor.id,
            action="matter_definition.evidence_assessment.activated",
            target_type="matter_definition_assessment_run",
            target_id=assessment.id,
            details={
                "matter_id": str(matter.id),
                "configuration_hash": frozen["configuration_hash"],
                "evidence_artifacts_authoritative": False,
                "assessment_synthesis_selected": True,
            },
        )
        db.commit()
        return frozen


def _validate_stage_configuration(
    assessment: MatterDefinitionAssessmentRun,
    *,
    stage: str,
    prepared_binding_snapshot: dict[str, Any],
    resolved_model: str,
) -> None:
    orchestration = assessment.configuration_snapshot.get("evidence_first_orchestration")
    expected = (orchestration or {}).get("stages", {}).get(stage)
    if not isinstance(expected, dict):
        raise TypeError(f"Assessment has no frozen {stage.lower()} configuration")
    actual_hash = content_hash(
        {
            "binding_snapshot": prepared_binding_snapshot,
            "resolved_model": resolved_model,
        }
    )
    if actual_hash != expected.get("configuration_hash"):
        raise ValueError(f"Active {stage.lower()} binding differs from the frozen assessment")


@DBOS.step(name="execute_evidence_assessment_extraction", retries_allowed=True, max_attempts=3)
def execute_extraction(
    assessment_id: str,
    document_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    with SessionLocal() as db:
        prepared = prepare_evidence_shadow_launch(
            db,
            actor_user_id=uuid.UUID(actor_user_id),
            assessment_id=uuid.UUID(assessment_id),
            document_ids=[uuid.UUID(document_id)],
        )
        _validate_stage_configuration(
            prepared.assessment,
            stage="EXTRACTION",
            prepared_binding_snapshot=prepared.binding_snapshot,
            resolved_model=prepared.resolved_model,
        )
        return asyncio.run(execute_prepared_evidence_shadow(db, prepared))


def _prepared_extraction(db, *, assessment_id: str, document_id: str, actor_user_id: str):
    prepared = prepare_evidence_shadow_launch(
        db,
        actor_user_id=uuid.UUID(actor_user_id),
        assessment_id=uuid.UUID(assessment_id),
        document_ids=[uuid.UUID(document_id)],
    )
    _validate_stage_configuration(
        prepared.assessment,
        stage="EXTRACTION",
        prepared_binding_snapshot=prepared.binding_snapshot,
        resolved_model=prepared.resolved_model,
    )
    return prepared


@DBOS.step(name="prepare_evidence_assessment_extraction_v2", retries_allowed=True, max_attempts=3)
def prepare_extraction_fanout(
    assessment_id: str,
    document_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    with SessionLocal() as db:
        prepared = _prepared_extraction(
            db,
            assessment_id=assessment_id,
            document_id=document_id,
            actor_user_id=actor_user_id,
        )
        return initialize_prepared_evidence_shadow(db, prepared)


@DBOS.step(name="execute_evidence_assessment_extraction_unit_v2", retries_allowed=True, max_attempts=3)
def execute_extraction_unit(
    assessment_id: str,
    document_id: str,
    actor_user_id: str,
    workflow_run_id: str,
    unit_id: str,
) -> dict[str, Any]:
    with SessionLocal() as db:
        prepared = _prepared_extraction(
            db,
            assessment_id=assessment_id,
            document_id=document_id,
            actor_user_id=actor_user_id,
        )
        return asyncio.run(
            execute_prepared_evidence_unit(
                db,
                prepared,
                workflow_run_id=uuid.UUID(workflow_run_id),
                unit_id=unit_id,
            )
        )


@DBOS.step(name="finalize_evidence_assessment_extraction_v2", retries_allowed=True, max_attempts=5)
def finalize_extraction_fanout(
    assessment_id: str,
    document_id: str,
    actor_user_id: str,
    workflow_run_id: str,
    unit_results: list[dict[str, Any]],
) -> dict[str, Any]:
    with SessionLocal() as db:
        prepared = _prepared_extraction(
            db,
            assessment_id=assessment_id,
            document_id=document_id,
            actor_user_id=actor_user_id,
        )
        return finalize_prepared_evidence_shadow(
            db,
            prepared,
            workflow_run_id=uuid.UUID(workflow_run_id),
            unit_results=unit_results,
        )


@DBOS.step(name="execute_evidence_assessment_adjudication", retries_allowed=True, max_attempts=3)
def execute_adjudication(
    assessment_id: str,
    observations_artifact_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    with SessionLocal() as db:
        prepared = prepare_adjudication_shadow_launch(
            db,
            actor_user_id=uuid.UUID(actor_user_id),
            assessment_id=uuid.UUID(assessment_id),
            observations_artifact_id=uuid.UUID(observations_artifact_id),
        )
        _validate_stage_configuration(
            prepared.assessment,
            stage="ADJUDICATION",
            prepared_binding_snapshot=prepared.binding_snapshot,
            resolved_model=prepared.resolved_model,
        )
        return asyncio.run(execute_prepared_adjudication_shadow(db, prepared))


@DBOS.step(name="execute_evidence_assessment_narrative", retries_allowed=True, max_attempts=3)
def execute_narrative(
    assessment_id: str,
    compatibility_artifact_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    with SessionLocal() as db:
        prepared = prepare_narrative_shadow_launch(
            db,
            actor_user_id=uuid.UUID(actor_user_id),
            assessment_id=uuid.UUID(assessment_id),
            compatibility_artifact_id=uuid.UUID(compatibility_artifact_id),
        )
        _validate_stage_configuration(
            prepared.assessment,
            stage="NARRATIVE",
            prepared_binding_snapshot=prepared.binding_snapshot,
            resolved_model=prepared.resolved_model,
        )
        return asyncio.run(execute_prepared_narrative_shadow(db, prepared))


def _document_failure(document_id: str, stage: str, exc: BaseException) -> dict[str, Any]:
    summary = " ".join(str(exc).split()) or type(exc).__name__
    return {
        "matter_document_id": document_id,
        "status": "ACTION_REQUIRED",
        "failed_stage": stage,
        "error_type": type(exc).__name__,
        "error_summary": summary[:1_000],
    }


@DBOS.workflow(name="matter_definition_evidence_extraction_unit_v2")
def evidence_extraction_unit_workflow(
    assessment_id: str,
    document_id: str,
    actor_user_id: str,
    workflow_run_id: str,
    unit_id: str,
) -> dict[str, Any]:
    return execute_extraction_unit(
        assessment_id,
        document_id,
        actor_user_id,
        workflow_run_id,
        unit_id,
    )


@DBOS.workflow(name="matter_definition_evidence_adjudication_v2")
def evidence_adjudication_workflow(
    assessment_id: str,
    observations_artifact_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    return execute_adjudication(assessment_id, observations_artifact_id, actor_user_id)


@DBOS.workflow(name="matter_definition_evidence_narrative_v2")
def evidence_narrative_workflow(
    assessment_id: str,
    compatibility_artifact_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    return execute_narrative(assessment_id, compatibility_artifact_id, actor_user_id)


@DBOS.step(name="record_evidence_assessment_document_progress_v1", retries_allowed=True, max_attempts=5)
def record_document_progress(
    assessment_id: str,
    document_id: str,
    result: dict[str, Any],
) -> dict[str, int]:
    """Persist one terminal document result and refresh cheap aggregate counters."""

    with SessionLocal() as db:
        assessment = db.scalar(
            select(MatterDefinitionAssessmentRun)
            .where(MatterDefinitionAssessmentRun.id == uuid.UUID(assessment_id))
            .with_for_update()
        )
        if assessment is None or assessment.review_batch_run_id is None:
            raise ValueError("Evidence-first assessment batch run is unavailable")
        run_document = db.scalar(
            select(ReviewBatchRunDocument)
            .where(
                ReviewBatchRunDocument.review_batch_run_id == assessment.review_batch_run_id,
                ReviewBatchRunDocument.matter_document_id == uuid.UUID(document_id),
            )
            .with_for_update()
        )
        if run_document is None:
            raise ValueError("Evidence-first assessment document is unavailable")
        result_status = str(result.get("status") or "ACTION_REQUIRED")
        run_document.completed_at = utcnow()
        if result_status in {"READY", "READY_WITH_FALLBACK"}:
            run_document.status = "COMPLETED"
            run_document.failure_code = None
            run_document.failure_message = None
            run_document.failure_retriable = False
        else:
            failed_stage = str(result.get("failed_stage") or "EVIDENCE_ANALYSIS")
            error_type = str(result.get("error_type") or "FAILED")
            run_document.status = "FAILED"
            run_document.failure_code = f"{failed_stage}_{error_type}"[:100]
            run_document.failure_message = str(
                result.get("error_summary")
                or f"{failed_stage.title()} did not produce a synthesis-ready result"
            )[:4_000]
            run_document.failure_retriable = failed_stage in {
                "EXTRACTION",
                "ADJUDICATION",
                "NARRATIVE",
            }
        db.flush()
        counts = refresh_progress(db, assessment.id, commit=False)
        db.commit()
        return counts


def _finish_document(
    assessment_id: str,
    document_id: str,
    result: dict[str, Any],
    *,
    publish_progress: bool,
) -> dict[str, Any]:
    if publish_progress:
        record_document_progress(assessment_id, document_id, result)
    return result


def _run_evidence_document_pipeline(
    assessment_id: str,
    document_id: str,
    actor_user_id: str,
    *,
    publish_progress: bool,
) -> dict[str, Any]:
    try:
        prepared = prepare_extraction_fanout(assessment_id, document_id, actor_user_id)
        workflow_run_id = str(prepared["workflow_run_id"])
        plan_hash = str(prepared["plan_hash"])
        unit_handles = []
        for index, unit_id in enumerate(prepared["unit_ids"]):
            with SetWorkflowID(
                f"definition-evidence-unit:{assessment_id}:{document_id}:{plan_hash}:{index}"
            ):
                unit_handles.append(
                    EVIDENCE_EXTRACTION_UNIT_QUEUE.enqueue(
                        evidence_extraction_unit_workflow,
                        assessment_id,
                        document_id,
                        actor_user_id,
                        workflow_run_id,
                        unit_id,
                    )
                )
        # All units are dispatched before the first wait. The extraction queue,
        # rather than document order, now governs provider concurrency.
        unit_results = [handle.get_result() for handle in unit_handles]
        extraction = finalize_extraction_fanout(
            assessment_id,
            document_id,
            actor_user_id,
            workflow_run_id,
            unit_results,
        )
    except Exception as exc:
        logger.exception(
            "Evidence extraction failed assessment_id=%s document_id=%s",
            assessment_id,
            document_id,
        )
        return _finish_document(
            assessment_id,
            document_id,
            _document_failure(document_id, "EXTRACTION", exc),
            publish_progress=publish_progress,
        )
    report = (extraction.get("documents") or [{}])[0]
    observations_artifact_id = report.get("observations_artifact_id")
    if not observations_artifact_id:
        return _finish_document(
            assessment_id,
            document_id,
            {
                "matter_document_id": document_id,
                "status": "ACTION_REQUIRED",
                "failed_stage": "EXTRACTION",
                "error_type": report.get("error_type") or "INCOMPLETE_EXTRACTION",
            },
            publish_progress=publish_progress,
        )
    try:
        with SetWorkflowID(
            f"definition-evidence-adjudication:{assessment_id}:{document_id}:{observations_artifact_id}"
        ):
            adjudication_handle = EVIDENCE_ADJUDICATION_QUEUE.enqueue(
                evidence_adjudication_workflow,
                assessment_id,
                str(observations_artifact_id),
                actor_user_id,
            )
        adjudication = adjudication_handle.get_result()
    except Exception as exc:
        logger.exception(
            "Evidence adjudication failed assessment_id=%s document_id=%s",
            assessment_id,
            document_id,
        )
        return _finish_document(
            assessment_id,
            document_id,
            _document_failure(document_id, "ADJUDICATION", exc),
            publish_progress=publish_progress,
        )
    compatibility_artifact_id = adjudication.get("compatibility_result_artifact_id")
    if adjudication.get("compatibility_projection_status") != "COMPLETED" or not compatibility_artifact_id:
        return _finish_document(
            assessment_id,
            document_id,
            {
                "matter_document_id": document_id,
                "status": "ACTION_REQUIRED",
                "failed_stage": "ADJUDICATION",
                "error_type": "INCOMPLETE_COMPATIBILITY_PROJECTION",
            },
            publish_progress=publish_progress,
        )
    try:
        with SetWorkflowID(
            f"definition-evidence-narrative:{assessment_id}:{document_id}:{compatibility_artifact_id}"
        ):
            narrative_handle = EVIDENCE_NARRATIVE_QUEUE.enqueue(
                evidence_narrative_workflow,
                assessment_id,
                str(compatibility_artifact_id),
                actor_user_id,
            )
        narrative = narrative_handle.get_result()
    except Exception as exc:
        logger.exception(
            "Optional evidence narrative failed assessment_id=%s document_id=%s",
            assessment_id,
            document_id,
        )
        return _finish_document(
            assessment_id,
            document_id,
            {
                "matter_document_id": document_id,
                "status": "READY_WITH_FALLBACK",
                "compatibility_result_artifact_id": str(compatibility_artifact_id),
                "failed_stage": "NARRATIVE",
                "error_type": type(exc).__name__,
            },
            publish_progress=publish_progress,
        )
    return _finish_document(
        assessment_id,
        document_id,
        {
            "matter_document_id": document_id,
            "status": (
                "READY"
                if narrative.get("narrative_status") == "COMPLETED"
                else "READY_WITH_FALLBACK"
            ),
            "compatibility_result_artifact_id": str(compatibility_artifact_id),
            "narrative_artifact_id": narrative.get("narrative_artifact_id"),
        },
        publish_progress=publish_progress,
    )


@DBOS.workflow(name="matter_definition_evidence_document_v2")
def evidence_document_workflow(
    assessment_id: str,
    document_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    return _run_evidence_document_pipeline(
        assessment_id,
        document_id,
        actor_user_id,
        publish_progress=False,
    )


@DBOS.workflow(name="matter_definition_evidence_document_v3")
def evidence_document_workflow_v3(
    assessment_id: str,
    document_id: str,
    actor_user_id: str,
) -> dict[str, Any]:
    return _run_evidence_document_pipeline(
        assessment_id,
        document_id,
        actor_user_id,
        publish_progress=True,
    )


def _last_workflow_id(outcome) -> uuid.UUID | None:
    for stage in (
        outcome.narrative,
        outcome.compatibility_projection,
        outcome.adjudication,
        outcome.extraction,
    ):
        if stage.workflow_run_id is not None:
            return stage.workflow_run_id
    return None


def _reconciled_readiness(outcome, child_result: dict[str, Any]) -> str:
    readiness = str(outcome.synthesis_readiness)
    if child_result.get("status") == "ACTION_REQUIRED":
        return "ACTION_REQUIRED"
    if child_result.get("status") == "READY_WITH_FALLBACK" and readiness == "READY":
        return "READY_WITH_FALLBACK"
    return readiness


@DBOS.step(name="reconcile_evidence_assessment_outcomes", retries_allowed=True, max_attempts=5)
def reconcile_evidence_outcomes(
    assessment_id: str,
    actor_user_id: str,
    document_results: list[dict[str, Any]],
) -> dict[str, int]:
    with SessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(assessment_id))
        if assessment is None or assessment.review_batch_run_id is None:
            raise ValueError("Evidence-first assessment batch run is unavailable")
        prepared = prepare_evidence_outcome_manifest(
            db,
            actor_user_id=uuid.UUID(actor_user_id),
            assessment_id=assessment.id,
            require_operator_role=True,
        )
        results_by_document = {str(value.get("matter_document_id")): value for value in document_results}
        counts = {"completed": 0, "failed": 0, "fallback": 0}
        for outcome in prepared.manifest.documents:
            run_document = db.get(
                ReviewBatchRunDocument,
                (assessment.review_batch_run_id, outcome.matter_document_id),
            )
            if run_document is None:
                raise ValueError("Evidence outcome is missing its review-batch document")
            child_result = results_by_document.get(str(outcome.matter_document_id), {})
            readiness = _reconciled_readiness(outcome, child_result)
            run_document.last_workflow_run_id = _last_workflow_id(outcome)
            run_document.completed_at = utcnow()
            if readiness in {"READY", "READY_WITH_FALLBACK"}:
                run_document.status = "COMPLETED"
                run_document.failure_code = None
                run_document.failure_message = None
                run_document.failure_retriable = False
                counts["completed"] += 1
                if readiness == "READY_WITH_FALLBACK":
                    counts["fallback"] += 1
            else:
                run_document.status = "FAILED"
                failed_stage = str(child_result.get("failed_stage") or "EVIDENCE_ANALYSIS")
                error_type = str(child_result.get("error_type") or "FAILED")
                run_document.failure_code = f"{failed_stage}_{error_type}"[:100]
                run_document.failure_message = str(
                    child_result.get("error_summary")
                    or f"{failed_stage.title()} did not produce a synthesis-ready result"
                )[:4_000]
                run_document.failure_retriable = bool(outcome.retryable_stages)
                counts["failed"] += 1
        orchestration = dict(assessment.configuration_snapshot.get("evidence_first_orchestration") or {})
        orchestration["document_outcome_counts"] = counts
        orchestration["reconciled_at"] = datetime.now(timezone.utc).isoformat()
        assessment.configuration_snapshot = {
            **assessment.configuration_snapshot,
            "evidence_first_orchestration": orchestration,
        }
        db.commit()
        return counts


def _run_evidence_assessment(
    assessment_id: str,
    actor_user_id: str,
    attempt_id: str,
    *,
    document_workflow: Any,
) -> None:
    logger.info("Starting evidence-first Matter Definition assessment assessment_id=%s", assessment_id)
    try:
        activate_evidence_first_assessment(assessment_id, actor_user_id)
        plan(assessment_id)
        document_ids = retrieve(assessment_id)
        if not document_ids:
            raise RuntimeError("Assessment retrieval stopped before creating a partial batch")
        sync_batch_search(assessment_id)
        handles = []
        for index, document_id in enumerate(document_ids):
            with SetWorkflowID(f"definition-evidence-assessment:{assessment_id}:{attempt_id}:document:{index}"):
                handles.append(
                    EVIDENCE_DOCUMENT_QUEUE.enqueue(
                        document_workflow,
                        assessment_id,
                        document_id,
                        actor_user_id,
                    )
                )
        document_results = [handle.get_result() for handle in handles]
        reconcile_evidence_outcomes(assessment_id, actor_user_id, document_results)
        synthesize(assessment_id)
        sync_batch_search(assessment_id)
        complete(assessment_id)
        logger.info("Completed evidence-first assessment assessment_id=%s", assessment_id)
    except Exception as exc:
        logger.exception("Evidence-first assessment failed assessment_id=%s", assessment_id)
        fail(assessment_id, str(exc))
        raise


@DBOS.workflow(name="matter_definition_evidence_assessment_v2")
def evidence_assessment_workflow(
    assessment_id: str,
    actor_user_id: str,
    attempt_id: str,
) -> None:
    _run_evidence_assessment(
        assessment_id,
        actor_user_id,
        attempt_id,
        document_workflow=evidence_document_workflow,
    )


@DBOS.workflow(name="matter_definition_evidence_assessment_v3")
def evidence_assessment_workflow_v3(
    assessment_id: str,
    actor_user_id: str,
    attempt_id: str,
) -> None:
    _run_evidence_assessment(
        assessment_id,
        actor_user_id,
        attempt_id,
        document_workflow=evidence_document_workflow_v3,
    )
