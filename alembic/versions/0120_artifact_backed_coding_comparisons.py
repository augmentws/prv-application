"""Add artifact-backed matter coding comparison jobs.

Revision ID: 0120_artifact_coding_compare
Revises: 0119_skill_run_error_details
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0120_artifact_coding_compare"
down_revision: str | None = "0119_skill_run_error_details"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matter_coding_comparison",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("configuration_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("current_workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("current_stage", sa.String(length=100), nullable=True),
        sa.Column("population_document_count", sa.BigInteger(), nullable=False),
        sa.Column("compared_document_count", sa.BigInteger(), nullable=False),
        sa.Column("population_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("population_content_hash", sa.String(length=64), nullable=True),
        sa.Column("population_byte_length", sa.BigInteger(), nullable=True),
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
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_matter_coding_comparison_status",
        ),
        sa.CheckConstraint(
            "population_document_count >= 0 AND compared_document_count >= 0 "
            "AND compared_document_count <= population_document_count",
            name="ck_matter_coding_comparison_counts",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["current_workflow_run_id"], ["workflow_run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
        sa.UniqueConstraint(
            "matter_id", "created_by_user_id", "idempotency_key", name="uq_matter_coding_comparison_idempotency"
        ),
    )
    op.create_index(
        "ix_matter_coding_comparison_matter_created", "matter_coding_comparison", ["matter_id", "created_at"]
    )
    op.create_index(
        "ix_matter_coding_comparison_matter_status",
        "matter_coding_comparison",
        ["matter_id", "status", "created_at"],
    )
    for column in (
        "tenant_id",
        "matter_id",
        "configuration_hash",
        "status",
        "current_workflow_run_id",
        "population_artifact_id",
        "result_artifact_id",
        "created_by_user_id",
    ):
        op.create_index(f"ix_matter_coding_comparison_{column}", "matter_coding_comparison", [column])


def downgrade() -> None:
    op.drop_table("matter_coding_comparison")
