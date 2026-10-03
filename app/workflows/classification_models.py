import logging
import uuid

from dbos import DBOS, Queue

from app.classification_builds import (
    fail_training_run,
    finalize_training_run,
    invoke_runtime,
    prepare_runtime_invocation,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

BUILD_QUEUE = Queue(
    "classification-model-builds",
    global_concurrency=get_settings().classification_max_concurrency,
)


@DBOS.step(name="prepare_classification_model_build", retries_allowed=True, max_attempts=3)
def prepare(training_run_id: str) -> dict | None:
    return prepare_runtime_invocation(uuid.UUID(training_run_id))


@DBOS.step(
    name="invoke_classification_model_runtime",
    retries_allowed=True,
    interval_seconds=2,
    backoff_rate=2,
    max_attempts=3,
)
def train(invocation: dict) -> dict:
    return invoke_runtime(invocation)


@DBOS.step(name="finalize_classification_model_build", retries_allowed=True, max_attempts=5)
def finalize(training_run_id: str, runtime_output: dict) -> str | None:
    version_id = finalize_training_run(uuid.UUID(training_run_id), runtime_output)
    return str(version_id) if version_id is not None else None


@DBOS.step(name="fail_classification_model_build", retries_allowed=True, max_attempts=3)
def fail(training_run_id: str, message: str) -> None:
    fail_training_run(uuid.UUID(training_run_id), message)


@DBOS.workflow(name="classification_model_build_v1")
def classification_model_build(training_run_id: str) -> str | None:
    try:
        invocation = prepare(training_run_id)
        if invocation is None:
            return None
        if invocation.get("completed"):
            return None
        runtime_output = train(invocation)
        return finalize(training_run_id, runtime_output)
    except Exception as exc:
        logger.exception(
            "Classification model build failed training_run_id=%s",
            training_run_id,
        )
        fail(training_run_id, str(exc))
        return None
