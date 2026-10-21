import base64
import binascii
import json
import uuid
from datetime import datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.authorization import ResourceRef, user_can
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.models import Client, Matter, Notification, NotificationRecipient, utcnow
from app.schemas import (
    NotificationBulkRead,
    NotificationCounts,
    NotificationMutationResult,
    NotificationPage,
    NotificationRead,
)

router = APIRouter(prefix="/v1/notifications", tags=["notifications"])
NotificationState = Literal["ALL", "UNREAD", "ACTION_REQUIRED"]


def _encode_cursor(notification: Notification) -> str:
    payload = json.dumps(
        {"created_at": notification.created_at.isoformat(), "id": str(notification.id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        return datetime.fromisoformat(payload["created_at"]), uuid.UUID(payload["id"])
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid notification cursor") from exc


def _resource_ref(notification: Notification) -> ResourceRef:
    if notification.scope_type == "TENANT":
        return ResourceRef.tenant(notification.tenant_scope_id or notification.tenant_id)
    if notification.scope_type == "CLIENT" and notification.client_id is not None:
        return ResourceRef.client(notification.client_id)
    if notification.scope_type == "MATTER" and notification.matter_id is not None:
        return ResourceRef.matter(notification.matter_id)
    if (
        notification.scope_type == "COLLECTION"
        and notification.collection_id is not None
        and notification.client_id is not None
    ):
        return ResourceRef.collection(
            notification.collection_id,
            tenant_id=notification.tenant_id,
            client_id=notification.client_id,
        )
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification scope is unavailable")


def _can_view(db: Session, principal: Principal, notification: Notification) -> bool:
    permission = {
        "TENANT": "tenant.view",
        "CLIENT": "client.view",
        "COLLECTION": "collection.view",
        "MATTER": "matter.view",
    }.get(notification.scope_type)
    return bool(permission and user_can(db, principal.user, permission, _resource_ref(notification)))


def _read_model(db: Session, notification: Notification, recipient: NotificationRecipient) -> NotificationRead:
    matter = db.get(Matter, notification.matter_id) if notification.matter_id is not None else None
    client_id = notification.client_id or (matter.client_id if matter is not None else None)
    client = db.get(Client, client_id) if client_id is not None else None
    return NotificationRead(
        id=notification.id,
        tenant_id=notification.tenant_id,
        scope_type=cast(Literal["TENANT", "CLIENT", "COLLECTION", "MATTER"], notification.scope_type),
        client_id=client_id,
        client_name=client.name if client is not None else None,
        matter_id=notification.matter_id,
        matter_name=matter.name if matter is not None else None,
        type_key=notification.type_key,
        category=notification.category,
        severity=cast(Literal["INFO", "SUCCESS", "WARNING", "ERROR"], notification.severity),
        kind=cast(Literal["INFORMATIONAL", "ACTION_REQUIRED"], notification.kind),
        title=notification.title,
        summary=notification.summary,
        source_type=notification.source_type,
        source_id=notification.source_id,
        primary_route_name=notification.primary_route_name,
        primary_route_params=notification.primary_route_params,
        occurred_at=notification.occurred_at,
        created_at=notification.created_at,
        resolved_at=notification.resolved_at,
        resolution_code=notification.resolution_code,
        seen_at=recipient.seen_at,
        read_at=recipient.read_at,
        dismissed_at=recipient.dismissed_at,
    )


def _base_statement(principal: Principal):
    return (
        select(Notification, NotificationRecipient)
        .join(NotificationRecipient, NotificationRecipient.notification_id == Notification.id)
        .where(NotificationRecipient.user_id == principal.user.id)
    )


@router.get("", response_model=NotificationPage)
def list_notifications(
    state: NotificationState = Query(default="ALL"),
    matter_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    category: Annotated[str | None, Query(min_length=1, max_length=50)] = None,
    cursor: Annotated[str | None, Query(min_length=1, max_length=1000)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> NotificationPage:
    statement = _base_statement(principal).where(
        or_(Notification.expires_at.is_(None), Notification.expires_at > utcnow()),
    )
    if state == "UNREAD":
        statement = statement.where(
            NotificationRecipient.read_at.is_(None),
            NotificationRecipient.dismissed_at.is_(None),
        )
    elif state == "ACTION_REQUIRED":
        statement = statement.where(
            Notification.kind == "ACTION_REQUIRED",
            Notification.resolved_at.is_(None),
        )
    else:
        statement = statement.where(
            or_(
                NotificationRecipient.dismissed_at.is_(None),
                and_(Notification.kind == "ACTION_REQUIRED", Notification.resolved_at.is_(None)),
            )
        )
    if matter_id is not None:
        statement = statement.where(Notification.matter_id == matter_id)
    if client_id is not None:
        statement = statement.where(
            or_(
                Notification.client_id == client_id,
                Notification.matter_id.in_(select(Matter.id).where(Matter.client_id == client_id)),
            )
        )
    if category is not None:
        statement = statement.where(Notification.category == category.upper())
    if cursor is not None:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        statement = statement.where(
            or_(
                Notification.created_at < cursor_created_at,
                and_(Notification.created_at == cursor_created_at, Notification.id < cursor_id),
            )
        )

    visible: list[tuple[Notification, NotificationRecipient]] = []
    scan_statement = statement
    batch_size = min(max(limit * 2, 50), 200)
    while len(visible) < limit + 1:
        rows = list(
            db.execute(
                scan_statement.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(batch_size)
            ).all()
        )
        if not rows:
            break
        visible.extend(
            (notification, recipient) for notification, recipient in rows if _can_view(db, principal, notification)
        )
        if len(rows) < batch_size:
            break
        last_notification = rows[-1][0]
        scan_statement = statement.where(
            or_(
                Notification.created_at < last_notification.created_at,
                and_(
                    Notification.created_at == last_notification.created_at,
                    Notification.id < last_notification.id,
                ),
            )
        )

    has_more = len(visible) > limit
    page_rows = visible[:limit]
    return NotificationPage(
        items=[_read_model(db, notification, recipient) for notification, recipient in page_rows],
        next_cursor=_encode_cursor(page_rows[-1][0]) if has_more and page_rows else None,
    )


@router.get("/counts", response_model=NotificationCounts)
def notification_counts(
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> NotificationCounts:
    rows = list(
        db.execute(
            _base_statement(principal).where(
                or_(Notification.expires_at.is_(None), Notification.expires_at > utcnow()),
                or_(
                    NotificationRecipient.dismissed_at.is_(None),
                    and_(Notification.kind == "ACTION_REQUIRED", Notification.resolved_at.is_(None)),
                ),
            )
        ).all()
    )
    visible = [(notification, recipient) for notification, recipient in rows if _can_view(db, principal, notification)]
    return NotificationCounts(
        unread=sum(1 for _, recipient in visible if recipient.read_at is None),
        action_required=sum(
            1
            for notification, _ in visible
            if notification.kind == "ACTION_REQUIRED" and notification.resolved_at is None
        ),
    )


def _recipient_for_update(
    db: Session,
    *,
    notification_id: uuid.UUID,
    principal: Principal,
) -> tuple[Notification, NotificationRecipient]:
    row = db.execute(
        _base_statement(principal).where(Notification.id == notification_id).with_for_update()
    ).one_or_none()
    if row is None or not _can_view(db, principal, row[0]):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    return row[0], row[1]


@router.post("/{notification_id}/read", response_model=NotificationMutationResult)
def mark_notification_read(
    notification_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> NotificationMutationResult:
    _, recipient = _recipient_for_update(db, notification_id=notification_id, principal=principal)
    now = utcnow()
    updated = recipient.read_at is None or recipient.seen_at is None
    recipient.seen_at = recipient.seen_at or now
    recipient.read_at = recipient.read_at or now
    db.commit()
    return NotificationMutationResult(updated=1 if updated else 0)


@router.post("/{notification_id}/dismiss", response_model=NotificationMutationResult)
def dismiss_notification(
    notification_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> NotificationMutationResult:
    _, recipient = _recipient_for_update(db, notification_id=notification_id, principal=principal)
    updated = recipient.dismissed_at is None
    recipient.dismissed_at = recipient.dismissed_at or utcnow()
    db.commit()
    return NotificationMutationResult(updated=1 if updated else 0)


@router.post("/mark-read", response_model=NotificationMutationResult)
def mark_notifications_read(
    payload: NotificationBulkRead,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> NotificationMutationResult:
    rows = list(
        db.execute(
            _base_statement(principal).where(Notification.id.in_(set(payload.notification_ids))).with_for_update()
        ).all()
    )
    now = utcnow()
    updated = 0
    for notification, recipient in rows:
        if not _can_view(db, principal, notification):
            continue
        if recipient.read_at is None or recipient.seen_at is None:
            updated += 1
        recipient.seen_at = recipient.seen_at or now
        recipient.read_at = recipient.read_at or now
    db.commit()
    return NotificationMutationResult(updated=updated)
