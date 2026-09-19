"""Add the canonical collection-item file date.

Revision ID: 0006_collection_item_file_date
Revises: 0005_collection_deletion
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_collection_item_file_date"
down_revision: str | None = "0005_collection_deletion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "collection_item",
        sa.Column("file_date", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_collection_item_file_date",
        "collection_item",
        ["collection_id", "file_date"],
    )
    # Emails use their sent date.
    op.execute(
        """
        UPDATE collection_item AS item
        SET file_date = email.sent_at
        FROM collection_item_email AS email
        WHERE email.collection_item_id = item.id
          AND email.sent_at IS NOT NULL
        """
    )
    # Email attachments and other child files inherit their parent's canonical date.
    op.execute(
        """
        UPDATE collection_item AS child
        SET file_date = parent.file_date
        FROM collection_item AS parent
        WHERE child.parent_collection_item_id = parent.id
          AND parent.file_date IS NOT NULL
        """
    )
    # Standalone collected files use the filesystem/source last-modified timestamp.
    op.execute(
        """
        UPDATE collection_item
        SET file_date = source_modified_at
        WHERE file_date IS NULL
          AND parent_collection_item_id IS NULL
          AND record_type <> 'EMAIL'
          AND source_modified_at IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_collection_item_file_date", table_name="collection_item")
    op.drop_column("collection_item", "file_date")
