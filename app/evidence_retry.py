"""Durable, stage-scoped retry scheduling for evidence-first analysis."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import Settings, get_settings
from app.evidence_adjudication_launcher import prepare_adjudication_shadow_launch
from app.evidence_first_launcher import prepare_evidence_shadow_launch
from app.evidence_narrative_launcher import prepare_narrative_shadow_launch
from app.evidence_outcome_launcher import prepare_evidence_outcome_manifest
from app.models import WorkflowRun, WorkflowStepRun, utcnow
from app.workflows.dispatcher import enqueue_evidence_stage_retry

EvidenceRetryStage = Literal["EXTRACTION", "ADJUDICATION", "NARRATIVE"]


class EvidenceStageRetryError(ValueError):
    """Raised when an evidence stage cannot be retried safely."""


@dataclass(frozen=True)
class EvidenceStageRetry:
    assessment_id: uuid.UUID
    matter_document_id: uuid.UUID
    stage: EvidenceRetryStage
    target_workflow_run_id: uuid.UUID
    operation_workflow_id: str
    reused_completed_work: bool = True
    superseded_workflow_run_ids: tuple[uuid.UUID, ...] = ()


_DOWNSTREAM_JOB_TYPES: dict[EvidenceRetryStage, frozenset[str]] = {
    "EXTRACTION": frozenset(
        {
            "MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW",
            "MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW",
        }
    ),
    "ADJUDICATION": frozenset({"MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW"}),
    "NARRATIVE": frozenset(),
}
_ACTIVE_WORKFLOW_STATUSES = frozenset({"QUEUED", "RUNNING", "WAITING"})


def _downstream_workflows(
    db: Session,
    *,
    assessment_id: uuid.UUID,
    matter_document_id: uuid.UUID,
    stage: EvidenceRetryStage,
    matter_id: uuid.UUID | None,
) -> list[WorkflowRun]:
    job_types = _DOWNSTREAM_JOB_TYPES[stage]
    if not job_types:
        return []
    candidates = list(
        db.scalars(
            select(WorkflowRun)
            .where(
                WorkflowRun.matter_id == matter_id,
                WorkflowRun.job_type.in_(job_types),
            )
            .order_by(WorkflowRun.created_at, WorkflowRun.id)
            .with_for_update()
        )
    )
    return [
        workflow
        for workflow in candidates
        if str((workflow.input_snapshot or {}).get("assessment_id")) == str(assessment_id)
        and str((workflow.input_snapshot or {}).get("matter_document_id"))
        == str(matter_document_id)
        and not (workflow.configuration_snapshot or {}).get(
            "superseded_by_evidence_retry"
        )
    ]


def _supersede_downstream_workflows(
    workflows: list[WorkflowRun],
    *,
    stage: EvidenceRetryStage,
    attempt_id: uuid.UUID,
    operation_workflow_id: str,
    requested_at: datetime,
) -> tuple[uuid.UUID, ...]:
    active = [workflow for workflow in workflows if workflow.status in _ACTIVE_WORKFLOW_STATUSES]
    if active:
        labels = ", ".join(str(workflow.id) for workflow in active)
        raise EvidenceStageRetryError(
            "A downstream evidence stage is still active; wait for it to finish before "
            f"retrying {stage.lower()} ({labels})"
        )
    superseded: list[uuid.UUID] = []
    marker = {
        "attempt_id": str(attempt_id),
        "operation_workflow_id": operation_workflow_id,
        "upstream_stage": stage,
        "requested_at": requested_at.isoformat(),
    }
    for workflow in workflows:
        workflow.status = "CANCELED"
        workflow.current_stage = "COMPLETED"
        workflow.completed_at = requested_at
        workflow.failure_code = "UPSTREAM_RETRY_SUPERSEDED"
        workflow.failure_summary = (
            f"Superseded because the upstream {stage.lower()} stage was retried"
        )
        workflow.failure_retriable = False
        workflow.error_message = None
        workflow.configuration_snapshot = {
            **(workflow.configuration_snapshot or {}),
            "superseded_by_evidence_retry": marker,
        }
        superseded.append(workflow.id)
    return tuple(superseded)


def _retry_stage_outcome(document, stage: EvidenceRetryStage):
    return {
        "EXTRACTION": document.extraction,
        "ADJUDICATION": document.adjudication,
        "NARRATIVE": document.narrative,
    }[stage]


def _validate_frozen_retry_input(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    assessment_id: uuid.UUID,
    stage: EvidenceRetryStage,
    workflow: WorkflowRun,
    settings: Settings,
) -> None:
    snapshot = workflow.input_snapshot or {}
    if stage == "EXTRACTION":
        document_ids = [uuid.UUID(str(value)) for value in snapshot.get("matter_document_ids") or ()]
        if not document_ids:
            raise EvidenceStageRetryError("The extraction retry has no frozen document population")
        prepared = prepare_evidence_shadow_launch(
            db,
            actor_user_id=actor_user_id,
            assessment_id=assessment_id,
            document_ids=document_ids,
            settings=settings,
        )
        if snapshot.get("plan_hashes") != [value.plan.plan_hash for value in prepared.documents]:
            raise EvidenceStageRetryError(
                "The extraction plan no longer matches the frozen run; start a new shadow run"
            )
        return
    if stage == "ADJUDICATION":
        artifact_id = snapshot.get("observations_artifact_id")
        if not artifact_id:
            raise EvidenceStageRetryError("The adjudication retry has no frozen observations artifact")
        prepared = prepare_adjudication_shadow_launch(
            db,
            actor_user_id=actor_user_id,
            assessment_id=assessment_id,
            observations_artifact_id=uuid.UUID(str(artifact_id)),
            settings=settings,
        )
        if snapshot.get("adjudication_plan_hash") != prepared.plan.plan_hash:
            raise EvidenceStageRetryError(
                "The adjudication plan no longer matches the frozen run; start a new shadow run"
            )
        return
    artifact_id = snapshot.get("compatibility_result_artifact_id")
    if not artifact_id:
        raise EvidenceStageRetryError("The narrative retry has no deterministic compatibility result")
    prepared = prepare_narrative_shadow_launch(
        db,
        actor_user_id=actor_user_id,
        assessment_id=assessment_id,
        compatibility_artifact_id=uuid.UUID(str(artifact_id)),
        settings=settings,
    )
    if snapshot.get("compatibility_result_hash") != prepared.compatibility_result.result_hash:
        raise EvidenceStageRetryError(
            "The narrative input no longer matches the frozen run; start a new shadow run"
        )


def _queue_steps(db: Session, workflow: WorkflowRun, stage: EvidenceRetryStage) -> None:
    rows = list(
        db.scalars(
            select(WorkflowStepRun)
            .where(WorkflowStepRun.workflow_run_id == workflow.id)
            .order_by(WorkflowStepRun.ordinal)
        )
    )
    selected = rows if stage == "ADJUDICATION" else rows[:1]
    for step in selected:
        step.status = "QUEUED"
        step.started_at = None
        step.completed_at = None
        step.error_message = None
        if stage == "NARRATIVE":
            step.completed_count = 0
            step.failed_count = 0


def schedule_evidence_stage_retry(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    assessment_id: uuid.UUID,
    matter_document_id: uuid.UUID,
    stage: EvidenceRetryStage,
    settings: Settings | None = None,
) -> EvidenceStageRetry:
    active_settings = settings or get_settings()
    if not active_settings.definition_assessment_evidence_shadow_enabled:
        raise EvidenceStageRetryError("Evidence-first shadow execution is disabled")
    if not active_settings.dbos_enabled:
        raise EvidenceStageRetryError("Durable workflow execution is disabled")

    prepared_outcomes = prepare_evidence_outcome_manifest(
        db,
        actor_user_id=actor_user_id,
        assessment_id=assessment_id,
    )
    document = next(
        (
            value
            for value in prepared_outcomes.manifest.documents
            if value.matter_document_id == matter_document_id
        ),
        None,
    )
    if document is None:
        raise EvidenceStageRetryError("Document is not in the frozen assessment population")
    if stage not in document.retryable_stages:
        raise EvidenceStageRetryError(f"{stage.title()} is not currently retryable")
    stage_outcome = _retry_stage_outcome(document, stage)
    if stage_outcome.workflow_run_id is None:
        raise EvidenceStageRetryError(f"{stage.title()} has no durable workflow to resume")

    workflow = db.scalar(
        select(WorkflowRun)
        .where(WorkflowRun.id == stage_outcome.workflow_run_id)
        .with_for_update()
    )
    if workflow is None:
        raise EvidenceStageRetryError("The retry target workflow is unavailable")
    if workflow.status in _ACTIVE_WORKFLOW_STATUSES:
        raise EvidenceStageRetryError("This evidence stage is already active")
    _validate_frozen_retry_input(
        db,
        actor_user_id=actor_user_id,
        assessment_id=assessment_id,
        stage=stage,
        workflow=workflow,
        settings=active_settings,
    )

    attempt_id = uuid.uuid4()
    operation_workflow_id = f"evidence-stage-retry:{workflow.id}:{attempt_id}"
    requested_at = utcnow()
    downstream = _downstream_workflows(
        db,
        assessment_id=assessment_id,
        matter_document_id=matter_document_id,
        stage=stage,
        matter_id=getattr(workflow, "matter_id", None),
    )
    superseded_workflow_run_ids = _supersede_downstream_workflows(
        downstream,
        stage=stage,
        attempt_id=attempt_id,
        operation_workflow_id=operation_workflow_id,
        requested_at=requested_at,
    )
    workflow.status = "QUEUED"
    workflow.current_stage = stage
    workflow.completed_at = None
    workflow.failure_code = None
    workflow.failure_summary = None
    workflow.failure_retriable = False
    workflow.error_message = None
    workflow.configuration_snapshot = {
        **(workflow.configuration_snapshot or {}),
        "retry_request": {
            "attempt_id": str(attempt_id),
            "operation_workflow_id": operation_workflow_id,
            "stage": stage,
            "requested_by_user_id": str(actor_user_id),
            "requested_at": requested_at.isoformat(),
            "superseded_workflow_run_ids": [
                str(value) for value in superseded_workflow_run_ids
            ],
        },
    }
    if stage == "EXTRACTION":
        reports = []
        for report in (workflow.progress or {}).get("documents", []):
            value = dict(report)
            if str(value.get("matter_document_id")) == str(matter_document_id):
                value["status"] = "QUEUED"
            reports.append(value)
        workflow.progress = {**(workflow.progress or {}), "documents": reports}
    elif stage == "ADJUDICATION":
        workflow.progress = {
            key: value
            for key, value in (workflow.progress or {}).items()
            if key
            not in {
                "criterion_decisions_artifact_id",
                "document_decision_status",
                "document_decision_artifact_id",
                "compatibility_projection_status",
                "compatibility_result_artifact_id",
            }
        }
    else:
        workflow.progress = {
            key: value
            for key, value in (workflow.progress or {}).items()
            if key not in {"narrative_status", "narrative_artifact_id"}
        }
    _queue_steps(db, workflow, stage)
    record_audit(
        db,
        tenant_id=workflow.tenant_id,
        actor_user_id=actor_user_id,
        action="matter_definition.evidence_stage_retry.queued",
        target_type="workflow_run",
        target_id=workflow.id,
        details={
            "assessment_id": str(assessment_id),
            "matter_document_id": str(matter_document_id),
            "stage": stage,
            "operation_workflow_id": operation_workflow_id,
            "reuses_completed_work": True,
            "superseded_workflow_run_ids": [
                str(value) for value in superseded_workflow_run_ids
            ],
            "authoritative": False,
        },
    )
    db.flush()
    enqueue_evidence_stage_retry(
        db,
        operation_workflow_id,
        assessment_id=str(assessment_id),
        target_workflow_run_id=str(workflow.id),
        stage=stage,
        actor_user_id=str(actor_user_id),
        matter_document_id=str(matter_document_id),
    )
    return EvidenceStageRetry(
        assessment_id=assessment_id,
        matter_document_id=matter_document_id,
        stage=stage,
        target_workflow_run_id=workflow.id,
        operation_workflow_id=operation_workflow_id,
        superseded_workflow_run_ids=superseded_workflow_run_ids,
    )
