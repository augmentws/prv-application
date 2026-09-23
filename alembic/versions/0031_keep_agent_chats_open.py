"""Keep agent chats open after individual turns complete.

Revision ID: 0031_keep_agent_chats_open
Revises: 0030_batch_chat_conversations
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0031_keep_agent_chats_open"
down_revision: str | None = "0030_batch_chat_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE agent_conversation SET status = 'ACTIVE' WHERE status = 'COMPLETED'")
    op.drop_constraint("ck_agent_conversation_status", "agent_conversation", type_="check")
    op.create_check_constraint(
        "ck_agent_conversation_status",
        "agent_conversation",
        "status IN ('ACTIVE', 'WAITING_APPROVAL', 'FAILED', 'ARCHIVED')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_conversation_status", "agent_conversation", type_="check")
    op.create_check_constraint(
        "ck_agent_conversation_status",
        "agent_conversation",
        "status IN ('ACTIVE', 'WAITING_APPROVAL', 'COMPLETED', 'FAILED', 'ARCHIVED')",
    )
