"""make oversized analysis document failures retryable

Revision ID: 0080_analysis_input_retry
Revises: 0079_legacy_analysis_retry
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0080_analysis_input_retry"
down_revision: str | None = "0079_legacy_analysis_retry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_MESSAGE = (
    "Document exceeds the single-request analysis limit; windowed evaluation is not yet enabled"
)


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE review_batch_run_document
            SET failure_code = 'ANALYSIS_DOCUMENT_INPUT_TOO_LARGE',
                failure_retriable = true,
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'FAILED'
              AND failure_code = 'ANALYSIS_DOCUMENT_CONFLICT'
              AND failure_message = :legacy_message
            """
        ).bindparams(legacy_message=_LEGACY_MESSAGE)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE review_batch_run_document
            SET failure_code = 'ANALYSIS_DOCUMENT_CONFLICT',
                failure_retriable = false,
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'FAILED'
              AND failure_code = 'ANALYSIS_DOCUMENT_INPUT_TOO_LARGE'
              AND failure_message = :legacy_message
            """
        ).bindparams(legacy_message=_LEGACY_MESSAGE)
    )
