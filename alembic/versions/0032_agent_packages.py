"""Add versioned invocation contracts to agent definitions.

Revision ID: 0032_agent_packages
Revises: 0031_keep_agent_chats_open
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0032_agent_packages"
down_revision: str | None = "0031_keep_agent_chats_open"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_definition_version",
        sa.Column("invocation_mode", sa.String(length=20), nullable=False, server_default="CHAT"),
    )
    op.add_column("agent_definition_version", sa.Column("usage_instructions", sa.Text(), nullable=True))
    op.add_column(
        "agent_definition_version",
        sa.Column("scope_types", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
    )
    op.add_column(
        "agent_definition_version",
        sa.Column("input_schema", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.create_check_constraint(
        "ck_agent_version_invocation_mode",
        "agent_definition_version",
        "invocation_mode IN ('CHAT', 'STRUCTURED')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_version_invocation_mode", "agent_definition_version", type_="check")
    op.drop_column("agent_definition_version", "input_schema")
    op.drop_column("agent_definition_version", "scope_types")
    op.drop_column("agent_definition_version", "usage_instructions")
    op.drop_column("agent_definition_version", "invocation_mode")
