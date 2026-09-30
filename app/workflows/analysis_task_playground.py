import logging
import uuid

from dbos import DBOS, Queue

from app.analysis_task_playground import (
    execute_analysis_task_playground,
    fail_analysis_task_playground,
)
from app.database import SessionLocal

logger = logging.getLogger(__name__)

PLAYGROUND_QUEUE = Queue("analysis-task-playground", global_concurrency=4)


@DBOS.step(name="evaluate_analysis_task_playground_document", retries_allowed=True, max_attempts=3)
def evaluate_document(workflow_run_id: str) -> dict:
    with SessionLocal() as db:
        try:
            result = execute_analysis_task_playground(db, uuid.UUID(workflow_run_id))
            return {"result_id": str(result.id), "status": result.status}
        except Exception:
            db.commit()
            raise


@DBOS.step(name="fail_analysis_task_playground")
def mark_playground_failed(workflow_run_id: str, message: str) -> None:
    with SessionLocal() as db:
        fail_analysis_task_playground(db, uuid.UUID(workflow_run_id), message)


@DBOS.workflow(name="matter_analysis_task_playground_v1")
def analysis_task_playground_workflow(workflow_run_id: str) -> dict | None:
    try:
        return evaluate_document(workflow_run_id)
    except Exception as exc:
        logger.exception("Analysis task playground failed workflow_run_id=%s", workflow_run_id)
        mark_playground_failed(workflow_run_id, str(exc))
        return None
