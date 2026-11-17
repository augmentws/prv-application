"""Worker-owned orchestration for the smallest evidence-first retry boundary."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from dbos import DBOS, Queue
from sqlalchemy import select

from app.audit import record_audit
from app.database import SessionLocal
from app.evidence_adjudication_launcher import (
    execute_prepared_adjudication_shadow,
    prepare_adjudication_shadow_launch,
)
from app.evidence_first_launcher import (
    execute_prepared_evidence_shadow,
    prepare_evidence_shadow_launch,
)
from app.evidence_narrative_launcher import (
    execute_prepared_narrative_shadow,
    prepare_narrative_shadow_launch,
)
from app.evidence_routing import (
    EVIDENCE_FIRST_SYNTHESIS_SOURCE,
    SYNTHESIS_DOCUMENT_SOURCE_KEY,
)
from app.models import (
    MatterDefinitionAssessmentRun,
    ReviewBatchRun,
    WorkflowRun,
    WorkflowStepRun,
    utcnow,
)
from app.workflows.evidence_assessments import reconcile_evidence_outcomes
from app.workflows.matter_definition_assessments import (
    complete,
    sync_batch_search,
    synthesize,
)

logger = logging.getLogger(__name__)

EVIDENCE_RETRY_QUEUE = Queue("evidence-stage-retries", global_concurrency=2)
EVIDENCE_RETRY_STAGES = frozenset({"EXTRACTION", "ADJUDICATION", "NARRATIVE"})


def _target_workflow(db, target_workflow_run_id: str) -> WorkflowRun:
    workflow = db.get(WorkflowRun, uuid.UUID(target_workflow_run_id))
    if workflow is None:
        raise ValueError("Evidence retry target workflow is unavailable")
    return workflow


def _reactivate_matching_superseded_workflow(db, *, dbos_workflow_id: str) -> None:
    workflow = db.scalar(
        select(WorkflowRun)
        .where(WorkflowRun.dbos_workflow_id == dbos_workflow_id)
        .with_for_update()
    )
    if workflow is None:
        return
    configuration = dict(workflow.configuration_snapshot or {})
    marker = configuration.pop("superseded_by_evidence_retry", None)
    if not marker:
        return
    history = list(configuration.get("evidence_retry_reactivation_history") or ())
    history.append(marker)
    configuration["evidence_retry_reactivation_history"] = history[-25:]
    workflow.configuration_snapshot = configuration
    db.commit()


@DBOS.step(name="execute_evidence_stage_retry")
def execute_evidence_stage_retry(
    assessment_id: str,
    target_workflow_run_id: str,
    stage: str,
    actor_user_id: str,
) -> dict[str, Any]:
    if stage not in EVIDENCE_RETRY_STAGES:
        raise ValueError(f"Unsupported evidence retry stage: {stage}")
    assessment_uuid = uuid.UUID(assessment_id)
    workflow_uuid = uuid.UUID(target_workflow_run_id)
    actor_uuid = uuid.UUID(actor_user_id)
    with SessionLocal() as db:
        workflow = _target_workflow(db, target_workflow_run_id)
        snapshot = workflow.input_snapshot or {}
        if stage == "EXTRACTION":
            document_ids = [
                uuid.UUID(str(value))
                for value in snapshot.get("matter_document_ids") or ()
            ]
            if not document_ids:
                raise ValueError("Evidence extraction retry has no frozen documents")
            prepared = prepare_evidence_shadow_launch(
                db,
                actor_user_id=actor_uuid,
                assessment_id=assessment_uuid,
                document_ids=document_ids,
            )
            return asyncio.run(
                execute_prepared_evidence_shadow(
                    db,
                    prepared,
                    retry_failed=True,
                    workflow_run_id=workflow_uuid,
                )
            )
        if stage == "ADJUDICATION":
            artifact_id = snapshot.get("observations_artifact_id")
            if not artifact_id:
                raise ValueError("Evidence adjudication retry has no observations artifact")
            prepared = prepare_adjudication_shadow_launch(
                db,
                actor_user_id=actor_uuid,
                assessment_id=assessment_uuid,
                observations_artifact_id=uuid.UUID(str(artifact_id)),
            )
            return asyncio.run(
                execute_prepared_adjudication_shadow(
                    db,
                    prepared,
                    retry_failed=True,
                    workflow_run_id=workflow_uuid,
                )
            )
        artifact_id = snapshot.get("compatibility_result_artifact_id")
        if not artifact_id:
            raise ValueError("Evidence narrative retry has no compatibility result")
        prepared = prepare_narrative_shadow_launch(
            db,
            actor_user_id=actor_uuid,
            assessment_id=assessment_uuid,
            compatibility_artifact_id=uuid.UUID(str(artifact_id)),
        )
        return asyncio.run(
            execute_prepared_narrative_shadow(
                db,
                prepared,
                retry_failed=True,
                workflow_run_id=workflow_uuid,
            )
        )


@DBOS.step(name="propagate_evidence_retry_adjudication", retries_allowed=True, max_attempts=3)
def propagate_evidence_retry_adjudication(
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
        _reactivate_matching_superseded_workflow(
            db,
            dbos_workflow_id=(
                f"definition-evidence-adjudication-shadow:{prepared.launch_key}"
            ),
        )
        result = asyncio.run(execute_prepared_adjudication_shadow(db, prepared))
        workflow_id = result.get("workflow_run_id")
        if workflow_id:
            workflow = db.get(WorkflowRun, uuid.UUID(str(workflow_id)))
            if workflow is not None and "superseded_by_evidence_retry" in (
                workflow.configuration_snapshot or {}
            ):
                configuration = dict(workflow.configuration_snapshot or {})
                configuration.pop("superseded_by_evidence_retry", None)
                workflow.configuration_snapshot = configuration
                db.commit()
        return result


@DBOS.step(name="propagate_evidence_retry_narrative", retries_allowed=True, max_attempts=3)
def propagate_evidence_retry_narrative(
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
        _reactivate_matching_superseded_workflow(
            db,
            dbos_workflow_id=f"definition-evidence-narrative-shadow:{prepared.launch_key}",
        )
        result = asyncio.run(execute_prepared_narrative_shadow(db, prepared))
        workflow_id = result.get("workflow_run_id")
        if workflow_id:
            workflow = db.get(WorkflowRun, uuid.UUID(str(workflow_id)))
            if workflow is not None and "superseded_by_evidence_retry" in (
                workflow.configuration_snapshot or {}
            ):
                configuration = dict(workflow.configuration_snapshot or {})
                configuration.pop("superseded_by_evidence_retry", None)
                workflow.configuration_snapshot = configuration
                db.commit()
        return result


def _extraction_document_report(
    result: dict[str, Any], matter_document_id: str
) -> dict[str, Any]:
    return next(
        (
            value
            for value in result.get("documents") or ()
            if str(value.get("matter_document_id")) == matter_document_id
        ),
        {},
    )


def _document_result_from_narrative(
    *,
    matter_document_id: str,
    compatibility_artifact_id: str,
    narrative: dict[str, Any],
) -> dict[str, Any]:
    narrative_status = str(narrative.get("narrative_status") or "FAILED")
    return {
        "matter_document_id": matter_document_id,
        "status": "READY" if narrative_status == "COMPLETED" else "READY_WITH_FALLBACK",
        "compatibility_result_artifact_id": compatibility_artifact_id,
        "narrative_artifact_id": narrative.get("narrative_artifact_id"),
        **(
            {}
            if narrative_status == "COMPLETED"
            else {"failed_stage": "NARRATIVE", "error_type": "NARRATIVE_FALLBACK"}
        ),
    }


def _propagate_retry_descendants(
    *,
    assessment_id: str,
    matter_document_id: str,
    stage: str,
    actor_user_id: str,
    stage_result: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    propagated: list[str] = []
    if stage == "NARRATIVE":
        compatibility_artifact_id = str(
            stage_result.get("fallback_compatibility_result_artifact_id") or ""
        )
        if not compatibility_artifact_id:
            raise ValueError("Narrative retry did not retain its compatibility result")
        return propagated, _document_result_from_narrative(
            matter_document_id=matter_document_id,
            compatibility_artifact_id=compatibility_artifact_id,
            narrative=stage_result,
        )

    adjudication = stage_result
    if stage == "EXTRACTION":
        extraction_report = _extraction_document_report(stage_result, matter_document_id)
        observations_artifact_id = extraction_report.get("observations_artifact_id")
        if not observations_artifact_id:
            raise ValueError("Extraction retry did not produce observations for the selected document")
        adjudication = propagate_evidence_retry_adjudication(
            assessment_id,
            str(observations_artifact_id),
            actor_user_id,
        )
        propagated.append("ADJUDICATION")

    compatibility_artifact_id = adjudication.get("compatibility_result_artifact_id")
    if (
        adjudication.get("compatibility_projection_status") != "COMPLETED"
        or not compatibility_artifact_id
    ):
        return propagated, {
            "matter_document_id": matter_document_id,
            "status": "ACTION_REQUIRED",
            "failed_stage": "ADJUDICATION",
            "error_type": "INCOMPLETE_COMPATIBILITY_PROJECTION",
        }
    narrative = propagate_evidence_retry_narrative(
        assessment_id,
        str(compatibility_artifact_id),
        actor_user_id,
    )
    propagated.append("NARRATIVE")
    return propagated, _document_result_from_narrative(
        matter_document_id=matter_document_id,
        compatibility_artifact_id=str(compatibility_artifact_id),
        narrative=narrative,
    )


@DBOS.step(name="evidence_retry_is_authoritative")
def evidence_retry_is_authoritative(assessment_id: str) -> bool:
    with SessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(assessment_id))
        if assessment is None:
            raise ValueError("Matter Definition assessment not found")
        return bool(
            assessment.configuration_snapshot.get(SYNTHESIS_DOCUMENT_SOURCE_KEY)
            == EVIDENCE_FIRST_SYNTHESIS_SOURCE
            and isinstance(
                assessment.configuration_snapshot.get("evidence_first_orchestration"),
                dict,
            )
        )


@DBOS.step(name="prepare_evidence_retry_resynthesis", retries_allowed=True, max_attempts=3)
def prepare_evidence_retry_resynthesis(
    assessment_id: str,
    matter_document_id: str,
    stage: str,
    actor_user_id: str,
) -> bool:
    with SessionLocal() as db:
        assessment = db.scalar(
            select(MatterDefinitionAssessmentRun)
            .where(MatterDefinitionAssessmentRun.id == uuid.UUID(assessment_id))
            .with_for_update()
        )
        if assessment is None:
            raise ValueError("Matter Definition assessment not found")
        orchestration = assessment.configuration_snapshot.get(
            "evidence_first_orchestration"
        )
        if (
            assessment.configuration_snapshot.get(SYNTHESIS_DOCUMENT_SOURCE_KEY)
            != EVIDENCE_FIRST_SYNTHESIS_SOURCE
            or not isinstance(orchestration, dict)
        ):
            return False

        history = list(
            assessment.configuration_snapshot.get("evidence_resynthesis_history") or ()
        )
        history.append(
            {
                "matter_document_id": matter_document_id,
                "upstream_stage": stage,
                "requested_by_user_id": actor_user_id,
                "requested_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        assessment.configuration_snapshot = {
            **assessment.configuration_snapshot,
            "evidence_resynthesis_history": history[-25:],
        }
        assessment.status = "SYNTHESIZING"
        assessment.error_message = None
        assessment.completed_at = None
        assessment.canceled_at = None
        assessment.synthesis_result = None
        assessment.synthesis_status = "NOT_STARTED"
        assessment.completion_disposition = None
        assessment.current_synthesis_attempt_id = None
        assessment.synthesis_diagnostics = {}
        assessment.coverage_snapshot = None
        assessment.partial_coverage_count = 0
        assessment.invalid_result_count = 0

        workflow = db.scalar(
            select(WorkflowRun)
            .where(WorkflowRun.id == assessment.workflow_run_id)
            .with_for_update()
        )
        if workflow is None:
            raise ValueError("Assessment workflow is unavailable")
        workflow.status = "RUNNING"
        workflow.current_stage = "SYNTHESIS"
        workflow.completed_at = None
        workflow.failure_code = None
        workflow.failure_summary = None
        workflow.failure_retriable = False
        workflow.error_message = None
        workflow.configuration_snapshot = {
            **(workflow.configuration_snapshot or {}),
            "last_evidence_resynthesis": history[-1],
        }
        synthesis_step = db.scalar(
            select(WorkflowStepRun).where(
                WorkflowStepRun.workflow_run_id == workflow.id,
                WorkflowStepRun.ordinal == 4,
            )
        )
        if synthesis_step is None:
            synthesis_step = WorkflowStepRun(
                workflow_run_id=workflow.id,
                role_key="assessment_synthesis",
                ordinal=4,
                status="QUEUED",
                total_count=1,
            )
            db.add(synthesis_step)
        else:
            synthesis_step.status = "QUEUED"
            synthesis_step.started_at = None
            synthesis_step.completed_at = None
            synthesis_step.completed_count = 0
            synthesis_step.failed_count = 0
            synthesis_step.error_message = None
        review_run = (
            db.get(ReviewBatchRun, assessment.review_batch_run_id)
            if assessment.review_batch_run_id is not None
            else None
        )
        if review_run is not None:
            review_run.status = "RUNNING"
            review_run.completed_at = None
            review_run.error_message = None
        record_audit(
            db,
            tenant_id=workflow.tenant_id,
            actor_user_id=uuid.UUID(actor_user_id),
            action="matter_definition.evidence_retry.resynthesis_started",
            target_type="matter_definition_assessment_run",
            target_id=assessment.id,
            details=history[-1],
        )
        db.commit()
        return True


@DBOS.step(name="fail_evidence_stage_retry")
def fail_evidence_stage_retry(
    target_workflow_run_id: str,
    stage: str,
    error_type: str,
) -> None:
    with SessionLocal() as db:
        workflow = db.scalar(
            select(WorkflowRun)
            .where(WorkflowRun.id == uuid.UUID(target_workflow_run_id))
            .with_for_update()
        )
        if workflow is None:
            return
        completed_at = utcnow()
        message = f"{error_type}: {stage.lower()} retry did not complete"
        if workflow.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
            workflow.configuration_snapshot = {
                **(workflow.configuration_snapshot or {}),
                "downstream_retry_failure": {
                    "stage": stage,
                    "error_type": error_type,
                    "recorded_at": completed_at.isoformat(),
                },
            }
            db.commit()
            return
        if not workflow.failure_code:
            workflow.failure_code = f"{stage}_RETRY_FAILED"
        if not workflow.failure_summary:
            workflow.failure_summary = message
        workflow.failure_retriable = True
        workflow.error_message = workflow.error_message or message
        workflow.status = "COMPLETED_WITH_ERRORS" if stage == "NARRATIVE" else "FAILED"
        workflow.current_stage = "COMPLETED"
        workflow.completed_at = completed_at
        steps = list(
            db.scalars(
                select(WorkflowStepRun)
                .where(
                    WorkflowStepRun.workflow_run_id == workflow.id,
                    WorkflowStepRun.status.in_(("QUEUED", "RUNNING")),
                )
                .order_by(WorkflowStepRun.ordinal)
            )
        )
        for step in steps:
            step.status = "COMPLETED_WITH_ERRORS" if stage == "NARRATIVE" else "FAILED"
            step.failed_count = max(1, step.failed_count)
            step.error_message = step.error_message or message
            step.completed_at = completed_at
        db.commit()


@DBOS.workflow(name="matter_definition_evidence_stage_retry_v1")
def evidence_stage_retry_workflow(
    assessment_id: str,
    target_workflow_run_id: str,
    stage: str,
    actor_user_id: str,
    matter_document_id: str | None = None,
) -> dict[str, Any] | None:
    try:
        result = execute_evidence_stage_retry(
            assessment_id,
            target_workflow_run_id,
            stage,
            actor_user_id,
        )
        if matter_document_id is None:
            return result
        propagated_stages, document_result = _propagate_retry_descendants(
            assessment_id=assessment_id,
            matter_document_id=matter_document_id,
            stage=stage,
            actor_user_id=actor_user_id,
            stage_result=result,
        )
        authoritative = evidence_retry_is_authoritative(assessment_id)
        if authoritative:
            reconcile_evidence_outcomes(
                assessment_id,
                actor_user_id,
                [document_result],
            )
            if not prepare_evidence_retry_resynthesis(
                assessment_id,
                matter_document_id,
                stage,
                actor_user_id,
            ):
                raise RuntimeError("Evidence-first assessment lost its authoritative routing")
            synthesize(assessment_id)
            sync_batch_search(assessment_id)
            complete(assessment_id)
        return {
            **result,
            "propagated_stages": propagated_stages,
            "document_result": document_result,
            "assessment_resynthesized": authoritative,
        }
    except Exception as exc:
        logger.exception(
            "Evidence stage retry failed assessment_id=%s target_workflow_run_id=%s stage=%s",
            assessment_id,
            target_workflow_run_id,
            stage,
        )
        fail_evidence_stage_retry(target_workflow_run_id, stage, type(exc).__name__)
        return None
