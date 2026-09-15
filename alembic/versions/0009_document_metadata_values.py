"""Add document metadata event ledger and current projection.

Revision ID: 0009_document_metadata_values
Revises: 0008_search_projection
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_document_metadata_values"
down_revision: str | None = "0008_search_projection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


VALUE_COUNT = (
    "CASE WHEN value_text IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_long IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_float IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_boolean IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_date IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_datetime IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_json IS NOT NULL THEN 1 ELSE 0 END"
)
NO_VALUE = (
    "value_text IS NULL AND value_long IS NULL AND value_float IS NULL AND "
    "value_boolean IS NULL AND value_date IS NULL AND value_datetime IS NULL AND value_json IS NULL"
)


def upgrade() -> None:
    op.create_table(
        "metadata_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("matter_document_id", sa.Uuid(), nullable=False),
        sa.Column("metadata_definition_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=20), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_long", sa.BigInteger(), nullable=True),
        sa.Column("value_float", sa.Float(), nullable=True),
        sa.Column("value_boolean", sa.Boolean(), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=True),
        sa.Column("value_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("source_id", sa.String(length=500), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("target_event_id", sa.Uuid(), nullable=True),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "operation IN ('SET', 'ADD', 'REMOVE', 'CLEAR', 'CONFIRM', 'REJECT')",
            name="ck_metadata_event_operation",
        ),
        sa.CheckConstraint(
            "source_type IN ('HUMAN', 'AGENT', 'EXTRACTOR', 'IMPORT', 'RULE', 'SYSTEM')",
            name="ck_metadata_event_source_type",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_metadata_event_confidence",
        ),
        sa.CheckConstraint(
            f"(operation IN ('SET', 'ADD') AND ({VALUE_COUNT}) = 1) OR "
            f"(operation IN ('REMOVE', 'CLEAR', 'CONFIRM', 'REJECT') AND {NO_VALUE})",
            name="ck_metadata_event_value_shape",
        ),
        sa.CheckConstraint(
            "(operation IN ('REMOVE', 'CONFIRM', 'REJECT') AND target_event_id IS NOT NULL) OR "
            "(operation IN ('SET', 'ADD', 'CLEAR') AND target_event_id IS NULL)",
            name="ck_metadata_event_target_shape",
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["app_user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matter_document_id"], ["matter_document.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["metadata_definition_id"], ["metadata_definition.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_id"], ["metadata_event.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_event_id"], ["metadata_event.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "matter_id",
        "matter_document_id",
        "metadata_definition_id",
        "actor_id",
        "agent_run_id",
        "target_event_id",
        "supersedes_id",
    ):
        op.create_index(f"ix_metadata_event_{column}", "metadata_event", [column])
    op.create_index(
        "ix_metadata_event_document_definition_created",
        "metadata_event",
        ["matter_document_id", "metadata_definition_id", "created_at", "id"],
    )
    op.create_index(
        "ix_metadata_event_matter_created",
        "metadata_event",
        ["matter_id", "created_at"],
    )

    op.create_table(
        "document_metadata_current",
        sa.Column("matter_document_id", sa.Uuid(), nullable=False),
        sa.Column("metadata_definition_id", sa.Uuid(), nullable=False),
        sa.Column("value_ordinal", sa.Integer(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("resolution_state", sa.String(length=20), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_long", sa.BigInteger(), nullable=True),
        sa.Column("value_float", sa.Float(), nullable=True),
        sa.Column("value_boolean", sa.Boolean(), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=True),
        sa.Column("value_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("source_event_id", sa.Uuid(), nullable=True),
        sa.Column("supporting_event_ids", sa.JSON(), nullable=False),
        sa.Column("pending_event_ids", sa.JSON(), nullable=False),
        sa.Column("conflicting_event_ids", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "resolution_state IN ('VALUE', 'EMPTY', 'PENDING', 'CONFLICTED')",
            name="ck_document_metadata_current_state",
        ),
        sa.CheckConstraint("value_ordinal >= 0", name="ck_document_metadata_current_ordinal"),
        sa.CheckConstraint(
            f"(resolution_state = 'EMPTY' AND source_event_id IS NULL AND {NO_VALUE}) OR "
            "(resolution_state = 'PENDING' AND source_event_id IS NOT NULL) OR "
            f"(resolution_state IN ('VALUE', 'CONFLICTED') AND source_event_id IS NOT NULL AND ({VALUE_COUNT}) = 1)",
            name="ck_document_metadata_current_value_shape",
        ),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matter_document_id"], ["matter_document.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["metadata_definition_id"], ["metadata_definition.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_event_id"], ["metadata_event.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("matter_document_id", "metadata_definition_id", "value_ordinal"),
    )
    op.create_index("ix_document_metadata_current_matter_id", "document_metadata_current", ["matter_id"])
    op.create_index(
        "ix_document_metadata_current_source_event_id",
        "document_metadata_current",
        ["source_event_id"],
    )
    op.create_index(
        "ix_document_metadata_current_matter_definition",
        "document_metadata_current",
        ["matter_id", "metadata_definition_id"],
    )


def downgrade() -> None:
    op.drop_table("document_metadata_current")
    op.drop_table("metadata_event")
