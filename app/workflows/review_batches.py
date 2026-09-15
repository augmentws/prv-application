import uuid

from dbos import DBOS, Queue

from app.config import get_settings
from app.database import SessionLocal
from app.review_batches import materialize_review_batch

BUILD_QUEUE = Queue("review-batch-builds", global_concurrency=2)


@DBOS.step(name="materialize_review_batch", retries_allowed=True, max_attempts=5)
def materialize(batch_id: str) -> None:
    with SessionLocal() as db:
        materialize_review_batch(db, uuid.UUID(batch_id), get_settings())


@DBOS.workflow(name="review_batch_build")
def review_batch_build(batch_id: str) -> None:
    materialize(batch_id)
