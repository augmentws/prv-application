"""Add versioned analysis-task destination fields.

Revision ID: 0063_analysis_destinations
Revises: 0062_class_exclusions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0063_analysis_destinations"
down_revision: str | None = "0062_class_exclusions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column(
        "matter_analysis_task_version",
        sa.Column("destination_metadata_definition_ids", json_document, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("matter_analysis_task_version", "destination_metadata_definition_ids")
