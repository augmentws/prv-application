import uuid

from dbos import DBOS, Queue

from app.config import get_settings
from app.database import SessionLocal
from app.fact_exports import fail_fact_export_job, record_fact_export_attempt_failure, run_fact_export

FACT_EXPORT_QUEUE = Queue("matter-fact-exports", global_concurrency=1)


@DBOS.step(name="stream_matter_fact_export", retries_allowed=True, max_attempts=3)
def stream_export(job_id: str) -> None:
    parsed_job_id = uuid.UUID(job_id)
    try:
        run_fact_export(parsed_job_id, get_settings(), session_factory=SessionLocal)
    except Exception as exc:
        # Preserve the underlying classification before DBOS wraps the exhausted step exception.
        with SessionLocal() as db:
            record_fact_export_attempt_failure(db, parsed_job_id, exc)
            db.commit()
        raise


@DBOS.step(name="fail_matter_fact_export", retries_allowed=True, max_attempts=3)
def fail_export(job_id: str, error_message: str) -> None:
    with SessionLocal() as db:
        fail_fact_export_job(db, uuid.UUID(job_id), RuntimeError(error_message))
        db.commit()


@DBOS.workflow(name="matter_fact_export_v1")
def matter_fact_export(job_id: str) -> None:
    try:
        stream_export(job_id)
    except Exception as exc:
        fail_export(job_id, str(exc)[:4000])
        raise
