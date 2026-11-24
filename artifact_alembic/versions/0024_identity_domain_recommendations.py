"""Admit Identity Analysis recommendation artifacts.

Revision ID: 0024_identity_domain_recs
Revises: 0023_identity_analysis
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024_identity_domain_recs"
down_revision: str | None = "0023_identity_analysis"
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
    "ANALYSIS_NARRATIVE_PROJECTION",
    "ANALYSIS_OUTCOME_MANIFEST",
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
    "CODING_COMPARISON_POPULATION",
    "CODING_COMPARISON_RESULT",
    "IDENTITY_ANALYSIS_RESULT",
)


def _replace(values: tuple[str, ...]) -> None:
    allowed = ", ".join(f"'{value}'" for value in values)
    op.drop_constraint("ck_workflow_artifact_type", "workflow_artifact", type_="check")
    op.create_check_constraint("ck_workflow_artifact_type", "workflow_artifact", f"artifact_type IN ({allowed})")


def upgrade() -> None:
    _replace((*_OLD_TYPES, "IDENTITY_DOMAIN_RECOMMENDATIONS"))


def downgrade() -> None:
    _replace(_OLD_TYPES)
