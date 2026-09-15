"""Add durable matter topic clustering jobs and assignments.

Revision ID: 0014_matter_topic_jobs
Revises: 0013_agent_run_actor
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_matter_topic_jobs"
down_revision: str | None = "0013_agent_run_actor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matter_topic_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_job_id", sa.Uuid(), nullable=False),
        sa.Column("metadata_definition_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("operating_mode", sa.String(length=20), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("requested_topic_count", sa.Integer(), nullable=True),
        sa.Column("assignment_mode", sa.String(length=20), nullable=False),
        sa.Column("configuration_hash", sa.String(length=64), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("sampled_chunk_count", sa.Integer(), nullable=False),
        sa.Column("processed_document_count", sa.Integer(), nullable=False),
        sa.Column("assigned_document_count", sa.Integer(), nullable=False),
        sa.Column("topic_count", sa.Integer(), nullable=False),
        sa.Column("outlier_document_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'SAMPLING', 'CLUSTERING', 'PUBLISHING', 'COMPLETED', "
            "'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_matter_topic_job_status",
        ),
        sa.CheckConstraint("operating_mode IN ('AUTO', 'FIXED')", name="ck_matter_topic_job_operating_mode"),
        sa.CheckConstraint("assignment_mode IN ('REPLACE', 'APPEND')", name="ck_matter_topic_job_assignment_mode"),
        sa.CheckConstraint(
            "sample_size > 0 AND document_count >= 0 AND sampled_chunk_count >= 0 AND "
            "processed_document_count >= 0 AND assigned_document_count >= 0 AND topic_count >= 0 AND "
            "outlier_document_count >= 0 AND failed_count >= 0",
            name="ck_matter_topic_job_counts",
        ),
        sa.CheckConstraint(
            "(operating_mode = 'FIXED' AND requested_topic_count IS NOT NULL AND requested_topic_count >= 2) OR "
            "(operating_mode = 'AUTO' AND requested_topic_count IS NULL)",
            name="ck_matter_topic_job_requested_count",
        ),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["embedding_job_id"], ["matter_embedding_job.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["metadata_definition_id"], ["metadata_definition.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
    )
    op.create_index("ix_matter_topic_job_matter_id", "matter_topic_job", ["matter_id"])
    op.create_index("ix_matter_topic_job_embedding_job_id", "matter_topic_job", ["embedding_job_id"])
    op.create_index("ix_matter_topic_job_metadata_definition_id", "matter_topic_job", ["metadata_definition_id"])
    op.create_index("ix_matter_topic_job_status", "matter_topic_job", ["status"])
    op.create_index("ix_matter_topic_job_configuration_hash", "matter_topic_job", ["configuration_hash"])
    op.create_index("ix_matter_topic_job_created_by_user_id", "matter_topic_job", ["created_by_user_id"])
    op.create_index("ix_matter_topic_job_matter_created", "matter_topic_job", ["matter_id", "created_at"])
    op.create_index(
        "uq_matter_topic_job_active",
        "matter_topic_job",
        ["matter_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'SAMPLING', 'CLUSTERING', 'PUBLISHING')"),
        sqlite_where=sa.text("status IN ('QUEUED', 'SAMPLING', 'CLUSTERING', 'PUBLISHING')"),
    )

    op.create_table(
        "matter_topic_cluster",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("topic_key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("centroid", sa.JSON(), nullable=False),
        sa.Column("sampled_chunk_count", sa.Integer(), nullable=False),
        sa.Column("assigned_chunk_count", sa.Integer(), nullable=False),
        sa.Column("assigned_document_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0 AND sampled_chunk_count >= 0 AND assigned_chunk_count >= 0 AND assigned_document_count >= 0",
            name="ck_matter_topic_cluster_counts",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["matter_topic_job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "ordinal", name="uq_matter_topic_cluster_ordinal"),
        sa.UniqueConstraint("job_id", "topic_key", name="uq_matter_topic_cluster_key"),
    )
    op.create_index("ix_matter_topic_cluster_job_id", "matter_topic_cluster", ["job_id"])

    op.create_table(
        "matter_topic_batch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("batch_number", sa.Integer(), nullable=False),
        sa.Column("document_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("assigned_count", sa.Integer(), nullable=False),
        sa.Column("outlier_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("metadata_applied", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_matter_topic_batch_status",
        ),
        sa.CheckConstraint(
            "item_count >= 0 AND processed_count >= 0 AND assigned_count >= 0 AND "
            "outlier_count >= 0 AND failed_count >= 0",
            name="ck_matter_topic_batch_counts",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["matter_topic_job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "batch_number", name="uq_matter_topic_batch_number"),
    )
    op.create_index("ix_matter_topic_batch_job_id", "matter_topic_batch", ["job_id"])

    op.create_table(
        "matter_topic_assignment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("matter_document_id", sa.Uuid(), nullable=False),
        sa.Column("topic_cluster_id", sa.Uuid(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("assigned_chunk_count", sa.Integer(), nullable=False),
        sa.Column("supporting_chunk_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_matter_topic_assignment_confidence"),
        sa.ForeignKeyConstraint(["job_id"], ["matter_topic_job.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matter_document_id"], ["matter_document.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["topic_cluster_id"], ["matter_topic_cluster.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "matter_document_id", "topic_cluster_id", name="uq_matter_topic_assignment"),
    )
    op.create_index("ix_matter_topic_assignment_job_id", "matter_topic_assignment", ["job_id"])
    op.create_index("ix_matter_topic_assignment_matter_document_id", "matter_topic_assignment", ["matter_document_id"])
    op.create_index("ix_matter_topic_assignment_topic_cluster_id", "matter_topic_assignment", ["topic_cluster_id"])


def downgrade() -> None:
    op.drop_table("matter_topic_assignment")
    op.drop_table("matter_topic_batch")
    op.drop_table("matter_topic_cluster")
    op.drop_table("matter_topic_job")
