import base64
import binascii
import json
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.authorization import legacy_or_can_manage_tenant
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.models import AuditRecord, Tenant, User
from app.schemas import AuditRecordPage, AuditRecordRead

router = APIRouter(prefix="/v1/tenants/{tenant_id}/audit-records", tags=["audit records"])


def _encode_cursor(record: AuditRecord) -> str:
    payload = json.dumps(
        {"created_at": record.created_at.isoformat(), "id": str(record.id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        return datetime.fromisoformat(payload["created_at"]), uuid.UUID(payload["id"])
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid audit cursor") from exc


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("", response_model=AuditRecordPage)
def list_audit_records(
    tenant_id: uuid.UUID,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    actor_user_id: uuid.UUID | None = None,
    system_actor: bool | None = None,
    action: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    action_prefix: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    target_type: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    target_id: uuid.UUID | None = None,
    cursor: Annotated[str | None, Query(min_length=1, max_length=1000)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AuditRecordPage:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if not legacy_or_can_manage_tenant(db, principal, tenant.id, "tenant.view_audit"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission required: tenant.view_audit")
    if created_from is not None and created_to is not None and created_from >= created_to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="created_from must be before created_to")
    if actor_user_id is not None and system_actor:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="actor_user_id and system_actor cannot be combined",
        )
    if action is not None and action_prefix is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="action and action_prefix cannot be combined",
        )

    statement = (
        select(AuditRecord, User)
        .outerjoin(User, User.id == AuditRecord.actor_user_id)
        .where(AuditRecord.tenant_id == tenant.id)
    )
    if created_from is not None:
        statement = statement.where(AuditRecord.created_at >= created_from)
    if created_to is not None:
        statement = statement.where(AuditRecord.created_at < created_to)
    if actor_user_id is not None:
        statement = statement.where(AuditRecord.actor_user_id == actor_user_id)
    elif system_actor:
        statement = statement.where(AuditRecord.actor_user_id.is_(None))
    if action is not None:
        statement = statement.where(AuditRecord.action == action)
    elif action_prefix is not None:
        statement = statement.where(AuditRecord.action.like(f"{_escape_like(action_prefix)}%", escape="\\"))
    if target_type is not None:
        statement = statement.where(AuditRecord.target_type == target_type)
    if target_id is not None:
        statement = statement.where(AuditRecord.target_id == target_id)
    if cursor is not None:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        statement = statement.where(
            or_(
                AuditRecord.created_at < cursor_created_at,
                and_(AuditRecord.created_at == cursor_created_at, AuditRecord.id < cursor_id),
            )
        )

    rows = list(
        db.execute(
            statement.order_by(AuditRecord.created_at.desc(), AuditRecord.id.desc()).limit(limit + 1)
        ).all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        AuditRecordRead(
            id=record.id,
            tenant_id=record.tenant_id,
            actor_user_id=record.actor_user_id,
            actor_display_name=actor.display_name if actor is not None else None,
            actor_email=actor.email if actor is not None else None,
            action=record.action,
            target_type=record.target_type,
            target_id=record.target_id,
            details=record.details,
            created_at=record.created_at,
        )
        for record, actor in rows
    ]
    return AuditRecordPage(
        items=items,
        next_cursor=_encode_cursor(rows[-1][0]) if has_more and rows else None,
    )
