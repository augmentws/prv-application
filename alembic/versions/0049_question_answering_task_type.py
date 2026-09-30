"""Consolidate analysis tasks as Question Answering.

Revision ID: 0049_question_answering
Revises: 0048_allow_definition_tasks
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0049_question_answering"
down_revision: str | None = "0048_allow_definition_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM matter_analysis_task AS task
                JOIN matter_analysis_task_version AS version
                  ON version.matter_analysis_task_id = task.id
                WHERE task.matter_definition_id IS NOT NULL
                  AND (
                    EXISTS (
                        SELECT 1 FROM review_decision_result AS result
                        WHERE result.matter_analysis_task_version_id = version.id
                    )
                    OR EXISTS (
                        SELECT 1 FROM workflow_run AS workflow
                        WHERE workflow.input_snapshot ->> 'task_id' = CAST(task.id AS text)
                          AND workflow.workflow_key IN (
                            'matter_analysis_task_playground_v1',
                            'matter_analysis_task_batch_v1'
                          )
                    )
                  )
            ) THEN
                RAISE EXCEPTION 'Cannot remove a linked Matter Definition task that has document runs';
            END IF;
        END $$
        """
    )
    op.execute("DELETE FROM matter_analysis_task WHERE matter_definition_id IS NOT NULL")

    op.drop_constraint("ck_matter_analysis_task_type", "matter_analysis_task", type_="check")
    op.execute(
        "UPDATE matter_analysis_task SET task_type = 'QUESTION_ANSWERING', "
        "workflow_key = 'question_answering_v1'"
    )
    op.create_check_constraint(
        "ck_matter_analysis_task_type",
        "matter_analysis_task",
        "task_type IN ('QUESTION_ANSWERING')",
    )
    op.drop_index(
        "ix_matter_analysis_task_matter_definition_id",
        table_name="matter_analysis_task",
    )
    op.drop_constraint(
        "uq_matter_analysis_task_definition_source",
        "matter_analysis_task",
        type_="unique",
    )
    op.drop_column("matter_analysis_task", "matter_definition_id")


def downgrade() -> None:
    op.add_column(
        "matter_analysis_task",
        sa.Column(
            "matter_definition_id",
            sa.Uuid(),
            sa.ForeignKey("matter_definition.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_unique_constraint(
        "uq_matter_analysis_task_definition_source",
        "matter_analysis_task",
        ["matter_definition_id"],
    )
    op.create_index(
        "ix_matter_analysis_task_matter_definition_id",
        "matter_analysis_task",
        ["matter_definition_id"],
    )
    op.drop_constraint("ck_matter_analysis_task_type", "matter_analysis_task", type_="check")
    op.execute(
        "UPDATE matter_analysis_task SET task_type = 'CUSTOM_DECISION', "
        "workflow_key = 'custom_decision_v1'"
    )
    op.create_check_constraint(
        "ck_matter_analysis_task_type",
        "matter_analysis_task",
        "task_type IN ('MATTER_DEFINITION', 'ISSUE_REVIEW', 'PRIVILEGE_REVIEW', "
        "'TOPIC_GENERATION', 'DATA_EXPLORATION', 'CUSTOM_DECISION')",
    )
