"""Scope pre-guidance chats to a matter's sole guidance profile.

Revision ID: 0052_scope_pre_guidance_chats
Revises: 0051_path_facets
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0052_scope_pre_guidance_chats"
down_revision: str | None = "0051_path_facets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE agent_conversation AS conversation "
            "SET matter_definition_id = definition.id "
            "FROM matter_definition AS definition "
            "WHERE conversation.workflow_type = 'MATTER_DEFINITION_SETUP' "
            "AND conversation.matter_definition_id IS NULL "
            "AND conversation.matter_id = definition.matter_id "
            "AND NOT EXISTS ("
            "SELECT 1 FROM matter_definition AS other "
            "WHERE other.matter_id = definition.matter_id "
            "AND other.id <> definition.id"
            ")"
        )
    )


def downgrade() -> None:
    # The assigned guidance remains a valid scope on the previous schema, and
    # reverting it would discard information about which chat belongs where.
    pass
