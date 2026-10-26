from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.authorization import ResolvedResource, ResourceRef, resolve_resource, user_can
from app.models import (
    AccessGroup,
    AccessGroupMember,
    AuthorizationGrant,
    Notification,
    NotificationAudience,
    NotificationRecipient,
    NotificationRecipientAudience,
    Role,
    User,
    utcnow,
)
from app.rbac_manifest import ALL_PERMISSION_KEYS

MAX_NOTIFICATION_RECIPIENTS = 500
MAX_TEMPLATE_DATA_BYTES = 8_192
MAX_ROUTE_PARAMS_BYTES = 4_096


class NotificationError(ValueError):
    pass


@dataclass(frozen=True)
class UserAudience:
    user_id: uuid.UUID


@dataclass(frozen=True)
class AccessGroupAudience:
    access_group_id: uuid.UUID


@dataclass(frozen=True)
class RoleAudience:
    role_id: uuid.UUID


@dataclass(frozen=True)
class PermissionAudience:
    permission_key: str


Audience = UserAudience | AccessGroupAudience | RoleAudience | PermissionAudience


@dataclass(frozen=True)
class NotificationSource:
    source_type: str
    source_id: uuid.UUID


@dataclass(frozen=True)
class NotificationTypeSpec:
    key: str
    category: str
    severity: Literal["INFO", "SUCCESS", "WARNING", "ERROR"]
    kind: Literal["INFORMATIONAL", "ACTION_REQUIRED"]
    allowed_scope_types: frozenset[str]
    source_types: frozenset[str]
    required_template_fields: frozenset[str]
    allowed_audience_kinds: frozenset[str]
    route_name: str
    required_route_params: frozenset[str]
    title_template: str
    summary_template: str
    allow_empty: bool = False


NOTIFICATION_TYPES = {
    spec.key: spec
    for spec in (
        NotificationTypeSpec(
            key="job.completed",
            category="PROCESSING",
            severity="SUCCESS",
            kind="INFORMATIONAL",
            allowed_scope_types=frozenset({"MATTER"}),
            source_types=frozenset({"workflow_run"}),
            required_template_fields=frozenset({"job_label"}),
            allowed_audience_kinds=frozenset({"USER", "ACCESS_GROUP", "ROLE", "PERMISSION"}),
            route_name="MATTER_JOBS",
            required_route_params=frozenset({"client_id", "matter_id"}),
            title_template="{job_label} completed",
            summary_template="The job finished and its results are ready to review.",
            allow_empty=True,
        ),
        NotificationTypeSpec(
            key="job.failed",
            category="PROCESSING",
            severity="ERROR",
            kind="INFORMATIONAL",
            allowed_scope_types=frozenset({"MATTER"}),
            source_types=frozenset({"workflow_run"}),
            required_template_fields=frozenset({"job_label"}),
            allowed_audience_kinds=frozenset({"USER", "ACCESS_GROUP", "ROLE", "PERMISSION"}),
            route_name="MATTER_JOBS",
            required_route_params=frozenset({"client_id", "matter_id"}),
            title_template="{job_label} failed",
            summary_template="The job could not be completed. Open the matter jobs area for details and retry options.",
            allow_empty=True,
        ),
        NotificationTypeSpec(
            key="agent_action.requested",
            category="AGENT",
            severity="WARNING",
            kind="ACTION_REQUIRED",
            allowed_scope_types=frozenset({"MATTER"}),
            source_types=frozenset({"agent_action_request"}),
            required_template_fields=frozenset(),
            allowed_audience_kinds=frozenset({"USER", "ACCESS_GROUP", "ROLE", "PERMISSION"}),
            route_name="AGENT_CONVERSATION",
            required_route_params=frozenset(
                {"client_id", "matter_id", "conversation_id", "workflow_type", "review_batch_id"}
            ),
            title_template="Agent approval required",
            summary_template="An agent is waiting for approval before it can continue.",
        ),
        NotificationTypeSpec(
            key="review_program.action_required",
            category="REVIEW_PROGRAM",
            severity="WARNING",
            kind="ACTION_REQUIRED",
            allowed_scope_types=frozenset({"MATTER"}),
            source_types=frozenset({"review_program_action"}),
            required_template_fields=frozenset({"program_name", "action_title"}),
            allowed_audience_kinds=frozenset({"PERMISSION"}),
            route_name="REVIEW_PROGRAM",
            required_route_params=frozenset(
                {"client_id", "matter_id", "program_id", "run_id", "action_id"}
            ),
            title_template="{program_name}: {action_title}",
            summary_template="This Review Program is waiting for an authorized decision.",
        ),
        NotificationTypeSpec(
            key="review_program.attention_required",
            category="REVIEW_PROGRAM",
            severity="ERROR",
            kind="INFORMATIONAL",
            allowed_scope_types=frozenset({"MATTER"}),
            source_types=frozenset({"review_program_run"}),
            required_template_fields=frozenset({"program_name", "stage_name"}),
            allowed_audience_kinds=frozenset({"USER"}),
            route_name="REVIEW_PROGRAM",
            required_route_params=frozenset({"client_id", "matter_id", "program_id", "run_id"}),
            title_template="{program_name} needs attention",
            summary_template="The Review Program is blocked at {stage_name}.",
            allow_empty=True,
        ),
        NotificationTypeSpec(
            key="review_program.completed",
            category="REVIEW_PROGRAM",
            severity="SUCCESS",
            kind="INFORMATIONAL",
            allowed_scope_types=frozenset({"MATTER"}),
            source_types=frozenset({"review_program_run"}),
            required_template_fields=frozenset({"program_name"}),
            allowed_audience_kinds=frozenset({"USER"}),
            route_name="REVIEW_PROGRAM",
            required_route_params=frozenset({"client_id", "matter_id", "program_id", "run_id"}),
            title_template="{program_name} completed",
            summary_template="The Review Program completed and its results are ready to review.",
            allow_empty=True,
        ),
        NotificationTypeSpec(
            key="review_program.stopped",
            category="REVIEW_PROGRAM",
            severity="INFO",
            kind="INFORMATIONAL",
            allowed_scope_types=frozenset({"MATTER"}),
            source_types=frozenset({"review_program_run"}),
            required_template_fields=frozenset({"program_name"}),
            allowed_audience_kinds=frozenset({"USER"}),
            route_name="REVIEW_PROGRAM",
            required_route_params=frozenset({"client_id", "matter_id", "program_id", "run_id"}),
            title_template="{program_name} stopped",
            summary_template="The Review Program was stopped. Its history and completed work remain available.",
            allow_empty=True,
        ),
    )
}


