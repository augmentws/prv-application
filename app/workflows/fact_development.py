import logging
import uuid

from dbos import DBOS, Queue, SetWorkflowID

from app.config import get_settings
from app.database import SessionLocal
from app.fact_batches import poll_chunk_batch, submit_chunk_batch
from app.fact_development import (
    FactDevelopmentRunError,
    chunk_window,
    fail_fact_development_run,
    finalize_fact_development_run,
    prepare_fact_development_run,
    process_fact_development_chunk,
    run_ordered_window,
)

logger = logging.getLogger(__name__)

RUN_QUEUE = Queue("fact-development-runs", global_concurrency=2)
CHUNK_QUEUE = Queue(
    "fact-development-chunks",
    global_concurrency=get_settings().fact_development_document_concurrency,
)


def _should_retry(error: BaseException) -> bool:
    return not isinstance(error, FactDevelopmentRunError)


@DBOS.step(name="prepare_fact_development_run", retries_allowed=True, max_attempts=3, should_retry=_should_retry)
def prepare(run_id: str) -> list[str]:
    with SessionLocal() as db:
        return prepare_fact_development_run(db, uuid.UUID(run_id))


@DBOS.step(name="read_fact_development_chunk_window")
def read_window(run_id: str) -> int:
    with SessionLocal() as db:
        return chunk_window(db, uuid.UUID(run_id))


@DBOS.step(name="process_fact_development_chunk", retries_allowed=True, max_attempts=3, should_retry=_should_retry)
def process(chunk_id: str) -> dict:
    return process_fact_development_chunk(uuid.UUID(chunk_id))


@DBOS.step(name="submit_fact_development_chunk_batch", retries_allowed=True, max_attempts=3, should_retry=_should_retry)
def submit_batch(chunk_id: str) -> dict:
    with SessionLocal() as db:
        return submit_chunk_batch(db, uuid.UUID(chunk_id))


@DBOS.step(name="poll_fact_development_chunk_batch", retries_allowed=True, max_attempts=10, should_retry=_should_retry)
def poll_batch(chunk_id: str) -> bool:
    with SessionLocal() as db:
        return poll_chunk_batch(db, uuid.UUID(chunk_id))


@DBOS.workflow(name="fact_development_chunk_v1")
def fact_development_chunk(chunk_id: str) -> dict:
    # With the Batch API, wait durably for the chunk's provider batch; real-time runs go straight to processing.
    submission = submit_batch(chunk_id)
    if submission["submitted"]:
        while poll_batch(chunk_id):
            DBOS.sleep(float(submission["poll_seconds"]))
    return process(chunk_id)


@DBOS.step(name="finalize_fact_development_run", retries_allowed=True, max_attempts=3, should_retry=_should_retry)
def finalize(run_id: str) -> None:
    with SessionLocal() as db:
        finalize_fact_development_run(db, uuid.UUID(run_id))


@DBOS.step(name="fail_fact_development_run", retries_allowed=True, max_attempts=3)
def fail(run_id: str, message: str, code: str, retriable: bool) -> None:
    with SessionLocal() as db:
        fail_fact_development_run(db, uuid.UUID(run_id), message, code=code, retriable=retriable)


def _enqueue_chunk(chunk_id: str):
    with SetWorkflowID(f"{DBOS.workflow_id}:chunk:{chunk_id}"):
        return CHUNK_QUEUE.enqueue(fact_development_chunk, chunk_id)


@DBOS.workflow(name="fact_development_v1")
def fact_development_run(run_id: str) -> None:
    try:
        chunk_ids = prepare(run_id)
        window = read_window(run_id)
        run_ordered_window(chunk_ids, window, _enqueue_chunk, lambda handle: handle.get_result())
        finalize(run_id)
    except Exception as exc:
        logger.exception("Fact-development run failed run_id=%s", run_id)
        code = getattr(exc, "code", None) or "FACT_DEVELOPMENT_RUN_FAILED"
        fail(run_id, str(exc)[:4000], code, not isinstance(exc, FactDevelopmentRunError))
        raise
