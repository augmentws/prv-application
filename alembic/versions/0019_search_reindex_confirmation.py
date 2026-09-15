"""Require user confirmation for search reindex operations.

Revision ID: 0019_reindex_confirmation
Revises: 0018_batch_search_projection
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019_reindex_confirmation"
down_revision: str | None = "0018_batch_search_projection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_search_projection_status", "search_projection_operation", type_="check")
    op.create_check_constraint(
        "ck_search_projection_status",
        "search_projection_operation",
        "status IN ('QUEUED', 'RUNNING', 'AWAITING_USER', 'COMPLETED', 'FAILED')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE search_projection_operation SET status = 'FAILED', "
        "error_message = COALESCE(error_message, 'Reindex confirmation was not completed before downgrade') "
        "WHERE status = 'AWAITING_USER'"
    )
    op.drop_constraint("ck_search_projection_status", "search_projection_operation", type_="check")
    op.create_check_constraint(
        "ck_search_projection_status",
        "search_projection_operation",
        "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')",
    )
