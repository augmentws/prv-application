"""Add durable matter embedding jobs and frozen batches.

Revision ID: 0011_matter_embedding_jobs
Revises: 0010_agents_matter_definitions
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_matter_embedding_jobs"
down_revision: str | None = "0010_agents_matter_definitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matter_embedding_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("configuration_hash", sa.String(length=64), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("embedding_model", sa.String(length=500), nullable=False),
        sa.Column("embedding_model_revision", sa.String(length=255), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding_normalized", sa.Boolean(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("batch_count", sa.Integer(), nullable=False),
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("embedded_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'PLANNING', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', "
            "'FAILED', 'CANCELED')",
            name="ck_matter_embedding_job_status",
        ),
        sa.CheckConstraint(
            "total_count >= 0 AND batch_count >= 0 AND processed_count >= 0 AND "
            "embedded_count >= 0 AND skipped_count >= 0 AND failed_count >= 0 AND chunk_count >= 0",
            name="ck_matter_embedding_job_counts",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", name="uq_matter_embedding_job_workflow_id"),
    )
    op.create_index("ix_matter_embedding_job_matter_id", "matter_embedding_job", ["matter_id"])
    op.create_index("ix_matter_embedding_job_status", "matter_embedding_job", ["status"])
    op.create_index("ix_matter_embedding_job_configuration_hash", "matter_embedding_job", ["configuration_hash"])
    op.create_index("ix_matter_embedding_job_created_by_user_id", "matter_embedding_job", ["created_by_user_id"])
    op.create_index(
        "ix_matter_embedding_job_matter_created",
        "matter_embedding_job",
        ["matter_id", "created_at"],
    )
    op.create_index(
        "uq_matter_embedding_job_active",
        "matter_embedding_job",
        ["matter_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'PLANNING', 'RUNNING')"),
        sqlite_where=sa.text("status IN ('QUEUED', 'PLANNING', 'RUNNING')"),
    )

    op.create_table(
        "matter_embedding_batch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("batch_number", sa.Integer(), nullable=False),
        sa.Column("document_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("embedded_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_matter_embedding_batch_status",
        ),
        sa.CheckConstraint(
            "item_count >= 0 AND processed_count >= 0 AND embedded_count >= 0 AND "
            "skipped_count >= 0 AND failed_count >= 0 AND chunk_count >= 0",
            name="ck_matter_embedding_batch_counts",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["matter_embedding_job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "batch_number", name="uq_matter_embedding_batch_number"),
    )
    op.create_index("ix_matter_embedding_batch_job_id", "matter_embedding_batch", ["job_id"])


def downgrade() -> None:
    op.drop_table("matter_embedding_batch")
    op.drop_table("matter_embedding_job")
