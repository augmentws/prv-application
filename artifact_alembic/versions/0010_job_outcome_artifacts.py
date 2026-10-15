"""Add generic job item outcome workflow artifacts.

Revision ID: 0010_job_outcomes
Revises: 0009_search_result_exports
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_job_outcomes"
down_revision: str | None = "0009_search_result_exports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CLASSIFICATION_TYPES = (
    "'CLASSIFICATION_POPULATION', 'CLASSIFICATION_FEATURES', "
    "'CLASSIFICATION_LABELS', 'CLASSIFICATION_SPLITS', 'CLASSIFICATION_MODEL_PACKAGE', "
    "'CLASSIFICATION_PREDICTIONS', 'CLASSIFICATION_EVALUATION', "
    "'CLASSIFICATION_RESULT_MANIFEST', 'SEARCH_RESULT_EXPORT'"
)


def upgrade() -> None:
    op.drop_constraint("ck_workflow_artifact_type", "workflow_artifact", type_="check")
    op.create_check_constraint(
        "ck_workflow_artifact_type",
        "workflow_artifact",
        f"artifact_type IN ({_CLASSIFICATION_TYPES}, "
        "'JOB_ITEM_OUTCOME_FRAGMENT', 'JOB_ITEM_OUTCOMES')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workflow_artifact_type", "workflow_artifact", type_="check")
    op.create_check_constraint(
        "ck_workflow_artifact_type",
        "workflow_artifact",
        f"artifact_type IN ({_CLASSIFICATION_TYPES})",
    )
