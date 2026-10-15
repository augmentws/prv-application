"""allow safe retry of legacy analysis document failures

Revision ID: 0079_legacy_analysis_retry
Revises: 0078_analysis_doc_failures
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0079_legacy_analysis_retry"
down_revision: str | None = "0078_analysis_doc_failures"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE review_batch_run_document
            SET failure_retriable = true
            WHERE status = 'FAILED'
              AND failure_code = 'LEGACY_FAILURE_DETAILS_UNAVAILABLE'
              AND failure_message =
                  'This document failed before per-document failure details were retained.'
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE review_batch_run_document
            SET failure_retriable = false
            WHERE status = 'FAILED'
              AND failure_code = 'LEGACY_FAILURE_DETAILS_UNAVAILABLE'
              AND failure_message =
                  'This document failed before per-document failure details were retained.'
            """
        )
    )
