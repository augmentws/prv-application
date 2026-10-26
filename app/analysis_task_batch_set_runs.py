from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis_task_batch import cancel_analysis_task_batch_run, queue_analysis_task_batch
from app.classification_analysis_tasks import cancel_classification_analysis_run, create_classification_analysis_run
from app.fact_development import cancel_fact_development_run, create_fact_development_run
from app.job_executions import (
    ANALYSIS_TASK_BATCH_SET_RUN_JOB_TYPE,
    JobExecutionConflict,
    JobScope,
    cancel_job_execution,
    create_job_execution,
    current_job_execution,
    transition_job_execution,
)
from app.models import (
    AnalysisTaskBatchSet,
    AnalysisTaskBatchSetRun,
    AnalysisTaskBatchSetRunChild,
    AnalysisTaskExecutionProfile,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskRun,
    MatterAnalysisTaskVersion,
    ReviewBatch,
    ReviewBatchRun,
    WorkflowRun,
    utcnow,
)
from app.schemas import MatterAnalysisTaskRunCreate


class AnalysisTaskBatchSetRunError(ValueError):
    pass


@dataclass(frozen=True)
class LaunchedChild:
    child_id: uuid.UUID
    run_type: str
    child_run_id: uuid.UUID
    workflow_record_id: uuid.UUID
    workflow_id: str
    use_batch_api: bool = False


def _execution(
    db: Session,
    *,
    run: AnalysisTaskBatchSetRun,
    matter: Matter,
    actor_user_id: uuid.UUID,
    retry_of: WorkflowRun | None = None,
    notification_parent_type: str | None = None,
    notification_parent_id: uuid.UUID | None = None,
) -> WorkflowRun:
    return create_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_RUN_JOB_TYPE,
        job_id=run.id,
        scope=JobScope(run.tenant_id, matter.client_id, matter.id),
        workflow_key="analysis_task_batch_set_run_v1",
        code_version="1",
        dbos_workflow_id=run.workflow_id,
        initiated_by_user_id=actor_user_id,
        idempotency_key=run.idempotency_key if retry_of is None else None,
        current_stage="QUEUED",
        progress_total=run.child_count,
        progress_unit="child_batches",
        input_snapshot={
            "analysis_task_batch_set_id": str(run.analysis_task_batch_set_id),
            "matter_analysis_task_id": str(run.matter_analysis_task_id),
            "matter_analysis_task_version_id": str(run.matter_analysis_task_version_id),
            "task_type": run.task_type,
            "child_count": run.child_count,
        },
        configuration_snapshot=run.configuration_snapshot,
        retry_of=retry_of,
        notification_policy="PARENT_MANAGED" if notification_parent_id is not None else "DEFAULT",
        notification_parent_type=notification_parent_type,
        notification_parent_id=notification_parent_id,
    )


