from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.analysis_tasks import MatterAnalysisTaskConflict, decision_provider_engine_key
from app.config import get_settings
from app.execution_accounting import refresh_workflow_execution_usage
from app.job_executions import (
    ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
    JobExecutionConflict,
    JobScope,
    cancel_job_execution,
    create_job_execution,
    current_job_execution,
    transition_job_execution,
)
from app.models import (
    AnalysisTaskBatchSet,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    MetadataDefinition,
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
    dispatch: bool = True,
) -> tuple[WorkflowRun, ReviewBatchRun]:
    if batch.matter_id != matter.id or batch.status != "READY":
        raise MatterAnalysisTaskConflict("A ready review batch is required")
    if task.matter_id != matter.id or task_version.matter_analysis_task_id != task.id:
        raise MatterAnalysisTaskConflict("Analysis task version does not belong to this matter")
    batch_set = None
    if batch.analysis_task_batch_set_id is not None:
        batch_set = db.get(AnalysisTaskBatchSet, batch.analysis_task_batch_set_id)
        if (
            batch_set is None
            or batch_set.matter_analysis_task_id != task.id
            or batch_set.matter_analysis_task_version_id != task_version.id
        ):
            raise MatterAnalysisTaskConflict("Managed batch must run with its Batch Set's pinned task version")
    elif "AD_HOC_BATCH" not in (task_version.scope_policy or {}).get("allowed_modes", ["AD_HOC_BATCH"]):
        raise MatterAnalysisTaskConflict("This task version does not allow ad hoc batch execution")
    allowed_version_statuses = {"PUBLISHED", "RETIRED"} if batch_set is not None else {"PUBLISHED"}
    if task_version.status not in allowed_version_statuses or task_version.compilation_status != "READY":
        raise MatterAnalysisTaskConflict("Batch evaluation requires a ready, published task version")
    if task_version.decision_provider == "OPENAI_DECISIONS" and not get_settings().openai_api_key:
        raise MatterAnalysisTaskConflict(
            "OpenAI Decisions is not configured. Set OPENAI_API_KEY before starting this analysis run."
        )

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
    engine_key = decision_provider_engine_key(task_version.decision_provider)
    model_key = task_version.decision_model or str(role_binding["model_key"])
    destination_ids = [uuid.UUID(value) for value in (task_version.destination_metadata_definition_ids or [])]
    destination_definitions = {
        definition.id: definition
        for definition in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter.id,
                MetadataDefinition.id.in_(destination_ids),
            )
        )
    }
    destination_fields = [
        {
            "id": str(definition.id),
            "key": definition.key,
            "display_name": definition.display_name,
        }
        for destination_id in destination_ids
        if (definition := destination_definitions.get(destination_id)) is not None
    ]
    workflow_id = f"analysis-task:{task.id}:version:{task_version.version}:batch:{batch.id}:{uuid.uuid4()}"
    review_run_id = uuid.uuid4()
    workflow = WorkflowRun(
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        matter_id=matter.id,
        job_type=ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
        job_id=review_run_id,
        attempt_number=1,
        workflow_key=MATTER_ANALYSIS_TASK_BATCH_SPEC.key,
        code_version=MATTER_ANALYSIS_TASK_BATCH_SPEC.code_version,
        dbos_workflow_id=workflow_id,
        status="QUEUED",
        current_stage="QUEUED",
        progress_completed=0,
        progress_total=len(documents),
        progress_unit="documents",
        input_snapshot={
            "task_id": str(task.id),
            "task_name": task.name,
            "task_version_id": str(task_version.id),
            "task_version": task_version.version,
            "definition_content_hash": task_version.definition_content_hash,
            "specification_content_hash": task_version.specification_content_hash,
            "review_batch_id": str(batch.id),
            "analysis_task_batch_set_id": str(batch_set.id) if batch_set else None,
            "batch_set_ordinal": batch.batch_set_ordinal,
            "document_count": len(documents),
        },
        binding_snapshot=bindings,
        configuration_snapshot={
            "engine_key": engine_key,
            "model_key": model_key,
            "decision_provider": task_version.decision_provider,
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
        id=review_run_id,
        review_batch_id=batch.id,
        analysis_task_batch_set_id=batch_set.id if batch_set else None,
        batch_set_ordinal=batch.batch_set_ordinal if batch_set else None,
        run_type="WORKFLOW",
        purpose="REVIEW",
        status="QUEUED",
        result_policy="ISOLATED",
        workflow_run_record_id=workflow.id,
        current_workflow_run_id=workflow.id,
        configuration_snapshot={
            "mode": "BATCH",
            "task_id": str(task.id),
            "task_name": task.name,
            "task_version_id": str(task_version.id),
            "task_version": task_version.version,
            "analysis_task_batch_set_id": str(batch_set.id) if batch_set else None,
            "batch_set_ordinal": batch.batch_set_ordinal,
            "engine_key": engine_key,
            "model_key": model_key,
            "decision_provider": task_version.decision_provider,
            "destination_fields": destination_fields,
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
                last_workflow_run_id=workflow.id,
            )
            for document in documents
        ]
    )
    db.flush()
    if dispatch:
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
    _, review_run, step = _records(db, workflow_run_id)
    document_ids = list(
        db.scalars(
            select(ReviewBatchRunDocument.matter_document_id)
            .where(
                ReviewBatchRunDocument.review_batch_run_id == review_run.id,
                ReviewBatchRunDocument.last_workflow_run_id == workflow_run_id,
                ReviewBatchRunDocument.status == "QUEUED",
            )
            .order_by(ReviewBatchRunDocument.matter_document_id)
        )
    )
    if not document_ids:
        raise MatterAnalysisTaskConflict("Analysis task batch run has no documents")
    transition_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
        job_id=review_run.id,
        status="RUNNING",
        current_stage="EVALUATING",
        progress_completed=0,
        progress_total=len(document_ids),
        progress={"stage": "EVALUATING", "total": len(document_ids), "completed": 0, "failed": 0},
    )
    now = utcnow()
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
            ReviewBatchRunDocument.last_workflow_run_id == workflow_run_id,
            ReviewBatchRunDocument.status != "COMPLETED",
            ReviewBatchRunDocument.matter_document_id.in_(
                select(ReviewDecisionResult.matter_document_id).where(
                    ReviewDecisionResult.review_batch_run_id == review_run.id
                )
            ),
        )
        .values(
            status="COMPLETED",
            failure_code=None,
            failure_message=None,
            failure_retriable=False,
            completed_at=utcnow(),
        )
    )
    db.flush()
    counts = dict(
        db.execute(
            select(ReviewBatchRunDocument.status, func.count())
            .where(
                ReviewBatchRunDocument.review_batch_run_id == review_run.id,
                ReviewBatchRunDocument.last_workflow_run_id == workflow_run_id,
            )
            .group_by(ReviewBatchRunDocument.status)
        ).all()
    )
    completed = int(counts.get("COMPLETED", 0))
    failed = int(counts.get("FAILED", 0))
    skipped = int(counts.get("SKIPPED", 0))
    processed = completed + failed + skipped
    review_run.processed_document_count = int(
        db.scalar(
            select(func.count())
            .select_from(ReviewBatchRunDocument)
            .where(
                ReviewBatchRunDocument.review_batch_run_id == review_run.id,
                ReviewBatchRunDocument.status.in_(("COMPLETED", "SKIPPED", "FAILED")),
            )
        )
        or 0
    )
    step.completed_count = completed + skipped
    step.failed_count = failed
    workflow.progress = {
        "stage": "EVALUATING",
        "total": step.total_count,
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
    }
    transition_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
        job_id=review_run.id,
        current_stage="EVALUATING",
        progress_completed=processed,
        progress=workflow.progress,
    )
    db.commit()
    return {"completed": completed, "failed": failed, "skipped": skipped, "processed": processed}


