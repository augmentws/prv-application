"""Initial Core schema.

Revision ID: 0001_core
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_core"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamp_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "tenant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("parent_tenant_id", sa.Uuid(), nullable=True),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("is_root", sa.Boolean(), nullable=False),
        *timestamp_columns(),
        sa.CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_tenant_status"),
        sa.CheckConstraint(
            "(is_root AND parent_tenant_id IS NULL) OR (NOT is_root AND parent_tenant_id IS NOT NULL)",
            name="ck_tenant_root_parent",
        ),
        sa.ForeignKeyConstraint(["parent_tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tenant_parent_tenant_id", "tenant", ["parent_tenant_id"])
    op.create_index("ix_tenant_slug", "tenant", ["slug"], unique=True)
    op.create_index(
        "uq_single_root_tenant",
        "tenant",
        ["is_root"],
        unique=True,
        postgresql_where=sa.text("is_root = true"),
        sqlite_where=sa.text("is_root = 1"),
    )

    op.create_table(
        "app_user",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("normalized_email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("tenant_role", sa.String(length=20), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), nullable=False),
        sa.Column("last_authenticated_at", sa.DateTime(timezone=True), nullable=True),
        *timestamp_columns(),
        sa.CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_user_status"),
        sa.CheckConstraint("tenant_role = 'ADMIN'", name="ck_user_tenant_role"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "normalized_email", name="uq_user_tenant_email"),
    )
    op.create_index("ix_app_user_tenant_id", "app_user", ["tenant_id"])

    op.create_table(
        "password_credential",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("hash_scheme", sa.String(length=50), nullable=False),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failed_attempt_count", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "auth_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("refresh_jti_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("refresh_jti_hash"),
    )
    op.create_index("ix_auth_session_user_id", "auth_session", ["user_id"])

    op.create_table(
        "client",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        *timestamp_columns(),
        sa.CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_client_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_client_tenant_name"),
    )
    op.create_index("ix_client_tenant_id", "client", ["tenant_id"])

    op.create_table(
        "client_membership",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role = 'ADMIN'", name="ck_client_membership_role"),
        sa.ForeignKeyConstraint(["client_id"], ["client.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "user_id", name="uq_client_membership"),
    )
    op.create_index("ix_client_membership_client_id", "client_membership", ["client_id"])
    op.create_index("ix_client_membership_user_id", "client_membership", ["user_id"])

    op.create_table(
        "matter",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        *timestamp_columns(),
        sa.CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_matter_status"),
        sa.ForeignKeyConstraint(["client_id"], ["client.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "name", name="uq_matter_client_name"),
    )
    op.create_index("ix_matter_client_id", "matter", ["client_id"])

    op.create_table(
        "matter_membership",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role = 'ADMIN'", name="ck_matter_membership_role"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("matter_id", "user_id", name="uq_matter_membership"),
    )
    op.create_index("ix_matter_membership_matter_id", "matter_membership", ["matter_id"])
    op.create_index("ix_matter_membership_user_id", "matter_membership", ["user_id"])

    op.create_table(
        "metadata_definition",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("cardinality", sa.String(length=20), nullable=False),
        sa.Column("allowed_values", sa.JSON(), nullable=True),
        sa.Column("assertion_policy", sa.String(length=30), nullable=False),
        sa.Column("resolution_policy", sa.String(length=30), nullable=False),
        sa.Column("searchable", sa.Boolean(), nullable=False),
        sa.Column("facetable", sa.Boolean(), nullable=False),
        sa.Column("reviewable", sa.Boolean(), nullable=False),
        sa.Column("ai_assignable", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        *timestamp_columns(),
        sa.CheckConstraint(
            "type IN ('TEXT', 'LONG_TEXT', 'INTEGER', 'DECIMAL', 'BOOLEAN', 'DATE', 'DATETIME', 'ENUM', 'JSON')",
            name="ck_metadata_definition_type",
        ),
        sa.CheckConstraint("cardinality IN ('SINGLE', 'MULTIPLE')", name="ck_metadata_definition_cardinality"),
        sa.CheckConstraint(
            "assertion_policy IN ('IMMEDIATE', 'REQUIRES_CONFIRMATION')",
            name="ck_metadata_definition_assertion_policy",
        ),
        sa.CheckConstraint(
            "resolution_policy IN ('EXPLICIT_ONLY', 'LATEST_VALID', 'HUMAN_PRECEDENCE')",
            name="ck_metadata_definition_resolution_policy",
        ),
        sa.CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_metadata_definition_status"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("matter_id", "key", name="uq_metadata_definition_matter_key"),
    )
    op.create_index("ix_metadata_definition_matter_id", "metadata_definition", ["matter_id"])

    op.create_table(
        "audit_record",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("target_type", sa.String(length=100), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_record_tenant_id", "audit_record", ["tenant_id"])
    op.create_index("ix_audit_record_actor_user_id", "audit_record", ["actor_user_id"])
    op.create_index("ix_audit_record_action", "audit_record", ["action"])
    op.create_index("ix_audit_record_created_at", "audit_record", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_record")
    op.drop_table("metadata_definition")
    op.drop_table("matter_membership")
    op.drop_table("matter")
    op.drop_table("client_membership")
    op.drop_table("client")
    op.drop_table("auth_session")
    op.drop_table("password_credential")
    op.drop_table("app_user")
    op.drop_table("tenant")
