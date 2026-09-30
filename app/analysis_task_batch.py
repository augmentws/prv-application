from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.analysis_tasks import MatterAnalysisTaskConflict
from app.execution_accounting import refresh_workflow_execution_usage
from app.models import (
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    ReviewBatch,
    ReviewBatchDocument,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewDecisionResult,
    WorkflowRun,
    WorkflowStepRun,
    utcnow,
)
from app.workflow_specs import (
    MATTER_ANALYSIS_TASK_BATCH_SPEC,
    MATTER_ANALYSIS_TASK_PLAYGROUND_SPEC,
    binding_snapshot,
    resolve_workflow_skill_bindings,
    validate_skill_version_for_role,
)
from app.workflows.dispatcher import enqueue_analysis_task_batch


def queue_analysis_task_batch(
    db: Session,
    *,
    matter: Matter,
    batch: ReviewBatch,
    task: MatterAnalysisTask,
    task_version: MatterAnalysisTaskVersion,
    initiated_by_user_id: uuid.UUID,
) -> tuple[WorkflowRun, ReviewBatchRun]:
    if batch.matter_id != matter.id or batch.status != "READY":
        raise MatterAnalysisTaskConflict("A ready review batch is required")
    if task.matter_id != matter.id or task_version.matter_analysis_task_id != task.id:
        raise MatterAnalysisTaskConflict("Analysis task version does not belong to this matter")
    if task_version.status != "PUBLISHED" or task_version.compilation_status != "READY":
        raise MatterAnalysisTaskConflict("Batch evaluation requires a ready, published task version")

    documents = list(
        db.scalars(
            select(ReviewBatchDocument)
            .where(ReviewBatchDocument.review_batch_id == batch.id)
            .order_by(ReviewBatchDocument.sequence_number)
        )
    )
    if not documents:
        raise MatterAnalysisTaskConflict("The review batch has no documents")
    active_runs = db.scalars(
        select(ReviewBatchRun).where(
            ReviewBatchRun.review_batch_id == batch.id,
            ReviewBatchRun.run_type == "WORKFLOW",
            ReviewBatchRun.status.in_(("QUEUED", "RUNNING")),
        )
    )
    if any(run.configuration_snapshot.get("task_version_id") == str(task_version.id) for run in active_runs):
        raise MatterAnalysisTaskConflict("This task version is already running against the batch")

    resolved, binding_workflow_key = _resolve_decision_binding(db, matter.client.tenant_id)
    bindings = binding_snapshot(resolved)
    role_binding = bindings["decision_evaluation"]
    role_binding["binding_workflow_key"] = binding_workflow_key
    engine_key = str(role_binding["configuration"].get("engine_key") or "jev")
    workflow_id = f"analysis-task:{task.id}:version:{task_version.version}:batch:{batch.id}:{uuid.uuid4()}"
    workflow = WorkflowRun(
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        matter_id=matter.id,
        workflow_key=MATTER_ANALYSIS_TASK_BATCH_SPEC.key,
        code_version=MATTER_ANALYSIS_TASK_BATCH_SPEC.code_version,
        dbos_workflow_id=workflow_id,
        status="QUEUED",
        input_snapshot={
            "task_id": str(task.id),
            "task_name": task.name,
            "task_version_id": str(task_version.id),
            "task_version": task_version.version,
            "definition_content_hash": task_version.definition_content_hash,
            "specification_content_hash": task_version.specification_content_hash,
            "review_batch_id": str(batch.id),
            "document_count": len(documents),
        },
        binding_snapshot=bindings,
        configuration_snapshot={
            "engine_key": engine_key,
            "model_key": role_binding["model_key"],
            "result_policy": "ISOLATED",
        },
        progress={"stage": "QUEUED", "total": len(documents), "completed": 0, "failed": 0},
        initiated_by_user_id=initiated_by_user_id,
    )
    db.add(workflow)
    db.flush()
    db.add(
        WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key="decision_evaluation",
            ordinal=1,
            fan_out_group="documents",
            status="QUEUED",
            total_count=len(documents),
        )
    )
    review_run = ReviewBatchRun(
        review_batch_id=batch.id,
        run_type="WORKFLOW",
        purpose="REVIEW",
        status="QUEUED",
        result_policy="ISOLATED",
        workflow_run_record_id=workflow.id,
        configuration_snapshot={
            "mode": "BATCH",
            "task_id": str(task.id),
            "task_name": task.name,
            "task_version_id": str(task_version.id),
            "task_version": task_version.version,
            "engine_key": engine_key,
            "model_key": role_binding["model_key"],
        },
        initiated_by_user_id=initiated_by_user_id,
        dbos_workflow_id=workflow_id,
    )
    db.add(review_run)
    db.flush()
    db.add_all(
        [
            ReviewBatchRunDocument(
                review_batch_run_id=review_run.id,
                matter_document_id=document.matter_document_id,
                status="QUEUED",
            )
            for document in documents
        ]
    )
    db.flush()
    enqueue_analysis_task_batch(db, workflow_id, str(workflow.id))
    return workflow, review_run


