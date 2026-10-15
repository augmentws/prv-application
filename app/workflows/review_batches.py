import uuid

from dbos import DBOS, Queue

from app.config import get_settings
from app.database import SessionLocal
from app.models import ReviewBatch, ReviewBatchRun
from app.review_batches import (
    complete_review_batch_execution,
    fail_review_batch_execution,
    materialize_review_batch,
)
from app.search.service import sync_review_batch_search

BUILD_QUEUE = Queue("review-batch-builds", global_concurrency=2)


@DBOS.step(name="materialize_review_batch", retries_allowed=True, max_attempts=5)
def materialize(batch_id: str) -> None:
    with SessionLocal() as db:
        materialize_review_batch(db, uuid.UUID(batch_id), get_settings())


@DBOS.step(name="sync_review_batch_search", retries_allowed=True, max_attempts=5)
def sync_search(batch_id: str) -> None:
    parsed_id = uuid.UUID(batch_id)
    try:
        sync_review_batch_search(parsed_id)
    except Exception as exc:  # noqa: BLE001 - persist the terminal projection outcome
        with SessionLocal() as db:
            complete_review_batch_execution(db, parsed_id, search_error=str(exc)[:4000])
            db.commit()
        return
    with SessionLocal() as db:
        complete_review_batch_execution(db, parsed_id)
        db.commit()


@DBOS.step(name="fail_review_batch_build")
def fail_build(batch_id: str, error_message: str) -> None:
    with SessionLocal() as db:
        parsed_id = uuid.UUID(batch_id)
        fail_review_batch_execution(db, parsed_id, error_message)
        batch = db.get(ReviewBatch, parsed_id)
        if batch is not None and batch.selection_type == "CLASSIFICATION_PREDICTIONS":
            run_id = batch.selection_definition.get("classification_projection_run_id")
            run = db.get(ReviewBatchRun, uuid.UUID(str(run_id))) if run_id else None
            if run is not None and run.status == "QUEUED":
                from app.classification_projections import fail_classifier_projection

                fail_classifier_projection(db, run.id, f"Derived review batch failed: {error_message}")
        db.commit()


@DBOS.step(name="enqueue_classification_review_batch_projection")
def enqueue_derived_projection(batch_id: str) -> None:
    """Start the classifier-value projection after derived membership is searchable."""

    parsed_id = uuid.UUID(batch_id)
    with SessionLocal() as db:
        batch = db.get(ReviewBatch, parsed_id)
        if batch is None or batch.selection_type != "CLASSIFICATION_PREDICTIONS":
            return
        run_id = batch.selection_definition.get("classification_projection_run_id")
        run = db.get(ReviewBatchRun, uuid.UUID(str(run_id))) if run_id else None
        if run is None or run.status != "QUEUED":
            return
        if batch.status != "READY" or batch.search_status != "READY":
            from app.classification_projections import fail_classifier_projection

            fail_classifier_projection(db, run.id, "Derived review batch search projection is not ready")
            return
        from app.workflows.dispatcher import enqueue_classification_projection

        enqueue_classification_projection(db, run.dbos_workflow_id, str(run.id))
        db.commit()


@DBOS.workflow(name="review_batch_build")
def review_batch_build(batch_id: str) -> None:
    try:
        materialize(batch_id)
        sync_search(batch_id)
        enqueue_derived_projection(batch_id)
    except Exception as exc:  # noqa: BLE001 - DBOS exhausted the retrying child step
        fail_build(batch_id, str(exc)[:4000])
