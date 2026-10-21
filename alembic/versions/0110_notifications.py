"""Add the in-application notification foundation.

Revision ID: 0110_notifications
Revises: 0109_family_relationship_filters
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0110_notifications"
down_revision: str = "0109_family_relationship_filters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("tenant_scope_id", sa.Uuid(), nullable=True),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("matter_id", sa.Uuid(), nullable=True),
        sa.Column("collection_id", sa.Uuid(), nullable=True),
        sa.Column("type_key", sa.String(length=120), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.String(length=1000), nullable=False),
        sa.Column("template_data", sa.JSON(), nullable=False),
        sa.Column("source_type", sa.String(length=100), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("primary_route_name", sa.String(length=80), nullable=False),
        sa.Column("primary_route_params", sa.JSON(), nullable=False),
        sa.Column("created_by_kind", sa.String(length=20), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=500), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_code", sa.String(length=50), nullable=True),
        sa.CheckConstraint(
            "(scope_type = 'TENANT' AND tenant_scope_id IS NOT NULL AND client_id IS NULL "
            "AND matter_id IS NULL AND collection_id IS NULL) OR "
            "(scope_type = 'CLIENT' AND tenant_scope_id IS NULL AND client_id IS NOT NULL "
            "AND matter_id IS NULL AND collection_id IS NULL) OR "
            "(scope_type = 'MATTER' AND tenant_scope_id IS NULL AND client_id IS NULL "
            "AND matter_id IS NOT NULL AND collection_id IS NULL) OR "
            "(scope_type = 'COLLECTION' AND tenant_scope_id IS NULL AND client_id IS NOT NULL "
            "AND matter_id IS NULL AND collection_id IS NOT NULL)",
            name="ck_notification_scope",
        ),
        sa.CheckConstraint("schema_version > 0", name="ck_notification_schema_version"),
        sa.CheckConstraint("severity IN ('INFO', 'SUCCESS', 'WARNING', 'ERROR')", name="ck_notification_severity"),
        sa.CheckConstraint("kind IN ('INFORMATIONAL', 'ACTION_REQUIRED')", name="ck_notification_kind"),
        sa.CheckConstraint(
            "created_by_kind IN ('USER', 'SERVICE', 'WORKFLOW', 'SYSTEM')",
            name="ck_notification_created_by_kind",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["client.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_scope_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "type_key", "idempotency_key", name="uq_notification_idempotency"),
    )
    op.create_index("ix_notification_tenant_id", "notification", ["tenant_id"])
    op.create_index("ix_notification_client_id", "notification", ["client_id"])
    op.create_index("ix_notification_matter_id", "notification", ["matter_id"])
    op.create_index("ix_notification_collection_id", "notification", ["collection_id"])
    op.create_index("ix_notification_type_key", "notification", ["type_key"])
    op.create_index("ix_notification_category", "notification", ["category"])
    op.create_index("ix_notification_severity", "notification", ["severity"])
    op.create_index("ix_notification_kind", "notification", ["kind"])
    op.create_index("ix_notification_resolved_at", "notification", ["resolved_at"])
    op.create_index("ix_notification_matter_created", "notification", ["matter_id", "created_at"])
    op.create_index("ix_notification_client_created", "notification", ["client_id", "created_at"])
    op.create_index("ix_notification_source", "notification", ["source_type", "source_id"])

    op.create_table(
        "notification_audience",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("audience_kind", sa.String(length=30), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("access_group_id", sa.Uuid(), nullable=True),
        sa.Column("role_id", sa.Uuid(), nullable=True),
        sa.Column("permission_key", sa.String(length=120), nullable=True),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(audience_kind = 'USER' AND user_id IS NOT NULL AND access_group_id IS NULL "
            "AND role_id IS NULL AND permission_key IS NULL) OR "
            "(audience_kind = 'ACCESS_GROUP' AND user_id IS NULL AND access_group_id IS NOT NULL "
            "AND role_id IS NULL AND permission_key IS NULL) OR "
            "(audience_kind = 'ROLE' AND user_id IS NULL AND access_group_id IS NULL "
            "AND role_id IS NOT NULL AND permission_key IS NULL) OR "
            "(audience_kind = 'PERMISSION' AND user_id IS NULL AND access_group_id IS NULL "
            "AND role_id IS NULL AND permission_key IS NOT NULL)",
            name="ck_notification_audience_selector",
        ),
        sa.CheckConstraint(
            "scope_type IN ('TENANT', 'CLIENT', 'COLLECTION', 'MATTER')",
            name="ck_notification_audience_scope_type",
        ),
        sa.ForeignKeyConstraint(["access_group_id"], ["access_group.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["notification_id"], ["notification.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["role.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notification_audience_notification", "notification_audience", ["notification_id"])

    op.create_table(
        "notification_recipient",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["notification_id"], ["notification.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("notification_id", "user_id", name="uq_notification_recipient"),
    )
    op.create_index("ix_notification_recipient_notification_id", "notification_recipient", ["notification_id"])
    op.create_index("ix_notification_recipient_user_id", "notification_recipient", ["user_id"])
    op.create_index("ix_notification_recipient_user_created", "notification_recipient", ["user_id", "created_at"])
    op.create_index("ix_notification_recipient_user_read", "notification_recipient", ["user_id", "read_at"])

    op.create_table(
        "notification_recipient_audience",
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("audience_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["audience_id"], ["notification_audience.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_id"], ["notification_recipient.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("recipient_id", "audience_id"),
    )


def downgrade() -> None:
    op.drop_table("notification_recipient_audience")
    op.drop_index("ix_notification_recipient_user_read", table_name="notification_recipient")
    op.drop_index("ix_notification_recipient_user_created", table_name="notification_recipient")
    op.drop_index("ix_notification_recipient_user_id", table_name="notification_recipient")
    op.drop_index("ix_notification_recipient_notification_id", table_name="notification_recipient")
    op.drop_table("notification_recipient")
    op.drop_index("ix_notification_audience_notification", table_name="notification_audience")
    op.drop_table("notification_audience")
    op.drop_index("ix_notification_source", table_name="notification")
    op.drop_index("ix_notification_client_created", table_name="notification")
    op.drop_index("ix_notification_matter_created", table_name="notification")
    op.drop_index("ix_notification_resolved_at", table_name="notification")
    op.drop_index("ix_notification_kind", table_name="notification")
    op.drop_index("ix_notification_severity", table_name="notification")
    op.drop_index("ix_notification_category", table_name="notification")
    op.drop_index("ix_notification_type_key", table_name="notification")
    op.drop_index("ix_notification_collection_id", table_name="notification")
    op.drop_index("ix_notification_matter_id", table_name="notification")
    op.drop_index("ix_notification_client_id", table_name="notification")
    op.drop_index("ix_notification_tenant_id", table_name="notification")
    op.drop_table("notification")