def _resolve_decision_binding(db: Session, tenant_id: uuid.UUID):
    try:
        return (
            resolve_workflow_skill_bindings(
                db,
                workflow_key=MATTER_ANALYSIS_TASK_BATCH_SPEC.key,
                tenant_id=tenant_id,
            ),
            MATTER_ANALYSIS_TASK_BATCH_SPEC.key,
        )
    except ValueError as batch_error:
        # Existing installations predate the batch workflow binding. Its decision role has the
        # same contract as the playground role, so reuse that configured Jev binding after
        # validating it against the batch specification. A later bootstrap will add the
        # dedicated binding without changing already frozen run provenance.
        try:
            fallback = resolve_workflow_skill_bindings(
                db,
                workflow_key=MATTER_ANALYSIS_TASK_PLAYGROUND_SPEC.key,
                tenant_id=tenant_id,
            )
            binding, definition, version = fallback["decision_evaluation"]
            validate_skill_version_for_role(
                definition,
                version,
                workflow_key=MATTER_ANALYSIS_TASK_BATCH_SPEC.key,
                role_key="decision_evaluation",
            )
        except ValueError as fallback_error:
            raise MatterAnalysisTaskConflict(
                f"Decision evaluation managed skill is not configured: {batch_error}"
            ) from fallback_error
        return {"decision_evaluation": (binding, definition, version)}, MATTER_ANALYSIS_TASK_PLAYGROUND_SPEC.key


def prepare_analysis_task_batch(db: Session, workflow_run_id: uuid.UUID) -> list[uuid.UUID]:
    workflow, review_run, step = _records(db, workflow_run_id)
    document_ids = list(
        db.scalars(
            select(ReviewBatchRunDocument.matter_document_id)
            .where(ReviewBatchRunDocument.review_batch_run_id == review_run.id)
            .order_by(ReviewBatchRunDocument.matter_document_id)
        )
    )
    if not document_ids:
        raise MatterAnalysisTaskConflict("Analysis task batch run has no documents")
    now = utcnow()
    workflow.status = "RUNNING"
    workflow.started_at = workflow.started_at or now
    workflow.progress = {"stage": "EVALUATING", "total": len(document_ids), "completed": 0, "failed": 0}
    review_run.status = "RUNNING"
    review_run.started_at = review_run.started_at or now
    step.status = "RUNNING"
    step.started_at = step.started_at or now
    db.commit()
    return document_ids


def refresh_analysis_task_batch_progress(db: Session, workflow_run_id: uuid.UUID) -> dict[str, int]:
    workflow, review_run, step = _records(db, workflow_run_id)
    # A durable step may be recovered after its application transaction commits but before DBOS
    # records the step result. The immutable Decision Result is authoritative: never allow a late
    # failure callback from a duplicate/recovered attempt to leave that document marked failed.
    db.execute(
        update(ReviewBatchRunDocument)
        .where(
            ReviewBatchRunDocument.review_batch_run_id == review_run.id,
            ReviewBatchRunDocument.status != "COMPLETED",
            ReviewBatchRunDocument.matter_document_id.in_(
                select(ReviewDecisionResult.matter_document_id).where(
                    ReviewDecisionResult.review_batch_run_id == review_run.id
                )
            ),
        )
        .values(status="COMPLETED", completed_at=utcnow())
    )
    db.flush()
    counts = dict(
        db.execute(
            select(ReviewBatchRunDocument.status, func.count())
            .where(ReviewBatchRunDocument.review_batch_run_id == review_run.id)
            .group_by(ReviewBatchRunDocument.status)
        ).all()
    )
    completed = int(counts.get("COMPLETED", 0))
    failed = int(counts.get("FAILED", 0))
    skipped = int(counts.get("SKIPPED", 0))
    processed = completed + failed + skipped
    review_run.processed_document_count = processed
    step.completed_count = completed + skipped
    step.failed_count = failed
    workflow.progress = {
        "stage": "EVALUATING",
        "total": step.total_count,
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
    }
    db.commit()
    return {"completed": completed, "failed": failed, "skipped": skipped, "processed": processed}


