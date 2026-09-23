"""Add user-facing agent conversation titles.

Revision ID: 0029_agent_conversation_titles
Revises: 0028_assessment_names
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029_agent_conversation_titles"
down_revision: str | None = "0028_assessment_names"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_conversation", sa.Column("title", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_conversation", "title")
