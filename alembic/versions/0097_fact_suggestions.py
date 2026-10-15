"""Store agent-suggested changes to reviewer-owned facts.

Revision ID: 0097_fact_suggestions
Revises: 0096_fact_run_input_manifests
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0097_fact_suggestions"
down_revision: str | None = "0096_fact_run_input_manifests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matter_fact_suggestion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("fact_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("target_fact_id", sa.Uuid(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("created_by_workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('REVISION', 'MERGE')", name="ck_matter_fact_suggestion_kind"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'DISMISSED')", name="ck_matter_fact_suggestion_status"
        ),
        sa.CheckConstraint(
            "(kind = 'MERGE') = (target_fact_id IS NOT NULL)", name="ck_matter_fact_suggestion_merge_target"
        ),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fact_id"], ["matter_fact.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_fact_id"], ["matter_fact.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_workflow_run_id"], ["workflow_run.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_matter_fact_suggestion_fact_status", "matter_fact_suggestion", ["fact_id", "status"])
    op.create_index("ix_matter_fact_suggestion_matter_status", "matter_fact_suggestion", ["matter_id", "status"])
    op.create_index(
        op.f("ix_matter_fact_suggestion_created_by_workflow_run_id"),
        "matter_fact_suggestion",
        ["created_by_workflow_run_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_matter_fact_suggestion_created_by_workflow_run_id"), table_name="matter_fact_suggestion")
    op.drop_index("ix_matter_fact_suggestion_matter_status", table_name="matter_fact_suggestion")
    op.drop_index("ix_matter_fact_suggestion_fact_status", table_name="matter_fact_suggestion")
    op.drop_table("matter_fact_suggestion")