def fail_analysis_task_batch_document(
    db: Session,
    workflow_run_id: uuid.UUID,
    matter_document_id: uuid.UUID,
    message: str,
) -> None:
    _, review_run, _ = _records(db, workflow_run_id)
    db.execute(
        update(ReviewBatchRunDocument)
        .where(
            ReviewBatchRunDocument.review_batch_run_id == review_run.id,
            ReviewBatchRunDocument.matter_document_id == matter_document_id,
            ReviewBatchRunDocument.status != "COMPLETED",
            ~select(ReviewDecisionResult.id)
            .where(
                ReviewDecisionResult.review_batch_run_id == review_run.id,
                ReviewDecisionResult.matter_document_id == matter_document_id,
            )
            .exists(),
        )
        .values(status="FAILED", completed_at=utcnow())
    )
    # Do not update the shared workflow or step rows here. Document workers run concurrently;
    # aggregate status and errors once after all children have finished.
    db.commit()


def complete_analysis_task_batch(db: Session, workflow_run_id: uuid.UUID) -> None:
    progress = refresh_analysis_task_batch_progress(db, workflow_run_id)
    workflow, review_run, step = _records(db, workflow_run_id)
    now = utcnow()
    status = "COMPLETED_WITH_ERRORS" if progress["failed"] else "COMPLETED"
    workflow.status = status
    workflow.progress = {"stage": status, "total": step.total_count, **progress}
    workflow.completed_at = now
    review_run.status = status
    review_run.completed_at = now
    step.status = status
    step.completed_at = now
    if progress["failed"]:
        message = f"{progress['failed']} documents could not be evaluated"
        workflow.error_message = message
        review_run.error_message = message
        step.error_message = message
    refresh_workflow_execution_usage(db, workflow.id)
    db.commit()


def fail_analysis_task_batch(db: Session, workflow_run_id: uuid.UUID, message: str) -> None:
    workflow, review_run, step = _records(db, workflow_run_id)
    if workflow.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        return
    now = utcnow()
    workflow.status = "FAILED"
    workflow.error_message = message[:4000]
    workflow.progress = {"stage": "FAILED"}
    workflow.completed_at = now
    review_run.status = "FAILED"
    review_run.error_message = message[:4000]
    review_run.completed_at = now
    step.status = "FAILED"
    step.error_message = message[:4000]
    step.completed_at = now
    for run_document in db.scalars(
        select(ReviewBatchRunDocument).where(
            ReviewBatchRunDocument.review_batch_run_id == review_run.id,
            ReviewBatchRunDocument.status.in_(("QUEUED", "IN_PROGRESS")),
        )
    ):
        run_document.status = "FAILED"
        run_document.completed_at = now
    db.commit()


def _records(
    db: Session,
    workflow_run_id: uuid.UUID,
) -> tuple[WorkflowRun, ReviewBatchRun, WorkflowStepRun]:
    workflow = db.get(WorkflowRun, workflow_run_id)
    if workflow is None or workflow.workflow_key != MATTER_ANALYSIS_TASK_BATCH_SPEC.key:
        raise MatterAnalysisTaskConflict("Analysis task batch workflow not found")
    review_run = db.scalar(
        select(ReviewBatchRun).where(ReviewBatchRun.workflow_run_record_id == workflow.id)
    )
    step = db.scalar(
        select(WorkflowStepRun).where(
            WorkflowStepRun.workflow_run_id == workflow.id,
            WorkflowStepRun.ordinal == 1,
        )
    )
    if review_run is None or step is None:
        raise MatterAnalysisTaskConflict("Analysis task batch provenance is incomplete")
    return workflow, review_run, step
