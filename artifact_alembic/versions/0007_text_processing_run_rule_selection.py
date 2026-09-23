"""Persist rule selection for collection text processing runs.

Revision ID: 0007_run_rule_selection
Revises: 0006_collection_item_file_date
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_run_rule_selection"
down_revision: str | None = "0006_collection_item_file_date"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "collection_text_processing_run",
        sa.Column("disabled_rule_ids", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
    )
    op.alter_column("collection_text_processing_run", "disabled_rule_ids", server_default=None)


def downgrade() -> None:
    op.drop_column("collection_text_processing_run", "disabled_rule_ids")
