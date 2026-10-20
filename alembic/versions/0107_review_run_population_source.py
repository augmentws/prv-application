"""Require one population source for analysis-task batch runs.

Revision ID: 0107_review_run_source
Revises: 0106_whole_matter_manifests
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0107_review_run_source"
down_revision: str = "0106_whole_matter_manifests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_review_batch_run_population_source",
        "review_batch_run",
        "(review_batch_id IS NOT NULL AND analysis_input_manifest_id IS NULL) OR "
        "(review_batch_id IS NULL AND analysis_input_manifest_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_review_batch_run_population_source",
        "review_batch_run",
        type_="check",
    )
