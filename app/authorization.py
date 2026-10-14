import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AccessGroup,
    AccessGroupMember,
    AuthorizationGrant,
    Client,
    ClientMembership,
    Matter,
    MatterMembership,
    Permission,
    Role,
    RolePermission,
    Tenant,
    User,
)
from app.rbac_manifest import ALL_PERMISSION_KEYS

if TYPE_CHECKING:
    from app.dependencies import Principal

ResourceType = Literal["TENANT", "CLIENT", "COLLECTION", "MATTER"]


@dataclass(frozen=True)
class ResourceRef:
    resource_type: ResourceType
    resource_id: uuid.UUID
    tenant_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None

    @classmethod
    def tenant(cls, tenant_id: uuid.UUID) -> "ResourceRef":
        return cls("TENANT", tenant_id, tenant_id=tenant_id)

    @classmethod
    def client(cls, client_id: uuid.UUID) -> "ResourceRef":
        return cls("CLIENT", client_id)

    @classmethod
    def matter(cls, matter_id: uuid.UUID) -> "ResourceRef":
        return cls("MATTER", matter_id)

    @classmethod
    def collection(
        cls,
        collection_id: uuid.UUID,
        *,
        tenant_id: uuid.UUID,
        client_id: uuid.UUID,
    ) -> "ResourceRef":
        return cls("COLLECTION", collection_id, tenant_id=tenant_id, client_id=client_id)


@dataclass(frozen=True)
class ResolvedResource:
    resource_type: ResourceType
    resource_id: uuid.UUID
    tenant_id: uuid.UUID
    client_id: uuid.UUID | None = None


@dataclass(frozen=True)
class AuthorizationExplanation:
    allowed: bool
    permission_key: str
    resource: ResolvedResource
    grant_id: uuid.UUID | None = None
    role_id: uuid.UUID | None = None
    role_key: str | None = None
    subject_type: str | None = None
    subject_id: uuid.UUID | None = None
    scope_type: str | None = None
    scope_id: uuid.UUID | None = None


def resolve_resource(db: Session, resource: ResourceRef) -> ResolvedResource | None:
    if resource.resource_type == "TENANT":
        tenant = db.get(Tenant, resource.resource_id)
        return (
            ResolvedResource("TENANT", tenant.id, tenant.id)
            if tenant is not None
            else None
        )
    if resource.resource_type == "CLIENT":
        client = db.get(Client, resource.resource_id)
        return (
            ResolvedResource("CLIENT", client.id, client.tenant_id, client.id)
            if client is not None
            else None
        )
    if resource.resource_type == "MATTER":
        row = db.execute(
            select(Matter.id, Client.tenant_id, Client.id)
            .join(Client, Client.id == Matter.client_id)
            .where(Matter.id == resource.resource_id)
        ).one_or_none()
        return ResolvedResource("MATTER", row[0], row[1], row[2]) if row is not None else None
    if resource.tenant_id is None or resource.client_id is None:
        raise ValueError("Collection resources require resolved tenant and client IDs")
    return ResolvedResource(
        "COLLECTION",
        resource.resource_id,
        resource.tenant_id,
        resource.client_id,
    )


def _scope_filter(resource: ResolvedResource):
    scopes = [
        and_(
            AuthorizationGrant.scope_type == "TENANT",
            AuthorizationGrant.tenant_scope_id == resource.tenant_id,
        )
    ]
    if resource.client_id is not None:
        scopes.append(
            and_(
                AuthorizationGrant.scope_type == "CLIENT",
                AuthorizationGrant.client_id == resource.client_id,
            )
        )
    if resource.resource_type == "MATTER":
        scopes.append(
            and_(
                AuthorizationGrant.scope_type == "MATTER",
                AuthorizationGrant.matter_id == resource.resource_id,
            )
        )
    elif resource.resource_type == "COLLECTION":
        scopes.append(
            and_(
                AuthorizationGrant.scope_type == "COLLECTION",
                AuthorizationGrant.collection_id == resource.resource_id,
            )
        )
    return or_(*scopes)


def _active_group_ids(db: Session, user: User) -> list[uuid.UUID]:
    return list(
        db.scalars(
            select(AccessGroup.id)
            .join(AccessGroupMember, AccessGroupMember.access_group_id == AccessGroup.id)
            .where(
                AccessGroupMember.user_id == user.id,
                AccessGroup.tenant_id == user.tenant_id,
                AccessGroup.status == "ACTIVE",
            )
        )
    )


