import uuid
from collections.abc import Iterable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.artifact_gateway import CollectionSnapshot, get_collection_snapshot
from app.audit import record_audit
from app.authorization import (
    ResourceRef,
    effective_permissions,
    explain,
    increment_authorization_revision,
    legacy_or_can_manage_tenant,
    resolve_resource,
)
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.models import (
    AccessGroup,
    AccessGroupMember,
    AuthorizationGrant,
    Client,
    Matter,
    Permission,
    Role,
    RolePermission,
    Tenant,
    User,
)
from app.rbac_catalog import ensure_rbac_catalog
from app.schemas import (
    AccessGroupCreate,
    AccessGroupMembersReplace,
    AccessGroupRead,
    AccessGroupUpdate,
    AuthorizationExplanationRead,
    AuthorizationGrantCreate,
    AuthorizationGrantRead,
    AuthorizationGrantUpdate,
    EffectivePermissionsRead,
    PermissionRead,
    RoleCreate,
    RolePermissionsReplace,
    RoleRead,
    RoleUpdate,
)
from artifact_service.auth import ArtifactPrincipal, get_embedded_artifact_principal
from artifact_service.database import get_artifact_db

router = APIRouter(prefix="/v1", tags=["authorization"])


def _conflict(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)


def _tenant(db: Session, tenant_id: uuid.UUID) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return tenant


def _require_tenant_admin(
    db: Session,
    principal: Principal,
    tenant_id: uuid.UUID,
    permission_key: str,
) -> Tenant:
    tenant = _tenant(db, tenant_id)
    if not legacy_or_can_manage_tenant(db, principal, tenant.id, permission_key):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission required: {permission_key}")
    return tenant


def _permission_keys(db: Session, role_id: uuid.UUID) -> list[str]:
    return sorted(
        db.scalars(
            select(Permission.key)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role_id)
        )
    )


def _role_read(db: Session, role: Role, tenant_id: uuid.UUID) -> RoleRead:
    grant_count = db.scalar(
        select(func.count(AuthorizationGrant.id)).where(
            AuthorizationGrant.tenant_id == tenant_id,
            AuthorizationGrant.role_id == role.id,
        )
    )
    return RoleRead(
        id=role.id,
        tenant_id=role.tenant_id,
        key=role.key,
        display_name=role.display_name,
        description=role.description,
        role_kind=role.role_kind,
        status=role.status,
        row_version=role.row_version,
        permission_keys=_permission_keys(db, role.id),
        grant_count=int(grant_count or 0),
        created_at=role.created_at,
        updated_at=role.updated_at,
    )


