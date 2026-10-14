"""Add durable end-to-end classification training jobs.

Revision ID: 0061_classification_training
Revises: 0060_imported_matter_labels
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0061_classification_training"
down_revision: str | None = "0060_imported_matter_labels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ACTIVE_STATUSES = "'QUEUED', 'SNAPSHOTTING', 'PREPARING_FEATURES', 'LAUNCHING', 'TRAINING', 'FINALIZING'"


def upgrade() -> None:
    json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "classification_training_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("classification_model_id", sa.Uuid(), nullable=False),
        sa.Column("configuration_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_job_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("training_run_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("workflow_id", sa.String(length=500), nullable=False),
        sa.Column("scope_type", sa.String(length=30), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("random_seed", sa.BigInteger(), nullable=False),
        sa.Column("validation_specification", json_document, nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="QUEUED"),
        sa.Column("current_stage", sa.String(length=100), nullable=False, server_default="QUEUED"),
        sa.Column("progress_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("progress_total", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("failure_retriable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope_type IN ('MATTER', 'REVIEW_BATCH')",
            name="ck_classification_training_job_scope_type",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'SNAPSHOTTING', 'PREPARING_FEATURES', 'LAUNCHING', "
            "'TRAINING', 'FINALIZING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_classification_training_job_status",
        ),
        sa.CheckConstraint(
            "progress_completed >= 0 AND progress_total > 0 AND progress_completed <= progress_total",
            name="ck_classification_training_job_progress",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["classification_model_id"],
            ["classification_model.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["configuration_id", "classification_model_id"],
            [
                "classification_model_configuration.id",
                "classification_model_configuration.classification_model_id",
            ],
            name="fk_classification_training_job_configuration",
        ),
        sa.ForeignKeyConstraint(["embedding_job_id"], ["matter_embedding_job.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["dataset_snapshot_id"],
            ["classification_dataset_snapshot.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["training_run_id"],
            ["classification_training_run.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "classification_model_id",
            "idempotency_key",
            name="uq_classification_training_job_idempotency",
        ),
        sa.UniqueConstraint("workflow_id", name="uq_classification_training_job_workflow"),
        sa.UniqueConstraint("training_run_id", name="uq_classification_training_job_training_run"),
    )
    for column in (
        "tenant_id",
        "matter_id",
        "classification_model_id",
        "configuration_id",
        "embedding_job_id",
        "dataset_snapshot_id",
        "scope_id",
        "status",
        "created_by_user_id",
    ):
        op.create_index(f"ix_classification_training_job_{column}", "classification_training_job", [column])
    op.create_index(
        "ix_classification_training_job_model_created",
        "classification_training_job",
        ["classification_model_id", "created_at"],
    )
    op.create_index(
        "uq_classification_training_job_active",
        "classification_training_job",
        ["classification_model_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({ACTIVE_STATUSES})"),
        sqlite_where=sa.text(f"status IN ({ACTIVE_STATUSES})"),
    )


def downgrade() -> None:
    op.drop_table("classification_training_job")
