"""Add user-facing Matter Definition assessment names.

Revision ID: 0028_assessment_names
Revises: 0027_assessment_topics
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_assessment_names"
down_revision: str | None = "0027_assessment_topics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column(
            "name",
            sa.String(length=200),
            nullable=False,
            server_default="Matter Definition assessment",
        ),
    )
    op.alter_column("matter_definition_assessment_run", "name", server_default=None)


def downgrade() -> None:
    op.drop_column("matter_definition_assessment_run", "name")
