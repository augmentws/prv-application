"""pin the decision provider on analysis task versions

Revision ID: 0076_analysis_task_provider
Revises: 0075_classification_tail
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0076_analysis_task_provider"
down_revision: str | None = "0075_classification_tail"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "matter_analysis_task_version",
        sa.Column("decision_provider", sa.String(length=30), nullable=False, server_default="JEV"),
    )
    op.add_column(
        "matter_analysis_task_version",
        sa.Column("decision_model", sa.String(length=200), nullable=True),
    )
    op.create_check_constraint(
        "ck_matter_analysis_task_version_decision_provider",
        "matter_analysis_task_version",
        "decision_provider IN ('JEV', 'OPENAI_DECISIONS')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_matter_analysis_task_version_decision_provider",
        "matter_analysis_task_version",
        type_="check",
    )
    op.drop_column("matter_analysis_task_version", "decision_model")
    op.drop_column("matter_analysis_task_version", "decision_provider")
