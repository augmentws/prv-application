"""Add durable matter document import jobs and links.

Revision ID: 0005_matter_documents
Revises: 0004_matter_configuration
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_matter_documents"
down_revision: str | None = "0004_matter_configuration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "matter_document_import_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("source_collection_id", sa.Uuid(), nullable=False),
        sa.Column("selection_type", sa.String(length=20), nullable=False),
        sa.Column("selection", sa.JSON(), nullable=False),
        sa.Column("selection_summary", sa.String(length=1000), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("artifact_selection_id", sa.Uuid(), nullable=True),
        sa.Column("matched_count", sa.Integer(), nullable=False),
        sa.Column("batch_count", sa.Integer(), nullable=False),
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("added_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.CheckConstraint("selection_type IN ('QUERY', 'EXPLICIT')", name="ck_matter_import_selection_type"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'SNAPSHOTTING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_matter_import_status",
        ),
        sa.CheckConstraint(
            "matched_count >= 0 AND batch_count >= 0 AND processed_count >= 0 AND "
            "added_count >= 0 AND duplicate_count >= 0 AND failed_count >= 0",
            name="ck_matter_import_counts",
        ),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
    )
    op.create_index("ix_matter_document_import_job_matter_id", "matter_document_import_job", ["matter_id"])
    op.create_index("ix_matter_document_import_job_source_collection_id", "matter_document_import_job", ["source_collection_id"])
    op.create_index("ix_matter_document_import_job_status", "matter_document_import_job", ["status"])
    op.create_index("ix_matter_document_import_job_created_by_user_id", "matter_document_import_job", ["created_by_user_id"])
    op.create_index("ix_matter_import_matter_created", "matter_document_import_job", ["matter_id", "created_at"])

    op.create_table(
        "matter_document_import_batch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("batch_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("added_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        *timestamps(),
        sa.CheckConstraint("status IN ('QUEUED', 'COMPLETED', 'FAILED')", name="ck_matter_import_batch_status"),
        sa.CheckConstraint(
            "item_count >= 0 AND added_count >= 0 AND duplicate_count >= 0 AND failed_count >= 0",
            name="ck_matter_import_batch_counts",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["matter_document_import_job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "batch_number", name="uq_matter_import_batch_number"),
    )
    op.create_index("ix_matter_document_import_batch_job_id", "matter_document_import_batch", ["job_id"])

    op.create_table(
        "matter_document",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("source_collection_id", sa.Uuid(), nullable=False),
        sa.Column("collection_item_id", sa.Uuid(), nullable=False),
        sa.Column("added_by_import_job_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["added_by_import_job_id"], ["matter_document_import_job.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("matter_id", "collection_item_id", name="uq_matter_document_source_item"),
    )
    op.create_index("ix_matter_document_matter_id", "matter_document", ["matter_id"])
    op.create_index("ix_matter_document_source_collection_id", "matter_document", ["source_collection_id"])
    op.create_index("ix_matter_document_collection_item_id", "matter_document", ["collection_item_id"])
    op.create_index("ix_matter_document_added_by_import_job_id", "matter_document", ["added_by_import_job_id"])
    op.create_index("ix_matter_document_matter_created", "matter_document", ["matter_id", "created_at"])


def downgrade() -> None:
    op.drop_table("matter_document")
    op.drop_table("matter_document_import_batch")
    op.drop_table("matter_document_import_job")
