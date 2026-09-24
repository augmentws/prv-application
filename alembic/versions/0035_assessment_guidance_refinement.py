"""Add assessment guidance refinement state and suggested answers.

Revision ID: 0035_assessment_refinement
Revises: 0034_assessment_batches
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035_assessment_refinement"
down_revision: str | None = "0034_assessment_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "matter_definition_assessment_question",
        sa.Column("suggested_answers", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
    )
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("guidance_refinement_status", sa.String(length=30), server_default="NOT_READY", nullable=False),
    )
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("guidance_refinement_workflow_run_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("refined_matter_definition_revision_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("guidance_refinement_error_message", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_definition_assessment_guidance_refinement_status",
        "matter_definition_assessment_run",
        "guidance_refinement_status IN "
        "('NOT_READY', 'QUEUED', 'RUNNING', 'COMPLETED', 'NOT_REQUIRED', 'FAILED')",
    )
    op.create_foreign_key(
        "fk_definition_assessment_refinement_workflow",
        "matter_definition_assessment_run",
        "workflow_run",
        ["guidance_refinement_workflow_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_definition_assessment_refined_revision",
        "matter_definition_assessment_run",
        "matter_definition_revision",
        ["refined_matter_definition_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_definition_assessment_refinement_workflow",
        "matter_definition_assessment_run",
        ["guidance_refinement_workflow_run_id"],
    )
    op.add_column(
        "matter_definition_revision",
        sa.Column("source_skill_run_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_matter_definition_revision_source_skill_run",
        "matter_definition_revision",
        "skill_run",
        ["source_skill_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_matter_definition_revision_source_skill_run_id",
        "matter_definition_revision",
        ["source_skill_run_id"],
    )
    op.drop_constraint(
        "ck_matter_definition_revision_source_kind",
        "matter_definition_revision",
        type_="check",
    )
    op.create_check_constraint(
        "ck_matter_definition_revision_source_kind",
        "matter_definition_revision",
        "source_kind IN ('PASTE', 'MARKDOWN', 'TEXT', 'DOCX', 'AGENT_EDIT', 'USER_EDIT', "
        "'ASSESSMENT_REFINEMENT')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_matter_definition_revision_source_kind",
        "matter_definition_revision",
        type_="check",
    )
    op.create_check_constraint(
        "ck_matter_definition_revision_source_kind",
        "matter_definition_revision",
        "source_kind IN ('PASTE', 'MARKDOWN', 'TEXT', 'DOCX', 'AGENT_EDIT', 'USER_EDIT')",
    )
    op.drop_index(
        "ix_matter_definition_revision_source_skill_run_id",
        table_name="matter_definition_revision",
    )
    op.drop_constraint(
        "fk_matter_definition_revision_source_skill_run",
        "matter_definition_revision",
        type_="foreignkey",
    )
    op.drop_column("matter_definition_revision", "source_skill_run_id")
    op.drop_constraint(
        "uq_definition_assessment_refinement_workflow",
        "matter_definition_assessment_run",
        type_="unique",
    )
    op.drop_constraint(
        "fk_definition_assessment_refined_revision",
        "matter_definition_assessment_run",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_definition_assessment_refinement_workflow",
        "matter_definition_assessment_run",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_definition_assessment_guidance_refinement_status",
        "matter_definition_assessment_run",
        type_="check",
    )
    op.drop_column("matter_definition_assessment_run", "guidance_refinement_error_message")
    op.drop_column("matter_definition_assessment_run", "refined_matter_definition_revision_id")
    op.drop_column("matter_definition_assessment_run", "guidance_refinement_workflow_run_id")
    op.drop_column("matter_definition_assessment_run", "guidance_refinement_status")
    op.drop_column("matter_definition_assessment_question", "suggested_answers")
