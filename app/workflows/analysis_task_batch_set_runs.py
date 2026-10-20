import uuid

from dbos import DBOS, Queue, SetWorkflowID

from app.analysis_task_batch_set_runs import (
    complete_batch_set_run,
    fail_child_launch,
    launch_child,
    pending_child_ids,
    refresh_child,
)
from app.database import SessionLocal
from app.job_executions import ANALYSIS_TASK_BATCH_SET_RUN_JOB_TYPE, transition_job_execution
from app.models import AnalysisTaskBatchSetRun
from app.workflows.analysis_task_batch import BATCH_QUEUE, analysis_task_batch_workflow
from app.workflows.classification_analysis_tasks import (
    CLASSIFICATION_ANALYSIS_QUEUE,
    classification_analysis_task,
)
from app.workflows.fact_development import BATCH_RUN_QUEUE, RUN_QUEUE, fact_development_run

PARENT_QUEUE = Queue("analysis-task-batch-set-runs", global_concurrency=2)


@DBOS.step(name="prepare_analysis_task_batch_set_run", retries_allowed=True, max_attempts=3)
def prepare(run_id: str) -> dict:
    with SessionLocal() as db:
        run = db.get(AnalysisTaskBatchSetRun, uuid.UUID(run_id))
        if run is None:
            raise ValueError("Batch Set run was not found")
        ids = pending_child_ids(db, run.id)
        execution = run.current_workflow_run
        if execution is not None:
            transition_job_execution(
                db,
                job_type=ANALYSIS_TASK_BATCH_SET_RUN_JOB_TYPE,
                job_id=run.id,
                status="RUNNING",
                current_stage="RUNNING_CHILDREN",
                progress_completed=run.completed_child_count,
                progress_total=run.child_count,
            )
        db.commit()
        return {"child_ids": [str(value) for value in ids], "max_concurrency": int(run.configuration_snapshot["max_concurrency"])}


@DBOS.step(name="launch_analysis_task_batch_set_child", retries_allowed=True, max_attempts=3)
def launch(child_id: str) -> dict:
    with SessionLocal() as db:
        child = launch_child(db, uuid.UUID(child_id))
        db.commit()
        return {
            "child_id": str(child.child_id),
            "run_type": child.run_type,
            "child_run_id": str(child.child_run_id),
            "workflow_record_id": str(child.workflow_record_id),
            "workflow_id": child.workflow_id,
            "use_batch_api": child.use_batch_api,
        }


@DBOS.step(name="refresh_analysis_task_batch_set_child", retries_allowed=True, max_attempts=3)
def refresh(child_id: str, error_message: str | None = None) -> str:
    with SessionLocal() as db:
        return refresh_child(db, uuid.UUID(child_id), error_message)


@DBOS.step(name="fail_analysis_task_batch_set_child_launch", retries_allowed=True, max_attempts=3)
def fail_launch(child_id: str, error_message: str) -> None:
    with SessionLocal() as db:
        fail_child_launch(db, uuid.UUID(child_id), error_message)


@DBOS.step(name="complete_analysis_task_batch_set_run", retries_allowed=True, max_attempts=3)
def complete(run_id: str) -> None:
    with SessionLocal() as db:
        complete_batch_set_run(db, uuid.UUID(run_id))
        db.commit()


def _enqueue_child(child: dict):
    with SetWorkflowID(child["workflow_id"]):
        if child["run_type"] == "QUESTION_ANSWERING":
            return BATCH_QUEUE.enqueue(analysis_task_batch_workflow, child["workflow_record_id"])
        if child["run_type"] == "CLASSIFICATION":
            return CLASSIFICATION_ANALYSIS_QUEUE.enqueue(classification_analysis_task, child["child_run_id"])
        queue = BATCH_RUN_QUEUE if child["use_batch_api"] else RUN_QUEUE
        return queue.enqueue(fact_development_run, child["child_run_id"])


@DBOS.workflow(name="analysis_task_batch_set_run_v1")
def analysis_task_batch_set_run(run_id: str) -> None:
    plan = prepare(run_id)
    child_ids = plan["child_ids"]
    window = max(1, plan["max_concurrency"])
    in_flight: list[tuple[str, object]] = []
    next_index = 0
    while next_index < len(child_ids) or in_flight:
        while next_index < len(child_ids) and len(in_flight) < window:
            child_id = child_ids[next_index]
            next_index += 1
            try:
                child = launch(child_id)
                in_flight.append((child["child_id"], _enqueue_child(child)))
            except Exception as exc:  # noqa: BLE001 - persist a terminal child failure and continue siblings
                fail_launch(child_id, str(exc)[:4000])
        if not in_flight:
            continue
        child_id, handle = in_flight.pop(0)
        error_message = None
        try:
            handle.get_result()
        except Exception as exc:  # noqa: BLE001 - child domain run records its own terminal failure
            error_message = str(exc)[:4000]
        refresh(child_id, error_message)
    complete(run_id)
