import logging
import uuid

from dbos import DBOS, Queue, SetWorkflowID

from app.matter_embeddings import complete_job, fail_job, plan_job, process_batch, refresh_job

logger = logging.getLogger(__name__)

PLAN_QUEUE = Queue("matter-embedding-plans", global_concurrency=2)
BATCH_QUEUE = Queue("matter-embedding-batches", global_concurrency=2)


@DBOS.step(name="plan_matter_embedding_job", retries_allowed=True, max_attempts=5)
def plan(job_id: str) -> list[str]:
    return [str(value) for value in plan_job(uuid.UUID(job_id))]


@DBOS.step(name="process_matter_embedding_batch", retries_allowed=True, max_attempts=5)
def process(batch_id: str) -> dict[str, int]:
    result = process_batch(uuid.UUID(batch_id))
    return result


@DBOS.workflow(name="matter_embedding_batch")
def matter_embedding_batch(batch_id: str) -> dict[str, int]:
    return process(batch_id)


@DBOS.step(name="refresh_matter_embedding_progress")
def refresh(job_id: str) -> None:
    refresh_job(uuid.UUID(job_id))


@DBOS.step(name="complete_matter_embedding_job")
def complete(job_id: str) -> None:
    complete_job(uuid.UUID(job_id))


@DBOS.step(name="fail_matter_embedding_job")
def fail(job_id: str, message: str) -> None:
    fail_job(uuid.UUID(job_id), message)


@DBOS.workflow(name="matter_embedding_job")
def matter_embedding_job(job_id: str) -> None:
    logger.info("Starting matter embedding job job_id=%s", job_id)
    try:
        batch_ids = plan(job_id)
        logger.info("Planned matter embedding job job_id=%s batch_count=%s", job_id, len(batch_ids))
        handles = []
        for batch_number, batch_id in enumerate(batch_ids):
            with SetWorkflowID(f"matter-embedding:{job_id}:batch:{batch_number}"):
                handles.append(BATCH_QUEUE.enqueue(matter_embedding_batch, batch_id))
        for handle in handles:
            handle.get_result()
            refresh(job_id)
        complete(job_id)
        logger.info("Completed matter embedding job job_id=%s", job_id)
    except Exception as exc:
        logger.exception("Matter embedding job failed job_id=%s", job_id)
        fail(job_id, str(exc))
        raise
