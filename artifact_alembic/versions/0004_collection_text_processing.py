"""Add collection text processing profiles, runs, and normalized text artifacts.

Revision ID: 0004_collection_text_processing
Revises: 0003_derived_artifact_identity
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_collection_text_processing"
down_revision: str | None = "0003_derived_artifact_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("client_collection", sa.Column("active_text_processing_run_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_client_collection_active_text_processing_run_id",
        "client_collection",
        ["active_text_processing_run_id"],
    )
    op.create_table(
        "collection_text_processing_profile",
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("custom_rules", sa.JSON(), nullable=False),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["collection_id"], ["client_collection.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("collection_id"),
    )
    op.create_table(
        "collection_text_processing_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("processor_version", sa.String(length=100), nullable=False),
        sa.Column("profile_revision", sa.Integer(), nullable=False),
        sa.Column("rules_snapshot", sa.JSON(), nullable=False),
        sa.Column("configuration_hash", sa.String(length=64), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("created_count", sa.Integer(), nullable=False),
        sa.Column("reused_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED')",
            name="ck_collection_text_processing_run_status",
        ),
        sa.ForeignKeyConstraint(["collection_id"], ["client_collection.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_collection_text_processing_run_collection_id",
        "collection_text_processing_run",
        ["collection_id"],
    )
    op.create_index(
        "ix_collection_text_processing_run_configuration_hash",
        "collection_text_processing_run",
        ["configuration_hash"],
    )
    op.create_index(
        "ix_collection_text_processing_run_collection_created",
        "collection_text_processing_run",
        ["collection_id", "created_at"],
    )

    op.drop_constraint("ck_artifact_type", "artifact", type_="check")
    op.create_check_constraint(
        "ck_artifact_type",
        "artifact",
        "artifact_type IN ('SOURCE_CONTAINER', 'NATIVE_FILE', 'EXTRACTED_TEXT', 'OCR_TEXT', "
        "'DOCUMENT_IMAGE', 'NORMALIZED_TEXT', 'CHUNK_SET', 'DOCUMENT_VECTOR', 'CHUNK_VECTOR_SET', "
        "'SUMMARY', 'THUMBNAIL')",
    )
    op.drop_constraint("ck_collection_item_artifact_role", "collection_item_artifact", type_="check")
    op.create_check_constraint(
        "ck_collection_item_artifact_role",
        "collection_item_artifact",
        "artifact_role IN ('NATIVE', 'EXTRACTED_TEXT', 'OCR_TEXT', 'DOCUMENT_IMAGE', 'NORMALIZED_TEXT', "
        "'CHUNK_SET', 'DOCUMENT_VECTOR', 'CHUNK_VECTOR_SET', 'SUMMARY', 'THUMBNAIL')",
    )
    op.drop_constraint("ck_artifact_lineage_relationship", "artifact_lineage", type_="check")
    op.create_check_constraint(
        "ck_artifact_lineage_relationship",
        "artifact_lineage",
        "relationship IN ('DERIVED_FROM', 'EXTRACTED_FROM', 'CHUNKED_FROM', 'EMBEDDED_FROM', "
        "'EXTRACTED_FROM_CONTAINER', 'NORMALIZED_FROM')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_artifact_lineage_relationship", "artifact_lineage", type_="check")
    op.create_check_constraint(
        "ck_artifact_lineage_relationship",
        "artifact_lineage",
        "relationship IN ('DERIVED_FROM', 'EXTRACTED_FROM', 'CHUNKED_FROM', 'EMBEDDED_FROM', "
        "'EXTRACTED_FROM_CONTAINER')",
    )
    op.drop_constraint("ck_collection_item_artifact_role", "collection_item_artifact", type_="check")
    op.create_check_constraint(
        "ck_collection_item_artifact_role",
        "collection_item_artifact",
        "artifact_role IN ('NATIVE', 'EXTRACTED_TEXT', 'OCR_TEXT', 'DOCUMENT_IMAGE', 'CHUNK_SET', "
        "'DOCUMENT_VECTOR', 'CHUNK_VECTOR_SET', 'SUMMARY', 'THUMBNAIL')",
    )
    op.drop_constraint("ck_artifact_type", "artifact", type_="check")
    op.create_check_constraint(
        "ck_artifact_type",
        "artifact",
        "artifact_type IN ('SOURCE_CONTAINER', 'NATIVE_FILE', 'EXTRACTED_TEXT', 'OCR_TEXT', "
        "'DOCUMENT_IMAGE', 'CHUNK_SET', 'DOCUMENT_VECTOR', 'CHUNK_VECTOR_SET', 'SUMMARY', 'THUMBNAIL')",
    )
    op.drop_table("collection_text_processing_run")
    op.drop_table("collection_text_processing_profile")
    op.drop_index("ix_client_collection_active_text_processing_run_id", table_name="client_collection")
    op.drop_column("client_collection", "active_text_processing_run_id")
