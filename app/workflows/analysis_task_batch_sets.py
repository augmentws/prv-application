import uuid

from dbos import DBOS, Queue

from app.analysis_task_batch_sets import materialize_batch_set
from app.database import SessionLocal

BUILD_QUEUE = Queue("analysis-task-batch-sets", global_concurrency=2)


@DBOS.step(name="materialize_analysis_task_batch_set", retries_allowed=True, max_attempts=5)
def materialize(batch_set_id: str) -> None:
    with SessionLocal() as db:
        materialize_batch_set(db, uuid.UUID(batch_set_id))


@DBOS.workflow(name="analysis_task_batch_set_materialization_v1")
def analysis_task_batch_set_materialization(batch_set_id: str) -> None:
    materialize(batch_set_id)
