"""Bind search-chat conversations to an immutable search definition.

Revision ID: 0101_search_chat_conversations
Revises: 0100_close_fact_export_job_runs
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0101_search_chat_conversations"
down_revision: str | None = "0100_close_fact_export_job_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_conversation", sa.Column("search_definition", sa.JSON(), nullable=True))
    op.drop_constraint("ck_agent_conversation_workflow_type", "agent_conversation", type_="check")
    op.drop_constraint("ck_agent_conversation_workflow_scope", "agent_conversation", type_="check")
    op.create_check_constraint(
        "ck_agent_conversation_workflow_type",
        "agent_conversation",
        "workflow_type IN ('MATTER_DEFINITION_SETUP', 'BATCH_CHAT', 'SEARCH_CHAT')",
    )
    op.create_check_constraint(
        "ck_agent_conversation_workflow_scope",
        "agent_conversation",
        "(workflow_type = 'MATTER_DEFINITION_SETUP' AND review_batch_id IS NULL "
        "AND search_definition IS NULL) OR "
        "(workflow_type = 'BATCH_CHAT' AND review_batch_id IS NOT NULL "
        "AND matter_definition_id IS NULL AND search_definition IS NULL) OR "
        "(workflow_type = 'SEARCH_CHAT' AND review_batch_id IS NULL "
        "AND matter_definition_id IS NULL AND search_definition IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute("DELETE FROM agent_conversation WHERE workflow_type = 'SEARCH_CHAT'")
    op.drop_constraint("ck_agent_conversation_workflow_scope", "agent_conversation", type_="check")
    op.drop_constraint("ck_agent_conversation_workflow_type", "agent_conversation", type_="check")
    op.create_check_constraint(
        "ck_agent_conversation_workflow_type",
        "agent_conversation",
        "workflow_type IN ('MATTER_DEFINITION_SETUP', 'BATCH_CHAT')",
    )
    op.create_check_constraint(
        "ck_agent_conversation_workflow_scope",
        "agent_conversation",
        "(workflow_type = 'MATTER_DEFINITION_SETUP' AND review_batch_id IS NULL) OR "
        "(workflow_type = 'BATCH_CHAT' AND review_batch_id IS NOT NULL "
        "AND matter_definition_id IS NULL)",
    )
    op.drop_column("agent_conversation", "search_definition")
