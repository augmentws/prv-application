"""Add role-based access control foundation.

Revision ID: 0067_rbac_foundation
Revises: 0066_audit_log
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op
from app.rbac_manifest import PERMISSIONS, SYSTEM_ROLE_BY_KEY, SYSTEM_ROLES

revision: str = "0067_rbac_foundation"
down_revision: str | None = "0066_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _partial_unique(name: str, columns: list[str], condition: str) -> None:
    op.create_index(
        name,
        "authorization_grant",
        columns,
        unique=True,
        postgresql_where=sa.text(condition),
        sqlite_where=sa.text(condition),
    )


def upgrade() -> None:
    op.add_column(
        "tenant",
        sa.Column("authorization_revision", sa.BigInteger(), nullable=False, server_default="1"),
    )
    op.drop_constraint("ck_user_tenant_role", "app_user", type_="check")
    op.create_check_constraint("ck_user_tenant_role", "app_user", "tenant_role IN ('ADMIN', 'USER')")
    op.alter_column("app_user", "tenant_role", server_default="USER")

    op.create_table(
        "permission",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("resource_type", sa.String(length=30), nullable=False),
        sa.Column("dangerous", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="ck_permission_status"),
        sa.CheckConstraint(
            "resource_type IN ('TENANT', 'CLIENT', 'COLLECTION', 'MATTER')",
            name="ck_permission_resource_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index("ix_permission_key", "permission", ["key"])
    op.create_index("ix_permission_category", "permission", ["category"])
    op.create_index("ix_permission_resource_type", "permission", ["resource_type"])

    op.create_table(
        "role",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("role_kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role_kind IN ('SYSTEM', 'CUSTOM')", name="ck_role_kind"),
        sa.CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="ck_role_status"),
        sa.CheckConstraint(
            "(role_kind = 'SYSTEM' AND tenant_id IS NULL) OR "
            "(role_kind = 'CUSTOM' AND tenant_id IS NOT NULL)",
            name="ck_role_kind_tenant",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_role_tenant_id", "role", ["tenant_id"])
    op.create_index(
        "uq_system_role_key",
        "role",
        ["key"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
        sqlite_where=sa.text("tenant_id IS NULL"),
    )
    op.create_index(
        "uq_tenant_role_key",
        "role",
        ["tenant_id", "key"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
        sqlite_where=sa.text("tenant_id IS NOT NULL"),
    )

    op.create_table(
        "role_permission",
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("permission_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["permission_id"], ["permission.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["role_id"], ["role.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_id", "permission_id"),
    )

    op.create_table(
        "access_group",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("group_kind", sa.String(length=20), nullable=False, server_default="LOCAL"),
        sa.Column("external_reference", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("group_kind IN ('LOCAL')", name="ck_access_group_kind"),
        sa.CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="ck_access_group_status"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_access_group_tenant_key"),
    )
    op.create_index("ix_access_group_tenant_id", "access_group", ["tenant_id"])

    op.create_table(
        "access_group_member",
        sa.Column("access_group_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("added_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["access_group_id"], ["access_group.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["added_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("access_group_id", "user_id"),
    )

    op.create_table(
        "authorization_grant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("access_group_id", sa.Uuid(), nullable=True),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("tenant_scope_id", sa.Uuid(), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("matter_id", sa.Uuid(), nullable=True),
        sa.Column("collection_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(user_id IS NOT NULL AND access_group_id IS NULL) OR "
            "(user_id IS NULL AND access_group_id IS NOT NULL)",
            name="ck_authorization_grant_subject",
        ),
        sa.CheckConstraint(
            "(scope_type = 'TENANT' AND tenant_scope_id IS NOT NULL AND client_id IS NULL "
            "AND matter_id IS NULL AND collection_id IS NULL) OR "
            "(scope_type = 'CLIENT' AND tenant_scope_id IS NULL AND client_id IS NOT NULL "
            "AND matter_id IS NULL AND collection_id IS NULL) OR "
            "(scope_type = 'MATTER' AND tenant_scope_id IS NULL AND client_id IS NULL "
            "AND matter_id IS NOT NULL AND collection_id IS NULL) OR "
            "(scope_type = 'COLLECTION' AND tenant_scope_id IS NULL AND client_id IS NULL "
            "AND matter_id IS NULL AND collection_id IS NOT NULL)",
            name="ck_authorization_grant_scope",
        ),
        sa.ForeignKeyConstraint(["access_group_id"], ["access_group.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["client_id"], ["client.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["role.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_scope_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_authorization_grant_tenant_id", "authorization_grant", ["tenant_id"])
    op.create_index("ix_authorization_grant_user_id", "authorization_grant", ["user_id"])
    op.create_index("ix_authorization_grant_access_group_id", "authorization_grant", ["access_group_id"])
    op.create_index("ix_authorization_grant_role_id", "authorization_grant", ["role_id"])
    op.create_index("ix_authorization_grant_tenant_scope", "authorization_grant", ["tenant_id", "scope_type"])
    op.create_index("ix_authorization_grant_client", "authorization_grant", ["client_id"])
    op.create_index("ix_authorization_grant_matter", "authorization_grant", ["matter_id"])
    op.create_index("ix_authorization_grant_collection", "authorization_grant", ["collection_id"])
    _partial_unique(
        "uq_authorization_grant_user_tenant",
        ["user_id", "role_id", "tenant_scope_id"],
        "scope_type = 'TENANT' AND user_id IS NOT NULL",
    )
    _partial_unique(
        "uq_authorization_grant_group_tenant",
        ["access_group_id", "role_id", "tenant_scope_id"],
        "scope_type = 'TENANT' AND access_group_id IS NOT NULL",
    )
    for scope, column in (("CLIENT", "client_id"), ("MATTER", "matter_id"), ("COLLECTION", "collection_id")):
        _partial_unique(
            f"uq_authorization_grant_user_{scope.lower()}",
            ["user_id", "role_id", column],
            f"scope_type = '{scope}' AND user_id IS NOT NULL",
        )
        _partial_unique(
            f"uq_authorization_grant_group_{scope.lower()}",
            ["access_group_id", "role_id", column],
            f"scope_type = '{scope}' AND access_group_id IS NOT NULL",
        )

    now = datetime.now(timezone.utc)
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
    op.bulk_insert(
        permission_table,
        [
            {
                "id": permission.id,
                "key": permission.key,
                "display_name": permission.display_name,
                "description": permission.description,
                "category": permission.category,
                "resource_type": permission.resource_type,
                "dangerous": permission.dangerous,
                "status": "ACTIVE",
                "created_at": now,
                "updated_at": now,
            }
            for permission in PERMISSIONS
        ],
    )
    role_table = sa.table(
        "role",
        sa.column("id", sa.Uuid()),
        sa.column("tenant_id", sa.Uuid()),
        sa.column("key", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("role_kind", sa.String()),
        sa.column("status", sa.String()),
        sa.column("row_version", sa.Integer()),
        sa.column("created_by_user_id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        role_table,
        [
            {
                "id": role.id,
                "tenant_id": None,
                "key": role.key,
                "display_name": role.display_name,
                "description": role.description,
                "role_kind": "SYSTEM",
                "status": "ACTIVE",
                "row_version": 1,
                "created_by_user_id": None,
                "created_at": now,
                "updated_at": now,
            }
            for role in SYSTEM_ROLES
        ],
    )
    role_permission_table = sa.table(
        "role_permission",
        sa.column("role_id", sa.Uuid()),
        sa.column("permission_id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    permission_ids = {permission.key: permission.id for permission in PERMISSIONS}
    op.bulk_insert(
        role_permission_table,
        [
            {"role_id": role.id, "permission_id": permission_ids[key], "created_at": now}
            for role in SYSTEM_ROLES
            for key in sorted(role.permission_keys)
        ],
    )

    connection = op.get_bind()
    migrated_grants: list[dict] = []
    audit_rows: list[dict] = []

    def add_grant(*, tenant_id, user_id, role_key, scope_type, scope_id, source: str) -> None:
        role_id = SYSTEM_ROLE_BY_KEY[role_key].id
        grant_id = uuid.uuid5(
            uuid.UUID("a65b53bf-d7d5-4501-ab12-cec6aed8fc36"),
            f"{user_id}:{role_id}:{scope_type}:{scope_id}",
        )
        row = {
            "id": grant_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "access_group_id": None,
            "role_id": role_id,
            "scope_type": scope_type,
            "tenant_scope_id": scope_id if scope_type == "TENANT" else None,
            "client_id": scope_id if scope_type == "CLIENT" else None,
            "matter_id": scope_id if scope_type == "MATTER" else None,
            "collection_id": None,
            "created_by_user_id": user_id,
            "updated_by_user_id": user_id,
            "row_version": 1,
            "created_at": now,
            "updated_at": now,
        }
        migrated_grants.append(row)
        audit_rows.append(
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "actor_user_id": None,
                "action": "authorization.grant.migrated",
                "target_type": "authorization_grant",
                "target_id": grant_id,
                "details": {
                    "user_id": str(user_id),
                    "role_id": str(role_id),
                    "scope_type": scope_type,
                    "scope_id": str(scope_id),
                    "source": source,
                },
                "created_at": now,
            }
        )

    tenant_admins = connection.execute(
        sa.text("SELECT id, tenant_id FROM app_user WHERE tenant_role = 'ADMIN'")
    ).mappings()
    for row in tenant_admins:
        add_grant(
            tenant_id=row["tenant_id"],
            user_id=row["id"],
            role_key="TENANT_ADMINISTRATOR",
            scope_type="TENANT",
            scope_id=row["tenant_id"],
            source="app_user.tenant_role",
        )
    client_memberships = connection.execute(
        sa.text(
            "SELECT cm.user_id, cm.client_id, c.tenant_id "
            "FROM client_membership cm JOIN client c ON c.id = cm.client_id"
        )
    ).mappings()
    for row in client_memberships:
        add_grant(
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            role_key="CLIENT_ADMINISTRATOR",
            scope_type="CLIENT",
            scope_id=row["client_id"],
            source="client_membership",
        )
    matter_memberships = connection.execute(
        sa.text(
            "SELECT mm.user_id, mm.matter_id, c.tenant_id "
            "FROM matter_membership mm JOIN matter m ON m.id = mm.matter_id "
            "JOIN client c ON c.id = m.client_id"
        )
    ).mappings()
    for row in matter_memberships:
        add_grant(
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            role_key="MATTER_ADMINISTRATOR",
            scope_type="MATTER",
            scope_id=row["matter_id"],
            source="matter_membership",
        )

    if migrated_grants:
        grant_table = sa.table(
            "authorization_grant",
            *[sa.column(key) for key in migrated_grants[0]],
        )
        op.bulk_insert(grant_table, migrated_grants)
    tenant_ids = list(connection.execute(sa.text("SELECT id FROM tenant")).scalars())
    for tenant_id in tenant_ids:
        audit_rows.append(
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "actor_user_id": None,
                "action": "authorization.permission.catalog_updated",
                "target_type": "tenant",
                "target_id": tenant_id,
                "details": {
                    "permission_count": len(PERMISSIONS),
                    "system_role_count": len(SYSTEM_ROLES),
                },
                "created_at": now,
            }
        )
    if audit_rows:
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
        op.bulk_insert(audit_table, audit_rows)


def downgrade() -> None:
    for scope in ("collection", "matter", "client", "tenant"):
        op.drop_index(f"uq_authorization_grant_group_{scope}", table_name="authorization_grant")
        op.drop_index(f"uq_authorization_grant_user_{scope}", table_name="authorization_grant")
    op.drop_index("ix_authorization_grant_collection", table_name="authorization_grant")
    op.drop_index("ix_authorization_grant_matter", table_name="authorization_grant")
    op.drop_index("ix_authorization_grant_client", table_name="authorization_grant")
    op.drop_index("ix_authorization_grant_tenant_scope", table_name="authorization_grant")
    op.drop_index("ix_authorization_grant_role_id", table_name="authorization_grant")
    op.drop_index("ix_authorization_grant_access_group_id", table_name="authorization_grant")
    op.drop_index("ix_authorization_grant_user_id", table_name="authorization_grant")
    op.drop_index("ix_authorization_grant_tenant_id", table_name="authorization_grant")
    op.drop_table("authorization_grant")
    op.drop_table("access_group_member")
    op.drop_index("ix_access_group_tenant_id", table_name="access_group")
    op.drop_table("access_group")
    op.drop_table("role_permission")
    op.drop_index("uq_tenant_role_key", table_name="role")
    op.drop_index("uq_system_role_key", table_name="role")
    op.drop_index("ix_role_tenant_id", table_name="role")
    op.drop_table("role")
    op.drop_index("ix_permission_resource_type", table_name="permission")
    op.drop_index("ix_permission_category", table_name="permission")
    op.drop_index("ix_permission_key", table_name="permission")
    op.drop_table("permission")
    op.execute("UPDATE app_user SET tenant_role = 'ADMIN' WHERE tenant_role = 'USER'")
    op.drop_constraint("ck_user_tenant_role", "app_user", type_="check")
    op.create_check_constraint("ck_user_tenant_role", "app_user", "tenant_role = 'ADMIN'")
    op.alter_column("app_user", "tenant_role", server_default=None)
    op.drop_column("tenant", "authorization_revision")
