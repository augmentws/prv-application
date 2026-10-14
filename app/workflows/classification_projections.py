import uuid

from dbos import DBOS, Queue

from app.classification_projections import execute_classifier_projection, fail_classifier_projection
from app.database import SessionLocal

CLASSIFICATION_PROJECTION_QUEUE = Queue("classification-projections", global_concurrency=1)


@DBOS.step(name="materialize_classification_projection", retries_allowed=True, max_attempts=3)
def materialize(run_id: str) -> None:
    with SessionLocal() as db:
        execute_classifier_projection(db, uuid.UUID(run_id))


@DBOS.step(name="fail_classification_projection")
def fail(run_id: str, message: str) -> None:
    with SessionLocal() as db:
        fail_classifier_projection(db, uuid.UUID(run_id), message)


@DBOS.workflow(name="classification_prediction_project_v1")
def classification_prediction_project(run_id: str) -> None:
    try:
        materialize(run_id)
    except Exception as exc:
        fail(run_id, str(exc))
        raise
