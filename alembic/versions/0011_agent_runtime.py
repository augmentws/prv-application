"""Add durable agent conversation, run, approval, and tool records.

Revision ID: 0011_agent_runtime
Revises: 0010_agents_matter_definitions
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_agent_runtime"
down_revision: str | None = "0010_agents_matter_definitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _indexes(table: str, columns: list[str]) -> None:
    for column in columns:
        op.create_index(f"ix_{table}_{column}", table, [column])


def upgrade() -> None:
    op.create_table(
        "agent_conversation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("agent_definition_id", sa.Uuid(), nullable=False),
        sa.Column("agent_definition_version_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("initiated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "workflow_type IN ('MATTER_DEFINITION_SETUP')", name="ck_agent_conversation_workflow_type"
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'WAITING_APPROVAL', 'COMPLETED', 'FAILED', 'ARCHIVED')",
            name="ck_agent_conversation_status",
        ),
        sa.ForeignKeyConstraint(["agent_definition_id"], ["agent_definition.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["agent_definition_version_id"], ["agent_definition_version.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["client_id"], ["client.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["initiated_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    _indexes(
        "agent_conversation",
        [
            "tenant_id",
            "client_id",
            "matter_id",
            "agent_definition_id",
            "agent_definition_version_id",
            "status",
            "initiated_by_user_id",
        ],
    )
    op.create_index(
        "ix_agent_conversation_matter_created", "agent_conversation", ["matter_id", "created_at"]
    )

    op.create_table(
        "agent_turn",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_agent_turn_sequence_positive"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_agent_turn_status",
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["agent_conversation.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_agent_turn_sequence"),
    )
    _indexes("agent_turn", ["conversation_id", "status", "created_by_user_id"])

    op.create_table(
        "agent_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("parent_run_id", sa.Uuid(), nullable=True),
        sa.Column("deferred_from_run_id", sa.Uuid(), nullable=True),
        sa.Column("agent_definition_version_id", sa.Uuid(), nullable=False),
        sa.Column("model_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("message_history", sa.JSON(), nullable=True),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("tool_call_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_agent_run_sequence_positive"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_agent_run_status",
        ),
        sa.ForeignKeyConstraint(["agent_definition_version_id"], ["agent_definition_version.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["conversation_id"], ["agent_conversation.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deferred_from_run_id"], ["agent_run.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_run_id"], ["agent_run.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["turn_id"], ["agent_turn.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("turn_id", "sequence", name="uq_agent_run_turn_sequence"),
        sa.UniqueConstraint("workflow_id", name="uq_agent_run_workflow_id"),
    )
    _indexes(
        "agent_run",
        [
            "conversation_id",
            "turn_id",
            "parent_run_id",
            "deferred_from_run_id",
            "agent_definition_version_id",
            "status",
        ],
    )

    op.create_table(
        "agent_message",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("message_data", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_agent_message_sequence_positive"),
        sa.CheckConstraint("role IN ('USER', 'ASSISTANT', 'SYSTEM', 'TOOL')", name="ck_agent_message_role"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_id"], ["agent_conversation.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["turn_id"], ["agent_turn.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_agent_message_sequence"),
    )
    _indexes("agent_message", ["conversation_id", "turn_id", "created_by_user_id", "agent_run_id"])

    op.create_table(
        "agent_action_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.String(length=500), nullable=False),
        sa.Column("tool_key", sa.String(length=150), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("summary", sa.String(length=1000), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXECUTED', 'FAILED')",
            name="ck_agent_action_request_status",
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["agent_conversation.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["agent_turn.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_run_id", "tool_call_id", name="uq_agent_action_request_tool_call"),
    )
    _indexes("agent_action_request", ["conversation_id", "turn_id", "agent_run_id", "status"])

    op.create_table(
        "agent_action_decision",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action_request_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=True),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("decision IN ('APPROVE', 'REJECT')", name="ck_agent_action_decision"),
        sa.ForeignKeyConstraint(["action_request_id"], ["agent_action_request.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("action_request_id", name="uq_agent_action_decision_request"),
    )
    _indexes("agent_action_decision", ["action_request_id", "decided_by_user_id"])

    op.create_table(
        "agent_tool_execution",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("action_request_id", sa.Uuid(), nullable=True),
        sa.Column("tool_call_id", sa.String(length=500), nullable=False),
        sa.Column("tool_key", sa.String(length=150), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('RUNNING', 'COMPLETED', 'FAILED')", name="ck_agent_tool_execution_status"),
        sa.ForeignKeyConstraint(["action_request_id"], ["agent_action_request.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _indexes("agent_tool_execution", ["agent_run_id", "action_request_id", "tool_key"])


def downgrade() -> None:
    op.drop_table("agent_tool_execution")
    op.drop_table("agent_action_decision")
    op.drop_table("agent_action_request")
    op.drop_table("agent_message")
    op.drop_table("agent_run")
    op.drop_table("agent_turn")
    op.drop_table("agent_conversation")
