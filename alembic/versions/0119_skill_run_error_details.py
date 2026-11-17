"""Persist structured skill-run failure diagnostics.

Revision ID: 0119_skill_run_error_details
Revises: 0118_synthesis_durability
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0119_skill_run_error_details"
down_revision: str | None = "0118_synthesis_durability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "skill_run",
        sa.Column("error_details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("skill_run", "error_details")
