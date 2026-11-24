"""Add artifact-backed matter identity analysis jobs.

Revision ID: 0122_matter_identity_analysis
Revises: 0121_all_saved_search_batches
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0122_matter_identity_analysis"
down_revision: str | None = "0121_all_saved_search_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matter_identity_analysis",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("current_workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("current_stage", sa.String(length=100), nullable=True),
        sa.Column("total_document_count", sa.BigInteger(), nullable=False),
        sa.Column("processed_document_count", sa.BigInteger(), nullable=False),
        sa.Column("documents_with_participants_count", sa.BigInteger(), nullable=False),
        sa.Column("identity_count", sa.BigInteger(), nullable=False),
        sa.Column("domain_count", sa.BigInteger(), nullable=False),
        sa.Column("invalid_participant_count", sa.BigInteger(), nullable=False),
        sa.Column("result_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("result_content_hash", sa.String(length=64), nullable=True),
        sa.Column("result_byte_length", sa.BigInteger(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("failure_retriable", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_matter_identity_analysis_status",
        ),
        sa.CheckConstraint(
            "total_document_count >= 0 AND processed_document_count >= 0 "
            "AND processed_document_count <= total_document_count "
            "AND documents_with_participants_count >= 0 "
            "AND identity_count >= 0 AND domain_count >= 0 AND invalid_participant_count >= 0",
            name="ck_matter_identity_analysis_counts",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["current_workflow_run_id"], ["workflow_run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
    )
    op.create_index(
        "ix_matter_identity_analysis_matter_created", "matter_identity_analysis", ["matter_id", "created_at"]
    )
    op.create_index(
        "ix_matter_identity_analysis_matter_status",
        "matter_identity_analysis",
        ["matter_id", "status", "created_at"],
    )
    op.create_index(
        "uq_matter_identity_analysis_active",
        "matter_identity_analysis",
        ["matter_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
        sqlite_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )
    for column in (
        "tenant_id",
        "matter_id",
        "status",
        "current_workflow_run_id",
        "result_artifact_id",
        "created_by_user_id",
    ):
        op.create_index(f"ix_matter_identity_analysis_{column}", "matter_identity_analysis", [column])


def downgrade() -> None:
    op.drop_table("matter_identity_analysis")
