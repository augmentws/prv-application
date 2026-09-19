import logging
import uuid

from dbos import DBOS, Queue

from artifact_service.text_processing import fail_run, process_run

logger = logging.getLogger(__name__)

PROCESSING_QUEUE = Queue("collection-text-processing", global_concurrency=2)


@DBOS.step(name="process_collection_text", retries_allowed=True, max_attempts=3)
def process(run_id: str) -> None:
    process_run(uuid.UUID(run_id))


@DBOS.step(name="fail_collection_text_processing")
def fail(run_id: str, message: str) -> None:
    fail_run(uuid.UUID(run_id), message)


@DBOS.workflow(name="collection_text_processing")
def collection_text_processing(run_id: str) -> None:
    logger.info("Starting collection text processing run_id=%s", run_id)
    try:
        process(run_id)
        logger.info("Completed collection text processing run_id=%s", run_id)
    except Exception as exc:
        logger.exception("Collection text processing failed run_id=%s", run_id)
        fail(run_id, str(exc))
        raise
