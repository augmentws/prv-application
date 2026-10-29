import logging
import uuid

from dbos import DBOS, Queue

from app.review_programs import (
    fail_review_program_whole_collection_scoring_preparation,
    prepare_review_program_whole_collection_scoring,
)

logger = logging.getLogger(__name__)

ORCHESTRATION_QUEUE = Queue("review-program-orchestration", global_concurrency=2)


@DBOS.step(
    name="prepare_review_program_whole_collection_scoring",
    retries_allowed=True,
    interval_seconds=2,
    backoff_rate=2,
    max_attempts=3,
)
def prepare_scoring(review_program_run_id: str, stage_attempt_id: str) -> None:
    prepare_review_program_whole_collection_scoring(
        uuid.UUID(review_program_run_id),
        uuid.UUID(stage_attempt_id),
    )


@DBOS.step(name="fail_review_program_whole_collection_scoring_preparation", retries_allowed=True, max_attempts=3)
def fail_scoring_preparation(review_program_run_id: str, stage_attempt_id: str, error_summary: str) -> None:
    fail_review_program_whole_collection_scoring_preparation(
        uuid.UUID(review_program_run_id),
        uuid.UUID(stage_attempt_id),
        error_summary,
    )


@DBOS.workflow(name="review_program_scoring_preparation_v1")
def review_program_scoring_preparation(review_program_run_id: str, stage_attempt_id: str) -> None:
    try:
        prepare_scoring(review_program_run_id, stage_attempt_id)
    except Exception as exc:
        logger.exception(
            "Review Program scoring preparation failed review_program_run_id=%s stage_attempt_id=%s",
            review_program_run_id,
            stage_attempt_id,
        )
        fail_scoring_preparation(review_program_run_id, stage_attempt_id, str(exc))
        raise