def _audience_kind(audience: Audience) -> str:
    if isinstance(audience, UserAudience):
        return "USER"
    if isinstance(audience, AccessGroupAudience):
        return "ACCESS_GROUP"
    if isinstance(audience, RoleAudience):
        return "ROLE"
    return "PERMISSION"


def _scope_values(resource: ResolvedResource) -> dict[str, uuid.UUID | None]:
    return {
        "tenant_scope_id": resource.resource_id if resource.resource_type == "TENANT" else None,
        "client_id": (
            resource.resource_id
            if resource.resource_type == "CLIENT"
            else resource.client_id
            if resource.resource_type == "COLLECTION"
            else None
        ),
        "matter_id": resource.resource_id if resource.resource_type == "MATTER" else None,
        "collection_id": resource.resource_id if resource.resource_type == "COLLECTION" else None,
    }


def _grant_scope_filter(resource: ResolvedResource):
    column = {
        "TENANT": AuthorizationGrant.tenant_scope_id,
        "CLIENT": AuthorizationGrant.client_id,
        "MATTER": AuthorizationGrant.matter_id,
        "COLLECTION": AuthorizationGrant.collection_id,
    }[resource.resource_type]
    return column == resource.resource_id


def _basic_view_permission(resource: ResolvedResource) -> str:
    return {
        "TENANT": "tenant.view",
        "CLIENT": "client.view",
        "COLLECTION": "collection.view",
        "MATTER": "matter.view",
    }[resource.resource_type]


def _eligible_users(
    db: Session,
    *,
    user_ids: set[uuid.UUID],
    resource_ref: ResourceRef,
    resource: ResolvedResource,
) -> set[uuid.UUID]:
    if not user_ids:
        return set()
    users = list(
        db.scalars(
            select(User).where(
                User.id.in_(user_ids),
                User.tenant_id == resource.tenant_id,
                User.status == "ACTIVE",
            )
        )
    )
    view_permission = _basic_view_permission(resource)
    return {user.id for user in users if user_can(db, user, view_permission, resource_ref)}


