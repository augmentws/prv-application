"""Add generic analysis input manifest workflow artifacts.

Revision ID: 0011_analysis_inputs
Revises: 0010_job_outcomes
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_analysis_inputs"
down_revision: str | None = "0010_job_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LEGACY_TYPES = (
    "'CLASSIFICATION_POPULATION', 'CLASSIFICATION_FEATURES', "
    "'CLASSIFICATION_LABELS', 'CLASSIFICATION_SPLITS', 'CLASSIFICATION_MODEL_PACKAGE', "
    "'CLASSIFICATION_PREDICTIONS', 'CLASSIFICATION_EVALUATION', "
    "'CLASSIFICATION_RESULT_MANIFEST', 'SEARCH_RESULT_EXPORT', "
    "'JOB_ITEM_OUTCOME_FRAGMENT', 'JOB_ITEM_OUTCOMES'"
)

_LEGACY_SCOPES = (
    "'MATTER', 'REVIEW_BATCH', 'CLASSIFICATION_MODEL', "
    "'CLASSIFICATION_CONFIGURATION', 'CLASSIFICATION_DATASET_SNAPSHOT', "
    "'CLASSIFICATION_TRAINING_RUN', 'CLASSIFICATION_SCORING_RUN'"
)


def upgrade() -> None:
    op.drop_constraint("ck_workflow_artifact_type", "workflow_artifact", type_="check")
    op.create_check_constraint(
        "ck_workflow_artifact_type",
        "workflow_artifact",
        f"artifact_type IN ('ANALYSIS_INPUT_MANIFEST', {_LEGACY_TYPES})",
    )
    op.drop_constraint("ck_workflow_artifact_scope_kind", "workflow_artifact", type_="check")
    op.create_check_constraint(
        "ck_workflow_artifact_scope_kind",
        "workflow_artifact",
        f"core_scope_kind IN ('ANALYSIS_INPUT_MANIFEST', {_LEGACY_SCOPES})",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workflow_artifact_scope_kind", "workflow_artifact", type_="check")
    op.create_check_constraint(
        "ck_workflow_artifact_scope_kind",
        "workflow_artifact",
        f"core_scope_kind IN ({_LEGACY_SCOPES})",
    )
    op.drop_constraint("ck_workflow_artifact_type", "workflow_artifact", type_="check")
    op.create_check_constraint(
        "ck_workflow_artifact_type",
        "workflow_artifact",
        f"artifact_type IN ({_LEGACY_TYPES})",
    )
