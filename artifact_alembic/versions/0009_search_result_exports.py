"""Add matter-scoped search result export workflow artifacts.

Revision ID: 0009_search_result_exports
Revises: 0008_workflow_artifacts
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_search_result_exports"
down_revision: str | None = "0008_workflow_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_workflow_artifact_type", "workflow_artifact", type_="check")
    op.create_check_constraint(
        "ck_workflow_artifact_type",
        "workflow_artifact",
        "artifact_type IN ('CLASSIFICATION_POPULATION', 'CLASSIFICATION_FEATURES', "
        "'CLASSIFICATION_LABELS', 'CLASSIFICATION_SPLITS', 'CLASSIFICATION_MODEL_PACKAGE', "
        "'CLASSIFICATION_PREDICTIONS', 'CLASSIFICATION_EVALUATION', "
        "'CLASSIFICATION_RESULT_MANIFEST', 'SEARCH_RESULT_EXPORT')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workflow_artifact_type", "workflow_artifact", type_="check")
    op.create_check_constraint(
        "ck_workflow_artifact_type",
        "workflow_artifact",
        "artifact_type IN ('CLASSIFICATION_POPULATION', 'CLASSIFICATION_FEATURES', "
        "'CLASSIFICATION_LABELS', 'CLASSIFICATION_SPLITS', 'CLASSIFICATION_MODEL_PACKAGE', "
        "'CLASSIFICATION_PREDICTIONS', 'CLASSIFICATION_EVALUATION', "
        "'CLASSIFICATION_RESULT_MANIFEST')",
    )
