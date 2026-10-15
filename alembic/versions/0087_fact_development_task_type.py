"""Add the Fact Development analysis-task type and its Review Guidance pin.

Revision ID: 0087_fact_development_task
Revises: 0086_reviewer_coding_history
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0087_fact_development_task"
down_revision: str | None = "0086_reviewer_coding_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_matter_analysis_task_type", "matter_analysis_task", type_="check")
    op.create_check_constraint(
        "ck_matter_analysis_task_type",
        "matter_analysis_task",
        "task_type IN ('QUESTION_ANSWERING', 'CLASSIFICATION', 'FACT_DEVELOPMENT')",
    )
    op.add_column("matter_analysis_task_version", sa.Column("review_guidance_id", sa.Uuid(), nullable=True))
    op.add_column(
        "matter_analysis_task_version",
        sa.Column("review_guidance_revision_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "matter_analysis_task_version",
        sa.Column("review_guidance_content_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "matter_analysis_task_version",
        sa.Column("fact_development_specification", sa.JSON(), nullable=True),
    )
    op.create_foreign_key(
        "fk_analysis_task_version_review_guidance",
        "matter_analysis_task_version",
        "matter_definition",
        ["review_guidance_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_analysis_task_version_review_guidance_revision",
        "matter_analysis_task_version",
        "matter_definition_revision",
        ["review_guidance_revision_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_matter_analysis_task_version_review_guidance_id",
        "matter_analysis_task_version",
        ["review_guidance_id"],
    )
    op.create_index(
        "ix_matter_analysis_task_version_review_guidance_revision_id",
        "matter_analysis_task_version",
        ["review_guidance_revision_id"],
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM matter_analysis_task_version WHERE matter_analysis_task_id IN "
        "(SELECT id FROM matter_analysis_task WHERE task_type = 'FACT_DEVELOPMENT')"
    )
    op.execute("DELETE FROM matter_analysis_task WHERE task_type = 'FACT_DEVELOPMENT'")
    op.drop_index(
        "ix_matter_analysis_task_version_review_guidance_revision_id",
        table_name="matter_analysis_task_version",
    )
    op.drop_index("ix_matter_analysis_task_version_review_guidance_id", table_name="matter_analysis_task_version")
    op.drop_constraint(
        "fk_analysis_task_version_review_guidance_revision",
        "matter_analysis_task_version",
        type_="foreignkey",
    )
    op.drop_constraint("fk_analysis_task_version_review_guidance", "matter_analysis_task_version", type_="foreignkey")
    op.drop_column("matter_analysis_task_version", "fact_development_specification")
    op.drop_column("matter_analysis_task_version", "review_guidance_content_hash")
    op.drop_column("matter_analysis_task_version", "review_guidance_revision_id")
    op.drop_column("matter_analysis_task_version", "review_guidance_id")
    op.drop_constraint("ck_matter_analysis_task_type", "matter_analysis_task", type_="check")
    op.create_check_constraint(
        "ck_matter_analysis_task_type",
        "matter_analysis_task",
        "task_type IN ('QUESTION_ANSWERING', 'CLASSIFICATION')",
    )
