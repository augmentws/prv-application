"""Allow review batches containing all saved-search matches.

Revision ID: 0121_all_saved_search_batches
Revises: 0120_artifact_coding_compare
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0121_all_saved_search_batches"
down_revision: str | None = "0120_artifact_coding_compare"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'SAVED_SEARCH', 'RANDOM_MATTER', "
        "'RANDOM_BATCH', 'RANDOM_SAVED_SEARCH', 'RANDOM_ASSESSMENT', 'DEFINITION_ASSESSMENT', "
        "'CLASSIFICATION_PREDICTIONS', 'COMPARISON_RESULT', 'TASK_BATCH_PARTITION', "
        "'EXPLICIT_DOCUMENTS')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH', "
        "'RANDOM_SAVED_SEARCH', 'RANDOM_ASSESSMENT', 'DEFINITION_ASSESSMENT', "
        "'CLASSIFICATION_PREDICTIONS', 'COMPARISON_RESULT', 'TASK_BATCH_PARTITION', "
        "'EXPLICIT_DOCUMENTS')",
    )