def fail_analysis_task_batch_document(
    db: Session,
    workflow_run_id: uuid.UUID,
    matter_document_id: uuid.UUID,
    message: str,
    *,
    failure_code: str = "ANALYSIS_DOCUMENT_FAILURE",
    retryable: bool = False,
) -> None:
    _, review_run, _ = _records(db, workflow_run_id)
    db.execute(
        update(ReviewBatchRunDocument)
        .where(
            ReviewBatchRunDocument.review_batch_run_id == review_run.id,
            ReviewBatchRunDocument.matter_document_id == matter_document_id,
            ReviewBatchRunDocument.last_workflow_run_id == workflow_run_id,
            ReviewBatchRunDocument.status.in_(("QUEUED", "IN_PROGRESS")),
            ~select(ReviewDecisionResult.id)
            .where(
                ReviewDecisionResult.review_batch_run_id == review_run.id,
                ReviewDecisionResult.matter_document_id == matter_document_id,
            )
            .exists(),
        )
        .values(
            status="FAILED",
            failure_code=failure_code[:100],
            failure_message=message[:4000],
            failure_retriable=retryable,
            completed_at=utcnow(),
        )
    )
    # Do not update the shared workflow or step rows here. Document workers run concurrently;
    # aggregate status and errors once after all children have finished.
    db.commit()


