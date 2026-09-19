import logging
import uuid

from dbos import DBOS, Queue, SetWorkflowID

from app.topic_clustering import (
    application_batch_ids,
    complete_job,
    discover_and_plan,
    fail_job,
    process_batch,
    refresh_job,
)

logger = logging.getLogger(__name__)

PLAN_QUEUE = Queue("matter-topic-plans", global_concurrency=1)
BATCH_QUEUE = Queue("matter-topic-batches", global_concurrency=2)


@DBOS.step(name="discover_matter_topics", retries_allowed=True, max_attempts=3)
def discover(job_id: str) -> int:
    return discover_and_plan(uuid.UUID(job_id))


@DBOS.step(name="plan_matter_topic_application", retries_allowed=True, max_attempts=3)
def plan_application(job_id: str) -> list[str]:
    return [str(value) for value in application_batch_ids(uuid.UUID(job_id))]


@DBOS.step(name="process_matter_topic_batch", retries_allowed=True, max_attempts=5)
def process(batch_id: str) -> dict[str, int]:
    return process_batch(uuid.UUID(batch_id))


@DBOS.workflow(name="matter_topic_batch")
def matter_topic_batch(batch_id: str) -> dict[str, int]:
    return process(batch_id)


@DBOS.step(name="refresh_matter_topic_progress")
def refresh(job_id: str) -> None:
    refresh_job(uuid.UUID(job_id))


@DBOS.step(name="complete_matter_topic_job")
def complete(job_id: str) -> None:
    complete_job(uuid.UUID(job_id))


@DBOS.step(name="fail_matter_topic_job")
def fail(job_id: str, message: str) -> None:
    fail_job(uuid.UUID(job_id), message)


@DBOS.workflow(name="matter_topic_job")
def matter_topic_job(job_id: str) -> None:
    logger.info("Starting matter topic discovery job_id=%s", job_id)
    try:
        topic_count = discover(job_id)
        logger.info("Topic proposals ready for review job_id=%s topic_count=%s", job_id, topic_count)
    except Exception as exc:
        logger.exception("Matter topic discovery failed job_id=%s", job_id)
        fail(job_id, str(exc))
        raise


@DBOS.workflow(name="matter_topic_application")
def matter_topic_application(job_id: str) -> None:
    logger.info("Starting approved matter topic application job_id=%s", job_id)
    try:
        batch_ids = plan_application(job_id)
        logger.info("Applying approved matter topics job_id=%s batch_count=%s", job_id, len(batch_ids))
        handles = []
        for batch_number, batch_id in enumerate(batch_ids):
            with SetWorkflowID(f"matter-topics:{job_id}:batch:{batch_number}"):
                handles.append(BATCH_QUEUE.enqueue(matter_topic_batch, batch_id))
        for handle in handles:
            handle.get_result()
            refresh(job_id)
        complete(job_id)
        logger.info("Completed approved matter topic application job_id=%s", job_id)
    except Exception as exc:
        logger.exception("Matter topic application failed job_id=%s", job_id)
        fail(job_id, str(exc))
        raise
