import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditRecord


def record_audit(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditRecord(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
    )
