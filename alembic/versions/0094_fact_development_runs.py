"""Let Matter Analysis Task runs carry fact-development runs, with frozen per-chunk document slices.

Revision ID: 0094_fact_development_runs
Revises: 0093_fact_summary_embeddings
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0094_fact_development_runs"
down_revision: str | None = "0093_fact_summary_embeddings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRIOR_STATUSES = (
    "'QUEUED', 'SNAPSHOTTING', 'PREPARING_FEATURES', 'SCORING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED'"
)


def upgrade() -> None:
    op.add_column(
        "matter_analysis_task_run",
        sa.Column("run_kind", sa.String(length=30), nullable=False, server_default="CLASSIFICATION"),
    )
    op.add_column(
        "matter_analysis_task_run",
        sa.Column("configuration_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "matter_analysis_task_run",
        sa.Column("report", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    for column in ("classification_model_id", "classification_model_version_id", "embedding_job_id"):
        op.alter_column("matter_analysis_task_run", column, existing_type=sa.Uuid(), nullable=True)
    op.drop_constraint("ck_analysis_task_run_status", "matter_analysis_task_run", type_="check")
    op.create_check_constraint(
        "ck_analysis_task_run_status",
        "matter_analysis_task_run",
        f"status IN ({PRIOR_STATUSES}, 'PREPARING', 'EXTRACTING')",
    )
    op.create_check_constraint(
        "ck_analysis_task_run_kind",
        "matter_analysis_task_run",
        "run_kind IN ('CLASSIFICATION', 'FACT_DEVELOPMENT')",
    )
    op.create_check_constraint(
        "ck_analysis_task_run_classification_inputs",
        "matter_analysis_task_run",
        "run_kind <> 'CLASSIFICATION' OR (classification_model_id IS NOT NULL "
        "AND classification_model_version_id IS NOT NULL AND embedding_job_id IS NOT NULL)",
    )
    op.create_table(
        "matter_analysis_task_run_chunk",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_number", sa.Integer(), nullable=False),
        sa.Column("ordinal_start", sa.Integer(), nullable=False),
        sa.Column("document_ids", sa.JSON(), nullable=False),
        sa.Column("issue_keys", sa.JSON(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'CANCELED')",
            name="ck_analysis_task_run_chunk_status",
        ),
        sa.CheckConstraint(
            "chunk_number >= 0 AND ordinal_start >= 0 AND item_count >= 0 AND processed_count >= 0 "
            "AND failed_count >= 0",
            name="ck_analysis_task_run_chunk_counts",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["matter_analysis_task_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "chunk_number", name="uq_analysis_task_run_chunk_number"),
    )
    op.create_index(
        op.f("ix_matter_analysis_task_run_chunk_run_id"), "matter_analysis_task_run_chunk", ["run_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_matter_analysis_task_run_chunk_run_id"), table_name="matter_analysis_task_run_chunk")
    op.drop_table("matter_analysis_task_run_chunk")
    # Fact-development runs cannot satisfy the classification-only shape; remove them first.
    op.execute("DELETE FROM matter_analysis_task_run WHERE run_kind = 'FACT_DEVELOPMENT'")
    op.drop_constraint("ck_analysis_task_run_classification_inputs", "matter_analysis_task_run", type_="check")
    op.drop_constraint("ck_analysis_task_run_kind", "matter_analysis_task_run", type_="check")
    op.drop_constraint("ck_analysis_task_run_status", "matter_analysis_task_run", type_="check")
    op.create_check_constraint("ck_analysis_task_run_status", "matter_analysis_task_run", f"status IN ({PRIOR_STATUSES})")
    for column in ("classification_model_id", "classification_model_version_id", "embedding_job_id"):
        op.alter_column("matter_analysis_task_run", column, existing_type=sa.Uuid(), nullable=False)
    op.drop_column("matter_analysis_task_run", "report")
    op.drop_column("matter_analysis_task_run", "configuration_snapshot")
    op.drop_column("matter_analysis_task_run", "run_kind")
