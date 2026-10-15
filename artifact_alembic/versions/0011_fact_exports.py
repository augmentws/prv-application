"""Allow matter-scoped fact export workflow artifacts.

Revision ID: 0011_fact_exports
Revises: 0010_job_outcomes
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_fact_exports"
down_revision: str | None = "0010_job_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRIOR_TYPES = (
    "'CLASSIFICATION_POPULATION', 'CLASSIFICATION_FEATURES', "
    "'CLASSIFICATION_LABELS', 'CLASSIFICATION_SPLITS', 'CLASSIFICATION_MODEL_PACKAGE', "
    "'CLASSIFICATION_PREDICTIONS', 'CLASSIFICATION_EVALUATION', "
    "'CLASSIFICATION_RESULT_MANIFEST', 'SEARCH_RESULT_EXPORT', "
    "'JOB_ITEM_OUTCOME_FRAGMENT', 'JOB_ITEM_OUTCOMES'"
)


def upgrade() -> None:
    op.drop_constraint("ck_workflow_artifact_type", "workflow_artifact", type_="check")
    op.create_check_constraint(
        "ck_workflow_artifact_type",
        "workflow_artifact",
        f"artifact_type IN ({PRIOR_TYPES}, 'FACT_EXPORT')",
    )


def downgrade() -> None:
    # Like 0009, this fails rather than deleting stored exports if any FACT_EXPORT artifacts exist.
    op.drop_constraint("ck_workflow_artifact_type", "workflow_artifact", type_="check")
    op.create_check_constraint("ck_workflow_artifact_type", "workflow_artifact", f"artifact_type IN ({PRIOR_TYPES})")
