"""Resolve search schema confirmations covered by a newer active generation.

Revision ID: 0085_resolve_search_ops
Revises: 0084_reviewer_coding
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0085_resolve_search_ops"
down_revision: str | None = "0084_reviewer_coding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE search_projection_operation AS operation
            SET status = 'COMPLETED',
                completed_at = generation.activated_at,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            FROM search_index_generation AS generation
            WHERE operation.matter_id = generation.matter_id
              AND operation.status = 'AWAITING_USER'
              AND generation.status = 'ACTIVE'
              AND generation.activated_at IS NOT NULL
              AND operation.updated_at <= generation.activated_at
            """
        )
    )


def downgrade() -> None:
    # A completed rebuild permanently satisfies the older projection request.
    # Reopening it on downgrade would incorrectly ask for another rebuild.
    pass
