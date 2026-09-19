"""Add review and approval between topic discovery and application.

Revision ID: 0020_topic_proposal_review
Revises: 0019_reindex_confirmation
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_topic_proposal_review"
down_revision: str | None = "0019_reindex_confirmation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("uq_matter_topic_job_active", table_name="matter_topic_job")
    op.drop_constraint("ck_matter_topic_job_status", "matter_topic_job", type_="check")
    op.create_check_constraint(
        "ck_matter_topic_job_status",
        "matter_topic_job",
        "status IN ('QUEUED', 'SAMPLING', 'CLUSTERING', 'AWAITING_REVIEW', 'PUBLISHING', "
        "'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
    )
    op.create_index(
        "uq_matter_topic_job_active",
        "matter_topic_job",
        ["matter_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('QUEUED', 'SAMPLING', 'CLUSTERING', 'AWAITING_REVIEW', 'PUBLISHING')"
        ),
    )
    op.add_column("matter_topic_job", sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True))
    op.add_column("matter_topic_job", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_matter_topic_job_reviewed_by_user_id_app_user",
        "matter_topic_job",
        "app_user",
        ["reviewed_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_matter_topic_job_reviewed_by_user_id",
        "matter_topic_job",
        ["reviewed_by_user_id"],
    )
    op.add_column(
        "matter_topic_cluster",
        sa.Column("representative_excerpts", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
    )
    op.add_column(
        "matter_topic_cluster",
        sa.Column("included", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.execute(
        "UPDATE matter_topic_job SET status = 'CANCELED', canceled_at = COALESCE(canceled_at, CURRENT_TIMESTAMP) "
        "WHERE status = 'AWAITING_REVIEW'"
    )
    op.drop_column("matter_topic_cluster", "included")
    op.drop_column("matter_topic_cluster", "representative_excerpts")
    op.drop_index("ix_matter_topic_job_reviewed_by_user_id", table_name="matter_topic_job")
    op.drop_constraint(
        "fk_matter_topic_job_reviewed_by_user_id_app_user",
        "matter_topic_job",
        type_="foreignkey",
    )
    op.drop_column("matter_topic_job", "reviewed_at")
    op.drop_column("matter_topic_job", "reviewed_by_user_id")
    op.drop_index("uq_matter_topic_job_active", table_name="matter_topic_job")
    op.drop_constraint("ck_matter_topic_job_status", "matter_topic_job", type_="check")
    op.create_check_constraint(
        "ck_matter_topic_job_status",
        "matter_topic_job",
        "status IN ('QUEUED', 'SAMPLING', 'CLUSTERING', 'PUBLISHING', 'COMPLETED', "
        "'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
    )
    op.create_index(
        "uq_matter_topic_job_active",
        "matter_topic_job",
        ["matter_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'SAMPLING', 'CLUSTERING', 'PUBLISHING')"),
    )
