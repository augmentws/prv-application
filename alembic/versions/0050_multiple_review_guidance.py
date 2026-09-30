"""Support multiple independently versioned review-guidance profiles per matter.

Revision ID: 0050_multiple_review_guidance
Revises: 0049_question_answering
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0050_multiple_review_guidance"
down_revision: str | None = "0049_question_answering"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("matter_definition", sa.Column("key", sa.String(length=100), nullable=True))
    op.add_column("matter_definition", sa.Column("name", sa.String(length=200), nullable=True))
    op.add_column("matter_definition", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("matter_definition", sa.Column("status", sa.String(length=20), nullable=True))
    op.execute(
        "UPDATE matter_definition SET "
        "key = 'general_review', "
        "name = 'General Review Guidance', "
        "description = 'Migrated from the original Matter Definition.', "
        "status = 'ACTIVE'"
    )
    op.alter_column("matter_definition", "key", nullable=False)
    op.alter_column("matter_definition", "name", nullable=False)
    op.alter_column("matter_definition", "status", nullable=False)
    op.drop_constraint("uq_matter_definition_matter", "matter_definition", type_="unique")
    op.create_unique_constraint(
        "uq_matter_definition_matter_key", "matter_definition", ["matter_id", "key"]
    )
    op.create_check_constraint(
        "ck_matter_definition_status",
        "matter_definition",
        "status IN ('ACTIVE', 'ARCHIVED')",
    )
    op.create_index(
        "ix_matter_definition_matter_status",
        "matter_definition",
        ["matter_id", "status"],
    )

    op.drop_constraint(
        "ck_matter_definition_revision_source_kind",
        "matter_definition_revision",
        type_="check",
    )
    op.create_check_constraint(
        "ck_matter_definition_revision_source_kind",
        "matter_definition_revision",
        "source_kind IN ('PASTE', 'MARKDOWN', 'TEXT', 'DOCX', 'AGENT_EDIT', "
        "'USER_EDIT', 'ASSESSMENT_REFINEMENT', 'CLONE')",
    )
    op.add_column(
        "matter_definition_revision",
        sa.Column("source_guidance_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "matter_definition_revision",
        sa.Column("source_revision_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "matter_definition_revision",
        sa.Column("source_content_hash", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_matter_definition_revision_source_guidance",
        "matter_definition_revision",
        "matter_definition",
        ["source_guidance_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_matter_definition_revision_source_revision",
        "matter_definition_revision",
        "matter_definition_revision",
        ["source_revision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_matter_definition_revision_source_guidance_id",
        "matter_definition_revision",
        ["source_guidance_id"],
    )
    op.create_index(
        "ix_matter_definition_revision_source_revision_id",
        "matter_definition_revision",
        ["source_revision_id"],
    )
    op.add_column(
        "agent_conversation",
        sa.Column("matter_definition_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_conversation_matter_definition",
        "agent_conversation",
        "matter_definition",
        ["matter_definition_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_agent_conversation_matter_definition_id",
        "agent_conversation",
        ["matter_definition_id"],
    )
    op.execute(
        "UPDATE agent_conversation AS conversation SET matter_definition_id = definition.id "
        "FROM matter_definition AS definition "
        "WHERE conversation.workflow_type = 'MATTER_DEFINITION_SETUP' "
        "AND conversation.matter_id = definition.matter_id "
        "AND definition.key = 'general_review'"
    )
    op.drop_constraint(
        "ck_agent_conversation_workflow_scope",
        "agent_conversation",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_conversation_workflow_scope",
        "agent_conversation",
        "(workflow_type = 'MATTER_DEFINITION_SETUP' AND review_batch_id IS NULL) OR "
        "(workflow_type = 'BATCH_CHAT' AND review_batch_id IS NOT NULL "
        "AND matter_definition_id IS NULL)",
    )


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.execute(
        sa.text(
            "SELECT matter_id FROM matter_definition GROUP BY matter_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "Cannot downgrade multiple review guidance while a matter has more than one profile"
        )

    op.drop_constraint(
        "ck_agent_conversation_workflow_scope",
        "agent_conversation",
        type_="check",
    )
    op.create_check_constraint(
        "ck_agent_conversation_workflow_scope",
        "agent_conversation",
        "(workflow_type = 'MATTER_DEFINITION_SETUP' AND review_batch_id IS NULL) OR "
        "(workflow_type = 'BATCH_CHAT' AND review_batch_id IS NOT NULL)",
    )
    op.drop_index(
        "ix_agent_conversation_matter_definition_id",
        table_name="agent_conversation",
    )
    op.drop_constraint(
        "fk_agent_conversation_matter_definition",
        "agent_conversation",
        type_="foreignkey",
    )
    op.drop_column("agent_conversation", "matter_definition_id")

    op.drop_index(
        "ix_matter_definition_revision_source_revision_id",
        table_name="matter_definition_revision",
    )
    op.drop_index(
        "ix_matter_definition_revision_source_guidance_id",
        table_name="matter_definition_revision",
    )
    op.drop_constraint(
        "fk_matter_definition_revision_source_revision",
        "matter_definition_revision",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_matter_definition_revision_source_guidance",
        "matter_definition_revision",
        type_="foreignkey",
    )
    op.drop_column("matter_definition_revision", "source_content_hash")
    op.drop_column("matter_definition_revision", "source_revision_id")
    op.drop_column("matter_definition_revision", "source_guidance_id")
    op.drop_constraint(
        "ck_matter_definition_revision_source_kind",
        "matter_definition_revision",
        type_="check",
    )
    op.create_check_constraint(
        "ck_matter_definition_revision_source_kind",
        "matter_definition_revision",
        "source_kind IN ('PASTE', 'MARKDOWN', 'TEXT', 'DOCX', 'AGENT_EDIT', "
        "'USER_EDIT', 'ASSESSMENT_REFINEMENT')",
    )

    op.drop_index("ix_matter_definition_matter_status", table_name="matter_definition")
    op.drop_constraint("ck_matter_definition_status", "matter_definition", type_="check")
    op.drop_constraint(
        "uq_matter_definition_matter_key", "matter_definition", type_="unique"
    )
    op.create_unique_constraint(
        "uq_matter_definition_matter", "matter_definition", ["matter_id"]
    )
    op.drop_column("matter_definition", "status")
    op.drop_column("matter_definition", "description")
    op.drop_column("matter_definition", "name")
    op.drop_column("matter_definition", "key")
