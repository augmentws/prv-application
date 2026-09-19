"""Add durable collection deletion jobs and blob cleanup manifests.

Revision ID: 0005_collection_deletion
Revises: 0004_collection_text_processing
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_collection_deletion"
down_revision: str | None = "0004_collection_text_processing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_artifact_collection_status", "client_collection", type_="check")
    op.create_check_constraint(
        "ck_artifact_collection_status",
        "client_collection",
        "status IN ('OPEN', 'SEALED', 'ARCHIVED', 'DELETING')",
    )
    op.create_table(
        "collection_deletion_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("collection_name", sa.String(length=200), nullable=False),
        sa.Column("previous_collection_status", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("artifact_count", sa.Integer(), nullable=False),
        sa.Column("blob_count", sa.Integer(), nullable=False),
        sa.Column("deleted_item_count", sa.Integer(), nullable=False),
        sa.Column("deleted_artifact_count", sa.Integer(), nullable=False),
        sa.Column("deleted_blob_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'VALIDATING', 'DELETING_DATABASE_ROWS', 'DELETING_BLOBS', "
            "'COMPLETED', 'FAILED')",
            name="ck_collection_deletion_job_status",
        ),
        sa.CheckConstraint(
            "item_count >= 0 AND artifact_count >= 0 AND blob_count >= 0 AND "
            "deleted_item_count >= 0 AND deleted_artifact_count >= 0 AND deleted_blob_count >= 0",
            name="ck_collection_deletion_job_counts",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
    )
    op.create_index("ix_collection_deletion_job_collection_id", "collection_deletion_job", ["collection_id"])
    op.create_index("ix_collection_deletion_job_tenant_id", "collection_deletion_job", ["tenant_id"])
    op.create_index("ix_collection_deletion_job_client_id", "collection_deletion_job", ["client_id"])
    op.create_index("ix_collection_deletion_job_status", "collection_deletion_job", ["status"])
    op.create_index(
        "ix_collection_deletion_job_collection_created",
        "collection_deletion_job",
        ["collection_id", "created_at"],
    )
    op.create_index(
        "uq_collection_deletion_job_active",
        "collection_deletion_job",
        ["collection_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('QUEUED', 'VALIDATING', 'DELETING_DATABASE_ROWS', 'DELETING_BLOBS')"
        ),
    )
    op.create_table(
        "collection_deletion_blob",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deletion_job_id", sa.Uuid(), nullable=False),
        sa.Column("content_blob_id", sa.Uuid(), nullable=False),
        sa.Column("bucket_name", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=1000), nullable=False),
        sa.Column("byte_length", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('PENDING', 'DELETED')", name="ck_collection_deletion_blob_status"),
        sa.ForeignKeyConstraint(
            ["deletion_job_id"], ["collection_deletion_job.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deletion_job_id", "bucket_name", "storage_key", name="uq_collection_deletion_blob_key"
        ),
    )
    op.create_index(
        "ix_collection_deletion_blob_deletion_job_id",
        "collection_deletion_blob",
        ["deletion_job_id"],
    )
    op.create_index(
        "ix_collection_deletion_blob_job_status",
        "collection_deletion_blob",
        ["deletion_job_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("collection_deletion_blob")
    op.drop_table("collection_deletion_job")
    op.drop_constraint("ck_artifact_collection_status", "client_collection", type_="check")
    op.create_check_constraint(
        "ck_artifact_collection_status",
        "client_collection",
        "status IN ('OPEN', 'SEALED', 'ARCHIVED')",
    )
