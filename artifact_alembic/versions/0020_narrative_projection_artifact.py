"""Admit the optional evidence narrative-projection artifact type.

Revision ID: 0020_narrative_projection
Revises: 0019_compatibility_result
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_narrative_projection"
down_revision: str | None = "0019_compatibility_result"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TYPES = (
    "ANALYSIS_INPUT_MANIFEST",
    "ANALYSIS_EVALUATION_PLAN",
    "ANALYSIS_OBSERVATION_FRAGMENT",
    "ANALYSIS_OBSERVATIONS",
    "ANALYSIS_ADJUDICATION_PLAN",
    "ANALYSIS_CRITERION_ADJUDICATION_FRAGMENT",
    "ANALYSIS_CRITERION_DECISIONS",
    "ANALYSIS_DOCUMENT_DECISION",
    "ANALYSIS_COMPATIBILITY_RESULT",
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
_NEW_TYPES = ("ANALYSIS_NARRATIVE_PROJECTION",)


def _constraint(values: tuple[str, ...]) -> sa.CheckConstraint:
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
        _constraint(_OLD_TYPES + _NEW_TYPES).sqltext,
    )


def downgrade() -> None:
    op.drop_constraint("ck_workflow_artifact_type", "workflow_artifact", type_="check")
    op.create_check_constraint(
        "ck_workflow_artifact_type",
        "workflow_artifact",
        _constraint(_OLD_TYPES).sqltext,
    )