def _resolve_audience(
    db: Session,
    audience: Audience,
    *,
    resource_ref: ResourceRef,
    resource: ResolvedResource,
) -> set[uuid.UUID]:
    if isinstance(audience, UserAudience):
        return _eligible_users(
            db,
            user_ids={audience.user_id},
            resource_ref=resource_ref,
            resource=resource,
        )

    if isinstance(audience, AccessGroupAudience):
        group = db.get(AccessGroup, audience.access_group_id)
        if group is None or group.tenant_id != resource.tenant_id or group.status != "ACTIVE":
            raise NotificationError("Notification access group is unavailable")
        user_ids = set(
            db.scalars(select(AccessGroupMember.user_id).where(AccessGroupMember.access_group_id == group.id))
        )
        return _eligible_users(
            db,
            user_ids=user_ids,
            resource_ref=resource_ref,
            resource=resource,
        )

    if isinstance(audience, RoleAudience):
        role = db.get(Role, audience.role_id)
        if (
            role is None
            or role.status != "ACTIVE"
            or (role.tenant_id is not None and role.tenant_id != resource.tenant_id)
        ):
            raise NotificationError("Notification role is unavailable")
        grants = list(
            db.scalars(
                select(AuthorizationGrant).where(
                    AuthorizationGrant.tenant_id == resource.tenant_id,
                    AuthorizationGrant.role_id == role.id,
                    AuthorizationGrant.scope_type == resource.resource_type,
                    _grant_scope_filter(resource),
                )
            )
        )
        user_ids = {grant.user_id for grant in grants if grant.user_id is not None}
        group_ids = {grant.access_group_id for grant in grants if grant.access_group_id is not None}
        if group_ids:
            active_group_ids = set(
                db.scalars(
                    select(AccessGroup.id).where(
                        AccessGroup.id.in_(group_ids),
                        AccessGroup.tenant_id == resource.tenant_id,
                        AccessGroup.status == "ACTIVE",
                    )
                )
            )
            if active_group_ids:
                user_ids.update(
                    db.scalars(
                        select(AccessGroupMember.user_id).where(AccessGroupMember.access_group_id.in_(active_group_ids))
                    )
                )
        return _eligible_users(
            db,
            user_ids=user_ids,
            resource_ref=resource_ref,
            resource=resource,
        )

    if audience.permission_key not in ALL_PERMISSION_KEYS:
        raise NotificationError("Notification permission is unavailable")
    candidates = list(
        db.scalars(
            select(User)
            .where(User.tenant_id == resource.tenant_id, User.status == "ACTIVE")
            .order_by(User.id)
            .limit(MAX_NOTIFICATION_RECIPIENTS + 1)
        )
    )
    if len(candidates) > MAX_NOTIFICATION_RECIPIENTS:
        raise NotificationError("Notification permission audience exceeds the synchronous fan-out limit")
    return {user.id for user in candidates if user_can(db, user, audience.permission_key, resource_ref)}


def _normalized_json(value: dict[str, Any], *, limit: int, label: str) -> dict[str, Any]:
    def normalize(item: Any) -> Any:
        if isinstance(item, uuid.UUID):
            return str(item)
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, dict):
            return {str(key): normalize(inner) for key, inner in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(inner) for inner in item]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        raise NotificationError(f"Unsupported {label} value")

    normalized = normalize(value)
    encoded = json.dumps(normalized, separators=(",", ":")).encode("utf-8")
    if len(encoded) > limit:
        raise NotificationError(f"{label} exceeds the configured size limit")
    return normalized


def _safe_text(value: Any, *, field: str, max_length: int = 120) -> str:
    if not isinstance(value, str):
        raise NotificationError(f"Notification template field {field} must be text")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > max_length:
        raise NotificationError(f"Notification template field {field} is invalid")
    return normalized


