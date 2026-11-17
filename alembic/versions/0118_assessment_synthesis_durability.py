"""Add durable staged assessment synthesis.

Revision ID: 0118_synthesis_durability
Revises: 0117_matter_deletion
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0118_synthesis_durability"
down_revision: str | None = "0117_matter_deletion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("synthesis_status", sa.String(length=30), nullable=False, server_default="NOT_STARTED"),
    )
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("completion_disposition", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("current_synthesis_attempt_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("synthesis_diagnostics", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_check_constraint(
        "ck_definition_assessment_synthesis_status",
        "matter_definition_assessment_run",
        "synthesis_status IN ('NOT_STARTED', 'RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED')",
    )
    op.create_check_constraint(
        "ck_definition_assessment_completion_disposition",
        "matter_definition_assessment_run",
        "completion_disposition IS NULL OR completion_disposition IN "
        "('READY', 'READY_WITH_WARNINGS', 'ACTION_REQUIRED')",
    )
    op.create_table(
        "matter_definition_assessment_synthesis_attempt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assessment_run_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_version", sa.String(length=100), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_summary", sa.JSON(), nullable=False),
        sa.Column("workflow_version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("completion_disposition", sa.String(length=30), nullable=True),
        sa.Column("total_unit_count", sa.Integer(), nullable=False),
        sa.Column("completed_unit_count", sa.Integer(), nullable=False),
        sa.Column("failed_unit_count", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("diagnostics", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED')",
            name="ck_definition_assessment_synthesis_attempt_status",
        ),
        sa.CheckConstraint(
            "completion_disposition IS NULL OR completion_disposition IN "
            "('READY', 'READY_WITH_WARNINGS', 'ACTION_REQUIRED')",
            name="ck_definition_assessment_synthesis_attempt_disposition",
        ),
        sa.CheckConstraint(
            "total_unit_count >= 0 AND completed_unit_count >= 0 AND failed_unit_count >= 0",
            name="ck_definition_assessment_synthesis_attempt_counts",
        ),
        sa.ForeignKeyConstraint(
            ["assessment_run_id"], ["matter_definition_assessment_run.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_definition_assessment_synthesis_attempt_assessment_run_id",
        "matter_definition_assessment_synthesis_attempt",
        ["assessment_run_id"],
    )
    op.create_index(
        "ix_definition_assessment_synthesis_attempt_run",
        "matter_definition_assessment_synthesis_attempt",
        ["assessment_run_id", "created_at"],
    )
    op.create_index(
        "ix_definition_assessment_synthesis_attempt_status",
        "matter_definition_assessment_synthesis_attempt",
        ["status"],
    )
    op.create_table(
        "matter_definition_assessment_synthesis_unit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("unit_type", sa.String(length=50), nullable=False),
        sa.Column("unit_key", sa.String(length=200), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("skill_run_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_details", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="ck_definition_assessment_synthesis_unit_status",
        ),
        sa.CheckConstraint(
            "ordinal > 0 AND attempt_count >= 0",
            name="ck_definition_assessment_synthesis_unit_counts",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["matter_definition_assessment_synthesis_attempt.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["skill_run_id"], ["skill_run.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attempt_id", "unit_type", "unit_key", name="uq_definition_assessment_synthesis_unit"
        ),
    )
    op.create_index(
        "ix_definition_assessment_synthesis_unit_attempt_id",
        "matter_definition_assessment_synthesis_unit",
        ["attempt_id"],
    )
    op.create_index(
        "ix_definition_assessment_synthesis_unit_attempt",
        "matter_definition_assessment_synthesis_unit",
        ["attempt_id", "ordinal"],
    )
    op.create_index(
        "ix_definition_assessment_synthesis_unit_status",
        "matter_definition_assessment_synthesis_unit",
        ["status"],
    )


def downgrade() -> None:
    op.drop_table("matter_definition_assessment_synthesis_unit")
    op.drop_table("matter_definition_assessment_synthesis_attempt")
    op.drop_constraint(
        "ck_definition_assessment_completion_disposition",
        "matter_definition_assessment_run",
        type_="check",
    )
    op.drop_constraint(
        "ck_definition_assessment_synthesis_status",
        "matter_definition_assessment_run",
        type_="check",
    )
    op.drop_column("matter_definition_assessment_run", "synthesis_diagnostics")
    op.drop_column("matter_definition_assessment_run", "current_synthesis_attempt_id")
    op.drop_column("matter_definition_assessment_run", "completion_disposition")
    op.drop_column("matter_definition_assessment_run", "synthesis_status")