def create_batch_set_run(
    db: Session,
    *,
    matter: Matter,
    batch_set: AnalysisTaskBatchSet,
    task: MatterAnalysisTask,
    task_version: MatterAnalysisTaskVersion,
    configuration: dict,
    idempotency_key: str,
    actor_user_id: uuid.UUID,
    execution_profile: AnalysisTaskExecutionProfile | None = None,
    notification_parent_type: str | None = None,
    notification_parent_id: uuid.UUID | None = None,
) -> tuple[AnalysisTaskBatchSetRun, bool]:
    if (notification_parent_type is None) != (notification_parent_id is None):
        raise AnalysisTaskBatchSetRunError(
            "Batch Set run notification parent type and id must be supplied together"
        )
    existing = db.scalar(
        select(AnalysisTaskBatchSetRun).where(
            AnalysisTaskBatchSetRun.analysis_task_batch_set_id == batch_set.id,
            AnalysisTaskBatchSetRun.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing, False
    if batch_set.status != "READY":
        raise AnalysisTaskBatchSetRunError("Every child must be ready before running the Batch Set")
    if (
        batch_set.matter_id != matter.id
        or batch_set.matter_analysis_task_id != task.id
        or batch_set.matter_analysis_task_version_id != task_version.id
    ):
        raise AnalysisTaskBatchSetRunError("Batch Set task provenance is inconsistent")
    if (
        execution_profile is not None
        and execution_profile.matter_analysis_task_version_id != task_version.id
    ):
        raise AnalysisTaskBatchSetRunError("Execution profile does not belong to the Batch Set task version")
    children = list(
        db.scalars(
            select(ReviewBatch)
            .where(ReviewBatch.analysis_task_batch_set_id == batch_set.id)
            .order_by(ReviewBatch.batch_set_ordinal)
        )
    )
    if not children or any(child.status != "READY" for child in children):
        raise AnalysisTaskBatchSetRunError("Every child must be ready before running the Batch Set")
    run_id = uuid.uuid4()
    run = AnalysisTaskBatchSetRun(
        id=run_id,
        tenant_id=matter.client.tenant_id,
        matter_id=matter.id,
        analysis_task_batch_set_id=batch_set.id,
        matter_analysis_task_id=task.id,
        matter_analysis_task_version_id=task_version.id,
        execution_profile_id=execution_profile.id if execution_profile is not None else None,
        task_type=task.task_type,
        configuration_snapshot={
            **configuration,
            "execution_profile_id": str(execution_profile.id) if execution_profile is not None else None,
            "execution_profile_content_hash": (
                execution_profile.content_hash if execution_profile is not None else None
            ),
        },
        idempotency_key=idempotency_key,
        workflow_id=f"analysis-task-batch-set-run:{run_id}",
        child_count=len(children),
        created_by_user_id=actor_user_id,
    )
    db.add(run)
    db.flush()
    db.add_all(
        AnalysisTaskBatchSetRunChild(
            batch_set_run_id=run.id,
            review_batch_id=child.id,
            ordinal=child.batch_set_ordinal or index,
        )
        for index, child in enumerate(children, start=1)
    )
    db.flush()
    _execution(
        db,
        run=run,
        matter=matter,
        actor_user_id=actor_user_id,
        notification_parent_type=notification_parent_type,
        notification_parent_id=notification_parent_id,
    )
    return run, True


def pending_child_ids(db: Session, run_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        db.scalars(
            select(AnalysisTaskBatchSetRunChild.id)
            .where(
                AnalysisTaskBatchSetRunChild.batch_set_run_id == run_id,
                AnalysisTaskBatchSetRunChild.status == "PENDING",
            )
            .order_by(AnalysisTaskBatchSetRunChild.ordinal)
        )
    )


def launch_child(db: Session, child_id: uuid.UUID) -> LaunchedChild:
    child = db.scalar(
        select(AnalysisTaskBatchSetRunChild)
        .where(AnalysisTaskBatchSetRunChild.id == child_id)
        .with_for_update()
    )
    if child is None:
        raise AnalysisTaskBatchSetRunError("Batch Set run child was not found")
    if child.status != "PENDING":
        if child.child_run_id is None or child.run_type is None:
            raise AnalysisTaskBatchSetRunError("Batch Set run child provenance is incomplete")
        return _launched_child(db, child)
    parent = db.get(AnalysisTaskBatchSetRun, child.batch_set_run_id)
    batch = db.get(ReviewBatch, child.review_batch_id)
    if parent is None or batch is None:
        raise AnalysisTaskBatchSetRunError("Batch Set run child dependencies were not found")
    matter = db.get(Matter, parent.matter_id)
    task = db.get(MatterAnalysisTask, parent.matter_analysis_task_id)
    task_version = db.get(MatterAnalysisTaskVersion, parent.matter_analysis_task_version_id)
    execution_profile = (
        db.get(AnalysisTaskExecutionProfile, parent.execution_profile_id)
        if parent.execution_profile_id is not None
        else None
    )
    if matter is None or task is None or task_version is None:
        raise AnalysisTaskBatchSetRunError("Batch Set run task provenance is incomplete")
    config = parent.configuration_snapshot
    if parent.task_type == "QUESTION_ANSWERING":
        parent_execution = parent.current_workflow_run
        workflow, domain_run = queue_analysis_task_batch(
            db,
            matter=matter,
            batch=batch,
            task=task,
            task_version=task_version,
            initiated_by_user_id=parent.created_by_user_id,
            execution_profile=execution_profile,
            dispatch=False,
            notification_parent_type=(
                parent_execution.notification_parent_type if parent_execution is not None else None
            ),
            notification_parent_id=(
                parent_execution.notification_parent_id if parent_execution is not None else None
            ),
        )
        run_type = "QUESTION_ANSWERING"
        workflow_record_id = workflow.id
        workflow_id = workflow.dbos_workflow_id
    elif parent.task_type == "CLASSIFICATION":
        embedding_job_id = config.get("embedding_job_id")
        if not embedding_job_id:
            raise AnalysisTaskBatchSetRunError("Classification run-all requires document embeddings")
        domain_run, _ = create_classification_analysis_run(
            db,
            matter=matter,
            task=task,
            payload=MatterAnalysisTaskRunCreate(
                scope_type="REVIEW_BATCH",
                scope_id=batch.id,
                embedding_job_id=uuid.UUID(str(embedding_job_id)),
                matter_analysis_task_version_id=task_version.id,
                idempotency_key=f"batch-set-run:{parent.id}:child:{child.ordinal}:attempt:{child.attempt_count + 1}",
            ),
            actor_user_id=parent.created_by_user_id,
        )
        run_type = "CLASSIFICATION"
        workflow_record_id = domain_run.current_workflow_run_id
        workflow_id = domain_run.workflow_id
    else:
        domain_run, _ = create_fact_development_run(
            db,
            matter=matter,
            task=task,
            review_batch_id=batch.id,
            coding_source=str(config.get("coding_source", "MATTER_METADATA")),
            reviewer_coding_id=(uuid.UUID(str(config["reviewer_coding_id"])) if config.get("reviewer_coding_id") else None),
            idempotency_key=f"batch-set-run:{parent.id}:child:{child.ordinal}:attempt:{child.attempt_count + 1}",
            actor_user_id=parent.created_by_user_id,
            task_version_id=task_version.id,
            use_batch_api=bool(config.get("use_batch_api", False)),
        )
        run_type = "FACT_DEVELOPMENT"
        workflow_record_id = domain_run.current_workflow_run_id
        workflow_id = domain_run.workflow_id
    if workflow_record_id is None:
        raise AnalysisTaskBatchSetRunError("Child run execution was not created")
    child.run_type = run_type
    child.child_run_id = domain_run.id
    child.status = "QUEUED"
    child.attempt_count += 1
    child.error_message = None
    child.started_at = utcnow()
    parent.status = "RUNNING"
    db.flush()
    return LaunchedChild(
        child_id=child.id,
        run_type=run_type,
        child_run_id=domain_run.id,
        workflow_record_id=workflow_record_id,
        workflow_id=workflow_id,
        use_batch_api=bool(config.get("use_batch_api", False)),
    )


def _launched_child(db: Session, child: AnalysisTaskBatchSetRunChild) -> LaunchedChild:
    if child.run_type == "QUESTION_ANSWERING":
        run = db.get(ReviewBatchRun, child.child_run_id)
        workflow_record_id = run.workflow_run_record_id if run else None
        workflow_id = run.dbos_workflow_id if run else None
    else:
        run = db.get(MatterAnalysisTaskRun, child.child_run_id)
        workflow_record_id = run.current_workflow_run_id if run else None
        workflow_id = run.workflow_id if run else None
    if workflow_record_id is None or workflow_id is None:
        raise AnalysisTaskBatchSetRunError("Child run execution was not found")
    return LaunchedChild(
        child.id,
        child.run_type,
        child.child_run_id,
        workflow_record_id,
        workflow_id,
        bool(run.configuration_snapshot.get("use_batch_api", False)) if child.run_type == "FACT_DEVELOPMENT" else False,
    )


def refresh_child(db: Session, child_id: uuid.UUID, error_message: str | None = None) -> str:
    child = db.get(AnalysisTaskBatchSetRunChild, child_id)
    if child is None or child.child_run_id is None or child.run_type is None:
        raise AnalysisTaskBatchSetRunError("Batch Set run child provenance is incomplete")
    if child.run_type == "QUESTION_ANSWERING":
        domain_run = db.get(ReviewBatchRun, child.child_run_id)
    else:
        domain_run = db.get(MatterAnalysisTaskRun, child.child_run_id)
    if domain_run is None:
        child.status = "FAILED"
        child.error_message = error_message or "Child run was not found"
    else:
        child.status = domain_run.status
        child.error_message = error_message or getattr(domain_run, "error_message", None) or getattr(
            domain_run, "failure_summary", None
        )
    if child.status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}:
        child.completed_at = utcnow()
    db.commit()
    return child.status


def fail_child_launch(db: Session, child_id: uuid.UUID, error_message: str) -> None:
    child = db.get(AnalysisTaskBatchSetRunChild, child_id)
    if child is None:
        raise AnalysisTaskBatchSetRunError("Batch Set run child was not found")
    child.status = "FAILED"
    child.error_message = error_message[:4000]
    child.completed_at = utcnow()
    db.commit()


def complete_batch_set_run(db: Session, run_id: uuid.UUID) -> AnalysisTaskBatchSetRun:
    run = db.scalar(select(AnalysisTaskBatchSetRun).where(AnalysisTaskBatchSetRun.id == run_id).with_for_update())
    if run is None:
        raise AnalysisTaskBatchSetRunError("Batch Set run was not found")
    children = list(db.scalars(select(AnalysisTaskBatchSetRunChild).where(AnalysisTaskBatchSetRunChild.batch_set_run_id == run.id)))
    run.completed_child_count = sum(child.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"} for child in children)
    run.failed_child_count = sum(child.status in {"FAILED", "CANCELED"} for child in children)
    has_errors = run.failed_child_count > 0 or any(child.status == "COMPLETED_WITH_ERRORS" for child in children)
    workflow = transition_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_RUN_JOB_TYPE,
        job_id=run.id,
        status="COMPLETED_WITH_ERRORS" if has_errors else "COMPLETED",
        current_stage="COMPLETED_WITH_ERRORS" if has_errors else "COMPLETED",
        progress_completed=len(children),
        progress_total=len(children),
    )
    if workflow.notification_policy == "PARENT_MANAGED":
        from app.review_programs import project_review_program_discovery_qa_run_terminal

        project_review_program_discovery_qa_run_terminal(
            db,
            batch_run=run,
            workflow=workflow,
        )
    return run


def cancel_batch_set_run(
    db: Session,
    run: AnalysisTaskBatchSetRun,
    *,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowRun, bool, list[str]]:
    execution, canceled = cancel_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_RUN_JOB_TYPE,
        job_id=run.id,
        actor_user_id=actor_user_id,
    )
    child_workflow_ids: list[str] = []
    if canceled:
        for child in run.children:
            if child.status == "PENDING":
                child.status = "CANCELED"
                child.completed_at = utcnow()
                continue
            if child.status not in {"QUEUED", "RUNNING"} or child.child_run_id is None:
                continue
            if child.run_type == "QUESTION_ANSWERING":
                child_execution, child_canceled = cancel_analysis_task_batch_run(
                    db,
                    child.child_run_id,
                    actor_user_id=actor_user_id,
                )
                child_workflow_ids.append(child_execution.dbos_workflow_id)
            else:
                domain_run = db.get(MatterAnalysisTaskRun, child.child_run_id)
                if domain_run is None:
                    child_canceled = False
                elif child.run_type == "CLASSIFICATION":
                    child_canceled = cancel_classification_analysis_run(
                        db,
                        domain_run,
                        actor_user_id=actor_user_id,
                    )
                    child_workflow_ids.append(domain_run.workflow_id)
                else:
                    child_execution, child_canceled = cancel_fact_development_run(
                        db,
                        domain_run,
                        actor_user_id=actor_user_id,
                    )
                    child_workflow_ids.append(child_execution.dbos_workflow_id)
            if child_canceled:
                child.status = "CANCELED"
                child.completed_at = utcnow()
    return execution, canceled, child_workflow_ids


