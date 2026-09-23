"""Bind batch-chat conversations to review batches.

Revision ID: 0030_batch_chat_conversations
Revises: 0029_agent_conversation_titles
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030_batch_chat_conversations"
down_revision: str | None = "0029_agent_conversation_titles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_agent_conversation_workflow_type", "agent_conversation", type_="check")
    op.add_column(
        "agent_conversation",
        sa.Column("review_batch_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_conversation_review_batch_id_review_batch",
        "agent_conversation",
        "review_batch",
        ["review_batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_agent_conversation_review_batch_id",
        "agent_conversation",
        ["review_batch_id"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_agent_conversation_workflow_type",
        "agent_conversation",
        "workflow_type IN ('MATTER_DEFINITION_SETUP', 'BATCH_CHAT')",
    )
    op.create_check_constraint(
        "ck_agent_conversation_workflow_scope",
        "agent_conversation",
        "(workflow_type = 'MATTER_DEFINITION_SETUP' AND review_batch_id IS NULL) OR "
        "(workflow_type = 'BATCH_CHAT' AND review_batch_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_conversation_workflow_scope", "agent_conversation", type_="check")
    op.drop_constraint("ck_agent_conversation_workflow_type", "agent_conversation", type_="check")
    op.drop_index("ix_agent_conversation_review_batch_id", table_name="agent_conversation")
    op.drop_constraint(
        "fk_agent_conversation_review_batch_id_review_batch",
        "agent_conversation",
        type_="foreignkey",
    )
    op.drop_column("agent_conversation", "review_batch_id")
    op.create_check_constraint(
        "ck_agent_conversation_workflow_type",
        "agent_conversation",
        "workflow_type IN ('MATTER_DEFINITION_SETUP')",
    )
