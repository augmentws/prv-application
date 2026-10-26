"""Add immutable input-manifest populations to Analysis Task Batch Sets.

Revision ID: 0113_batch_set_input_manifests
Revises: 0112_task_execution_profiles
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0113_batch_set_input_manifests"
down_revision: str = "0112_task_execution_profiles"
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
        "population_source_type IN "
        "('ALL_MATTER', 'SOURCE_BATCH', 'SEARCH_QUERY', 'SAVED_SEARCH', 'INPUT_MANIFEST')",
    )
    op.add_column(
        "analysis_task_batch_set",
        sa.Column("input_manifest_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_analysis_task_batch_set_input_manifest",
        "analysis_task_batch_set",
        "analysis_input_manifest",
        ["input_manifest_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_analysis_task_batch_set_input_manifest_id",
        "analysis_task_batch_set",
        ["input_manifest_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analysis_task_batch_set_input_manifest_id",
        table_name="analysis_task_batch_set",
    )
    op.drop_constraint(
        "fk_analysis_task_batch_set_input_manifest",
        "analysis_task_batch_set",
        type_="foreignkey",
    )
    op.drop_column("analysis_task_batch_set", "input_manifest_id")
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
