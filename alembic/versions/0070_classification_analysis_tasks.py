"""add classification analysis tasks and durable runs

Revision ID: 0070_classification_analysis
Revises: 0069_classifier_batch_runs
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0070_classification_analysis"
down_revision: str | None = "0069_classifier_batch_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_matter_analysis_task_type", "matter_analysis_task", type_="check")
    op.create_check_constraint(
        "ck_matter_analysis_task_type",
        "matter_analysis_task",
        "task_type IN ('QUESTION_ANSWERING', 'CLASSIFICATION')",
    )
    op.add_column(
        "matter_analysis_task_version",
        sa.Column("classification_model_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "matter_analysis_task_version",
        sa.Column("classification_model_version_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_analysis_task_version_classification_model",
        "matter_analysis_task_version",
        "classification_model",
        ["classification_model_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_analysis_task_version_classification_version",
        "matter_analysis_task_version",
        "classification_model_version",
        ["classification_model_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_analysis_task_version_classification_model",
        "matter_analysis_task_version",
        ["classification_model_id"],
    )
    op.create_index(
        "ix_analysis_task_version_classification_version",
        "matter_analysis_task_version",
        ["classification_model_version_id"],
    )

    op.drop_constraint(
        "ck_classification_dataset_snapshot_purpose",
        "classification_dataset_snapshot",
        type_="check",
    )
    op.drop_constraint(
        "ck_classification_dataset_snapshot_status",
        "classification_dataset_snapshot",
        type_="check",
    )
    op.drop_constraint(
        "ck_classification_dataset_snapshot_base_artifacts",
        "classification_dataset_snapshot",
        type_="check",
    )
    op.create_check_constraint(
        "ck_classification_dataset_snapshot_purpose",
        "classification_dataset_snapshot",
        "purpose IN ('TRAINING', 'SCORING')",
    )
    op.create_check_constraint(
        "ck_classification_dataset_snapshot_status",
        "classification_dataset_snapshot",
        "status IN ('BUILDING', 'READY_FOR_FEATURES', 'READY_FOR_TRAINING', 'READY_FOR_SCORING', 'FAILED')",
    )
    op.create_check_constraint(
        "ck_classification_dataset_snapshot_base_artifacts",
        "classification_dataset_snapshot",
        "status NOT IN ('READY_FOR_FEATURES', 'READY_FOR_TRAINING', 'READY_FOR_SCORING') OR "
        "(population_artifact_id IS NOT NULL AND population_content_hash IS NOT NULL AND "
        "(purpose = 'SCORING' OR (label_artifact_id IS NOT NULL AND label_content_hash IS NOT NULL)))",
    )

    op.create_table(
        "matter_analysis_task_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("matter_analysis_task_id", sa.Uuid(), nullable=False),
        sa.Column("matter_analysis_task_version_id", sa.Uuid(), nullable=False),
        sa.Column("scope_type", sa.String(length=30), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("classification_model_id", sa.Uuid(), nullable=False),
        sa.Column("classification_model_version_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_job_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("classification_scoring_run_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("workflow_id", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="QUEUED"),
        sa.Column("current_stage", sa.String(length=100), nullable=False, server_default="QUEUED"),
        sa.Column("progress_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("progress_total", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("population_document_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("failure_retriable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope_type IN ('MATTER', 'REVIEW_BATCH')", name="ck_analysis_task_run_scope_type"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'SNAPSHOTTING', 'PREPARING_FEATURES', 'SCORING', "
            "'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_analysis_task_run_status",
        ),
        sa.CheckConstraint(
            "progress_completed >= 0 AND progress_total > 0 AND progress_completed <= progress_total",
            name="ck_analysis_task_run_progress",
        ),
        sa.CheckConstraint("population_document_count >= 0", name="ck_analysis_task_run_population_count"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matter_analysis_task_id"], ["matter_analysis_task.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matter_analysis_task_version_id"], ["matter_analysis_task_version.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["classification_model_id"], ["classification_model.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["classification_model_version_id"], ["classification_model_version.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["embedding_job_id"], ["matter_embedding_job.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["dataset_snapshot_id"], ["classification_dataset_snapshot.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["classification_scoring_run_id"], ["classification_scoring_run.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("matter_analysis_task_id", "idempotency_key", name="uq_analysis_task_run_idempotency"),
        sa.UniqueConstraint("workflow_id", name="uq_analysis_task_run_workflow"),
    )
    op.create_index("ix_analysis_task_run_task_created", "matter_analysis_task_run", ["matter_analysis_task_id", "created_at"])
    op.create_index("ix_analysis_task_run_matter_status", "matter_analysis_task_run", ["matter_id", "status"])
    op.create_index("ix_analysis_task_run_scoring", "matter_analysis_task_run", ["classification_scoring_run_id"])


def downgrade() -> None:
    op.drop_table("matter_analysis_task_run")
    op.drop_index("ix_analysis_task_version_classification_version", table_name="matter_analysis_task_version")
    op.drop_index("ix_analysis_task_version_classification_model", table_name="matter_analysis_task_version")
    op.drop_constraint("fk_analysis_task_version_classification_version", "matter_analysis_task_version", type_="foreignkey")
    op.drop_constraint("fk_analysis_task_version_classification_model", "matter_analysis_task_version", type_="foreignkey")
    op.drop_column("matter_analysis_task_version", "classification_model_version_id")
    op.drop_column("matter_analysis_task_version", "classification_model_id")
    op.drop_constraint("ck_matter_analysis_task_type", "matter_analysis_task", type_="check")
    op.create_check_constraint("ck_matter_analysis_task_type", "matter_analysis_task", "task_type IN ('QUESTION_ANSWERING')")
    op.drop_constraint("ck_classification_dataset_snapshot_base_artifacts", "classification_dataset_snapshot", type_="check")
    op.drop_constraint("ck_classification_dataset_snapshot_status", "classification_dataset_snapshot", type_="check")
    op.drop_constraint("ck_classification_dataset_snapshot_purpose", "classification_dataset_snapshot", type_="check")
    op.create_check_constraint("ck_classification_dataset_snapshot_purpose", "classification_dataset_snapshot", "purpose = 'TRAINING'")
    op.create_check_constraint("ck_classification_dataset_snapshot_status", "classification_dataset_snapshot", "status IN ('BUILDING', 'READY_FOR_FEATURES', 'READY_FOR_TRAINING', 'FAILED')")
    op.create_check_constraint(
        "ck_classification_dataset_snapshot_base_artifacts",
        "classification_dataset_snapshot",
        "status NOT IN ('READY_FOR_FEATURES', 'READY_FOR_TRAINING') OR "
        "(population_artifact_id IS NOT NULL AND population_content_hash IS NOT NULL AND "
        "label_artifact_id IS NOT NULL AND label_content_hash IS NOT NULL)",
    )
