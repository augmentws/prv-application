"""Publish matching collection metadata as imported matter labels.

Revision ID: 0060_imported_matter_labels
Revises: 0059_classification_scoring
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0060_imported_matter_labels"
down_revision: str | None = "0059_classification_scoring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_counts(table: str) -> None:
    for column in (
        "imported_label_document_count",
        "imported_label_event_count",
        "imported_label_warning_count",
    ):
        op.add_column(
            table,
            sa.Column(column, sa.Integer(), nullable=False, server_default="0"),
        )


def upgrade() -> None:
    op.drop_constraint("ck_matter_import_counts", "matter_document_import_job", type_="check")
    op.drop_constraint(
        "ck_matter_import_batch_counts",
        "matter_document_import_batch",
        type_="check",
    )
    _add_counts("matter_document_import_job")
    _add_counts("matter_document_import_batch")
    op.create_check_constraint(
        "ck_matter_import_counts",
        "matter_document_import_job",
        "matched_count >= 0 AND batch_count >= 0 AND processed_count >= 0 AND "
        "added_count >= 0 AND duplicate_count >= 0 AND failed_count >= 0 AND "
        "imported_label_document_count >= 0 AND imported_label_event_count >= 0 AND "
        "imported_label_warning_count >= 0",
    )
    op.create_check_constraint(
        "ck_matter_import_batch_counts",
        "matter_document_import_batch",
        "item_count >= 0 AND added_count >= 0 AND duplicate_count >= 0 AND failed_count >= 0 AND "
        "imported_label_document_count >= 0 AND imported_label_event_count >= 0 AND "
        "imported_label_warning_count >= 0",
    )
    op.create_index(
        "uq_metadata_event_import_source",
        "metadata_event",
        ["matter_document_id", "metadata_definition_id", "source_id"],
        unique=True,
        postgresql_where=sa.text("source_type = 'IMPORT' AND source_id IS NOT NULL"),
        sqlite_where=sa.text("source_type = 'IMPORT' AND source_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_metadata_event_import_source", table_name="metadata_event")
    op.drop_constraint(
        "ck_matter_import_batch_counts",
        "matter_document_import_batch",
        type_="check",
    )
    op.drop_constraint("ck_matter_import_counts", "matter_document_import_job", type_="check")
    for table in ("matter_document_import_batch", "matter_document_import_job"):
        for column in (
            "imported_label_warning_count",
            "imported_label_event_count",
            "imported_label_document_count",
        ):
            op.drop_column(table, column)
    op.create_check_constraint(
        "ck_matter_import_counts",
        "matter_document_import_job",
        "matched_count >= 0 AND batch_count >= 0 AND processed_count >= 0 AND "
        "added_count >= 0 AND duplicate_count >= 0 AND failed_count >= 0",
    )
    op.create_check_constraint(
        "ck_matter_import_batch_counts",
        "matter_document_import_batch",
        "item_count >= 0 AND added_count >= 0 AND duplicate_count >= 0 AND failed_count >= 0",
    )
