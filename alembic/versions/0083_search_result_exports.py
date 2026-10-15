"""Add durable matter search result exports.

Revision ID: 0083_search_result_exports
Revises: 0082_soft_delete_permissions
"""

import json
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op

revision: str = "0083_search_result_exports"
down_revision: str | None = "0082_soft_delete_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RBAC_NAMESPACE = uuid.UUID("c803b7f5-9fe8-4af3-b68a-e9900dfbe192")
SYSTEM_ROLE_KEYS = (
    "ROOT_ADMINISTRATOR",
    "TENANT_ADMINISTRATOR",
    "CLIENT_ADMINISTRATOR",
    "MATTER_ADMINISTRATOR",
)


def _catalog_id(kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(RBAC_NAMESPACE, f"{kind}:{key}")


def upgrade() -> None:
    op.create_table(
        "matter_search_export_job",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("matter_id", sa.Uuid(), sa.ForeignKey("matter.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "search_index_generation_id",
            sa.Uuid(),
            sa.ForeignKey("search_index_generation.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("search_definition", sa.JSON(), nullable=False),
        sa.Column("search_definition_hash", sa.String(length=64), nullable=False),
        sa.Column("search_index_snapshot", sa.JSON(), nullable=False),
        sa.Column("field_snapshot", sa.JSON(), nullable=False),
        sa.Column("field_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("csv_policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("requested_filename", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="QUEUED"),
        sa.Column(
            "current_workflow_run_id",
            sa.Uuid(),
            sa.ForeignKey("workflow_run.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("workflow_id", sa.String(length=255), nullable=False, unique=True),
        sa.Column("current_stage", sa.String(length=100), nullable=True),
        sa.Column("matched_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("exported_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("output_content_hash", sa.String(length=64), nullable=True),
        sa.Column("output_byte_length", sa.BigInteger(), nullable=True),
        sa.Column("output_filename", sa.String(length=255), nullable=True),
        sa.Column("snapshot_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("failure_retriable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("app_user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_matter_search_export_status",
        ),
        sa.CheckConstraint(
            "matched_count >= 0 AND exported_count >= 0 AND page_count >= 0 "
            "AND (output_byte_length IS NULL OR output_byte_length >= 0)",
            name="ck_matter_search_export_counts",
        ),
        sa.UniqueConstraint(
            "matter_id",
            "created_by_user_id",
            "idempotency_key",
            name="uq_matter_search_export_idempotency",
        ),
    )
    op.create_index("ix_matter_search_export_job_tenant_id", "matter_search_export_job", ["tenant_id"])
    op.create_index("ix_matter_search_export_job_matter_id", "matter_search_export_job", ["matter_id"])
    op.create_index(
        "ix_matter_search_export_job_search_index_generation_id",
        "matter_search_export_job",
        ["search_index_generation_id"],
    )
    op.create_index(
        "ix_matter_search_export_job_current_workflow_run_id",
        "matter_search_export_job",
        ["current_workflow_run_id"],
    )
    op.create_index("ix_matter_search_export_job_output_artifact_id", "matter_search_export_job", ["output_artifact_id"])
    op.create_index("ix_matter_search_export_job_expires_at", "matter_search_export_job", ["expires_at"])
    op.create_index("ix_matter_search_export_job_created_by_user_id", "matter_search_export_job", ["created_by_user_id"])
    op.create_index("ix_matter_search_export_job_search_definition_hash", "matter_search_export_job", ["search_definition_hash"])
    op.create_index("ix_matter_search_export_job_field_snapshot_hash", "matter_search_export_job", ["field_snapshot_hash"])
    op.create_index("ix_matter_search_export_matter_created", "matter_search_export_job", ["matter_id", "created_at"])
    op.create_index(
        "ix_matter_search_export_matter_status_created",
        "matter_search_export_job",
        ["matter_id", "status", "created_at"],
    )

    connection = op.get_bind()
    now = datetime.now(timezone.utc)
    permission_id = _catalog_id("permission", "document.export")
    connection.execute(
        sa.text(
            "INSERT INTO permission "
            "(id, key, display_name, description, category, resource_type, dangerous, status, created_at, updated_at) "
            "VALUES (:id, 'document.export', 'Export', 'Allows document export.', 'Documents', 'MATTER', "
            "true, 'ACTIVE', :now, :now) ON CONFLICT (key) DO NOTHING"
        ),
        {"id": permission_id, "now": now},
    )
    for role_key in SYSTEM_ROLE_KEYS:
        role_id = _catalog_id("system-role", role_key)
        connection.execute(
            sa.text(
                "INSERT INTO role_permission (role_id, permission_id, created_at) "
                "VALUES (:role_id, :permission_id, :now) ON CONFLICT DO NOTHING"
            ),
            {"role_id": role_id, "permission_id": permission_id, "now": now},
        )
    tenant_ids = list(connection.execute(sa.text("SELECT id FROM tenant")).scalars())
    for tenant_id in tenant_ids:
        connection.execute(
            sa.text(
                "INSERT INTO audit_record "
                "(id, tenant_id, actor_user_id, action, target_type, target_id, details, created_at) "
                "VALUES (:id, :tenant_id, NULL, 'authorization.permission.catalog_updated', "
                "'tenant', :tenant_id, CAST(:details AS jsonb), :now)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "details": json.dumps(
                    {"permission_keys": ["document.export"], "reason": "search_result_exports"}
                ),
                "now": now,
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    permission_id = _catalog_id("permission", "document.export")
    connection.execute(sa.text("DELETE FROM role_permission WHERE permission_id = :id"), {"id": permission_id})
    connection.execute(sa.text("DELETE FROM permission WHERE id = :id"), {"id": permission_id})
    op.drop_table("matter_search_export_job")
