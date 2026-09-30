"""Add the Matter Definition analysis task type.

Revision ID: 0045_matter_definition_task
Revises: 0044_widen_definition_source
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0045_matter_definition_task"
down_revision: str | None = "0044_widen_definition_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_matter_analysis_task_type", "matter_analysis_task", type_="check")
    op.create_check_constraint(
        "ck_matter_analysis_task_type",
        "matter_analysis_task",
        "task_type IN ('MATTER_DEFINITION', 'ISSUE_REVIEW', 'PRIVILEGE_REVIEW', "
        "'TOPIC_GENERATION', 'DATA_EXPLORATION', 'CUSTOM_DECISION')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_matter_analysis_task_type", "matter_analysis_task", type_="check")
    op.create_check_constraint(
        "ck_matter_analysis_task_type",
        "matter_analysis_task",
        "task_type IN ('ISSUE_REVIEW', 'PRIVILEGE_REVIEW', 'TOPIC_GENERATION', "
        "'DATA_EXPLORATION', 'CUSTOM_DECISION')",
    )
