"""Allow deterministic review-batch samples from saved searches.

Revision ID: 0042_saved_search_batches
Revises: 0041_decision_results
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0042_saved_search_batches"
down_revision: str | None = "0041_decision_results"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH', "
        "'RANDOM_SAVED_SEARCH', 'DEFINITION_ASSESSMENT')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH', "
        "'DEFINITION_ASSESSMENT')",
    )
