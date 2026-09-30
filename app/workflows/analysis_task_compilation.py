import logging
import uuid

from dbos import DBOS, Queue

from app.analysis_task_compilation import compile_analysis_task_version, fail_analysis_task_compilation
from app.database import SessionLocal
from app.model_execution import ModelExecutionError

logger = logging.getLogger(__name__)

COMPILATION_QUEUE = Queue("analysis-task-compilations", global_concurrency=2)


def should_retry_compilation(exc: Exception) -> bool:
    return isinstance(exc, ModelExecutionError) and exc.retryable


def compilation_failure_message(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if isinstance(errors, list) and errors:
        return str(errors[-1])
    return str(exc)


@DBOS.step(
    name="compile_analysis_task_decision_specification",
    retries_allowed=True,
    interval_seconds=2,
    backoff_rate=2,
    max_attempts=3,
    should_retry=should_retry_compilation,
)
def compile_specification(task_version_id: str) -> dict:
    with SessionLocal() as db:
        try:
            return compile_analysis_task_version(db, uuid.UUID(task_version_id))
        except Exception:
            db.commit()
            raise


@DBOS.step(name="fail_analysis_task_decision_specification")
def mark_compilation_failed(task_version_id: str, message: str) -> None:
    with SessionLocal() as db:
        fail_analysis_task_compilation(db, uuid.UUID(task_version_id), message)


@DBOS.workflow(name="matter_analysis_task_compilation_v1")
def analysis_task_compilation_workflow(task_version_id: str) -> dict | None:
    try:
        return compile_specification(task_version_id)
    except Exception as exc:
        logger.exception("Analysis task compilation failed task_version_id=%s", task_version_id)
        mark_compilation_failed(task_version_id, compilation_failure_message(exc))
        return None
