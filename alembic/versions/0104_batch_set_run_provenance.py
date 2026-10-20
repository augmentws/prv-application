"""Add Batch Set provenance to child Analysis Task runs.

Revision ID: 0104_batch_set_runs
Revises: 0103_batch_set_search
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0104_batch_set_runs"
down_revision: str = "0103_batch_set_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_provenance(table_name: str) -> None:
    op.add_column(table_name, sa.Column("analysis_task_batch_set_id", sa.Uuid(), nullable=True))
    op.add_column(table_name, sa.Column("batch_set_ordinal", sa.Integer(), nullable=True))
    op.create_foreign_key(
        f"fk_{table_name}_analysis_task_batch_set",
        table_name,
        "analysis_task_batch_set",
        ["analysis_task_batch_set_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        f"ck_{table_name}_batch_set_provenance",
        table_name,
        "(analysis_task_batch_set_id IS NULL AND batch_set_ordinal IS NULL) OR "
        "(analysis_task_batch_set_id IS NOT NULL AND batch_set_ordinal IS NOT NULL AND batch_set_ordinal > 0)",
    )
    op.create_index(f"ix_{table_name}_analysis_task_batch_set_id", table_name, ["analysis_task_batch_set_id"])


def _drop_provenance(table_name: str) -> None:
    op.drop_index(f"ix_{table_name}_analysis_task_batch_set_id", table_name=table_name)
    op.drop_constraint(f"ck_{table_name}_batch_set_provenance", table_name, type_="check")
    op.drop_constraint(f"fk_{table_name}_analysis_task_batch_set", table_name, type_="foreignkey")
    op.drop_column(table_name, "batch_set_ordinal")
    op.drop_column(table_name, "analysis_task_batch_set_id")


def upgrade() -> None:
    _add_provenance("review_batch_run")
    _add_provenance("matter_analysis_task_run")


def downgrade() -> None:
    _drop_provenance("matter_analysis_task_run")
    _drop_provenance("review_batch_run")
