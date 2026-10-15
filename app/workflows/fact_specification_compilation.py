import logging
import uuid

from dbos import DBOS, Queue

from app.database import SessionLocal
from app.fact_specification_compilation import (
    compile_fact_specification_version,
    fail_fact_specification_compilation,
)
from app.workflows.analysis_task_compilation import compilation_failure_message, should_retry_compilation

logger = logging.getLogger(__name__)

FACT_SPECIFICATION_COMPILATION_QUEUE = Queue("fact-specification-compilations", global_concurrency=2)


@DBOS.step(
    name="compile_fact_development_specification",
    retries_allowed=True,
    interval_seconds=2,
    backoff_rate=2,
    max_attempts=3,
    should_retry=should_retry_compilation,
)
def compile_specification(task_version_id: str) -> dict:
    with SessionLocal() as db:
        try:
            return compile_fact_specification_version(db, uuid.UUID(task_version_id))
        except Exception:
            db.commit()
            raise


@DBOS.step(name="fail_fact_development_specification")
def mark_compilation_failed(task_version_id: str, message: str) -> None:
    with SessionLocal() as db:
        fail_fact_specification_compilation(db, uuid.UUID(task_version_id), message)


@DBOS.workflow(name="fact_development_specification_compilation_v1")
def fact_specification_compilation_workflow(task_version_id: str) -> dict | None:
    try:
        return compile_specification(task_version_id)
    except Exception as exc:
        logger.exception("Fact specification compilation failed task_version_id=%s", task_version_id)
        mark_compilation_failed(task_version_id, compilation_failure_message(exc))
        return None
