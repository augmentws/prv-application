"""Add immutable Analysis Task execution profiles.

Revision ID: 0112_task_execution_profiles
Revises: 0111_review_program_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0112_task_execution_profiles"
down_revision: str = "0111_review_program_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_task_execution_profile",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_analysis_task_version_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("decision_provider", sa.String(length=30), nullable=False),
        sa.Column("model_key", sa.String(length=500), nullable=False),
        sa.Column("resolved_model", sa.String(length=500), nullable=False),
        sa.Column("model_policy", sa.JSON(), nullable=False),
        sa.Column("required_capabilities", sa.JSON(), nullable=False),
        sa.Column("skill_definition_version_id", sa.Uuid(), nullable=False),
        sa.Column("provider_adapter_version", sa.String(length=100), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision_provider IN ('JEV', 'OPENAI_DECISIONS')",
            name="ck_analysis_task_execution_profile_provider",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["app_user.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["matter_analysis_task_version_id"],
            ["matter_analysis_task_version.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["skill_definition_version_id"],
            ["skill_definition_version.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "matter_analysis_task_version_id",
            "content_hash",
            name="uq_analysis_task_execution_profile_content",
        ),
        sa.UniqueConstraint(
            "matter_analysis_task_version_id",
            "key",
            name="uq_analysis_task_execution_profile_key",
        ),
    )
    for name, columns in (
        ("ix_analysis_task_execution_profile_version", ["matter_analysis_task_version_id"]),
        ("ix_analysis_task_execution_profile_skill_definition_version_id", ["skill_definition_version_id"]),
        ("ix_analysis_task_execution_profile_content_hash", ["content_hash"]),
        ("ix_analysis_task_execution_profile_created_by_user_id", ["created_by_user_id"]),
    ):
        op.create_index(name, "analysis_task_execution_profile", columns)

    op.add_column(
        "analysis_task_batch_set_run",
        sa.Column("execution_profile_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_batch_set_run_execution_profile",
        "analysis_task_batch_set_run",
        "analysis_task_execution_profile",
        ["execution_profile_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_analysis_task_batch_set_run_execution_profile_id",
        "analysis_task_batch_set_run",
        ["execution_profile_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analysis_task_batch_set_run_execution_profile_id",
        table_name="analysis_task_batch_set_run",
    )
    op.drop_constraint(
        "fk_batch_set_run_execution_profile",
        "analysis_task_batch_set_run",
        type_="foreignkey",
    )
    op.drop_column("analysis_task_batch_set_run", "execution_profile_id")
    op.drop_table("analysis_task_execution_profile")
