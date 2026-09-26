"""Add durable agent conversation lifecycle events.

Revision ID: 0038_agent_events
Revises: 0037_bulk_tag_assignments
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0038_agent_events"
down_revision: str | None = "0037_bulk_tag_assignments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_conversation_event_cursor",
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("newest_sequence", sa.BigInteger(), nullable=False),
        sa.Column("oldest_sequence", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("newest_sequence >= 0", name="ck_agent_conversation_event_cursor_newest"),
        sa.CheckConstraint("oldest_sequence >= 1", name="ck_agent_conversation_event_cursor_oldest"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("matter_id"),
    )
    op.create_table(
        "agent_conversation_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_type", sa.String(length=50), nullable=False),
        sa.Column("review_batch_id", sa.Uuid(), nullable=True),
        sa.Column("matter_sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("action_request_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_id", sa.String(length=255), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("matter_sequence > 0", name="ck_agent_conversation_event_sequence_positive"),
        sa.CheckConstraint("schema_version > 0", name="ck_agent_conversation_event_schema_version_positive"),
        sa.ForeignKeyConstraint(["action_request_id"], ["agent_action_request.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_id"], ["agent_conversation.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["agent_message.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["review_batch_id"], ["review_batch.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["turn_id"], ["agent_turn.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("matter_id", "matter_sequence", name="uq_agent_conversation_event_matter_sequence"),
    )
    op.create_index("ix_agent_conversation_event_tenant_id", "agent_conversation_event", ["tenant_id"])
    op.create_index("ix_agent_conversation_event_matter_id", "agent_conversation_event", ["matter_id"])
    op.create_index("ix_agent_conversation_event_conversation_id", "agent_conversation_event", ["conversation_id"])
    op.create_index("ix_agent_conversation_event_review_batch_id", "agent_conversation_event", ["review_batch_id"])
    op.create_index("ix_agent_conversation_event_event_type", "agent_conversation_event", ["event_type"])
    op.create_index("ix_agent_conversation_event_turn_id", "agent_conversation_event", ["turn_id"])
    op.create_index("ix_agent_conversation_event_agent_run_id", "agent_conversation_event", ["agent_run_id"])
    op.create_index("ix_agent_conversation_event_message_id", "agent_conversation_event", ["message_id"])
    op.create_index("ix_agent_conversation_event_action_request_id", "agent_conversation_event", ["action_request_id"])
    op.create_index(
        "ix_agent_conversation_event_scope_sequence",
        "agent_conversation_event",
        ["matter_id", "workflow_type", "review_batch_id", "matter_sequence"],
    )
    op.create_index(
        "ix_agent_conversation_event_conversation_sequence",
        "agent_conversation_event",
        ["conversation_id", "matter_sequence"],
    )


def downgrade() -> None:
    op.drop_table("agent_conversation_event")
    op.drop_table("agent_conversation_event_cursor")
