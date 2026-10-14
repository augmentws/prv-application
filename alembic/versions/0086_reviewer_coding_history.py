"""Retain immutable human reviewer coding revisions.

Revision ID: 0086_reviewer_coding_history
Revises: 0085_resolve_search_ops
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0086_reviewer_coding_history"
down_revision: str | None = "0085_resolve_search_ops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


VALUE_SHAPE = (
    "(CASE WHEN value_text IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_long IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_float IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_boolean IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_date IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_datetime IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN value_json IS NOT NULL THEN 1 ELSE 0 END) = 1"
)


def _typed_value_columns() -> list[sa.Column]:
    return [
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_long", sa.BigInteger(), nullable=True),
        sa.Column("value_float", sa.Float(), nullable=True),
        sa.Column("value_boolean", sa.Boolean(), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=True),
        sa.Column("value_datetime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value_json", sa.JSON(), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "review_batch_reviewer_revision",
        sa.Column(
            "reviewer_coding_id",
            sa.Uuid(),
            sa.ForeignKey("review_batch_reviewer_coding.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column(
            "actor_user_id",
            sa.Uuid(),
            sa.ForeignKey("app_user.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("origin", sa.String(length=30), nullable=False),
        sa.Column("request_key", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_reviewer_revision_number"),
        sa.CheckConstraint(
            "origin IN ('REVIEW_UI', 'IMPORT', 'SYSTEM_MIGRATION')",
            name="ck_reviewer_revision_origin",
        ),
        sa.UniqueConstraint(
            "reviewer_coding_id",
            "request_key",
            name="uq_reviewer_revision_request",
        ),
    )
    op.create_index(
        "ix_reviewer_revision_actor_user_id",
        "review_batch_reviewer_revision",
        ["actor_user_id"],
    )
    op.create_index(
        "ix_reviewer_revision_coding_desc",
        "review_batch_reviewer_revision",
        ["reviewer_coding_id", "revision"],
    )

    op.create_table(
        "review_batch_reviewer_document_revision",
        sa.Column("reviewer_coding_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column(
            "matter_document_id",
            sa.Uuid(),
            sa.ForeignKey("matter_document.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["reviewer_coding_id", "revision"],
            [
                "review_batch_reviewer_revision.reviewer_coding_id",
                "review_batch_reviewer_revision.revision",
            ],
            name="fk_reviewer_doc_revision_header",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_reviewer_doc_revision_number"),
        sa.CheckConstraint(
            "status IN ('IN_PROGRESS', 'COMPLETED', 'SKIPPED')",
            name="ck_reviewer_doc_revision_status",
        ),
    )
    op.create_index(
        "ix_reviewer_doc_revision_lookup",
        "review_batch_reviewer_document_revision",
        ["reviewer_coding_id", "matter_document_id", "revision"],
    )

    op.create_table(
        "review_batch_reviewer_field_revision",
        sa.Column("reviewer_coding_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column("matter_document_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "metadata_definition_id",
            sa.Uuid(),
            sa.ForeignKey("metadata_definition.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_coding_id", "revision", "matter_document_id"],
            [
                "review_batch_reviewer_document_revision.reviewer_coding_id",
                "review_batch_reviewer_document_revision.revision",
                "review_batch_reviewer_document_revision.matter_document_id",
            ],
            name="fk_reviewer_field_revision_document",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_reviewer_field_revision_number"),
    )
    op.create_index(
        "ix_reviewer_field_revision_lookup",
        "review_batch_reviewer_field_revision",
        ["reviewer_coding_id", "matter_document_id", "metadata_definition_id", "revision"],
    )

    op.create_table(
        "review_batch_reviewer_value_revision",
        sa.Column("reviewer_coding_id", sa.Uuid(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column("matter_document_id", sa.Uuid(), primary_key=True),
        sa.Column("metadata_definition_id", sa.Uuid(), primary_key=True),
        sa.Column("value_ordinal", sa.Integer(), primary_key=True),
        *_typed_value_columns(),
        sa.ForeignKeyConstraint(
            [
                "reviewer_coding_id",
                "revision",
                "matter_document_id",
                "metadata_definition_id",
            ],
            [
                "review_batch_reviewer_field_revision.reviewer_coding_id",
                "review_batch_reviewer_field_revision.revision",
                "review_batch_reviewer_field_revision.matter_document_id",
                "review_batch_reviewer_field_revision.metadata_definition_id",
            ],
            name="fk_reviewer_value_revision_field",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_reviewer_value_revision_number"),
        sa.CheckConstraint("value_ordinal >= 0", name="ck_reviewer_value_revision_ordinal"),
        sa.CheckConstraint(VALUE_SHAPE, name="ck_reviewer_value_revision_shape"),
    )
    op.create_index(
        "ix_reviewer_value_revision_document",
        "review_batch_reviewer_value_revision",
        ["reviewer_coding_id", "matter_document_id", "metadata_definition_id"],
    )

    # A legacy source can have projection rows with revision zero only if data was
    # inserted outside the canonical command. Give that known state revision one.
    op.execute(
        sa.text(
            """
            UPDATE review_batch_reviewer_coding AS coding
            SET revision = 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE coding.revision = 0
              AND (
                EXISTS (
                    SELECT 1 FROM review_batch_reviewer_document AS document
                    WHERE document.reviewer_coding_id = coding.id
                )
                OR EXISTS (
                    SELECT 1 FROM review_batch_reviewer_value AS value
                    WHERE value.reviewer_coding_id = coding.id
                )
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO review_batch_reviewer_revision (
                reviewer_coding_id, revision, actor_user_id, origin, request_key, created_at
            )
            SELECT coding.id,
                   coding.revision,
                   coding.reviewer_user_id,
                   'SYSTEM_MIGRATION',
                   NULL,
                   coding.updated_at
            FROM review_batch_reviewer_coding AS coding
            WHERE coding.revision >= 1
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO review_batch_reviewer_document_revision (
                reviewer_coding_id, revision, matter_document_id,
                status, started_at, completed_at
            )
            SELECT document.reviewer_coding_id,
                   coding.revision,
                   document.matter_document_id,
                   document.status,
                   document.started_at,
                   document.completed_at
            FROM review_batch_reviewer_document AS document
            JOIN review_batch_reviewer_coding AS coding
              ON coding.id = document.reviewer_coding_id
            WHERE coding.revision >= 1
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO review_batch_reviewer_field_revision (
                reviewer_coding_id, revision, matter_document_id, metadata_definition_id
            )
            SELECT DISTINCT document.reviewer_coding_id,
                   coding.revision,
                   document.matter_document_id,
                   field.metadata_definition_id
            FROM review_batch_reviewer_document AS document
            JOIN review_batch_reviewer_coding AS coding
              ON coding.id = document.reviewer_coding_id
            JOIN review_batch_coding_group AS coding_group
              ON coding_group.review_batch_id = coding.review_batch_id
            JOIN review_batch_coding_field AS field
              ON field.review_batch_coding_group_id = coding_group.id
            WHERE document.status = 'COMPLETED'
              AND coding.revision >= 1
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO review_batch_reviewer_field_revision (
                reviewer_coding_id, revision, matter_document_id, metadata_definition_id
            )
            SELECT DISTINCT value.reviewer_coding_id,
                   coding.revision,
                   value.matter_document_id,
                   value.metadata_definition_id
            FROM review_batch_reviewer_value AS value
            JOIN review_batch_reviewer_coding AS coding
              ON coding.id = value.reviewer_coding_id
            WHERE coding.revision >= 1
            ON CONFLICT DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO review_batch_reviewer_value_revision (
                reviewer_coding_id, revision, matter_document_id,
                metadata_definition_id, value_ordinal,
                value_text, value_long, value_float, value_boolean,
                value_date, value_datetime, value_json
            )
            SELECT value.reviewer_coding_id,
                   coding.revision,
                   value.matter_document_id,
                   value.metadata_definition_id,
                   value.value_ordinal,
                   value.value_text,
                   value.value_long,
                   value.value_float,
                   value.value_boolean,
                   value.value_date,
                   value.value_datetime,
                   value.value_json
            FROM review_batch_reviewer_value AS value
            JOIN review_batch_reviewer_coding AS coding
              ON coding.id = value.reviewer_coding_id
            WHERE coding.revision >= 1
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reviewer_value_revision_document",
        table_name="review_batch_reviewer_value_revision",
    )
    op.drop_table("review_batch_reviewer_value_revision")
    op.drop_index(
        "ix_reviewer_field_revision_lookup",
        table_name="review_batch_reviewer_field_revision",
    )
    op.drop_table("review_batch_reviewer_field_revision")
    op.drop_index(
        "ix_reviewer_doc_revision_lookup",
        table_name="review_batch_reviewer_document_revision",
    )
    op.drop_table("review_batch_reviewer_document_revision")
    op.drop_index(
        "ix_reviewer_revision_coding_desc",
        table_name="review_batch_reviewer_revision",
    )
    op.drop_index(
        "ix_reviewer_revision_actor_user_id",
        table_name="review_batch_reviewer_revision",
    )
    op.drop_table("review_batch_reviewer_revision")
