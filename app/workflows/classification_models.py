import logging
import uuid

from dbos import DBOS, Queue, SetWorkflowID

from app.classification_builds import (
    fail_training_run,
    finalize_training_run,
    invoke_runtime,
    mark_training_runtime_completed,
    mark_training_runtime_started,
    prepare_runtime_invocation,
    training_failure_details,
)
from app.classification_training_jobs import (
    complete_training_job,
    fail_training_job,
    launch_training_job_build,
    materialize_training_job_features,
    materialize_training_job_snapshot,
    training_job_failure_details,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

BUILD_QUEUE = Queue(
    "classification-model-builds",
    global_concurrency=get_settings().classification_max_concurrency,
)
TRAINING_JOB_QUEUE = Queue(
    "classification-training-jobs",
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


@DBOS.step(name="mark_classification_model_training_started", retries_allowed=True, max_attempts=3)
def mark_training_started(training_run_id: str) -> None:
    mark_training_runtime_started(uuid.UUID(training_run_id))


@DBOS.step(name="mark_classification_model_runtime_completed", retries_allowed=True, max_attempts=3)
def mark_runtime_completed(training_run_id: str) -> None:
    mark_training_runtime_completed(uuid.UUID(training_run_id))


@DBOS.step(name="finalize_classification_model_build", retries_allowed=True, max_attempts=5)
def finalize(training_run_id: str, runtime_output: dict) -> str | None:
    version_id = finalize_training_run(uuid.UUID(training_run_id), runtime_output)
    return str(version_id) if version_id is not None else None


@DBOS.step(name="fail_classification_model_build", retries_allowed=True, max_attempts=3)
def fail(training_run_id: str, message: str, code: str, retriable: bool) -> None:
    fail_training_run(
        uuid.UUID(training_run_id),
        message,
        code=code,
        retriable=retriable,
    )


@DBOS.workflow(name="classification_model_build_v1")
def classification_model_build(training_run_id: str) -> str | None:
    try:
        invocation = prepare(training_run_id)
        if invocation is None:
            return None
        if invocation.get("completed"):
            return None
        mark_training_started(training_run_id)
        runtime_output = train(invocation)
        mark_runtime_completed(training_run_id)
        return finalize(training_run_id, runtime_output)
    except Exception as exc:
        logger.exception(
            "Classification model build failed training_run_id=%s",
            training_run_id,
        )
        code, retriable = training_failure_details(exc)
        fail(training_run_id, str(exc), code, retriable)
        raise


@DBOS.step(name="materialize_classification_training_job_snapshot", retries_allowed=True, max_attempts=3)
def materialize_job_snapshot(training_job_id: str) -> str | None:
    snapshot_id = materialize_training_job_snapshot(uuid.UUID(training_job_id))
    return str(snapshot_id) if snapshot_id is not None else None


@DBOS.step(name="materialize_classification_training_job_features", retries_allowed=False)
def materialize_job_features(training_job_id: str, snapshot_id: str) -> None:
    materialize_training_job_features(uuid.UUID(training_job_id), uuid.UUID(snapshot_id))


@DBOS.step(name="launch_classification_training_job_build", retries_allowed=True, max_attempts=3)
def launch_job_build(training_job_id: str) -> dict[str, str] | None:
    return launch_training_job_build(uuid.UUID(training_job_id))


@DBOS.step(name="complete_classification_training_job", retries_allowed=True, max_attempts=3)
def complete_job(training_job_id: str) -> None:
    complete_training_job(uuid.UUID(training_job_id))


@DBOS.step(name="fail_classification_training_job", retries_allowed=True, max_attempts=3)
def fail_job(training_job_id: str, message: str, code: str, retriable: bool) -> None:
    fail_training_job(uuid.UUID(training_job_id), message, code=code, retriable=retriable)


@DBOS.workflow(name="classification_training_job_v1")
def classification_training_job(training_job_id: str) -> str | None:
    try:
        snapshot_id = materialize_job_snapshot(training_job_id)
        if snapshot_id is None:
            return None
        materialize_job_features(training_job_id, snapshot_id)
        build = launch_job_build(training_job_id)
        if build is None:
            return None
        with SetWorkflowID(build["workflow_id"]):
            handle = BUILD_QUEUE.enqueue(
                classification_model_build,
                build["training_run_id"],
            )
        version_id = handle.get_result()
        complete_job(training_job_id)
        return version_id
    except Exception as exc:
        logger.exception(
            "Classification training job failed training_job_id=%s",
            training_job_id,
        )
        code, retriable, summary = training_job_failure_details(exc)
        fail_job(training_job_id, summary, code, retriable)
        raise
