import uuid

from dbos import DBOS, Queue

from app.coding_comparisons import fail_comparison, run_comparison
from app.database import SessionLocal

COMPARISON_QUEUE = Queue("coding-comparisons", global_concurrency=1)


@DBOS.step(name="build_artifact_backed_coding_comparison", retries_allowed=True, max_attempts=3)
def build_comparison(job_id: str) -> None:
    run_comparison(uuid.UUID(job_id), session_factory=SessionLocal)


@DBOS.step(name="fail_artifact_backed_coding_comparison", retries_allowed=True, max_attempts=3)
def mark_failed(job_id: str, error_message: str) -> None:
    with SessionLocal() as db:
        fail_comparison(db, uuid.UUID(job_id), RuntimeError(error_message))
        db.commit()


@DBOS.workflow(name="matter_coding_comparison_v1")
def matter_coding_comparison(job_id: str) -> None:
    try:
        build_comparison(job_id)
    except Exception as exc:
        mark_failed(job_id, str(exc)[:4000])
        raise
