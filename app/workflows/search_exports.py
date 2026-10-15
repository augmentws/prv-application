import uuid

from dbos import DBOS, Queue

from app.config import get_settings
from app.database import SessionLocal
from app.search_exports import fail_search_export_job, record_search_export_attempt_failure, run_search_export

EXPORT_QUEUE = Queue("matter-search-exports", global_concurrency=1)


@DBOS.step(name="stream_matter_search_export", retries_allowed=True, max_attempts=3)
def stream_export(job_id: str) -> None:
    parsed_job_id = uuid.UUID(job_id)
    try:
        run_search_export(parsed_job_id, get_settings(), session_factory=SessionLocal)
    except Exception as exc:
        # Preserve the underlying dependency/error classification before DBOS
        # replaces the exhausted step exception with DBOSMaxStepRetriesExceeded.
        with SessionLocal() as db:
            record_search_export_attempt_failure(db, parsed_job_id, exc)
            db.commit()
        raise


@DBOS.step(name="fail_matter_search_export", retries_allowed=True, max_attempts=3)
def fail_export(job_id: str, error_message: str) -> None:
    with SessionLocal() as db:
        fail_search_export_job(db, uuid.UUID(job_id), RuntimeError(error_message))
        db.commit()


@DBOS.workflow(name="matter_search_export_v1")
def matter_search_export(job_id: str) -> None:
    try:
        stream_export(job_id)
    except Exception as exc:
        fail_export(job_id, str(exc)[:4000])
        raise
