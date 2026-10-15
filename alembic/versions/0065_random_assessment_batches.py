"""Allow deterministic review-batch samples from assessment candidates.

Revision ID: 0065_random_assessment
Revises: 0064_analysis_promotions
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0065_random_assessment"
down_revision: str | None = "0064_analysis_promotions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH', "
        "'RANDOM_SAVED_SEARCH', 'RANDOM_ASSESSMENT', 'DEFINITION_ASSESSMENT')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH', "
        "'RANDOM_SAVED_SEARCH', 'DEFINITION_ASSESSMENT')",
    )