def complete_analysis_task_batch(db: Session, workflow_run_id: uuid.UUID) -> None:
    progress = refresh_analysis_task_batch_progress(db, workflow_run_id)
    workflow, review_run, step = _records(db, workflow_run_id)
    now = utcnow()
    remaining_failed = int(
        db.scalar(
            select(func.count())
            .select_from(ReviewBatchRunDocument)
            .where(
                ReviewBatchRunDocument.review_batch_run_id == review_run.id,
                ReviewBatchRunDocument.status == "FAILED",
            )
        )
        or 0
    )
    retryable_failed = int(
        db.scalar(
            select(func.count())
            .select_from(ReviewBatchRunDocument)
            .where(
                ReviewBatchRunDocument.review_batch_run_id == review_run.id,
                ReviewBatchRunDocument.status == "FAILED",
                ReviewBatchRunDocument.failure_retriable.is_(True),
            )
        )
        or 0
    )
    status = "COMPLETED_WITH_ERRORS" if remaining_failed else "COMPLETED"
    final_progress = {
        "stage": status,
        "total": step.total_count,
        **progress,
        "remaining_failed": remaining_failed,
        "retryable_failed": retryable_failed,
    }
    failure_summary = None
    if remaining_failed:
        failure_summary = f"{remaining_failed} documents could not be evaluated"
        if retryable_failed:
            failure_summary += f"; {retryable_failed} can be retried"
    transition_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
        job_id=review_run.id,
        status=status,
        current_stage=status,
        progress_completed=progress["processed"],
        progress=final_progress,
        failure_code="ANALYSIS_DOCUMENT_FAILURES" if remaining_failed else None,
        failure_summary=failure_summary,
        failure_retriable=retryable_failed > 0,
    )
    step.status = status
    step.completed_at = now
    if progress["failed"]:
        step.error_message = f"{progress['failed']} documents failed during this attempt"
    refresh_workflow_execution_usage(db, workflow.id)
    db.commit()


def retry_analysis_task_batch_documents(
    db: Session,
    review_run: ReviewBatchRun,
    *,
    initiated_by_user_id: uuid.UUID,
    document_ids: list[uuid.UUID] | None = None,
) -> WorkflowRun:
    current = current_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
        job_id=review_run.id,
        for_update=True,
    )
    if current is None:
        raise JobExecutionConflict("Analysis task batch execution was not found")
    if current.status != "COMPLETED_WITH_ERRORS":
        raise JobExecutionConflict("Only a completed analysis job with document errors can be retried")

    statement = select(ReviewBatchRunDocument).where(
        ReviewBatchRunDocument.review_batch_run_id == review_run.id,
        ReviewBatchRunDocument.status == "FAILED",
        ReviewBatchRunDocument.failure_retriable.is_(True),
    )
    requested = set(document_ids or [])
    if requested:
        statement = statement.where(ReviewBatchRunDocument.matter_document_id.in_(requested))
    failed_documents = list(db.scalars(statement.with_for_update()))
    selected_ids = {document.matter_document_id for document in failed_documents}
    if requested and selected_ids != requested:
        raise JobExecutionConflict("Every selected document must have a retryable failure")
    if not failed_documents:
        raise JobExecutionConflict("This job has no retryable failed documents")

    attempt_number = current.attempt_number + 1
    workflow_id = f"analysis-task-batch:{review_run.id}:attempt:{attempt_number}:{uuid.uuid4()}"
    input_snapshot = dict(current.input_snapshot or {})
    input_snapshot["retry_document_count"] = len(failed_documents)
    execution = create_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
        job_id=review_run.id,
        scope=JobScope(
            tenant_id=current.tenant_id,
            client_id=current.client_id,
            matter_id=current.matter_id,
        ),
        workflow_key=current.workflow_key,
        code_version=current.code_version,
        dbos_workflow_id=workflow_id,
        initiated_by_user_id=initiated_by_user_id,
        current_stage="QUEUED",
        progress_total=len(failed_documents),
        progress_unit="documents",
        input_snapshot=input_snapshot,
        binding_snapshot=dict(current.binding_snapshot or {}),
        configuration_snapshot=dict(current.configuration_snapshot or {}),
        retry_of=current,
    )
    execution.progress = {
        "stage": "QUEUED",
        "total": len(failed_documents),
        "completed": 0,
        "failed": 0,
        "skipped": 0,
    }
    db.add(
        WorkflowStepRun(
            workflow_run_id=execution.id,
            role_key="decision_evaluation",
            ordinal=1,
            fan_out_group="documents",
            status="QUEUED",
            total_count=len(failed_documents),
        )
    )
    for document in failed_documents:
        document.status = "QUEUED"
        document.last_workflow_run_id = execution.id
        document.failure_code = None
        document.failure_message = None
        document.failure_retriable = False
        document.started_at = utcnow()
        document.completed_at = None
    db.flush()
    return execution


