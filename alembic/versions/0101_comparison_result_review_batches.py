"""Allow review batches derived from coding-comparison disagreements.

Revision ID: 0101_comparison_result_batches
Revises: 0100_close_fact_export_job_runs
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0101_comparison_result_batches"
down_revision: str | None = "0100_close_fact_export_job_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH', "
        "'RANDOM_SAVED_SEARCH', 'RANDOM_ASSESSMENT', 'DEFINITION_ASSESSMENT', "
        "'CLASSIFICATION_PREDICTIONS', 'COMPARISON_RESULT')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH', "
        "'RANDOM_SAVED_SEARCH', 'RANDOM_ASSESSMENT', 'DEFINITION_ASSESSMENT', "
        "'CLASSIFICATION_PREDICTIONS')",
    )
