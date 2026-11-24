import uuid

from dbos import DBOS, Queue

from app.database import SessionLocal
from app.identity_domain_recommendations import (
    fail_identity_domain_recommendations,
    run_identity_domain_recommendations,
)

IDENTITY_DOMAIN_RECOMMENDATION_QUEUE = Queue("identity-domain-recommendations", global_concurrency=1)


@DBOS.step(name="review_matter_identity_domains", retries_allowed=False)
def review_identity_domains(run_id: str) -> None:
    run_identity_domain_recommendations(uuid.UUID(run_id), session_factory=SessionLocal)


@DBOS.step(name="fail_matter_identity_domain_review", retries_allowed=True, max_attempts=3)
def mark_identity_domain_review_failed(run_id: str, error_message: str) -> None:
    with SessionLocal() as db:
        fail_identity_domain_recommendations(db, uuid.UUID(run_id), RuntimeError(error_message))
        db.commit()


@DBOS.workflow(name="matter_identity_domain_recommendations_v1")
def matter_identity_domain_recommendations(run_id: str) -> None:
    try:
        review_identity_domains(run_id)
    except Exception as exc:
        mark_identity_domain_review_failed(run_id, str(exc)[:4000])
        raise
