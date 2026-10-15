"""Record per-document extraction fingerprints on fact-development run chunks so finished documents are reused.

Revision ID: 0095_fact_run_fingerprints
Revises: 0094_fact_development_runs
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0095_fact_run_fingerprints"
down_revision: str | None = "0094_fact_development_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "matter_analysis_task_run_chunk",
        sa.Column("fingerprints", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "matter_analysis_task_run_chunk",
        sa.Column("prior_fingerprints", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    op.drop_column("matter_analysis_task_run_chunk", "prior_fingerprints")
    op.drop_column("matter_analysis_task_run_chunk", "fingerprints")
