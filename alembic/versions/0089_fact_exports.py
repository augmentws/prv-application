"""Add durable fact export jobs.

Revision ID: 0089_fact_exports
Revises: 0088_fact_ledger_entities
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0089_fact_exports"
down_revision: str | None = "0088_fact_ledger_entities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matter_fact_export_job",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("matter_id", sa.Uuid(), sa.ForeignKey("matter.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filter_snapshot", sa.JSON(), nullable=False),
        sa.Column("filter_hash", sa.String(length=64), nullable=False),
        sa.Column("csv_policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("requested_filename", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column(
            "current_workflow_run_id",
            sa.Uuid(),
            sa.ForeignKey("workflow_run.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("current_stage", sa.String(length=100), nullable=True),
        sa.Column("fact_count", sa.Integer(), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False),
        sa.Column("facts_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("facts_content_hash", sa.String(length=64), nullable=True),
        sa.Column("facts_byte_length", sa.BigInteger(), nullable=True),
        sa.Column("facts_filename", sa.String(length=255), nullable=True),
        sa.Column("evidence_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("evidence_content_hash", sa.String(length=64), nullable=True),
        sa.Column("evidence_byte_length", sa.BigInteger(), nullable=True),
        sa.Column("evidence_filename", sa.String(length=255), nullable=True),
        sa.Column("snapshot_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("failure_retriable", sa.Boolean(), nullable=False),
        sa.Column(
            "created_by_user_id", sa.Uuid(), sa.ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_matter_fact_export_status",
        ),
        sa.CheckConstraint("fact_count >= 0 AND evidence_count >= 0", name="ck_matter_fact_export_counts"),
        sa.UniqueConstraint(
            "matter_id", "created_by_user_id", "idempotency_key", name="uq_matter_fact_export_idempotency"
        ),
        sa.UniqueConstraint("workflow_id"),
    )
    op.create_index("ix_matter_fact_export_matter_created", "matter_fact_export_job", ["matter_id", "created_at"])
    for column in ("tenant_id", "matter_id", "current_workflow_run_id", "created_by_user_id"):
        op.create_index(f"ix_matter_fact_export_job_{column}", "matter_fact_export_job", [column])


def downgrade() -> None:
    op.drop_table("matter_fact_export_job")
