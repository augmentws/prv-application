"""Widen Matter Definition revision source kinds.

Revision ID: 0044_widen_definition_source
Revises: 0043_batch_coding_search
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0044_widen_definition_source"
down_revision: str | None = "0043_batch_coding_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "matter_definition_revision",
        "source_kind",
        existing_type=sa.String(length=20),
        type_=sa.String(length=40),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "matter_definition_revision",
        "source_kind",
        existing_type=sa.String(length=40),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
