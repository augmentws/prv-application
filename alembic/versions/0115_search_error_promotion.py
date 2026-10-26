"""Allow search generations with projection errors to await promotion.

Revision ID: 0115_search_error_promotion
Revises: 0114_review_completion
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0115_search_error_promotion"
down_revision: str | None = "0114_review_completion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_search_index_generation_status", "search_index_generation", type_="check")
    op.create_check_constraint(
        "ck_search_index_generation_status",
        "search_index_generation",
        "status IN ('CREATING', 'READY_WITH_ERRORS', 'ACTIVE', 'RETIRED', 'FAILED')",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE search_index_generation SET status = 'FAILED' "
            "WHERE status = 'READY_WITH_ERRORS'"
        )
    )
    op.drop_constraint("ck_search_index_generation_status", "search_index_generation", type_="check")
    op.create_check_constraint(
        "ck_search_index_generation_status",
        "search_index_generation",
        "status IN ('CREATING', 'ACTIVE', 'RETIRED', 'FAILED')",
    )
