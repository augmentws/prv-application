"""Add immutable Review Program completion reports.

Revision ID: 0114_review_completion
Revises: 0113_batch_set_input_manifests
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0114_review_completion"
down_revision: str = "0113_batch_set_input_manifests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH', "
        "'RANDOM_SAVED_SEARCH', 'RANDOM_ASSESSMENT', 'DEFINITION_ASSESSMENT', "
        "'CLASSIFICATION_PREDICTIONS', 'COMPARISON_RESULT', 'TASK_BATCH_PARTITION', "
        "'EXPLICIT_DOCUMENTS')",
    )
    op.create_table(
        "review_program_completion_report",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("program_id", sa.Uuid(), nullable=False),
        sa.Column("program_version_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_content_hash", sa.String(length=64), nullable=False),
        sa.Column("completion_reason", sa.Text(), nullable=True),
        sa.Column("completed_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("schema_version > 0", name="ck_review_program_completion_report_schema_version"),
        sa.ForeignKeyConstraint(["completed_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["program_id"], ["review_program.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["program_version_id"], ["review_program_version.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["review_program_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_review_program_completion_report_run"),
    )
    for name, columns in (
        ("ix_review_program_completion_report_tenant_id", ["tenant_id"]),
        ("ix_review_program_completion_report_matter_id", ["matter_id"]),
        ("ix_review_program_completion_report_program_id", ["program_id"]),
        ("ix_review_program_completion_report_program_version_id", ["program_version_id"]),
        ("ix_review_program_completion_report_run_id", ["run_id"]),
        ("ix_review_program_completion_report_artifact_id", ["artifact_id"]),
        ("ix_review_program_completion_report_completed_by_user_id", ["completed_by_user_id"]),
        ("ix_review_program_completion_report_program_created", ["program_id", "created_at"]),
    ):
        op.create_index(name, "review_program_completion_report", columns)


def downgrade() -> None:
    op.drop_table("review_program_completion_report")
    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH', "
        "'RANDOM_SAVED_SEARCH', 'RANDOM_ASSESSMENT', 'DEFINITION_ASSESSMENT', "
        "'CLASSIFICATION_PREDICTIONS', 'COMPARISON_RESULT', 'TASK_BATCH_PARTITION')",
    )
