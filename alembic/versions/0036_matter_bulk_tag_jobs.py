"""Add durable matter bulk tag jobs.

Revision ID: 0036_matter_bulk_tags
Revises: 0035_assessment_refinement
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0036_matter_bulk_tags"
down_revision: str | None = "0035_assessment_refinement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matter_bulk_tag_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("metadata_definition_id", sa.Uuid(), nullable=False),
        sa.Column("search_index_generation_id", sa.Uuid(), nullable=True),
        sa.Column("search_index_snapshot", sa.JSON(), nullable=False),
        sa.Column("search_definition", sa.JSON(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("matched_count", sa.Integer(), nullable=False),
        sa.Column("batch_count", sa.Integer(), nullable=False),
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("tagged_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'SNAPSHOTTING', 'RUNNING', 'COMPLETED', "
            "'COMPLETED_WITH_ERRORS', 'FAILED')",
            name="ck_matter_bulk_tag_job_status",
        ),
        sa.CheckConstraint(
            "matched_count >= 0 AND batch_count >= 0 AND processed_count >= 0 AND "
            "tagged_count >= 0 AND failed_count >= 0",
            name="ck_matter_bulk_tag_job_counts",
        ),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["metadata_definition_id"], ["metadata_definition.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["search_index_generation_id"], ["search_index_generation.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
    )
    op.create_index("ix_matter_bulk_tag_job_matter_id", "matter_bulk_tag_job", ["matter_id"])
    op.create_index(
        "ix_matter_bulk_tag_job_metadata_definition_id",
        "matter_bulk_tag_job",
        ["metadata_definition_id"],
    )
    op.create_index(
        "ix_matter_bulk_tag_job_search_index_generation_id",
        "matter_bulk_tag_job",
        ["search_index_generation_id"],
    )
    op.create_index("ix_matter_bulk_tag_job_status", "matter_bulk_tag_job", ["status"])
    op.create_index(
        "ix_matter_bulk_tag_job_created_by_user_id",
        "matter_bulk_tag_job",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_matter_bulk_tag_job_matter_created",
        "matter_bulk_tag_job",
        ["matter_id", "created_at"],
    )

    op.create_table(
        "matter_bulk_tag_batch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("batch_number", sa.Integer(), nullable=False),
        sa.Column("document_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("tagged_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("metadata_applied", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="ck_matter_bulk_tag_batch_status",
        ),
        sa.CheckConstraint(
            "item_count >= 0 AND processed_count >= 0 AND tagged_count >= 0 AND failed_count >= 0",
            name="ck_matter_bulk_tag_batch_counts",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["matter_bulk_tag_job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "batch_number", name="uq_matter_bulk_tag_batch_number"),
    )
    op.create_index("ix_matter_bulk_tag_batch_job_id", "matter_bulk_tag_batch", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_matter_bulk_tag_batch_job_id", table_name="matter_bulk_tag_batch")
    op.drop_table("matter_bulk_tag_batch")
    op.drop_index("ix_matter_bulk_tag_job_matter_created", table_name="matter_bulk_tag_job")
    op.drop_index("ix_matter_bulk_tag_job_created_by_user_id", table_name="matter_bulk_tag_job")
    op.drop_index("ix_matter_bulk_tag_job_status", table_name="matter_bulk_tag_job")
    op.drop_index("ix_matter_bulk_tag_job_search_index_generation_id", table_name="matter_bulk_tag_job")
    op.drop_index("ix_matter_bulk_tag_job_metadata_definition_id", table_name="matter_bulk_tag_job")
    op.drop_index("ix_matter_bulk_tag_job_matter_id", table_name="matter_bulk_tag_job")
    op.drop_table("matter_bulk_tag_job")