def cancel_analysis_task_batch_run(
    db: Session,
    review_run_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowRun, bool]:
    # Document workers update the common execution before projecting onto the batch run.
    # Take those locks in the same order so cancellation cannot deadlock with progress updates.
    locked_execution = current_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
        job_id=review_run_id,
        for_update=True,
    )
    if locked_execution is None:
        raise MatterAnalysisTaskConflict("Analysis task batch execution not found")
    execution, canceled = cancel_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
        job_id=review_run_id,
        actor_user_id=actor_user_id,
    )
    review_run = db.get(ReviewBatchRun, review_run_id)
    if review_run is None:
        raise MatterAnalysisTaskConflict("Analysis task batch run not found")

    now = utcnow()
    db.execute(
        update(ReviewBatchRunDocument)
        .where(
            ReviewBatchRunDocument.review_batch_run_id == review_run_id,
            ReviewBatchRunDocument.status.in_(("QUEUED", "IN_PROGRESS")),
        )
        .values(status="SKIPPED", completed_at=now, updated_at=now)
    )
    db.flush()
    counts = dict(
        db.execute(
            select(ReviewBatchRunDocument.status, func.count())
            .where(ReviewBatchRunDocument.review_batch_run_id == review_run_id)
            .group_by(ReviewBatchRunDocument.status)
        ).all()
    )
    completed = int(counts.get("COMPLETED", 0))
    failed = int(counts.get("FAILED", 0))
    skipped = int(counts.get("SKIPPED", 0))
    processed = completed + failed + skipped
    total = sum(int(count) for count in counts.values())
    execution.current_stage = "CANCELED"
    execution.progress_completed = processed
    execution.progress_total = total
    execution.progress = {
        "stage": "CANCELED",
        "total": total,
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
    }
    review_run.processed_document_count = processed
    step = db.scalar(
        select(WorkflowStepRun).where(
            WorkflowStepRun.workflow_run_id == execution.id,
            WorkflowStepRun.ordinal == 1,
        )
    )
    if step is not None and step.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"}:
        step.status = "CANCELED"
        step.completed_count = completed + skipped
        step.failed_count = failed
        step.completed_at = step.completed_at or now
    return execution, canceled


def fail_analysis_task_batch(db: Session, workflow_run_id: uuid.UUID, message: str) -> None:
    workflow, review_run, step = _records(db, workflow_run_id)
    if workflow.status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}:
        return
    now = utcnow()
    transition_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
        job_id=review_run.id,
        status="FAILED",
        current_stage="FAILED",
        progress={"stage": "FAILED"},
        failure_code="ANALYSIS_TASK_BATCH_FAILED",
        failure_summary=message,
        failure_retriable=True,
    )
    step.status = "FAILED"
    step.error_message = message[:4000]
    step.completed_at = now
    for run_document in db.scalars(
        select(ReviewBatchRunDocument).where(
            ReviewBatchRunDocument.review_batch_run_id == review_run.id,
            ReviewBatchRunDocument.last_workflow_run_id == workflow_run_id,
            ReviewBatchRunDocument.status.in_(("QUEUED", "IN_PROGRESS")),
        )
    ):
        run_document.status = "FAILED"
        run_document.failure_code = "ANALYSIS_TASK_BATCH_FAILED"
        run_document.failure_message = message[:4000]
        run_document.failure_retriable = True
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
