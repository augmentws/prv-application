"""Track analysis-task compiler workflows.

Revision ID: 0040_task_compiler
Revises: 0039_analysis_tasks
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0040_task_compiler"
down_revision: str | None = "0039_analysis_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "matter_analysis_task_version",
        sa.Column("compiler_workflow_run_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_analysis_task_version_compiler_workflow",
        "matter_analysis_task_version",
        "workflow_run",
        ["compiler_workflow_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_analysis_task_version_workflow",
        "matter_analysis_task_version",
        ["compiler_workflow_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_analysis_task_version_workflow", table_name="matter_analysis_task_version")
    op.drop_constraint(
        "fk_analysis_task_version_compiler_workflow",
        "matter_analysis_task_version",
        type_="foreignkey",
    )
    op.drop_column("matter_analysis_task_version", "compiler_workflow_run_id")
