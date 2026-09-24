import logging
import uuid

from dbos import DBOS, Queue, SetWorkflowID

from app.bulk_tags import (
    complete_bulk_tag_job,
    fail_bulk_tag_job,
    process_bulk_tag_batch,
    refresh_bulk_tag_job,
    snapshot_bulk_tag_scope,
)

logger = logging.getLogger(__name__)

PLAN_QUEUE = Queue("matter-bulk-tag-plans", global_concurrency=2)
BATCH_QUEUE = Queue("matter-bulk-tag-batches", global_concurrency=4)


@DBOS.step(name="snapshot_matter_bulk_tag_scope", retries_allowed=True, max_attempts=5)
def snapshot(job_id: str) -> list[str]:
    return [str(value) for value in snapshot_bulk_tag_scope(uuid.UUID(job_id))]


@DBOS.step(name="process_matter_bulk_tag_batch", retries_allowed=True, max_attempts=5)
def process(batch_id: str) -> dict[str, int]:
    return process_bulk_tag_batch(uuid.UUID(batch_id))


@DBOS.workflow(name="matter_bulk_tag_batch")
def matter_bulk_tag_batch(batch_id: str) -> dict[str, int]:
    return process(batch_id)


@DBOS.step(name="refresh_matter_bulk_tag_progress")
def refresh(job_id: str) -> None:
    refresh_bulk_tag_job(uuid.UUID(job_id))


@DBOS.step(name="complete_matter_bulk_tag_job")
def complete(job_id: str) -> None:
    complete_bulk_tag_job(uuid.UUID(job_id))


@DBOS.step(name="fail_matter_bulk_tag_job")
def fail(job_id: str, message: str) -> None:
    fail_bulk_tag_job(uuid.UUID(job_id), message)


@DBOS.workflow(name="matter_bulk_tag_job")
def matter_bulk_tag_job(job_id: str) -> None:
    logger.info("Starting matter bulk tag job_id=%s", job_id)
    try:
        batch_ids = snapshot(job_id)
        handles = []
        for batch_number, batch_id in enumerate(batch_ids, start=1):
            with SetWorkflowID(f"matter-bulk-tag:{job_id}:batch:{batch_number}"):
                handles.append(BATCH_QUEUE.enqueue(matter_bulk_tag_batch, batch_id))
        for handle in handles:
            handle.get_result()
            refresh(job_id)
        complete(job_id)
        logger.info("Completed matter bulk tag job_id=%s", job_id)
    except Exception as exc:
        logger.exception("Matter bulk tag failed job_id=%s", job_id)
        fail(job_id, str(exc))
        raise
