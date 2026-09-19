"""Add review batches, coding snapshots, and isolated run results.

Revision ID: 0016_review_batches
Revises: 0015_matter_saved_searches
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_review_batches"
down_revision: str | None = "0015_matter_saved_searches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_batch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("selection_type", sa.String(30), nullable=False),
        sa.Column("selection_definition", sa.JSON(), nullable=False),
        sa.Column("source_batch_id", sa.Uuid(), nullable=True),
        sa.Column("search_index_generation_id", sa.Uuid(), nullable=True),
        sa.Column("sample_size", sa.Integer(), nullable=True),
        sa.Column("random_seed", sa.String(100), nullable=True),
        sa.Column("assigned_user_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewer_value_visibility", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("workflow_id", sa.String(255), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH')",
            name="ck_review_batch_selection_type",
        ),
        sa.CheckConstraint(
            "reviewer_value_visibility IN ('OWN_VALUES', 'ALL_REVIEWER_VALUES')",
            name="ck_review_batch_value_visibility",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'BUILDING', 'READY', 'FAILED', 'ARCHIVED')", name="ck_review_batch_status"
        ),
        sa.CheckConstraint("document_count >= 0", name="ck_review_batch_document_count"),
        sa.CheckConstraint("sample_size IS NULL OR sample_size > 0", name="ck_review_batch_sample_size"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_batch_id"], ["review_batch.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["search_index_generation_id"], ["search_index_generation.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["assigned_user_id"], ["app_user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["assigned_by_user_id"], ["app_user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
    )
    op.create_index("ix_review_batch_matter_created", "review_batch", ["matter_id", "created_at"])
    op.create_index("ix_review_batch_matter_id", "review_batch", ["matter_id"])
    op.create_index("ix_review_batch_status", "review_batch", ["status"])
    op.create_index("ix_review_batch_source_batch_id", "review_batch", ["source_batch_id"])
    op.create_index("ix_review_batch_search_index_generation_id", "review_batch", ["search_index_generation_id"])
    op.create_index("ix_review_batch_assigned_user_id", "review_batch", ["assigned_user_id"])
    op.create_index("ix_review_batch_assigned_by_user_id", "review_batch", ["assigned_by_user_id"])
    op.create_index("ix_review_batch_created_by_user_id", "review_batch", ["created_by_user_id"])

    op.create_table(
        "review_batch_document",
        sa.Column("review_batch_id", sa.Uuid(), nullable=False),
        sa.Column("matter_document_id", sa.Uuid(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("review_status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("sequence_number > 0", name="ck_review_batch_document_sequence"),
        sa.CheckConstraint(
            "review_status IN ('NOT_STARTED', 'IN_PROGRESS', 'COMPLETED', 'SKIPPED')",
            name="ck_review_batch_document_status",
        ),
        sa.ForeignKeyConstraint(["review_batch_id"], ["review_batch.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matter_document_id"], ["matter_document.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("review_batch_id", "matter_document_id"),
        sa.UniqueConstraint("review_batch_id", "sequence_number", name="uq_review_batch_document_sequence"),
    )
    op.create_index("ix_review_batch_document_matter_document_id", "review_batch_document", ["matter_document_id"])

    op.create_table(
        "review_batch_coding_group",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_batch_id", sa.Uuid(), nullable=False),
        sa.Column("source_metadata_group_id", sa.Uuid(), nullable=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["review_batch_id"], ["review_batch.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_metadata_group_id"], ["metadata_group.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_batch_id", "sort_order", name="uq_review_batch_group_order"),
    )
    op.create_index("ix_review_batch_coding_group_review_batch_id", "review_batch_coding_group", ["review_batch_id"])
    op.create_index(
        "ix_review_batch_coding_group_source_metadata_group_id",
        "review_batch_coding_group",
        ["source_metadata_group_id"],
    )

    op.create_table(
        "review_batch_coding_field",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_batch_coding_group_id", sa.Uuid(), nullable=False),
        sa.Column("metadata_definition_id", sa.Uuid(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("definition_snapshot", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["review_batch_coding_group_id"], ["review_batch_coding_group.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["metadata_definition_id"], ["metadata_definition.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_batch_coding_group_id", "metadata_definition_id", name="uq_review_batch_group_field"
        ),
        sa.UniqueConstraint("review_batch_coding_group_id", "sort_order", name="uq_review_batch_field_order"),
    )
    op.create_index(
        "ix_review_batch_coding_field_review_batch_coding_group_id",
        "review_batch_coding_field",
        ["review_batch_coding_group_id"],
    )
    op.create_index(
        "ix_review_batch_coding_field_metadata_definition_id", "review_batch_coding_field", ["metadata_definition_id"]
    )

    op.create_table(
        "review_batch_note",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_batch_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_type", sa.String(20), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), nullable=True),
        sa.Column("author_run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("author_type IN ('USER', 'AGENT')", name="ck_review_batch_note_author_type"),
        sa.CheckConstraint(
            "(author_type = 'USER' AND author_user_id IS NOT NULL AND author_run_id IS NULL) OR "
            "(author_type = 'AGENT' AND author_user_id IS NULL AND author_run_id IS NOT NULL)",
            name="ck_review_batch_note_author",
        ),
        sa.ForeignKeyConstraint(["review_batch_id"], ["review_batch.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_user_id"], ["app_user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_batch_note_review_batch_id", "review_batch_note", ["review_batch_id"])
    op.create_index("ix_review_batch_note_author_user_id", "review_batch_note", ["author_user_id"])
    op.create_index("ix_review_batch_note_author_run_id", "review_batch_note", ["author_run_id"])

    op.create_table(
        "review_batch_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_batch_id", sa.Uuid(), nullable=False),
        sa.Column("run_type", sa.String(20), nullable=False),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result_policy", sa.String(30), nullable=False),
        sa.Column("parent_run_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("agent_definition_version_id", sa.Uuid(), nullable=True),
        sa.Column("configuration_snapshot", sa.JSON(), nullable=False),
        sa.Column("initiated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_id", sa.String(255), nullable=True),
        sa.Column("processed_document_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("run_type IN ('HUMAN', 'AGENT')", name="ck_review_batch_run_type"),
        sa.CheckConstraint("purpose IN ('REVIEW', 'REFERENCE', 'CANDIDATE')", name="ck_review_batch_run_purpose"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')", name="ck_review_batch_run_status"
        ),
        sa.CheckConstraint("result_policy IN ('ISOLATED', 'PUBLISH_TO_MATTER')", name="ck_review_batch_run_policy"),
        sa.CheckConstraint(
            "(run_type = 'HUMAN' AND actor_user_id IS NOT NULL AND agent_definition_version_id IS NULL) OR (run_type = 'AGENT' AND actor_user_id IS NULL AND agent_definition_version_id IS NOT NULL)",
            name="ck_review_batch_run_actor",
        ),
        sa.CheckConstraint("processed_document_count >= 0", name="ck_review_batch_run_processed_count"),
        sa.ForeignKeyConstraint(["review_batch_id"], ["review_batch.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_run_id"], ["review_batch_run.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["agent_definition_version_id"], ["agent_definition_version.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["initiated_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
    )
    op.create_index("ix_review_batch_run_batch_created", "review_batch_run", ["review_batch_id", "created_at"])
    op.create_index("ix_review_batch_run_review_batch_id", "review_batch_run", ["review_batch_id"])
    op.create_index("ix_review_batch_run_status", "review_batch_run", ["status"])
    op.create_index("ix_review_batch_run_parent_run_id", "review_batch_run", ["parent_run_id"])
    op.create_index("ix_review_batch_run_actor_user_id", "review_batch_run", ["actor_user_id"])
    op.create_index(
        "ix_review_batch_run_agent_definition_version_id", "review_batch_run", ["agent_definition_version_id"]
    )
    op.create_index("ix_review_batch_run_initiated_by_user_id", "review_batch_run", ["initiated_by_user_id"])
    op.create_foreign_key(
        "fk_review_batch_note_author_run_id",
        "review_batch_note",
        "review_batch_run",
        ["author_run_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.create_table(
        "review_batch_run_value",
        sa.Column("review_batch_run_id", sa.Uuid(), nullable=False),
        sa.Column("matter_document_id", sa.Uuid(), nullable=False),
        sa.Column("metadata_definition_id", sa.Uuid(), nullable=False),
        sa.Column("value_ordinal", sa.Integer(), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_long", sa.BigInteger(), nullable=True),
        sa.Column("value_float", sa.Float(), nullable=True),
        sa.Column("value_boolean", sa.Boolean(), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=True),
        sa.Column("value_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value_json", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("value_ordinal >= 0", name="ck_review_batch_run_value_ordinal"),
        sa.CheckConstraint(
            "(CASE WHEN value_text IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN value_long IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN value_float IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN value_boolean IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN value_date IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN value_datetime IS NOT NULL THEN 1 ELSE 0 END + CASE WHEN value_json IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_review_batch_run_value_shape",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_review_batch_run_value_confidence",
        ),
        sa.ForeignKeyConstraint(["review_batch_run_id"], ["review_batch_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matter_document_id"], ["matter_document.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["metadata_definition_id"], ["metadata_definition.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("review_batch_run_id", "matter_document_id", "metadata_definition_id", "value_ordinal"),
    )
    op.create_index(
        "ix_review_batch_run_value_document", "review_batch_run_value", ["matter_document_id", "metadata_definition_id"]
    )


def downgrade() -> None:
    op.drop_table("review_batch_run_value")
    op.drop_table("review_batch_note")
    op.drop_table("review_batch_run")
    op.drop_table("review_batch_coding_field")
    op.drop_table("review_batch_coding_group")
    op.drop_table("review_batch_document")
    op.drop_table("review_batch")
