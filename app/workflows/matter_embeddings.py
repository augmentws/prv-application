import logging
import uuid

from dbos import DBOS, Queue, SetWorkflowID

from app.matter_embeddings import (
    complete_job,
    embedding_execution,
    fail_job,
    finalize_voyage_batch,
    index_job,
    plan_job,
    poll_voyage_batch,
    process_batch,
    refresh_job,
    submit_voyage_batch,
)
from app.search.client import is_retryable_opensearch_error
from embedding_service.config import get_embedding_settings

logger = logging.getLogger(__name__)

PLAN_QUEUE = Queue("matter-embedding-plans", global_concurrency=2)
BATCH_QUEUE = Queue("matter-embedding-batches", global_concurrency=2)
VOYAGE_REALTIME_QUEUE = Queue(
    "matter-embedding-voyage-realtime",
    global_concurrency=get_embedding_settings().voyage_realtime_concurrency,
)
VOYAGE_BATCH_QUEUE = Queue(
    "matter-embedding-voyage-batches",
    global_concurrency=get_embedding_settings().voyage_batch_concurrency,
)


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


@DBOS.step(name="read_matter_embedding_execution")
def read_execution(job_id: str) -> dict:
    return embedding_execution(uuid.UUID(job_id))


@DBOS.step(name="submit_matter_embedding_voyage_batch", retries_allowed=True, max_attempts=5)
def submit_voyage(batch_id: str) -> dict:
    return submit_voyage_batch(uuid.UUID(batch_id))


@DBOS.step(name="poll_matter_embedding_voyage_batch", retries_allowed=True, max_attempts=10)
def poll_voyage(batch_id: str) -> str:
    return poll_voyage_batch(uuid.UUID(batch_id))


@DBOS.step(name="finalize_matter_embedding_voyage_batch", retries_allowed=True, max_attempts=5)
def finalize_voyage(batch_id: str) -> dict[str, int]:
    return finalize_voyage_batch(uuid.UUID(batch_id))


@DBOS.workflow(name="matter_embedding_voyage_batch")
def matter_embedding_voyage_batch(
    batch_id: str,
    poll_seconds: float,
) -> dict[str, int]:
    submission = submit_voyage(batch_id)
    status = str(submission["status"])
    if status == "cancelled":
        return {key: 0 for key in ("processed_count", "embedded_count", "skipped_count", "failed_count", "chunk_count")}
    while status not in {"completed", "partially_completed", "not_required"}:
        DBOS.sleep(poll_seconds)
        status = poll_voyage(batch_id)
        if status == "cancelled":
            return {
                key: 0 for key in ("processed_count", "embedded_count", "skipped_count", "failed_count", "chunk_count")
            }
    return finalize_voyage(batch_id)


@DBOS.step(name="refresh_matter_embedding_progress")
def refresh(job_id: str) -> None:
    refresh_job(uuid.UUID(job_id))


@DBOS.step(
    name="index_matter_embedding_job",
    retries_allowed=True,
    interval_seconds=30,
    max_attempts=16,
    backoff_rate=2,
    should_retry=is_retryable_opensearch_error,
)
def index(job_id: str) -> str:
    return index_job(uuid.UUID(job_id))


@DBOS.step(name="complete_matter_embedding_job")
def complete(job_id: str) -> None:
    complete_job(uuid.UUID(job_id))


@DBOS.step(name="fail_matter_embedding_job")
def fail(job_id: str, message: str) -> None:
    fail_job(uuid.UUID(job_id), message)


@DBOS.workflow(name="matter_embedding_job")
def matter_embedding_job(job_id: str) -> None:
    """Legacy workflow retained unchanged so already-running jobs can replay."""
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
        index_status = index(job_id)
        logger.info(
            "Indexed matter embedding job job_id=%s status=%s",
            job_id,
            index_status,
        )
        complete(job_id)
        logger.info("Completed matter embedding job job_id=%s", job_id)
    except Exception as exc:
        logger.exception("Matter embedding job failed job_id=%s", job_id)
        fail(job_id, str(exc))
        raise


@DBOS.workflow(name="matter_embedding_job_v2")
def matter_embedding_job_v2(job_id: str) -> None:
    logger.info("Starting matter embedding job v2 job_id=%s", job_id)
    try:
        execution = read_execution(job_id)
        batch_ids = plan(job_id)
        logger.info("Planned matter embedding job job_id=%s batch_count=%s", job_id, len(batch_ids))
        handles = []
        for batch_number, batch_id in enumerate(batch_ids):
            with SetWorkflowID(f"matter-embedding:{job_id}:batch:{batch_number}"):
                if execution.get("mode") == "voyage_batch":
                    handles.append(
                        VOYAGE_BATCH_QUEUE.enqueue(
                            matter_embedding_voyage_batch,
                            batch_id,
                            float(execution.get("poll_seconds", 15.0)),
                        )
                    )
                elif execution.get("mode") == "voyage_realtime":
                    handles.append(VOYAGE_REALTIME_QUEUE.enqueue(matter_embedding_batch, batch_id))
                else:
                    handles.append(BATCH_QUEUE.enqueue(matter_embedding_batch, batch_id))
        for handle in handles:
            handle.get_result()
            refresh(job_id)
        index_status = index(job_id)
        logger.info(
            "Indexed matter embedding job job_id=%s status=%s",
            job_id,
            index_status,
        )
        complete(job_id)
        logger.info("Completed matter embedding job job_id=%s", job_id)
    except Exception as exc:
        logger.exception("Matter embedding job failed job_id=%s", job_id)
        fail(job_id, str(exc))
        raise
