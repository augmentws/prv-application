"""Add durable provider batches for Matter Definition assessments.

Revision ID: 0034_assessment_batches
Revises: 0033_lowercase_metadata
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034_assessment_batches"
down_revision: str | None = "0033_lowercase_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matter_definition_assessment_provider_batch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assessment_run_id", sa.Uuid(), nullable=False),
        sa.Column("review_batch_run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("document_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=500), nullable=False),
        sa.Column("provider_batch_id", sa.String(length=500), nullable=True),
        sa.Column("provider_status", sa.String(length=80), nullable=True),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'SUBMITTED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', "
            "'FAILED', 'CANCELED')",
            name="ck_definition_assessment_provider_batch_status",
        ),
        sa.CheckConstraint(
            "ordinal > 0 AND request_count >= 0",
            name="ck_definition_assessment_provider_batch_counts",
        ),
        sa.ForeignKeyConstraint(
            ["assessment_run_id"],
            ["matter_definition_assessment_run.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["review_batch_run_id"], ["review_batch_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_batch_run_id",
            "ordinal",
            name="uq_definition_assessment_provider_batch_ordinal",
        ),
        sa.UniqueConstraint("provider_batch_id", name="uq_definition_assessment_provider_batch_id"),
    )
    op.create_index(
        "ix_def_assess_provider_batch_assessment",
        "matter_definition_assessment_provider_batch",
        ["assessment_run_id", "status"],
    )
    op.create_index(
        "ix_def_assess_provider_batch_review_run",
        "matter_definition_assessment_provider_batch",
        ["review_batch_run_id"],
    )


def downgrade() -> None:
    op.drop_table("matter_definition_assessment_provider_batch")
