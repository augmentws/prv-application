"""Add versioned matter analysis tasks.

Revision ID: 0039_analysis_tasks
Revises: 0038_agent_events
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0039_analysis_tasks"
down_revision: str | None = "0038_agent_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matter_analysis_task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("task_type", sa.String(length=30), nullable=False),
        sa.Column("workflow_key", sa.String(length=100), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("published_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("current_version > 0", name="ck_matter_analysis_task_current_version"),
        sa.CheckConstraint(
            "published_version IS NULL OR (published_version > 0 AND published_version <= current_version)",
            name="ck_matter_analysis_task_published_version",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')",
            name="ck_matter_analysis_task_status",
        ),
        sa.CheckConstraint(
            "task_type IN ('ISSUE_REVIEW', 'PRIVILEGE_REVIEW', 'TOPIC_GENERATION', "
            "'DATA_EXPLORATION', 'CUSTOM_DECISION')",
            name="ck_matter_analysis_task_type",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("matter_id", "key", name="uq_matter_analysis_task_matter_key"),
    )
    op.create_index("ix_matter_analysis_task_matter_id", "matter_analysis_task", ["matter_id"])
    op.create_index("ix_matter_analysis_task_task_type", "matter_analysis_task", ["task_type"])
    op.create_index("ix_matter_analysis_task_created_by_user_id", "matter_analysis_task", ["created_by_user_id"])
    op.create_index(
        "ix_matter_analysis_task_matter_type",
        "matter_analysis_task",
        ["matter_id", "task_type"],
    )

    op.create_table(
        "matter_analysis_task_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_analysis_task_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("compilation_status", sa.String(length=30), nullable=False),
        sa.Column("definition_markdown", sa.Text(), nullable=False),
        sa.Column("definition_content_hash", sa.String(length=64), nullable=False),
        sa.Column("decision_specification", sa.JSON(), nullable=True),
        sa.Column("specification_content_hash", sa.String(length=64), nullable=True),
        sa.Column("input_contract", sa.JSON(), nullable=False),
        sa.Column("output_contract", sa.JSON(), nullable=False),
        sa.Column("evidence_policy", sa.JSON(), nullable=False),
        sa.Column("routing_policy", sa.JSON(), nullable=False),
        sa.Column("compiler_skill_definition_version_id", sa.Uuid(), nullable=True),
        sa.Column("compiler_skill_run_id", sa.Uuid(), nullable=True),
        sa.Column("compiler_model_configuration", sa.JSON(), nullable=False),
        sa.Column("validation_report", sa.JSON(), nullable=False),
        sa.Column("source_provenance", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("published_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "compilation_status IN ('NOT_GENERATED', 'STALE', 'GENERATING', 'READY', 'FAILED')",
            name="ck_matter_analysis_task_compilation_status",
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'RETIRED')", name="ck_matter_analysis_task_version_status"
        ),
        sa.CheckConstraint("version > 0", name="ck_matter_analysis_task_version_positive"),
        sa.ForeignKeyConstraint(
            ["compiler_skill_definition_version_id"], ["skill_definition_version.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["compiler_skill_run_id"], ["skill_run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_analysis_task_id"], ["matter_analysis_task.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["published_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("matter_analysis_task_id", "version", name="uq_matter_analysis_task_version"),
    )
    op.create_index(
        "ix_matter_analysis_task_version_matter_analysis_task_id",
        "matter_analysis_task_version",
        ["matter_analysis_task_id"],
    )
    op.create_index(
        "ix_matter_analysis_task_version_definition_content_hash",
        "matter_analysis_task_version",
        ["definition_content_hash"],
    )
    op.create_index(
        "ix_matter_analysis_task_version_specification_content_hash",
        "matter_analysis_task_version",
        ["specification_content_hash"],
    )
    op.create_index(
        "ix_analysis_task_version_skill_definition",
        "matter_analysis_task_version",
        ["compiler_skill_definition_version_id"],
    )
    op.create_index(
        "ix_analysis_task_version_skill_run",
        "matter_analysis_task_version",
        ["compiler_skill_run_id"],
    )
    op.create_index(
        "ix_analysis_task_version_creator",
        "matter_analysis_task_version",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_analysis_task_version_publisher",
        "matter_analysis_task_version",
        ["published_by_user_id"],
    )
    op.create_index(
        "ix_matter_analysis_task_version_task_status",
        "matter_analysis_task_version",
        ["matter_analysis_task_id", "status"],
    )

    op.create_table(
        "matter_analysis_task_version_dependency",
        sa.Column("matter_analysis_task_version_id", sa.Uuid(), nullable=False),
        sa.Column("dependency_task_version_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=100), nullable=False),
        sa.Column("dependency_content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "matter_analysis_task_version_id <> dependency_task_version_id",
            name="ck_matter_analysis_task_dependency_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["dependency_task_version_id"],
            ["matter_analysis_task_version.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["matter_analysis_task_version_id"],
            ["matter_analysis_task_version.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("matter_analysis_task_version_id", "dependency_task_version_id", "role"),
    )
    op.create_index(
        "ix_analysis_task_dependency_version",
        "matter_analysis_task_version_dependency",
        ["dependency_task_version_id"],
    )


def downgrade() -> None:
    op.drop_table("matter_analysis_task_version_dependency")
    op.drop_table("matter_analysis_task_version")
    op.drop_table("matter_analysis_task")