def _group_read(db: Session, group: AccessGroup) -> AccessGroupRead:
    member_ids = list(
        db.scalars(
            select(AccessGroupMember.user_id)
            .where(AccessGroupMember.access_group_id == group.id)
            .order_by(AccessGroupMember.created_at, AccessGroupMember.user_id)
        )
    )
    grant_count = db.scalar(
        select(func.count(AuthorizationGrant.id)).where(AuthorizationGrant.access_group_id == group.id)
    )
    return AccessGroupRead(
        id=group.id,
        tenant_id=group.tenant_id,
        key=group.key,
        display_name=group.display_name,
        description=group.description,
        group_kind=group.group_kind,
        status=group.status,
        row_version=group.row_version,
        member_ids=member_ids,
        grant_count=int(grant_count or 0),
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


def _role_for_tenant(db: Session, tenant_id: uuid.UUID, role_id: uuid.UUID) -> Role:
    role = db.get(Role, role_id)
    if role is None or (role.tenant_id is not None and role.tenant_id != tenant_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    if role.key == "ROOT_ADMINISTRATOR":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Root Administrator is not assignable")
    return role


def _locked_custom_role(db: Session, tenant_id: uuid.UUID, role_id: uuid.UUID) -> Role:
    role = db.scalar(select(Role).where(Role.id == role_id).with_for_update())
    if role is None or (role.tenant_id is not None and role.tenant_id != tenant_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    if role.role_kind != "CUSTOM":
        raise _conflict("System roles cannot be changed")
    return role


def _validated_permissions(db: Session, permission_keys: Iterable[str]) -> list[Permission]:
    keys = list(permission_keys)
    permissions = list(
        db.scalars(select(Permission).where(Permission.key.in_(keys), Permission.status == "ACTIVE"))
    ) if keys else []
    found = {permission.key for permission in permissions}
    missing = sorted(set(keys) - found)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown or inactive permissions: {', '.join(missing)}",
        )
    return permissions


def _scope_id(grant: AuthorizationGrant) -> uuid.UUID:
    value = {
        "TENANT": grant.tenant_scope_id,
        "CLIENT": grant.client_id,
        "MATTER": grant.matter_id,
        "COLLECTION": grant.collection_id,
    }[grant.scope_type]
    if value is None:
        raise RuntimeError("Authorization grant has an invalid scope")
    return value


def _grant_read(db: Session, grant: AuthorizationGrant) -> AuthorizationGrantRead:
    role = db.get(Role, grant.role_id)
    if role is None:
        raise RuntimeError("Authorization grant role is missing")
    if grant.user_id is not None:
        subject = db.get(User, grant.user_id)
        subject_type = "USER"
        subject_id = grant.user_id
        subject_name = subject.display_name if subject is not None else str(subject_id)
    else:
        group = db.get(AccessGroup, grant.access_group_id)
        subject_type = "ACCESS_GROUP"
        subject_id = grant.access_group_id
        subject_name = group.display_name if group is not None else str(subject_id)
    if subject_id is None:
        raise RuntimeError("Authorization grant subject is missing")
    scope_id = _scope_id(grant)
    if grant.scope_type == "TENANT":
        scope = db.get(Tenant, scope_id)
        scope_name = scope.name if scope is not None else str(scope_id)
    elif grant.scope_type == "CLIENT":
        scope = db.get(Client, scope_id)
        scope_name = scope.name if scope is not None else str(scope_id)
    elif grant.scope_type == "MATTER":
        scope = db.get(Matter, scope_id)
        scope_name = scope.name if scope is not None else str(scope_id)
    else:
        scope_name = f"Collection {scope_id}"
    return AuthorizationGrantRead(
        id=grant.id,
        tenant_id=grant.tenant_id,
        subject_type=subject_type,
        subject_id=subject_id,
        subject_display_name=subject_name,
        role_id=role.id,
        role_key=role.key,
        role_display_name=role.display_name,
        scope_type=grant.scope_type,
        scope_id=scope_id,
        scope_display_name=scope_name,
        row_version=grant.row_version,
        created_by_user_id=grant.created_by_user_id,
        updated_by_user_id=grant.updated_by_user_id,
        created_at=grant.created_at,
        updated_at=grant.updated_at,
    )


def _tenant_administrator_user_ids(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    excluding_grant_id: uuid.UUID | None = None,
    excluding_group_id: uuid.UUID | None = None,
    group_member_override: tuple[uuid.UUID, set[uuid.UUID]] | None = None,
) -> set[uuid.UUID]:
    role = db.scalar(select(Role).where(Role.key == "TENANT_ADMINISTRATOR", Role.tenant_id.is_(None)))
    if role is None:
        return set()
    grant_filters = [
        AuthorizationGrant.tenant_id == tenant_id,
        AuthorizationGrant.role_id == role.id,
        AuthorizationGrant.scope_type == "TENANT",
    ]
    if excluding_grant_id is not None:
        grant_filters.append(AuthorizationGrant.id != excluding_grant_id)
    direct_users = set(
        db.scalars(
            select(User.id)
            .join(AuthorizationGrant, AuthorizationGrant.user_id == User.id)
            .where(*grant_filters, User.status == "ACTIVE")
        )
    )
    group_statement = (
        select(AccessGroup.id, AccessGroupMember.user_id)
        .join(AuthorizationGrant, AuthorizationGrant.access_group_id == AccessGroup.id)
        .join(AccessGroupMember, AccessGroupMember.access_group_id == AccessGroup.id)
        .join(User, User.id == AccessGroupMember.user_id)
        .where(*grant_filters, AccessGroup.status == "ACTIVE", User.status == "ACTIVE")
    )
    if excluding_group_id is not None:
        group_statement = group_statement.where(AccessGroup.id != excluding_group_id)
    group_pairs = set(db.execute(group_statement))
    if group_member_override is not None:
        group_id, member_ids = group_member_override
        has_admin_grant = db.scalar(
            select(AuthorizationGrant.id).where(
                AuthorizationGrant.tenant_id == tenant_id,
                AuthorizationGrant.role_id == role.id,
                AuthorizationGrant.scope_type == "TENANT",
                AuthorizationGrant.access_group_id == group_id,
            )
        )
        if has_admin_grant is not None:
            active_override_users = set(
                db.scalars(
                    select(User.id).where(
                        User.id.in_(member_ids),
                        User.tenant_id == tenant_id,
                        User.status == "ACTIVE",
                    )
                )
            ) if member_ids else set()
            group_pairs = {(current_group_id, user_id) for current_group_id, user_id in group_pairs if current_group_id != group_id}
            group_pairs.update((group_id, user_id) for user_id in active_override_users)
    group_users = {user_id for _, user_id in group_pairs}
    return direct_users | group_users


def _require_tenant_administrator_remains(
    db: Session,
    tenant_id: uuid.UUID,
    **kwargs,
) -> None:
    if not _tenant_administrator_user_ids(db, tenant_id, **kwargs):
        raise _conflict("The tenant must retain at least one active Tenant Administrator")


def _group_has_tenant_administrator_grant(
    db: Session,
    tenant_id: uuid.UUID,
    group_id: uuid.UUID,
) -> bool:
    return db.scalar(
        select(AuthorizationGrant.id)
        .join(Role, Role.id == AuthorizationGrant.role_id)
        .where(
            AuthorizationGrant.tenant_id == tenant_id,
            AuthorizationGrant.access_group_id == group_id,
            AuthorizationGrant.scope_type == "TENANT",
            Role.key == "TENANT_ADMINISTRATOR",
        )
    ) is not None


def _resolve_scope(
    db: Session,
    payload: AuthorizationGrantCreate,
    artifact_principal: ArtifactPrincipal,
    artifact_db: Session,
) -> tuple[uuid.UUID, str, CollectionSnapshot | None]:
    scope = payload.scope
    collection: CollectionSnapshot | None = None
    if scope.type == "TENANT":
        resource = resolve_resource(db, ResourceRef.tenant(scope.id))
    elif scope.type == "CLIENT":
        resource = resolve_resource(db, ResourceRef.client(scope.id))
    elif scope.type == "MATTER":
        resource = resolve_resource(db, ResourceRef.matter(scope.id))
    else:
        try:
            collection = get_collection_snapshot(scope.id, artifact_principal, artifact_db)
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        if collection is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
        resource = resolve_resource(
            db,
            ResourceRef.collection(
                collection.id,
                tenant_id=collection.tenant_id,
                client_id=collection.client_id,
            ),
        )
    if resource is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{scope.type.title()} not found")
    return resource.tenant_id, resource.resource_type, collection


@router.get("/permissions", response_model=list[PermissionRead])
def list_permissions(
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[Permission]:
    ensure_rbac_catalog(db)
    if not legacy_or_can_manage_tenant(
        db,
        principal,
        principal.user.tenant_id,
        "tenant.manage_roles",
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission required: tenant.manage_roles")
    return list(db.scalars(select(Permission).where(Permission.status == "ACTIVE").order_by(Permission.category, Permission.key)))


@router.get("/tenants/{tenant_id}/roles", response_model=list[RoleRead])
def list_roles(
    tenant_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[RoleRead]:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_roles")
    ensure_rbac_catalog(db)
    roles = list(
        db.scalars(
            select(Role)
            .where(
                or_(Role.tenant_id == tenant_id, Role.tenant_id.is_(None)),
                Role.key != "ROOT_ADMINISTRATOR",
            )
            .order_by(Role.role_kind.desc(), Role.display_name)
        )
    )
    return [_role_read(db, role, tenant_id) for role in roles]


@router.post("/tenants/{tenant_id}/roles", response_model=RoleRead, status_code=status.HTTP_201_CREATED)
def create_role(
    tenant_id: uuid.UUID,
    payload: RoleCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> RoleRead:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_roles")
    ensure_rbac_catalog(db)
    permissions = _validated_permissions(db, payload.permission_keys)
    role = Role(
        tenant_id=tenant_id,
        key=payload.key,
        display_name=payload.display_name.strip(),
        description=payload.description,
        role_kind="CUSTOM",
        status="ACTIVE",
        created_by_user_id=principal.user.id,
    )
    db.add(role)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise _conflict("Role key already exists in this tenant") from exc
    db.add_all(RolePermission(role_id=role.id, permission_id=permission.id) for permission in permissions)
    before_revision, after_revision = increment_authorization_revision(db, tenant_id)
    record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=principal.user.id,
        action="authorization.role.created",
        target_type="role",
        target_id=role.id,
        details={
            "key": role.key,
            "permission_keys": sorted(payload.permission_keys),
            "authorization_revision_before": before_revision,
            "authorization_revision_after": after_revision,
        },
    )
    db.commit()
    return _role_read(db, role, tenant_id)


@router.get("/tenants/{tenant_id}/roles/{role_id}", response_model=RoleRead)
def get_role(
    tenant_id: uuid.UUID,
    role_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> RoleRead:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_roles")
    ensure_rbac_catalog(db)
    return _role_read(db, _role_for_tenant(db, tenant_id, role_id), tenant_id)


@router.patch("/tenants/{tenant_id}/roles/{role_id}", response_model=RoleRead)
def update_role(
    tenant_id: uuid.UUID,
    role_id: uuid.UUID,
    payload: RoleUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> RoleRead:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_roles")
    role = _locked_custom_role(db, tenant_id, role_id)
    if role.row_version != payload.row_version:
        raise _conflict("Role was changed by another administrator")
    before = {"display_name": role.display_name, "description": role.description}
    if payload.display_name is not None:
        role.display_name = payload.display_name.strip()
    if "description" in payload.model_fields_set:
        role.description = payload.description
    role.row_version += 1
    before_revision, after_revision = increment_authorization_revision(db, tenant_id)
    record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=principal.user.id,
        action="authorization.role.updated",
        target_type="role",
        target_id=role.id,
        details={
            "before": before,
            "after": {"display_name": role.display_name, "description": role.description},
            "authorization_revision_before": before_revision,
            "authorization_revision_after": after_revision,
        },
    )
    db.commit()
    return _role_read(db, role, tenant_id)


@router.put("/tenants/{tenant_id}/roles/{role_id}/permissions", response_model=RoleRead)
def replace_role_permissions(
    tenant_id: uuid.UUID,
    role_id: uuid.UUID,
    payload: RolePermissionsReplace,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> RoleRead:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_roles")
    ensure_rbac_catalog(db)
    role = _locked_custom_role(db, tenant_id, role_id)
    if role.row_version != payload.row_version:
        raise _conflict("Role was changed by another administrator")
    permissions = _validated_permissions(db, payload.permission_keys)
    previous = _permission_keys(db, role.id)
    db.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
    db.add_all(RolePermission(role_id=role.id, permission_id=permission.id) for permission in permissions)
    role.row_version += 1
    next_keys = sorted(payload.permission_keys)
    before_revision, after_revision = increment_authorization_revision(db, tenant_id)
    record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=principal.user.id,
        action="authorization.role.permissions_replaced",
        target_type="role",
        target_id=role.id,
        details={
            "before": previous,
            "after": next_keys,
            "added": sorted(set(next_keys) - set(previous)),
            "removed": sorted(set(previous) - set(next_keys)),
            "authorization_revision_before": before_revision,
            "authorization_revision_after": after_revision,
        },
    )
    db.commit()
    return _role_read(db, role, tenant_id)


@router.post("/tenants/{tenant_id}/roles/{role_id}/archive", response_model=RoleRead)
def archive_role(
    tenant_id: uuid.UUID,
    role_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> RoleRead:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_roles")
    role = _locked_custom_role(db, tenant_id, role_id)
    if role.status != "ARCHIVED":
        role.status = "ARCHIVED"
        role.row_version += 1
        before_revision, after_revision = increment_authorization_revision(db, tenant_id)
        record_audit(
            db,
            tenant_id=tenant_id,
            actor_user_id=principal.user.id,
            action="authorization.role.archived",
            target_type="role",
            target_id=role.id,
            details={
                "authorization_revision_before": before_revision,
                "authorization_revision_after": after_revision,
            },
        )
        db.commit()
    return _role_read(db, role, tenant_id)


@router.get("/tenants/{tenant_id}/access-groups", response_model=list[AccessGroupRead])
def list_access_groups(
    tenant_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[AccessGroupRead]:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_groups")
    groups = list(db.scalars(select(AccessGroup).where(AccessGroup.tenant_id == tenant_id).order_by(AccessGroup.display_name)))
    return [_group_read(db, group) for group in groups]


@router.post("/tenants/{tenant_id}/access-groups", response_model=AccessGroupRead, status_code=status.HTTP_201_CREATED)
def create_access_group(
    tenant_id: uuid.UUID,
    payload: AccessGroupCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AccessGroupRead:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_groups")
    group = AccessGroup(
        tenant_id=tenant_id,
        key=payload.key,
        display_name=payload.display_name.strip(),
        description=payload.description,
        group_kind="LOCAL",
        status="ACTIVE",
        created_by_user_id=principal.user.id,
    )
    db.add(group)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise _conflict("Access-group key already exists in this tenant") from exc
    before_revision, after_revision = increment_authorization_revision(db, tenant_id)
    record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=principal.user.id,
        action="authorization.access_group.created",
        target_type="access_group",
        target_id=group.id,
        details={
            "key": group.key,
            "authorization_revision_before": before_revision,
            "authorization_revision_after": after_revision,
        },
    )
    db.commit()
    return _group_read(db, group)


@router.get("/tenants/{tenant_id}/access-groups/{group_id}", response_model=AccessGroupRead)
def get_access_group(
    tenant_id: uuid.UUID,
    group_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AccessGroupRead:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_groups")
    group = db.get(AccessGroup, group_id)
    if group is None or group.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Access group not found")
    return _group_read(db, group)


@router.patch("/tenants/{tenant_id}/access-groups/{group_id}", response_model=AccessGroupRead)
def update_access_group(
    tenant_id: uuid.UUID,
    group_id: uuid.UUID,
    payload: AccessGroupUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AccessGroupRead:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_groups")
    group = db.scalar(select(AccessGroup).where(AccessGroup.id == group_id).with_for_update())
    if group is None or group.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Access group not found")
    if group.row_version != payload.row_version:
        raise _conflict("Access group was changed by another administrator")
    before = {"display_name": group.display_name, "description": group.description}
    if payload.display_name is not None:
        group.display_name = payload.display_name.strip()
    if "description" in payload.model_fields_set:
        group.description = payload.description
    group.row_version += 1
    before_revision, after_revision = increment_authorization_revision(db, tenant_id)
    record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=principal.user.id,
        action="authorization.access_group.updated",
        target_type="access_group",
        target_id=group.id,
        details={
            "before": before,
            "after": {"display_name": group.display_name, "description": group.description},
            "authorization_revision_before": before_revision,
            "authorization_revision_after": after_revision,
        },
    )
    db.commit()
    return _group_read(db, group)


@router.put("/tenants/{tenant_id}/access-groups/{group_id}/members", response_model=AccessGroupRead)
def replace_access_group_members(
    tenant_id: uuid.UUID,
    group_id: uuid.UUID,
    payload: AccessGroupMembersReplace,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AccessGroupRead:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_groups")
    group = db.scalar(select(AccessGroup).where(AccessGroup.id == group_id).with_for_update())
    if group is None or group.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Access group not found")
    if group.status != "ACTIVE":
        raise _conflict("Archived access groups cannot be changed")
    if group.row_version != payload.row_version:
        raise _conflict("Access group was changed by another administrator")
    users = list(db.scalars(select(User).where(User.id.in_(payload.user_ids)))) if payload.user_ids else []
    if len(users) != len(payload.user_ids) or any(user.tenant_id != tenant_id for user in users):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Every member must belong to the tenant")
    previous = set(db.scalars(select(AccessGroupMember.user_id).where(AccessGroupMember.access_group_id == group.id)))
    next_ids = set(payload.user_ids)
    if _group_has_tenant_administrator_grant(db, tenant_id, group.id):
        _require_tenant_administrator_remains(
            db,
            tenant_id,
            group_member_override=(group.id, next_ids),
        )
    db.execute(delete(AccessGroupMember).where(AccessGroupMember.access_group_id == group.id))
    db.add_all(
        AccessGroupMember(access_group_id=group.id, user_id=user_id, added_by_user_id=principal.user.id)
        for user_id in payload.user_ids
    )
    group.row_version += 1
    before_revision, after_revision = increment_authorization_revision(db, tenant_id)
    record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=principal.user.id,
        action="authorization.access_group.members_replaced",
        target_type="access_group",
        target_id=group.id,
        details={
            "added_user_ids": sorted(str(value) for value in next_ids - previous),
            "removed_user_ids": sorted(str(value) for value in previous - next_ids),
            "authorization_revision_before": before_revision,
            "authorization_revision_after": after_revision,
        },
    )
    db.commit()
    return _group_read(db, group)


@router.post("/tenants/{tenant_id}/access-groups/{group_id}/archive", response_model=AccessGroupRead)
def archive_access_group(
    tenant_id: uuid.UUID,
    group_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AccessGroupRead:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_groups")
    group = db.scalar(select(AccessGroup).where(AccessGroup.id == group_id).with_for_update())
    if group is None or group.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Access group not found")
    if group.status != "ARCHIVED":
        if _group_has_tenant_administrator_grant(db, tenant_id, group.id):
            _require_tenant_administrator_remains(db, tenant_id, excluding_group_id=group.id)
        group.status = "ARCHIVED"
        group.row_version += 1
        before_revision, after_revision = increment_authorization_revision(db, tenant_id)
        record_audit(
            db,
            tenant_id=tenant_id,
            actor_user_id=principal.user.id,
            action="authorization.access_group.archived",
            target_type="access_group",
            target_id=group.id,
            details={
                "authorization_revision_before": before_revision,
                "authorization_revision_after": after_revision,
            },
        )
        db.commit()
    return _group_read(db, group)


@router.get("/tenants/{tenant_id}/authorization-grants", response_model=list[AuthorizationGrantRead])
def list_authorization_grants(
    tenant_id: uuid.UUID,
    subject_id: uuid.UUID | None = None,
    role_id: uuid.UUID | None = None,
    scope_type: Annotated[str | None, Query(pattern="^(TENANT|CLIENT|COLLECTION|MATTER)$")] = None,
    scope_id: uuid.UUID | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[AuthorizationGrantRead]:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_roles")
    statement = select(AuthorizationGrant).where(AuthorizationGrant.tenant_id == tenant_id)
    if subject_id is not None:
        statement = statement.where(
            or_(AuthorizationGrant.user_id == subject_id, AuthorizationGrant.access_group_id == subject_id)
        )
    if role_id is not None:
        statement = statement.where(AuthorizationGrant.role_id == role_id)
    if scope_type is not None:
        statement = statement.where(AuthorizationGrant.scope_type == scope_type)
    if scope_id is not None:
        statement = statement.where(
            or_(
                AuthorizationGrant.tenant_scope_id == scope_id,
                AuthorizationGrant.client_id == scope_id,
                AuthorizationGrant.matter_id == scope_id,
                AuthorizationGrant.collection_id == scope_id,
            )
        )
    grants = list(db.scalars(statement.order_by(AuthorizationGrant.created_at.desc())))
    return [_grant_read(db, grant) for grant in grants]


@router.post(
    "/tenants/{tenant_id}/authorization-grants",
    response_model=AuthorizationGrantRead,
    status_code=status.HTTP_201_CREATED,
)
def create_authorization_grant(
    tenant_id: uuid.UUID,
    payload: AuthorizationGrantCreate,
    principal: Principal = Depends(get_principal),
    artifact_principal: ArtifactPrincipal = Depends(get_embedded_artifact_principal),
    db: Session = Depends(get_db),
    artifact_db: Session = Depends(get_artifact_db),
) -> AuthorizationGrantRead:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_roles")
    ensure_rbac_catalog(db)
    role = _role_for_tenant(db, tenant_id, payload.role_id)
    if role.status != "ACTIVE":
        raise _conflict("Archived roles cannot receive new grants")
    scope_tenant_id, scope_type, collection = _resolve_scope(db, payload, artifact_principal, artifact_db)
    if scope_tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Grant scope must belong to the tenant")
    if payload.subject.type == "USER":
        user = db.get(User, payload.subject.id)
        if user is None or user.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Grant user must belong to the tenant")
        user_id, group_id = user.id, None
    else:
        group = db.get(AccessGroup, payload.subject.id)
        if group is None or group.tenant_id != tenant_id or group.status != "ACTIVE":
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Grant group must be active in the tenant")
        user_id, group_id = None, group.id
    grant = AuthorizationGrant(
        tenant_id=tenant_id,
        user_id=user_id,
        access_group_id=group_id,
        role_id=role.id,
        scope_type=scope_type,
        tenant_scope_id=payload.scope.id if scope_type == "TENANT" else None,
        client_id=payload.scope.id if scope_type == "CLIENT" else None,
        matter_id=payload.scope.id if scope_type == "MATTER" else None,
        collection_id=payload.scope.id if scope_type == "COLLECTION" else None,
        created_by_user_id=principal.user.id,
        updated_by_user_id=principal.user.id,
    )
    db.add(grant)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise _conflict("This subject already has that role at the selected scope") from exc
    before_revision, after_revision = increment_authorization_revision(db, tenant_id)
    record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=principal.user.id,
        action="authorization.grant.created",
        target_type="authorization_grant",
        target_id=grant.id,
        details={
            "subject_type": payload.subject.type,
            "subject_id": str(payload.subject.id),
            "role_id": str(role.id),
            "scope_type": scope_type,
            "scope_id": str(payload.scope.id),
            "scope_display_name": collection.name if collection is not None else None,
            "authorization_revision_before": before_revision,
            "authorization_revision_after": after_revision,
        },
    )
    db.commit()
    return _grant_read(db, grant)


@router.patch("/tenants/{tenant_id}/authorization-grants/{grant_id}", response_model=AuthorizationGrantRead)
def update_authorization_grant(
    tenant_id: uuid.UUID,
    grant_id: uuid.UUID,
    payload: AuthorizationGrantUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AuthorizationGrantRead:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_roles")
    grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.id == grant_id).with_for_update())
    if grant is None or grant.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Authorization grant not found")
    if grant.row_version != payload.row_version:
        raise _conflict("Authorization grant was changed by another administrator")
    role = _role_for_tenant(db, tenant_id, payload.role_id)
    if role.status != "ACTIVE":
        raise _conflict("Archived roles cannot receive grants")
    previous_role_id = grant.role_id
    previous_role = db.get(Role, previous_role_id)
    if (
        previous_role is not None
        and previous_role.key == "TENANT_ADMINISTRATOR"
        and grant.scope_type == "TENANT"
        and role.id != previous_role.id
    ):
        _require_tenant_administrator_remains(db, tenant_id, excluding_grant_id=grant.id)
    grant.role_id = role.id
    grant.updated_by_user_id = principal.user.id
    grant.row_version += 1
    before_revision, after_revision = increment_authorization_revision(db, tenant_id)
    record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=principal.user.id,
        action="authorization.grant.role_changed",
        target_type="authorization_grant",
        target_id=grant.id,
        details={
            "before_role_id": str(previous_role_id),
            "after_role_id": str(role.id),
            "authorization_revision_before": before_revision,
            "authorization_revision_after": after_revision,
        },
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _conflict("This subject already has that role at the selected scope") from exc
    return _grant_read(db, grant)


@router.delete("/tenants/{tenant_id}/authorization-grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_authorization_grant(
    tenant_id: uuid.UUID,
    grant_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> None:
    _require_tenant_admin(db, principal, tenant_id, "tenant.manage_roles")
    grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.id == grant_id).with_for_update())
    if grant is None or grant.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Authorization grant not found")
    role = db.get(Role, grant.role_id)
    if role is not None and role.key == "TENANT_ADMINISTRATOR" and grant.scope_type == "TENANT":
        _require_tenant_administrator_remains(db, tenant_id, excluding_grant_id=grant.id)
    details = {
        "subject_type": "USER" if grant.user_id is not None else "ACCESS_GROUP",
        "subject_id": str(grant.user_id or grant.access_group_id),
        "role_id": str(grant.role_id),
        "scope_type": grant.scope_type,
        "scope_id": str(_scope_id(grant)),
    }
    db.delete(grant)
    before_revision, after_revision = increment_authorization_revision(db, tenant_id)
    details.update(
        authorization_revision_before=before_revision,
        authorization_revision_after=after_revision,
    )
    record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=principal.user.id,
        action="authorization.grant.removed",
        target_type="authorization_grant",
        target_id=grant.id,
        details=details,
    )
    db.commit()


def _resource_ref(
    db: Session,
    resource_type: str,
    resource_id: uuid.UUID,
    artifact_principal: ArtifactPrincipal,
    artifact_db: Session,
) -> ResourceRef:
    if resource_type == "TENANT":
        return ResourceRef.tenant(resource_id)
    if resource_type == "CLIENT":
        return ResourceRef.client(resource_id)
    if resource_type == "MATTER":
        return ResourceRef.matter(resource_id)
    try:
        collection = get_collection_snapshot(resource_id, artifact_principal, artifact_db)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    return ResourceRef.collection(
        collection.id,
        tenant_id=collection.tenant_id,
        client_id=collection.client_id,
    )


@router.get("/authorization/effective", response_model=EffectivePermissionsRead)
def get_effective_permissions(
    resource_type: Annotated[str, Query(pattern="^(TENANT|CLIENT|COLLECTION|MATTER)$")],
    resource_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    artifact_principal: ArtifactPrincipal = Depends(get_embedded_artifact_principal),
    db: Session = Depends(get_db),
    artifact_db: Session = Depends(get_artifact_db),
) -> EffectivePermissionsRead:
    ensure_rbac_catalog(db)
    ref = _resource_ref(db, resource_type, resource_id, artifact_principal, artifact_db)
    resource, permission_keys = effective_permissions(db, principal, ref)
    if resource is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
    tenant = db.get(Tenant, resource.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return EffectivePermissionsRead(
        resource_type=resource.resource_type,
        resource_id=resource.resource_id,
        authorization_revision=tenant.authorization_revision,
        permission_keys=permission_keys,
    )


@router.get("/authorization/explain", response_model=AuthorizationExplanationRead)
def explain_authorization(
    permission_key: Annotated[str, Query(min_length=1, max_length=120)],
    resource_type: Annotated[str, Query(pattern="^(TENANT|CLIENT|COLLECTION|MATTER)$")],
    resource_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    artifact_principal: ArtifactPrincipal = Depends(get_embedded_artifact_principal),
    db: Session = Depends(get_db),
    artifact_db: Session = Depends(get_artifact_db),
) -> AuthorizationExplanationRead:
    ref = _resource_ref(db, resource_type, resource_id, artifact_principal, artifact_db)
    result = explain(db, principal, permission_key, ref)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
    return AuthorizationExplanationRead(
        allowed=result.allowed,
        permission_key=result.permission_key,
        resource_type=result.resource.resource_type,
        resource_id=result.resource.resource_id,
        grant_id=result.grant_id,
        role_id=result.role_id,
        role_key=result.role_key,
        subject_type=result.subject_type,
        subject_id=result.subject_id,
        scope_type=result.scope_type,
        scope_id=result.scope_id,
    )
