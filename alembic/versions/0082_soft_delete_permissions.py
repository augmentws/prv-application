"""Add review batch and job soft-delete permissions.

Revision ID: 0082_soft_delete_permissions
Revises: 0081_job_archives
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op

revision: str = "0082_soft_delete_permissions"
down_revision: str | None = "0081_job_archives"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RBAC_NAMESPACE = uuid.UUID("c803b7f5-9fe8-4af3-b68a-e9900dfbe192")
SYSTEM_ROLE_KEYS = (
    "ROOT_ADMINISTRATOR",
    "TENANT_ADMINISTRATOR",
    "CLIENT_ADMINISTRATOR",
    "MATTER_ADMINISTRATOR",
)
PERMISSIONS = (
    {
        "key": "batch.delete",
        "display_name": "Delete",
        "description": "Allows batch delete.",
        "category": "Review batches",
    },
    {
        "key": "job.delete",
        "display_name": "Delete",
        "description": "Allows job delete.",
        "category": "Processing",
    },
)


def _catalog_id(kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(RBAC_NAMESPACE, f"{kind}:{key}")


def upgrade() -> None:
    connection = op.get_bind()
    now = datetime.now(timezone.utc)
    changed = False

    permission_table = sa.table(
        "permission",
        sa.column("id", sa.Uuid()),
        sa.column("key", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("category", sa.String()),
        sa.column("resource_type", sa.String()),
        sa.column("dangerous", sa.Boolean()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    role_table = sa.table(
        "role",
        sa.column("id", sa.Uuid()),
        sa.column("key", sa.String()),
        sa.column("role_kind", sa.String()),
    )
    role_permission_table = sa.table(
        "role_permission",
        sa.column("role_id", sa.Uuid()),
        sa.column("permission_id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )

    permission_ids: dict[str, uuid.UUID] = {}
    for permission in PERMISSIONS:
        permission_id = _catalog_id("permission", permission["key"])
        existing_id = connection.execute(
            sa.select(permission_table.c.id).where(permission_table.c.key == permission["key"])
        ).scalar_one_or_none()
        if existing_id is None:
            connection.execute(
                permission_table.insert().values(
                    id=permission_id,
                    key=permission["key"],
                    display_name=permission["display_name"],
                    description=permission["description"],
                    category=permission["category"],
                    resource_type="MATTER",
                    dangerous=True,
                    status="ACTIVE",
                    created_at=now,
                    updated_at=now,
                )
            )
            changed = True
        elif existing_id != permission_id:
            raise RuntimeError(f"Permission catalog key {permission['key']} has an unexpected ID")
        permission_ids[permission["key"]] = permission_id

    for role_key in SYSTEM_ROLE_KEYS:
        expected_role_id = _catalog_id("system-role", role_key)
        role_id = connection.execute(
            sa.select(role_table.c.id).where(
                role_table.c.key == role_key,
                role_table.c.role_kind == "SYSTEM",
            )
        ).scalar_one()
        if role_id != expected_role_id:
            raise RuntimeError(f"System role {role_key} has an unexpected ID")
        for permission_id in permission_ids.values():
            membership_exists = connection.execute(
                sa.select(role_permission_table.c.role_id).where(
                    role_permission_table.c.role_id == role_id,
                    role_permission_table.c.permission_id == permission_id,
                )
            ).first()
            if membership_exists is None:
                connection.execute(
                    role_permission_table.insert().values(
                        role_id=role_id,
                        permission_id=permission_id,
                        created_at=now,
                    )
                )
                changed = True

    if changed:
        audit_table = sa.table(
            "audit_record",
            sa.column("id", sa.Uuid()),
            sa.column("tenant_id", sa.Uuid()),
            sa.column("actor_user_id", sa.Uuid()),
            sa.column("action", sa.String()),
            sa.column("target_type", sa.String()),
            sa.column("target_id", sa.Uuid()),
            sa.column("details", sa.JSON()),
            sa.column("created_at", sa.DateTime(timezone=True)),
        )
        tenant_ids = list(connection.execute(sa.text("SELECT id FROM tenant")).scalars())
        if tenant_ids:
            connection.execute(
                audit_table.insert(),
                [
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "actor_user_id": None,
                        "action": "authorization.permission.catalog_updated",
                        "target_type": "tenant",
                        "target_id": tenant_id,
                        "details": {
                            "permission_keys": [permission["key"] for permission in PERMISSIONS],
                            "reason": "soft_delete_permissions",
                        },
                        "created_at": now,
                    }
                    for tenant_id in tenant_ids
                ],
            )


def downgrade() -> None:
    connection = op.get_bind()
    permission_ids = [_catalog_id("permission", permission["key"]) for permission in PERMISSIONS]
    connection.execute(
        sa.text("DELETE FROM role_permission WHERE permission_id IN :permission_ids").bindparams(
            sa.bindparam("permission_ids", expanding=True, value=permission_ids)
        )
    )
    connection.execute(
        sa.text("DELETE FROM permission WHERE id IN :permission_ids").bindparams(
            sa.bindparam("permission_ids", expanding=True, value=permission_ids)
        )
    )
