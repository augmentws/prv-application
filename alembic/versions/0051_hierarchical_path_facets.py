"""Add hierarchical path facet configuration.

Revision ID: 0051_path_facets
Revises: 0050_multiple_review_guidance
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0051_path_facets"
down_revision: str | None = "0050_multiple_review_guidance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "metadata_definition",
        sa.Column("hierarchy_separator", sa.String(length=10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("metadata_definition", "hierarchy_separator")
