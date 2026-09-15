"""Initial Artifact Service schema.

Revision ID: 0001_artifact
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_artifact"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "tenant_storage",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_slug_snapshot", sa.String(length=80), nullable=False),
        sa.Column("bucket_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_tenant_storage_status"),
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.UniqueConstraint("bucket_name"),
    )
    op.create_table(
        "client_collection",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        *timestamps(),
        sa.CheckConstraint("status IN ('OPEN', 'SEALED', 'ARCHIVED')", name="ck_artifact_collection_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "name", name="uq_artifact_collection_client_name"),
    )
    op.create_index("ix_client_collection_tenant_id", "client_collection", ["tenant_id"])
    op.create_index("ix_client_collection_client_id", "client_collection", ["client_id"])
    op.create_index("ix_artifact_collection_tenant_client", "client_collection", ["tenant_id", "client_id"])

    op.create_table(
        "collection_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("source_item_id", sa.String(length=500), nullable=False),
        sa.Column("record_type", sa.String(length=30), nullable=False),
        sa.Column("original_filename", sa.String(length=500), nullable=False),
        sa.Column("original_extension", sa.String(length=100), nullable=True),
        sa.Column("original_source_path", sa.Text(), nullable=True),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("family_id", sa.Uuid(), nullable=True),
        sa.Column("parent_collection_item_id", sa.Uuid(), nullable=True),
        sa.Column("processing_status", sa.String(length=30), nullable=False),
        sa.Column("raw_metadata", sa.JSON(), nullable=False),
        sa.Column("unmapped_metadata", sa.JSON(), nullable=False),
        *timestamps(),
        sa.CheckConstraint(
            "record_type IN ('EMAIL', 'FILE', 'CHAT', 'TRANSCRIPT', 'OTHER')",
            name="ck_collection_item_record_type",
        ),
        sa.CheckConstraint(
            "processing_status IN ('NOT_PROCESSED', 'METADATA_INCOMPLETE', 'READY', 'FAILED')",
            name="ck_collection_item_processing_status",
        ),
        sa.ForeignKeyConstraint(["collection_id"], ["client_collection.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_collection_item_id"], ["collection_item.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("collection_id", "source_item_id", name="uq_collection_item_source"),
    )
    op.create_index("ix_collection_item_tenant_id", "collection_item", ["tenant_id"])
    op.create_index("ix_collection_item_client_id", "collection_item", ["client_id"])
    op.create_index("ix_collection_item_collection_id", "collection_item", ["collection_id"])
    op.create_index("ix_collection_item_family_id", "collection_item", ["family_id"])
    op.create_index("ix_collection_item_parent_collection_item_id", "collection_item", ["parent_collection_item_id"])
    op.create_index("ix_collection_item_collection_type", "collection_item", ["collection_id", "record_type"])
    op.create_index("ix_collection_item_source_created", "collection_item", ["collection_id", "source_created_at"])
    op.create_index("ix_collection_item_source_modified", "collection_item", ["collection_id", "source_modified_at"])

    op.create_table(
        "collection_item_custodian",
        sa.Column("collection_item_id", sa.Uuid(), nullable=False),
        sa.Column("custodian_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(length=20), nullable=False),
        sa.CheckConstraint("relationship_type IN ('PRIMARY', 'COMMON')", name="ck_item_custodian_relationship"),
        sa.ForeignKeyConstraint(["collection_item_id"], ["collection_item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("collection_item_id", "custodian_id"),
    )
    op.create_index("ix_collection_item_custodian_custodian_id", "collection_item_custodian", ["custodian_id"])

    op.create_table(
        "collection_item_email",
        sa.Column("collection_item_id", sa.Uuid(), nullable=False),
        sa.Column("sender", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_id", sa.String(length=1000), nullable=True),
        sa.ForeignKeyConstraint(["collection_item_id"], ["collection_item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("collection_item_id"),
    )
    op.create_index("ix_collection_item_email_sent_at", "collection_item_email", ["sent_at"])
    op.create_index("ix_collection_item_email_received_at", "collection_item_email", ["received_at"])
    op.create_index("ix_collection_item_email_message_id", "collection_item_email", ["message_id"])

    op.create_table(
        "collection_item_email_recipient",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("collection_item_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_type", sa.String(length=10), nullable=False),
        sa.Column("display_name", sa.String(length=500), nullable=True),
        sa.Column("email_address", sa.String(length=500), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint("recipient_type IN ('TO', 'CC', 'BCC')", name="ck_email_recipient_type"),
        sa.ForeignKeyConstraint(["collection_item_id"], ["collection_item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "collection_item_id", "recipient_type", "ordinal", name="uq_email_recipient_item_type_ordinal"
        ),
    )
    op.create_index(
        "ix_collection_item_email_recipient_collection_item_id",
        "collection_item_email_recipient",
        ["collection_item_id"],
    )
    op.create_index(
        "ix_collection_item_email_recipient_email_address",
        "collection_item_email_recipient",
        ["email_address"],
    )

    op.create_table(
        "content_blob",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("bucket_name", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=1000), nullable=False),
        sa.Column("byte_length", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "sha256", name="uq_content_blob_tenant_sha256"),
    )
    op.create_index("ix_content_blob_tenant_id", "content_blob", ["tenant_id"])
    op.create_index("ix_content_blob_sha256", "content_blob", ["sha256"])

    op.create_table(
        "artifact",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_class", sa.String(length=30), nullable=False),
        sa.Column("artifact_type", sa.String(length=40), nullable=False),
        sa.Column("content_blob_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("byte_length", sa.BigInteger(), nullable=False),
        sa.Column("original_filename", sa.String(length=500), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.CheckConstraint("artifact_class IN ('EVIDENCE', 'DERIVED')", name="ck_artifact_class"),
        sa.CheckConstraint(
            "artifact_type IN ('SOURCE_CONTAINER', 'NATIVE_FILE', 'EXTRACTED_TEXT', 'OCR_TEXT', "
            "'DOCUMENT_IMAGE', 'CHUNK_SET', 'DOCUMENT_VECTOR', 'CHUNK_VECTOR_SET', 'SUMMARY', 'THUMBNAIL')",
            name="ck_artifact_type",
        ),
        sa.CheckConstraint("status IN ('UPLOADING', 'FINALIZED', 'QUARANTINED')", name="ck_artifact_status"),
        sa.ForeignKeyConstraint(["content_blob_id"], ["content_blob.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifact_tenant_id", "artifact", ["tenant_id"])
    op.create_index("ix_artifact_client_id", "artifact", ["client_id"])
    op.create_index("ix_artifact_content_hash", "artifact", ["content_hash"])

    op.create_table(
        "collection_artifact",
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_role", sa.String(length=40), nullable=False),
        sa.CheckConstraint("artifact_role = 'SOURCE_CONTAINER'", name="ck_collection_artifact_role"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["collection_id"], ["client_collection.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("artifact_id"),
    )
    op.create_index("ix_collection_artifact_collection_id", "collection_artifact", ["collection_id"])

    op.create_table(
        "collection_item_artifact",
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("collection_item_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_role", sa.String(length=40), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("processing_run_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "artifact_role IN ('NATIVE', 'EXTRACTED_TEXT', 'OCR_TEXT', 'DOCUMENT_IMAGE', 'CHUNK_SET', "
            "'DOCUMENT_VECTOR', 'CHUNK_VECTOR_SET', 'SUMMARY', 'THUMBNAIL')",
            name="ck_collection_item_artifact_role",
        ),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["collection_item_id"], ["collection_item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("artifact_id"),
    )
    op.create_index(
        "ix_collection_item_artifact_collection_item_id",
        "collection_item_artifact",
        ["collection_item_id"],
    )

    op.create_table(
        "artifact_lineage",
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("source_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("relationship", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "relationship IN ('DERIVED_FROM', 'EXTRACTED_FROM', 'CHUNKED_FROM', 'EMBEDDED_FROM', "
            "'EXTRACTED_FROM_CONTAINER')",
            name="ck_artifact_lineage_relationship",
        ),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_artifact_id"], ["artifact.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("artifact_id", "source_artifact_id"),
    )


def downgrade() -> None:
    op.drop_table("artifact_lineage")
    op.drop_table("collection_item_artifact")
    op.drop_table("collection_artifact")
    op.drop_table("artifact")
    op.drop_table("content_blob")
    op.drop_table("collection_item_email_recipient")
    op.drop_table("collection_item_email")
    op.drop_table("collection_item_custodian")
    op.drop_table("collection_item")
    op.drop_table("client_collection")
    op.drop_table("tenant_storage")