def retry_batch_set_run(db: Session, run: AnalysisTaskBatchSetRun, *, actor_user_id: uuid.UUID) -> WorkflowRun:
    current = current_job_execution(db, job_type=ANALYSIS_TASK_BATCH_SET_RUN_JOB_TYPE, job_id=run.id, for_update=True)
    if current is None or current.status not in {"FAILED", "CANCELED", "COMPLETED_WITH_ERRORS"}:
        raise JobExecutionConflict("Only failed, canceled, or partially failed Batch Set runs can be retried")
    retryable = [child for child in run.children if child.status in {"FAILED", "CANCELED"}]
    if not retryable:
        raise JobExecutionConflict("Batch Set run has no failed or canceled children to retry")
    for child in retryable:
        child.status = "PENDING"
        child.child_run_id = None
        child.run_type = None
        child.error_message = None
        child.started_at = None
        child.completed_at = None
    run.status = "QUEUED"
    run.completed_at = None
    run.canceled_at = None
    run.failed_child_count = 0
    matter = db.get(Matter, run.matter_id)
    if matter is None:
        raise JobExecutionConflict("Batch Set run matter was not found")
    run.workflow_id = f"analysis-task-batch-set-run:{run.id}:attempt:{current.attempt_number + 1}"
    return _execution(
        db,
        run=run,
        matter=matter,
        actor_user_id=actor_user_id,
        retry_of=current,
        notification_parent_type=current.notification_parent_type,
        notification_parent_id=current.notification_parent_id,
    )
