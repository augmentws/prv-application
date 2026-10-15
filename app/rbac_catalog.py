from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Permission, Role, RolePermission
from app.rbac_manifest import PERMISSIONS, SYSTEM_ROLES


def ensure_rbac_catalog(db: Session) -> None:
    """Idempotently materialize the code-owned catalog for fresh/test databases."""
    existing_permissions = {permission.id: permission for permission in db.scalars(select(Permission))}
    permission_ids_by_key = {permission.key: permission.id for permission in existing_permissions.values()}
    for spec in PERMISSIONS:
        existing = existing_permissions.get(spec.id)
        if existing is None:
            if spec.key in permission_ids_by_key:
                raise RuntimeError(f"Permission catalog key {spec.key} has an unexpected ID")
            db.add(
                Permission(
                    id=spec.id,
                    key=spec.key,
                    display_name=spec.display_name,
                    description=spec.description,
                    category=spec.category,
                    resource_type=spec.resource_type,
                    dangerous=spec.dangerous,
                    status="ACTIVE",
                )
            )
        elif (
            existing.key != spec.key
            or existing.display_name != spec.display_name
            or existing.description != spec.description
            or existing.category != spec.category
            or existing.resource_type != spec.resource_type
            or existing.dangerous != spec.dangerous
            or existing.status != "ACTIVE"
        ):
            raise RuntimeError(f"Permission catalog entry {spec.key} does not match the code-owned manifest")
    existing_roles = {role.id: role for role in db.scalars(select(Role).where(Role.role_kind == "SYSTEM"))}
    role_ids_by_key = {role.key: role.id for role in existing_roles.values()}
    for spec in SYSTEM_ROLES:
        existing = existing_roles.get(spec.id)
        if existing is None:
            if spec.key in role_ids_by_key:
                raise RuntimeError(f"System role {spec.key} has an unexpected ID")
            db.add(
                Role(
                    id=spec.id,
                    tenant_id=None,
                    key=spec.key,
                    display_name=spec.display_name,
                    description=spec.description,
                    role_kind="SYSTEM",
                    status="ACTIVE",
                )
            )
        elif (
            existing.tenant_id is not None
            or existing.key != spec.key
            or existing.display_name != spec.display_name
            or existing.description != spec.description
            or existing.status != "ACTIVE"
        ):
            raise RuntimeError(f"System role {spec.key} does not match the code-owned manifest")
    db.flush()
    permission_ids = {spec.key: spec.id for spec in PERMISSIONS}
    existing_memberships = set(db.execute(select(RolePermission.role_id, RolePermission.permission_id)).all())
    for role in SYSTEM_ROLES:
        for permission_key in role.permission_keys:
            membership = (role.id, permission_ids[permission_key])
            if membership not in existing_memberships:
                db.add(RolePermission(role_id=role.id, permission_id=permission_ids[permission_key]))
    db.flush()
    for role in SYSTEM_ROLES:
        actual = set(
            db.scalars(
                select(Permission.key)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == role.id)
            )
        )
        if actual != set(role.permission_keys):
            raise RuntimeError(f"System role permission set {role.key} does not match the code-owned manifest")
