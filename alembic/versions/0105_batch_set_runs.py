"""Add Analysis Task Batch Set parent runs.

Revision ID: 0105_batch_set_runs
Revises: 0104_batch_set_runs
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0105_batch_set_runs"
down_revision: str = "0104_batch_set_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_task_batch_set_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("matter_id", sa.Uuid(), sa.ForeignKey("matter.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "analysis_task_batch_set_id",
            sa.Uuid(),
            sa.ForeignKey("analysis_task_batch_set.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "matter_analysis_task_id",
            sa.Uuid(),
            sa.ForeignKey("matter_analysis_task.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "matter_analysis_task_version_id",
            sa.Uuid(),
            sa.ForeignKey("matter_analysis_task_version.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("task_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="QUEUED"),
        sa.Column("configuration_snapshot", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("workflow_id", sa.String(500), nullable=False),
        sa.Column("current_workflow_run_id", sa.Uuid(), sa.ForeignKey("workflow_run.id", ondelete="SET NULL")),
        sa.Column("child_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_child_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_child_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("canceled_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "task_type IN ('QUESTION_ANSWERING', 'CLASSIFICATION', 'FACT_DEVELOPMENT')",
            name="ck_analysis_task_batch_set_run_task_type",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_analysis_task_batch_set_run_status",
        ),
        sa.CheckConstraint(
            "child_count >= 0 AND completed_child_count >= 0 AND failed_child_count >= 0",
            name="ck_analysis_task_batch_set_run_counts",
        ),
        sa.UniqueConstraint("analysis_task_batch_set_id", "idempotency_key", name="uq_batch_set_run_idempotency"),
        sa.UniqueConstraint("workflow_id", name="uq_batch_set_run_workflow"),
    )
    op.create_index("ix_batch_set_run_set_created", "analysis_task_batch_set_run", ["analysis_task_batch_set_id", "created_at"])
    op.create_index("ix_batch_set_run_matter_status", "analysis_task_batch_set_run", ["matter_id", "status"])
    op.create_table(
        "analysis_task_batch_set_run_child",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "batch_set_run_id",
            sa.Uuid(),
            sa.ForeignKey("analysis_task_batch_set_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("review_batch_id", sa.Uuid(), sa.ForeignKey("review_batch.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("run_type", sa.String(30)),
        sa.Column("child_run_id", sa.Uuid()),
        sa.Column("status", sa.String(30), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("ordinal > 0", name="ck_batch_set_run_child_ordinal"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_batch_set_run_child_attempt_count"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_batch_set_run_child_status",
        ),
        sa.UniqueConstraint("batch_set_run_id", "ordinal", name="uq_batch_set_run_child_ordinal"),
        sa.UniqueConstraint("batch_set_run_id", "review_batch_id", name="uq_batch_set_run_child_batch"),
    )
    op.create_index("ix_batch_set_run_child_parent_status", "analysis_task_batch_set_run_child", ["batch_set_run_id", "status"])
    op.create_index("ix_batch_set_run_child_run", "analysis_task_batch_set_run_child", ["child_run_id"])


def downgrade() -> None:
    op.drop_table("analysis_task_batch_set_run_child")
    op.drop_table("analysis_task_batch_set_run")
