"""Record provider Batch API submissions on fact-development chunks.

Revision ID: 0099_fact_chunk_provider_batches
Revises: 0098_fact_exports_in_request
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0099_fact_chunk_provider_batches"
down_revision: str | None = "0098_fact_exports_in_request"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "matter_analysis_task_run_chunk"
COLUMNS = (
    ("provider_batch_status", sa.String(length=30)),
    ("provider", sa.String(length=50)),
    ("provider_model", sa.String(length=200)),
    ("provider_batch_id", sa.String(length=255)),
    ("provider_state", sa.String(length=100)),
    ("provider_manifest", sa.JSON()),
    ("provider_error", sa.Text()),
    ("provider_submitted_at", sa.DateTime(timezone=True)),
    ("provider_completed_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for name, type_ in COLUMNS:
        op.add_column(TABLE, sa.Column(name, type_, nullable=True))
    op.create_check_constraint(
        "ck_analysis_task_run_chunk_provider_batch_status",
        TABLE,
        "provider_batch_status IS NULL OR provider_batch_status IN "
        "('SUBMITTED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELED')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_analysis_task_run_chunk_provider_batch_status", TABLE, type_="check")
    for name, _ in reversed(COLUMNS):
        op.drop_column(TABLE, name)
