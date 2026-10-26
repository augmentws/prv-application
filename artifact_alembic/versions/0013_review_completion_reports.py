"""Allow immutable Review Program completion-report artifacts.

Revision ID: 0013_review_completion
Revises: 0012_fact_exports
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_review_completion"
down_revision: str | None = "0012_fact_exports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRIOR_TYPES = (
    "'ANALYSIS_INPUT_MANIFEST', 'CLASSIFICATION_POPULATION', 'CLASSIFICATION_FEATURES', "
    "'CLASSIFICATION_LABELS', 'CLASSIFICATION_SPLITS', 'CLASSIFICATION_MODEL_PACKAGE', "
    "'CLASSIFICATION_PREDICTIONS', 'CLASSIFICATION_EVALUATION', "
    "'CLASSIFICATION_RESULT_MANIFEST', 'SEARCH_RESULT_EXPORT', "
    "'JOB_ITEM_OUTCOME_FRAGMENT', 'JOB_ITEM_OUTCOMES', 'FACT_EXPORT'"
)


def upgrade() -> None:
    op.drop_constraint("ck_workflow_artifact_type", "workflow_artifact", type_="check")
    op.create_check_constraint(
        "ck_workflow_artifact_type",
        "workflow_artifact",
        f"artifact_type IN ({PRIOR_TYPES}, 'REVIEW_PROGRAM_COMPLETION_REPORT')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workflow_artifact_type", "workflow_artifact", type_="check")
    op.create_check_constraint("ck_workflow_artifact_type", "workflow_artifact", f"artifact_type IN ({PRIOR_TYPES})")
