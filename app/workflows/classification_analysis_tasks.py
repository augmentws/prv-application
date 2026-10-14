import logging
import uuid

from dbos import DBOS, Queue, SetWorkflowID

from app.classification_analysis_tasks import (
    ClassificationAnalysisTaskCanceled,
    classification_analysis_failure_details,
    complete_classification_analysis_run,
    fail_classification_analysis_run,
    launch_classification_analysis_scoring,
    materialize_classification_analysis_features,
    materialize_classification_analysis_snapshot,
)
from app.classification_scoring import scoring_failure_details
from app.config import get_settings
from app.workflows.classification_scoring import SCORING_QUEUE, classification_model_score

logger = logging.getLogger(__name__)

CLASSIFICATION_ANALYSIS_QUEUE = Queue(
    "classification-analysis-tasks",
    global_concurrency=get_settings().classification_max_concurrency,
)


@DBOS.step(name="freeze_classification_analysis_population", retries_allowed=True, max_attempts=3)
def freeze_population(run_id: str) -> str | None:
    snapshot_id = materialize_classification_analysis_snapshot(uuid.UUID(run_id))
    return str(snapshot_id) if snapshot_id else None


@DBOS.step(name="materialize_classification_analysis_features", retries_allowed=False)
def materialize_features(run_id: str, snapshot_id: str) -> None:
    materialize_classification_analysis_features(uuid.UUID(run_id), uuid.UUID(snapshot_id))


@DBOS.step(name="launch_classification_analysis_scoring", retries_allowed=True, max_attempts=3)
def launch_scoring(run_id: str) -> dict[str, str] | None:
    return launch_classification_analysis_scoring(uuid.UUID(run_id))


@DBOS.step(name="complete_classification_analysis_task", retries_allowed=True, max_attempts=3)
def complete(run_id: str) -> None:
    complete_classification_analysis_run(uuid.UUID(run_id))


@DBOS.step(name="fail_classification_analysis_task", retries_allowed=True, max_attempts=3)
def fail(run_id: str, message: str, code: str, retriable: bool) -> None:
    fail_classification_analysis_run(uuid.UUID(run_id), message, code=code, retriable=retriable)


@DBOS.workflow(name="classification_analysis_task_v1")
def classification_analysis_task(run_id: str) -> str | None:
    try:
        snapshot_id = freeze_population(run_id)
        if snapshot_id is None:
            return None
        materialize_features(run_id, snapshot_id)
        scoring = launch_scoring(run_id)
        if scoring is None:
            return None
        with SetWorkflowID(scoring["workflow_id"]):
            handle = SCORING_QUEUE.enqueue(classification_model_score, scoring["scoring_run_id"])
        handle.get_result()
        complete(run_id)
        return scoring["scoring_run_id"]
    except ClassificationAnalysisTaskCanceled:
        return None
    except Exception as exc:
        logger.exception("Classification Analysis Task failed run_id=%s", run_id)
        code, retriable = classification_analysis_failure_details(exc)
        nested_code, nested_retriable = scoring_failure_details(exc)
        if nested_code != "CLASSIFICATION_SCORING_FAILED":
            code, retriable = nested_code, nested_retriable
        fail(run_id, str(exc), code, retriable)
        raise
