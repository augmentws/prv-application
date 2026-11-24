import uuid

from dbos import DBOS, Queue

from app.database import SessionLocal
from app.identity_analysis import fail_identity_analysis, run_identity_analysis

IDENTITY_ANALYSIS_QUEUE = Queue("identity-analysis", global_concurrency=1)


@DBOS.step(name="build_matter_identity_inventory", retries_allowed=True, max_attempts=3)
def build_identity_inventory(analysis_id: str) -> None:
    run_identity_analysis(uuid.UUID(analysis_id), session_factory=SessionLocal)


@DBOS.step(name="fail_matter_identity_analysis", retries_allowed=True, max_attempts=3)
def mark_identity_analysis_failed(analysis_id: str, error_message: str) -> None:
    with SessionLocal() as db:
        fail_identity_analysis(db, uuid.UUID(analysis_id), RuntimeError(error_message))
        db.commit()


@DBOS.workflow(name="matter_identity_analysis_v1")
def matter_identity_analysis(analysis_id: str) -> None:
    try:
        build_identity_inventory(analysis_id)
    except Exception as exc:
        mark_identity_analysis_failed(analysis_id, str(exc)[:4000])
        raise
