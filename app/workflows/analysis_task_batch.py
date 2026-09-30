import logging
import uuid

from dbos import DBOS, Queue, SetWorkflowID

from app.analysis_task_batch import (
    complete_analysis_task_batch,
    fail_analysis_task_batch,
    fail_analysis_task_batch_document,
    prepare_analysis_task_batch,
    refresh_analysis_task_batch_progress,
)
from app.analysis_task_playground import execute_analysis_task_document
from app.analysis_tasks import MatterAnalysisTaskConflict
from app.config import get_settings
from app.database import SessionLocal

logger = logging.getLogger(__name__)

_SETTINGS = get_settings()

BATCH_QUEUE = Queue("analysis-task-batches", global_concurrency=2)
DOCUMENT_QUEUE = Queue(
    "analysis-task-batch-documents",
    global_concurrency=min(
        _SETTINGS.definition_assessment_document_concurrency,
        _SETTINGS.typesafe_concurrency,
    ),
)


def _should_retry_document(error: BaseException) -> bool:
    return not isinstance(error, MatterAnalysisTaskConflict)


def _failure_message(error: BaseException) -> str:
    errors = getattr(error, "errors", None)
    if isinstance(errors, list) and errors:
        return str(errors[-1])
    return str(error)


@DBOS.step(name="prepare_analysis_task_batch")
def prepare(workflow_run_id: str) -> list[str]:
    with SessionLocal() as db:
        return [str(value) for value in prepare_analysis_task_batch(db, uuid.UUID(workflow_run_id))]


@DBOS.step(
    name="evaluate_analysis_task_batch_document",
    retries_allowed=True,
    max_attempts=3,
    should_retry=_should_retry_document,
)
def evaluate_document(workflow_run_id: str, document_id: str) -> dict:
    with SessionLocal() as db:
        try:
            result = execute_analysis_task_document(
                db,
                uuid.UUID(workflow_run_id),
                uuid.UUID(document_id),
                complete_run=False,
            )
            return {"result_id": str(result.id), "status": result.status}
        except Exception:
            db.commit()
            raise


@DBOS.step(name="fail_analysis_task_batch_document")
def mark_document_failed(workflow_run_id: str, document_id: str, message: str) -> None:
    with SessionLocal() as db:
        fail_analysis_task_batch_document(
            db,
            uuid.UUID(workflow_run_id),
            uuid.UUID(document_id),
            message,
        )


@DBOS.workflow(name="matter_analysis_task_batch_document_v1")
def document_workflow(workflow_run_id: str, document_id: str) -> dict:
    try:
        return evaluate_document(workflow_run_id, document_id)
    except Exception as exc:
        logger.exception(
            "Analysis task batch document failed workflow_run_id=%s document_id=%s",
            workflow_run_id,
            document_id,
        )
        message = _failure_message(exc)
        mark_document_failed(workflow_run_id, document_id, message)
        return {"status": "FAILED", "error": message[:4000]}


@DBOS.step(name="refresh_analysis_task_batch_progress")
def refresh(workflow_run_id: str) -> dict[str, int]:
    with SessionLocal() as db:
        return refresh_analysis_task_batch_progress(db, uuid.UUID(workflow_run_id))


@DBOS.step(name="complete_analysis_task_batch")
def complete(workflow_run_id: str) -> None:
    with SessionLocal() as db:
        complete_analysis_task_batch(db, uuid.UUID(workflow_run_id))


@DBOS.step(name="fail_analysis_task_batch")
def fail(workflow_run_id: str, message: str) -> None:
    with SessionLocal() as db:
        fail_analysis_task_batch(db, uuid.UUID(workflow_run_id), message)


@DBOS.workflow(name="matter_analysis_task_batch_v1")
def analysis_task_batch_workflow(workflow_run_id: str) -> None:
    try:
        document_ids = prepare(workflow_run_id)
        handles = []
        for index, document_id in enumerate(document_ids):
            with SetWorkflowID(f"analysis-task-batch:{workflow_run_id}:document:{index}"):
                handles.append(DOCUMENT_QUEUE.enqueue(document_workflow, workflow_run_id, document_id))
        for handle in handles:
            handle.get_result()
        refresh(workflow_run_id)
        complete(workflow_run_id)
    except Exception as exc:
        logger.exception("Analysis task batch failed workflow_run_id=%s", workflow_run_id)
        fail(workflow_run_id, str(exc))
        raise
