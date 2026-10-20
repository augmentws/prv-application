import uuid

from dbos import DBOS, Queue

from app.analysis_task_batch_sets import (
    batch_set_child_ids,
    complete_batch_set_materialization,
    materialize_batch_set,
)
from app.config import get_settings
from app.database import SessionLocal
from app.search.service import sync_review_batch_search

BUILD_QUEUE = Queue("analysis-task-batch-sets", global_concurrency=2)


@DBOS.step(name="materialize_analysis_task_batch_set", retries_allowed=True, max_attempts=5)
def materialize(batch_set_id: str) -> None:
    with SessionLocal() as db:
        materialize_batch_set(db, uuid.UUID(batch_set_id), get_settings())


@DBOS.step(name="analysis_task_batch_set_child_ids", retries_allowed=True, max_attempts=5)
def child_ids(batch_set_id: str) -> list[str]:
    with SessionLocal() as db:
        return [str(value) for value in batch_set_child_ids(db, uuid.UUID(batch_set_id))]


@DBOS.step(name="sync_analysis_task_batch_set_child_search", retries_allowed=True, max_attempts=5)
def sync_child_search(batch_id: str) -> None:
    try:
        sync_review_batch_search(uuid.UUID(batch_id), get_settings())
    except Exception:  # noqa: BLE001 - child retains FAILED projection state
        # The child retains FAILED search state; authoritative membership remains usable.
        return


@DBOS.step(name="complete_analysis_task_batch_set", retries_allowed=True, max_attempts=5)
def complete(batch_set_id: str) -> None:
    with SessionLocal() as db:
        complete_batch_set_materialization(db, uuid.UUID(batch_set_id))
        db.commit()


@DBOS.workflow(name="analysis_task_batch_set_materialization_v1")
def analysis_task_batch_set_materialization(batch_set_id: str) -> None:
    materialize(batch_set_id)
    for batch_id in child_ids(batch_set_id):
        sync_child_search(batch_id)
    complete(batch_set_id)
