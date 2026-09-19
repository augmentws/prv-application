"""Track review batch search projection state.

Revision ID: 0018_batch_search_projection
Revises: 0017_review_batch_run_documents
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_batch_search_projection"
down_revision: str | None = "0017_review_batch_run_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_batch",
        sa.Column("search_status", sa.String(20), server_default="QUEUED", nullable=False),
    )
    op.add_column("review_batch", sa.Column("search_error_message", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_review_batch_search_status",
        "review_batch",
        "search_status IN ('QUEUED', 'SYNCING', 'READY', 'FAILED', 'NOT_CONFIGURED')",
    )
    op.create_index("ix_review_batch_search_status", "review_batch", ["search_status"])


def downgrade() -> None:
    op.drop_index("ix_review_batch_search_status", table_name="review_batch")
    op.drop_constraint("ck_review_batch_search_status", "review_batch", type_="check")
    op.drop_column("review_batch", "search_error_message")
    op.drop_column("review_batch", "search_status")
