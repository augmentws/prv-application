import uuid

from dbos import DBOS, Queue

from app.config import get_settings
from app.database import SessionLocal
from app.review_batches import (
    complete_review_batch_execution,
    fail_review_batch_execution,
    materialize_review_batch,
)
from app.search.service import sync_review_batch_search

BUILD_QUEUE = Queue("review-batch-builds", global_concurrency=2)


@DBOS.step(name="materialize_review_batch", retries_allowed=True, max_attempts=5)
def materialize(batch_id: str) -> None:
    with SessionLocal() as db:
        materialize_review_batch(db, uuid.UUID(batch_id), get_settings())


@DBOS.step(name="sync_review_batch_search", retries_allowed=True, max_attempts=5)
def sync_search(batch_id: str) -> None:
    parsed_id = uuid.UUID(batch_id)
    try:
        sync_review_batch_search(parsed_id)
    except Exception as exc:  # noqa: BLE001 - persist the terminal projection outcome
        with SessionLocal() as db:
            complete_review_batch_execution(db, parsed_id, search_error=str(exc)[:4000])
            db.commit()
        return
    with SessionLocal() as db:
        complete_review_batch_execution(db, parsed_id)
        db.commit()


@DBOS.step(name="fail_review_batch_build")
def fail_build(batch_id: str, error_message: str) -> None:
    with SessionLocal() as db:
        fail_review_batch_execution(db, uuid.UUID(batch_id), error_message)
        db.commit()


@DBOS.workflow(name="review_batch_build")
def review_batch_build(batch_id: str) -> None:
    try:
        materialize(batch_id)
        sync_search(batch_id)
    except Exception as exc:  # noqa: BLE001 - DBOS exhausted the retrying child step
        fail_build(batch_id, str(exc)[:4000])
