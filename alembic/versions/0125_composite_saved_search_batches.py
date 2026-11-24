"""Allow composite saved-search review batches.

Revision ID: 0125_composite_batches
Revises: 0124_entity_relationships
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0125_composite_batches"
down_revision: str | None = "0124_entity_relationships"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SELECTION_TYPES = (
    "'ALL_MATTER', 'SEARCH_QUERY', 'SAVED_SEARCH', 'RANDOM_MATTER', "
    "'RANDOM_BATCH', 'RANDOM_SAVED_SEARCH', 'COMPOSITE_SAVED_SEARCH', "
    "'RANDOM_ASSESSMENT', 'DEFINITION_ASSESSMENT', 'CLASSIFICATION_PREDICTIONS', "
    "'COMPARISON_RESULT', 'TASK_BATCH_PARTITION', 'EXPLICIT_DOCUMENTS'"
)

PREVIOUS_SELECTION_TYPES = (
    "'ALL_MATTER', 'SEARCH_QUERY', 'SAVED_SEARCH', 'RANDOM_MATTER', "
    "'RANDOM_BATCH', 'RANDOM_SAVED_SEARCH', 'RANDOM_ASSESSMENT', "
    "'DEFINITION_ASSESSMENT', 'CLASSIFICATION_PREDICTIONS', 'COMPARISON_RESULT', "
    "'TASK_BATCH_PARTITION', 'EXPLICIT_DOCUMENTS'"
)


def upgrade() -> None:
    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        f"selection_type IN ({SELECTION_TYPES})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        f"selection_type IN ({PREVIOUS_SELECTION_TYPES})",
    )
