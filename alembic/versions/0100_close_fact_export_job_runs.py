"""Close executions left by the removed fact export job.

Fact exports now run within the request (0098). Executions of the old MATTER_FACT_EXPORT job type that never finished
have no adapter or workflow left to finish them, so they are marked canceled. Their history is kept.

Revision ID: 0100_close_fact_export_job_runs
Revises: 0099_fact_chunk_provider_batches
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0100_close_fact_export_job_runs"
down_revision: str | None = "0099_fact_chunk_provider_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE workflow_run SET status = 'CANCELED', current_stage = 'CANCELED', "
        "completed_at = COALESCE(completed_at, now()), "
        "failure_summary = 'Fact export jobs were replaced by exports produced within the request' "
        "WHERE job_type = 'MATTER_FACT_EXPORT' AND status IN ('QUEUED', 'RUNNING', 'WAITING')"
    )


def downgrade() -> None:
    # The canceled executions cannot be resumed; there is nothing to restore.
    pass
