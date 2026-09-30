"""Allow independent Matter Definition analysis tasks.

Revision ID: 0048_allow_definition_tasks
Revises: 0047_reconcile_definition_task
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0048_allow_definition_tasks"
down_revision: str | None = "0047_reconcile_definition_task"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "matter_analysis_task",
        sa.Column(
            "matter_definition_id",
            sa.Uuid(),
            sa.ForeignKey("matter_definition.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE matter_analysis_task AS task SET matter_definition_id = definition.id "
        "FROM matter_definition AS definition WHERE task.matter_id = definition.matter_id "
        "AND task.task_type = 'MATTER_DEFINITION' AND EXISTS ("
        "SELECT 1 FROM matter_analysis_task_version AS version "
        "WHERE version.matter_analysis_task_id = task.id "
        "AND version.source_provenance ->> 'origin' = 'MATTER_DEFINITION_REVISION')"
    )
    op.drop_index(
        "uq_matter_analysis_task_matter_definition",
        table_name="matter_analysis_task",
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


def downgrade() -> None:
    op.drop_index(
        "ix_matter_analysis_task_matter_definition_id",
        table_name="matter_analysis_task",
    )
    op.drop_constraint(
        "uq_matter_analysis_task_definition_source",
        "matter_analysis_task",
        type_="unique",
    )
    op.create_index(
        "uq_matter_analysis_task_matter_definition",
        "matter_analysis_task",
        ["matter_id"],
        unique=True,
        postgresql_where=sa.text("task_type = 'MATTER_DEFINITION'"),
    )
    op.drop_column("matter_analysis_task", "matter_definition_id")