def _subject_filter(user_id: uuid.UUID, group_ids: list[uuid.UUID]):
    if group_ids:
        return or_(
            AuthorizationGrant.user_id == user_id,
            AuthorizationGrant.access_group_id.in_(group_ids),
        )
    return AuthorizationGrant.user_id == user_id


def _has_legacy_admin_access(db: Session, user: User, resource: ResolvedResource) -> bool:
    """Compatibility bridge while legacy ADMIN memberships are retired."""

    if resource.client_id is not None and db.scalar(
        select(ClientMembership.user_id).where(
            ClientMembership.client_id == resource.client_id,
            ClientMembership.user_id == user.id,
            ClientMembership.role == "ADMIN",
        )
    ) is not None:
        return True
    return resource.resource_type == "MATTER" and db.scalar(
        select(MatterMembership.user_id).where(
            MatterMembership.matter_id == resource.resource_id,
            MatterMembership.user_id == user.id,
            MatterMembership.role == "ADMIN",
        )
    ) is not None


def _base_grant_statement(
    db: Session,
    principal: "Principal",
    resource: ResolvedResource,
):
    return _base_grant_statement_for_user(db, principal.user, resource)


def _base_grant_statement_for_user(
    db: Session,
    user: User,
    resource: ResolvedResource,
):
    group_ids = _active_group_ids(db, user)
    return (
        select(AuthorizationGrant, Role, Permission)
        .join(Role, Role.id == AuthorizationGrant.role_id)
        .join(RolePermission, RolePermission.role_id == Role.id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(
            AuthorizationGrant.tenant_id == resource.tenant_id,
            _subject_filter(user.id, group_ids),
            _scope_filter(resource),
            Role.status == "ACTIVE",
            Permission.status == "ACTIVE",
        )
    )


def user_can(
    db: Session,
    user: User,
    permission_key: str,
    resource_ref: ResourceRef,
) -> bool:
    """Evaluate an active user's access without requiring their live session.

    This is used for access-aware recipient/assignee selection.  It must not
    be used to authenticate a request.
    """

    if permission_key not in ALL_PERMISSION_KEYS or user.status != "ACTIVE":
        return False
    resource = resolve_resource(db, resource_ref)
    if resource is None or user.tenant_id != resource.tenant_id:
        return False
    if user.tenant.is_root and user.tenant_role == "ADMIN":
        return True
    if user.tenant_role == "ADMIN":
        return True
    if _has_legacy_admin_access(db, user, resource):
        return True
    statement = (
        _base_grant_statement_for_user(db, user, resource)
        .where(Permission.key == permission_key)
        .limit(1)
    )
    return db.execute(statement).first() is not None


def can(
    db: Session,
    principal: "Principal",
    permission_key: str,
    resource_ref: ResourceRef,
) -> bool:
    if permission_key not in ALL_PERMISSION_KEYS or principal.user.status != "ACTIVE":
        return False
    resource = resolve_resource(db, resource_ref)
    if resource is None:
        return False
    if principal.is_root_admin:
        return True
    if principal.user.tenant_id != resource.tenant_id:
        return False
    if principal.user.tenant_role == "ADMIN":
        return True
    if _has_legacy_admin_access(db, principal.user, resource):
        return True
    statement = _base_grant_statement(db, principal, resource).where(Permission.key == permission_key).limit(1)
    return db.execute(statement).first() is not None


def require(
    db: Session,
    principal: "Principal",
    permission_key: str,
    resource_ref: ResourceRef,
) -> None:
    if not can(db, principal, permission_key, resource_ref):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission required: {permission_key}",
        )


def effective_permissions(
    db: Session,
    principal: "Principal",
    resource_ref: ResourceRef,
) -> tuple[ResolvedResource | None, list[str]]:
    resource = resolve_resource(db, resource_ref)
    if resource is None:
        return None, []
    if principal.is_root_admin:
        return resource, sorted(ALL_PERMISSION_KEYS)
    if principal.user.tenant_id != resource.tenant_id or principal.user.status != "ACTIVE":
        return resource, []
    if principal.user.tenant_role == "ADMIN":
        return resource, sorted(ALL_PERMISSION_KEYS)
    if _has_legacy_admin_access(db, principal.user, resource):
        return resource, sorted(ALL_PERMISSION_KEYS)
    statement = _base_grant_statement(db, principal, resource).with_only_columns(Permission.key).distinct()
    return resource, sorted(db.scalars(statement))