def publish_notification(
    db: Session,
    *,
    type_key: str,
    scope: ResourceRef,
    source: NotificationSource,
    audiences: tuple[Audience, ...],
    template_data: dict[str, Any],
    route_params: dict[str, Any],
    idempotency_key: str,
    created_by_kind: Literal["USER", "SERVICE", "WORKFLOW", "SYSTEM"] = "SYSTEM",
    created_by_user_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> Notification:
    spec = NOTIFICATION_TYPES.get(type_key)
    if spec is None:
        raise NotificationError("Unknown notification type")
    if source.source_type not in spec.source_types:
        raise NotificationError("Notification source type is not allowed")
    if not idempotency_key or len(idempotency_key) > 500:
        raise NotificationError("Notification idempotency key is invalid")

    resource = resolve_resource(db, scope)
    if resource is None:
        raise NotificationError("Notification scope was not found")
    if resource.resource_type not in spec.allowed_scope_types:
        raise NotificationError("Notification scope type is not allowed")
    existing = db.scalar(
        select(Notification).where(
            Notification.tenant_id == resource.tenant_id,
            Notification.type_key == type_key,
            Notification.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing

    if set(template_data) != set(spec.required_template_fields):
        raise NotificationError("Notification template fields do not match the registered type")
    if set(route_params) != set(spec.required_route_params):
        raise NotificationError("Notification route parameters do not match the registered type")
    if not audiences:
        raise NotificationError("Notification requires at least one audience selector")
    for audience in audiences:
        if _audience_kind(audience) not in spec.allowed_audience_kinds:
            raise NotificationError("Notification audience type is not allowed")

    normalized_template_data = _normalized_json(
        template_data,
        limit=MAX_TEMPLATE_DATA_BYTES,
        label="notification template data",
    )
    normalized_route_params = _normalized_json(
        route_params,
        limit=MAX_ROUTE_PARAMS_BYTES,
        label="notification route parameters",
    )
    if resource.resource_type == "MATTER":
        expected_route_scope = {
            "client_id": str(resource.client_id),
            "matter_id": str(resource.resource_id),
        }
        if any(normalized_route_params.get(key) != value for key, value in expected_route_scope.items()):
            raise NotificationError("Notification route does not match its resource scope")
    formatting = {
        field: _safe_text(normalized_template_data[field], field=field) for field in spec.required_template_fields
    }
    title = spec.title_template.format(**formatting)
    summary = spec.summary_template.format(**formatting)
    if len(title) > 200 or len(summary) > 1000:
        raise NotificationError("Rendered notification text exceeds its size limit")

    resolved_by_index: list[set[uuid.UUID]] = []
    all_user_ids: set[uuid.UUID] = set()
    for audience in audiences:
        user_ids = _resolve_audience(
            db,
            audience,
            resource_ref=scope,
            resource=resource,
        )
        resolved_by_index.append(user_ids)
        all_user_ids.update(user_ids)
        if len(all_user_ids) > MAX_NOTIFICATION_RECIPIENTS:
            raise NotificationError("Notification audience exceeds the synchronous fan-out limit")
    if not all_user_ids and not spec.allow_empty:
        raise NotificationError("Action-required notification has no eligible recipient")

    notification = Notification(
        tenant_id=resource.tenant_id,
        scope_type=resource.resource_type,
        **_scope_values(resource),
        type_key=spec.key,
        schema_version=1,
        category=spec.category,
        severity=spec.severity,
        kind=spec.kind,
        title=title,
        summary=summary,
        template_data=normalized_template_data,
        source_type=source.source_type,
        source_id=source.source_id,
        primary_route_name=spec.route_name,
        primary_route_params=normalized_route_params,
        created_by_kind=created_by_kind,
        created_by_user_id=created_by_user_id,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at or utcnow(),
    )
    try:
        with db.begin_nested():
            db.add(notification)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(Notification).where(
                Notification.tenant_id == resource.tenant_id,
                Notification.type_key == type_key,
                Notification.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            raise
        return existing

    audience_rows: list[NotificationAudience] = []
    for audience in audiences:
        row = NotificationAudience(
            notification_id=notification.id,
            audience_kind=_audience_kind(audience),
            user_id=audience.user_id if isinstance(audience, UserAudience) else None,
            access_group_id=(audience.access_group_id if isinstance(audience, AccessGroupAudience) else None),
            role_id=audience.role_id if isinstance(audience, RoleAudience) else None,
            permission_key=(audience.permission_key if isinstance(audience, PermissionAudience) else None),
            scope_type=resource.resource_type,
            scope_id=resource.resource_id,
        )
        db.add(row)
        audience_rows.append(row)
    db.flush()

    recipient_by_user: dict[uuid.UUID, NotificationRecipient] = {}
    for user_id in sorted(all_user_ids, key=str):
        recipient = NotificationRecipient(notification_id=notification.id, user_id=user_id)
        db.add(recipient)
        recipient_by_user[user_id] = recipient
    db.flush()
    for index, user_ids in enumerate(resolved_by_index):
        for user_id in user_ids:
            db.add(
                NotificationRecipientAudience(
                    recipient_id=recipient_by_user[user_id].id,
                    audience_id=audience_rows[index].id,
                )
            )
    db.flush()
    return notification


def resolve_source_notifications(
    db: Session,
    *,
    source: NotificationSource,
    resolution_code: str,
    resolved_at: datetime | None = None,
) -> list[Notification]:
    if not resolution_code or len(resolution_code) > 50:
        raise NotificationError("Notification resolution code is invalid")
    notifications = list(
        db.scalars(
            select(Notification).where(
                Notification.source_type == source.source_type,
                Notification.source_id == source.source_id,
                Notification.resolved_at.is_(None),
            )
        )
    )
    when = resolved_at or utcnow()
    for notification in notifications:
        notification.resolved_at = when
        notification.resolution_code = resolution_code
    return notifications
