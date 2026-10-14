import logging
import uuid

from dbos import DBOS, Queue

from app.classification_scoring import (
    fail_scoring_run,
    finalize_scoring_run,
    invoke_runtime,
    mark_scoring_runtime_started,
    prepare_runtime_invocation,
    scoring_failure_cause,
    scoring_failure_details,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

SCORING_QUEUE = Queue(
    "classification-model-scoring",
    global_concurrency=get_settings().classification_max_concurrency,
)


@DBOS.step(name="prepare_classification_model_scoring", retries_allowed=True, max_attempts=3)
def prepare(scoring_run_id: str) -> dict | None:
    return prepare_runtime_invocation(uuid.UUID(scoring_run_id))


@DBOS.step(
    name="invoke_classification_model_scoring_runtime",
    retries_allowed=True,
    interval_seconds=2,
    backoff_rate=2,
    max_attempts=3,
)
def score(invocation: dict) -> dict:
    mark_scoring_runtime_started(uuid.UUID(invocation["request"]["scoring_run_id"]))
    return invoke_runtime(invocation)


@DBOS.step(name="finalize_classification_model_scoring", retries_allowed=True, max_attempts=5)
def finalize(scoring_run_id: str, runtime_output: dict) -> None:
    finalize_scoring_run(uuid.UUID(scoring_run_id), runtime_output)


@DBOS.step(name="fail_classification_model_scoring", retries_allowed=True, max_attempts=3)
def fail(scoring_run_id: str, message: str, code: str, retriable: bool) -> None:
    fail_scoring_run(uuid.UUID(scoring_run_id), message, code=code, retriable=retriable)


@DBOS.workflow(name="classification_model_score_v1")
def classification_model_score(scoring_run_id: str) -> None:
    try:
        invocation = prepare(scoring_run_id)
        if invocation is None or invocation.get("completed"):
            return
        runtime_output = score(invocation)
        finalize(scoring_run_id, runtime_output)
    except Exception as exc:
        logger.exception("Classification scoring failed scoring_run_id=%s", scoring_run_id)
        code, retriable = scoring_failure_details(exc)
        fail(scoring_run_id, str(scoring_failure_cause(exc)), code, retriable)
        raise
