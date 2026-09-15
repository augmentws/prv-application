import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import SearchProjectionOperation
from app.workflows.dispatcher import enqueue_search_projection


def create_search_operation(
    db: Session,
    *,
    matter_id: uuid.UUID,
    kind: str,
    payload: dict[str, Any] | None = None,
    created_by_user_id: uuid.UUID | None = None,
    priority: int | None = None,
) -> SearchProjectionOperation:
    operation_id = uuid.uuid4()
    operation = SearchProjectionOperation(
        id=operation_id,
        matter_id=matter_id,
        kind=kind,
        payload=payload or {},
        status="QUEUED",
        workflow_id=f"search-projection:{operation_id}",
        created_by_user_id=created_by_user_id,
    )
    db.add(operation)
    db.flush()
    enqueue_search_projection(db, operation.workflow_id, str(operation.id), priority=priority)
    return operation