def explain(
    db: Session,
    principal: "Principal",
    permission_key: str,
    resource_ref: ResourceRef,
) -> AuthorizationExplanation | None:
    resource = resolve_resource(db, resource_ref)
    if resource is None:
        return None
    if permission_key not in ALL_PERMISSION_KEYS or principal.user.status != "ACTIVE":
        return AuthorizationExplanation(False, permission_key, resource)
    if principal.is_root_admin:
        return AuthorizationExplanation(
            allowed=True,
            permission_key=permission_key,
            resource=resource,
            subject_type="ROOT_BREAK_GLASS",
            subject_id=principal.user.id,
        )
    if principal.user.tenant_id != resource.tenant_id:
        return AuthorizationExplanation(False, permission_key, resource)
    if principal.user.tenant_role == "ADMIN":
        return AuthorizationExplanation(
            allowed=True,
            permission_key=permission_key,
            resource=resource,
            subject_type="LEGACY_TENANT_ADMIN",
            subject_id=principal.user.id,
        )
    if _has_legacy_admin_access(db, principal.user, resource):
        return AuthorizationExplanation(
            allowed=True,
            permission_key=permission_key,
            resource=resource,
            subject_type="LEGACY_ADMIN_MEMBERSHIP",
            subject_id=principal.user.id,
        )
    row = db.execute(
        _base_grant_statement(db, principal, resource)
        .where(Permission.key == permission_key)
        .order_by(AuthorizationGrant.created_at, AuthorizationGrant.id)
        .limit(1)
    ).first()
    if row is None:
        return AuthorizationExplanation(False, permission_key, resource)
    grant, role, _ = row
    scope_id = {
        "TENANT": grant.tenant_scope_id,
        "CLIENT": grant.client_id,
        "MATTER": grant.matter_id,
        "COLLECTION": grant.collection_id,
    }[grant.scope_type]
    return AuthorizationExplanation(
        allowed=True,
        permission_key=permission_key,
        resource=resource,
        grant_id=grant.id,
        role_id=role.id,
        role_key=role.key,
        subject_type="USER" if grant.user_id is not None else "ACCESS_GROUP",
        subject_id=grant.user_id or grant.access_group_id,
        scope_type=grant.scope_type,
        scope_id=scope_id,
    )


def increment_authorization_revision(db: Session, tenant_id: uuid.UUID) -> tuple[int, int]:
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    if tenant is None:
        raise ValueError("Tenant not found")
    before = tenant.authorization_revision
    tenant.authorization_revision += 1
    return before, tenant.authorization_revision


def legacy_or_can_manage_tenant(
    db: Session,
    principal: "Principal",
    tenant_id: uuid.UUID,
    permission_key: str,
) -> bool:
    if principal.is_root_admin:
        return True
    if (
        principal.user.tenant_id == tenant_id
        and principal.user.tenant_role == "ADMIN"
    ):
        return True
    return can(db, principal, permission_key, ResourceRef.tenant(tenant_id))


def accessible_client_ids(db: Session, principal: "Principal", permission_key: str) -> list[uuid.UUID]:
    client_ids = list(db.scalars(select(Client.id).where(Client.tenant_id == principal.user.tenant_id)))
    return [client_id for client_id in client_ids if can(db, principal, permission_key, ResourceRef.client(client_id))]


def accessible_matter_ids(db: Session, principal: "Principal", permission_key: str) -> list[uuid.UUID]:
    matter_ids = list(
        db.scalars(
            select(Matter.id)
            .join(Client, Client.id == Matter.client_id)
            .where(Client.tenant_id == principal.user.tenant_id)
        )
    )
    return [matter_id for matter_id in matter_ids if can(db, principal, permission_key, ResourceRef.matter(matter_id))]


def visible_client_ids(
    db: Session,
    principal: "Principal",
    tenant_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Return client shells the caller may navigate to.

    A matter-scoped grant deliberately does not imply ``client.view`` over all
    client content.  It does, however, need to expose the parent client shell
    so the user can reach that matter from the workspace hierarchy.
    """

    clients = list(db.scalars(select(Client).where(Client.tenant_id == tenant_id)))
    result: list[uuid.UUID] = []
    for client in clients:
        if can(db, principal, "client.view", ResourceRef.client(client.id)):
            result.append(client.id)
            continue
        matter_ids = db.scalars(select(Matter.id).where(Matter.client_id == client.id))
        if any(
            can(db, principal, "matter.view", ResourceRef.matter(matter_id))
            for matter_id in matter_ids
        ):
            result.append(client.id)
    return result
