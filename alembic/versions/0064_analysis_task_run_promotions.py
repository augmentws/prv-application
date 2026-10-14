"""Add durable promotion of analysis-task run coding.

Revision ID: 0064_analysis_promotions
Revises: 0063_analysis_destinations
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0064_analysis_promotions"
down_revision: str | None = "0063_analysis_destinations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "review_batch_run_promotion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("review_batch_id", sa.Uuid(), nullable=False),
        sa.Column("review_batch_run_id", sa.Uuid(), nullable=False),
        sa.Column("matter_analysis_task_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("destination_snapshot", json_document, nullable=False),
        sa.Column("total_document_count", sa.Integer(), nullable=False),
        sa.Column("eligible_document_count", sa.Integer(), nullable=False),
        sa.Column("processed_document_count", sa.Integer(), nullable=False),
        sa.Column("skipped_document_count", sa.Integer(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("dbos_workflow_id", sa.String(length=255), nullable=False),
        sa.Column("search_operation_id", sa.Uuid(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="ck_review_batch_run_promotion_status",
        ),
        sa.CheckConstraint("mode = 'REPLACE'", name="ck_review_batch_run_promotion_mode"),
        sa.CheckConstraint(
            "total_document_count >= 0 AND eligible_document_count >= 0 "
            "AND processed_document_count >= 0 AND skipped_document_count >= 0 "
            "AND event_count >= 0",
            name="ck_review_batch_run_promotion_counts",
        ),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["review_batch_id"], ["review_batch.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["review_batch_run_id"], ["review_batch_run.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["matter_analysis_task_version_id"],
            ["matter_analysis_task_version.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["search_operation_id"], ["search_projection_operation.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dbos_workflow_id"),
        sa.UniqueConstraint("review_batch_run_id", name="uq_review_batch_run_promotion_run"),
    )
    op.create_index(
        "ix_review_batch_run_promotion_matter_id",
        "review_batch_run_promotion",
        ["matter_id"],
    )
    op.create_index(
        "ix_review_batch_run_promotion_review_batch_id",
        "review_batch_run_promotion",
        ["review_batch_id"],
    )
    op.create_index(
        "ix_review_batch_run_promotion_review_batch_run_id",
        "review_batch_run_promotion",
        ["review_batch_run_id"],
    )
    op.create_index(
        "ix_review_batch_run_promotion_task_version_id",
        "review_batch_run_promotion",
        ["matter_analysis_task_version_id"],
    )
    op.create_index(
        "ix_review_batch_run_promotion_status",
        "review_batch_run_promotion",
        ["status"],
    )
    op.create_index(
        "ix_review_batch_run_promotion_created_by_user_id",
        "review_batch_run_promotion",
        ["created_by_user_id"],
    )


def downgrade() -> None:
    op.drop_table("review_batch_run_promotion")
