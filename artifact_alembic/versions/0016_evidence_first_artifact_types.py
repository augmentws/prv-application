"""Admit evidence-first workflow artifact types.

Revision ID: 0016_evidence_artifacts
Revises: 0015_artifact_content_blob_idx
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_evidence_artifacts"
down_revision: str | None = "0015_artifact_content_blob_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TYPES = (
    "ANALYSIS_INPUT_MANIFEST",
    "CLASSIFICATION_POPULATION",
    "CLASSIFICATION_FEATURES",
    "CLASSIFICATION_LABELS",
    "CLASSIFICATION_SPLITS",
    "CLASSIFICATION_MODEL_PACKAGE",
    "CLASSIFICATION_PREDICTIONS",
    "CLASSIFICATION_EVALUATION",
    "CLASSIFICATION_RESULT_MANIFEST",
    "SEARCH_RESULT_EXPORT",
    "JOB_ITEM_OUTCOME_FRAGMENT",
    "JOB_ITEM_OUTCOMES",
    "FACT_EXPORT",
    "REVIEW_PROGRAM_COMPLETION_REPORT",
)
_NEW_TYPES = (
    "ANALYSIS_EVALUATION_PLAN",
    "ANALYSIS_OBSERVATION_FRAGMENT",
    "ANALYSIS_OBSERVATIONS",
)


def _artifact_type_constraint(values: tuple[str, ...]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(
        f"artifact_type IN ({allowed})",
        name="ck_workflow_artifact_type",
    )


def upgrade() -> None:
    op.drop_constraint("ck_workflow_artifact_type", "workflow_artifact", type_="check")
    op.create_check_constraint(
        "ck_workflow_artifact_type",
        "workflow_artifact",
        _artifact_type_constraint(_OLD_TYPES + _NEW_TYPES).sqltext,
    )


def downgrade() -> None:
    op.drop_constraint("ck_workflow_artifact_type", "workflow_artifact", type_="check")
    op.create_check_constraint(
        "ck_workflow_artifact_type",
        "workflow_artifact",
        _artifact_type_constraint(_OLD_TYPES).sqltext,
    )
