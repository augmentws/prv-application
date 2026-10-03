import uuid

from dbos import DBOS, Queue

from app.classification_lifecycle import promote_model_version
from app.config import get_settings
from app.database import SessionLocal

PROMOTION_QUEUE = Queue(
    "classification-model-promotions",
    global_concurrency=get_settings().classification_max_concurrency,
)


@DBOS.step(name="apply_classification_model_promotion", retries_allowed=True, max_attempts=3)
def apply_promotion(
    workflow_id: str,
    matter_id: str,
    classification_model_id: str,
    model_version_id: str,
    action: str,
    expected_model_row_version: int,
    preview_token: str,
    reason: str,
    actor_user_id: str,
) -> str:
    if action not in {"ACTIVATION", "ROLLBACK"}:
        raise ValueError("Unsupported classification model promotion action")
    with SessionLocal() as db:
        decision = promote_model_version(
            db,
            matter_id=uuid.UUID(matter_id),
            classification_model_id=uuid.UUID(classification_model_id),
            model_version_id=uuid.UUID(model_version_id),
            action=action,
            expected_model_row_version=expected_model_row_version,
            preview_token=preview_token,
            reason=reason,
            actor_user_id=uuid.UUID(actor_user_id),
            workflow_id=workflow_id,
        )
        db.commit()
        return str(decision.id)


@DBOS.workflow(name="classification_model_promote_v1")
def classification_model_promote(
    workflow_id: str,
    matter_id: str,
    classification_model_id: str,
    model_version_id: str,
    action: str,
    expected_model_row_version: int,
    preview_token: str,
    reason: str,
    actor_user_id: str,
) -> str:
    return apply_promotion(
        workflow_id,
        matter_id,
        classification_model_id,
        model_version_id,
        action,
        expected_model_row_version,
        preview_token,
        reason,
        actor_user_id,
    )
