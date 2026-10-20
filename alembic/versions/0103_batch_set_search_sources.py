"""Add search-backed Analysis Task Batch Set sources.

Revision ID: 0103_batch_set_search
Revises: 0102_task_batch_sets
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0103_batch_set_search"
down_revision: str = "0102_task_batch_sets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_analysis_task_batch_set_source_type",
        "analysis_task_batch_set",
        type_="check",
    )
    op.create_check_constraint(
        "ck_analysis_task_batch_set_source_type",
        "analysis_task_batch_set",
        "population_source_type IN ('ALL_MATTER', 'SOURCE_BATCH', 'SEARCH_QUERY', 'SAVED_SEARCH')",
    )
    op.add_column(
        "analysis_task_batch_set",
        sa.Column("search_index_generation_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_analysis_task_batch_set_search_index_generation",
        "analysis_task_batch_set",
        "search_index_generation",
        ["search_index_generation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_analysis_task_batch_set_search_index_generation_id",
        "analysis_task_batch_set",
        ["search_index_generation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analysis_task_batch_set_search_index_generation_id",
        table_name="analysis_task_batch_set",
    )
    op.drop_constraint(
        "fk_analysis_task_batch_set_search_index_generation",
        "analysis_task_batch_set",
        type_="foreignkey",
    )
    op.drop_column("analysis_task_batch_set", "search_index_generation_id")
    op.drop_constraint(
        "ck_analysis_task_batch_set_source_type",
        "analysis_task_batch_set",
        type_="check",
    )
    op.create_check_constraint(
        "ck_analysis_task_batch_set_source_type",
        "analysis_task_batch_set",
        "population_source_type IN ('ALL_MATTER', 'SOURCE_BATCH')",
    )
