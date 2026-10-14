import uuid

from dbos import DBOS, Queue

from app.analysis_task_promotions import (
    PROMOTION_CHUNK_SIZE,
    apply_analysis_task_promotion_chunk,
    complete_analysis_task_promotion,
    fail_analysis_task_promotion,
    prepare_analysis_task_promotion,
)
from app.database import SessionLocal

PROMOTION_QUEUE = Queue("analysis-task-promotions", global_concurrency=1)


@DBOS.step(name="prepare_analysis_task_promotion")
def prepare(promotion_id: str) -> list[str]:
    with SessionLocal() as db:
        return prepare_analysis_task_promotion(db, uuid.UUID(promotion_id))


@DBOS.step(name="apply_analysis_task_promotion_chunk", retries_allowed=True, max_attempts=3)
def apply_chunk(promotion_id: str, document_ids: list[str]) -> None:
    with SessionLocal() as db:
        apply_analysis_task_promotion_chunk(
            db,
            uuid.UUID(promotion_id),
            [uuid.UUID(value) for value in document_ids],
        )


@DBOS.step(name="complete_analysis_task_promotion")
def complete(promotion_id: str) -> None:
    with SessionLocal() as db:
        complete_analysis_task_promotion(db, uuid.UUID(promotion_id))


@DBOS.step(name="fail_analysis_task_promotion")
def fail(promotion_id: str, message: str) -> None:
    with SessionLocal() as db:
        fail_analysis_task_promotion(db, uuid.UUID(promotion_id), message)


@DBOS.workflow(name="analysis_task_run_promotion_v1")
def analysis_task_run_promotion(promotion_id: str) -> None:
    try:
        document_ids = prepare(promotion_id)
        for offset in range(0, len(document_ids), PROMOTION_CHUNK_SIZE):
            apply_chunk(promotion_id, document_ids[offset : offset + PROMOTION_CHUNK_SIZE])
        complete(promotion_id)
    except Exception as exc:
        fail(promotion_id, str(exc))
        raise
