"""Add durable classification scoring runs.

Revision ID: 0059_classification_scoring
Revises: 0058_classification_lifecycle
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0059_classification_scoring"
down_revision: str | None = "0058_classification_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "classification_scoring_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("classification_model_id", sa.Uuid(), nullable=False),
        sa.Column("model_version_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("workflow_id", sa.String(length=500), nullable=False),
        sa.Column("derivation_key", sa.String(length=64), nullable=False),
        sa.Column("configuration_hash", sa.String(length=64), nullable=False),
        sa.Column("model_content_hash", sa.String(length=64), nullable=False),
        sa.Column("feature_content_hash", sa.String(length=64), nullable=False),
        sa.Column("feature_specification_hash", sa.String(length=64), nullable=False),
        sa.Column("scoring_policy", json_document, nullable=False),
        sa.Column("scoring_policy_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("current_stage", sa.String(length=100), nullable=False),
        sa.Column("progress_completed", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("population_document_count", sa.Integer(), nullable=False),
        sa.Column("scored_document_count", sa.Integer(), nullable=False),
        sa.Column("abstained_document_count", sa.Integer(), nullable=False),
        sa.Column("excluded_document_count", sa.Integer(), nullable=False),
        sa.Column("failed_document_count", sa.Integer(), nullable=False),
        sa.Column("prediction_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("prediction_content_hash", sa.String(length=64), nullable=True),
        sa.Column("result_manifest_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("result_manifest_content_hash", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("failure_retriable", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("execution_actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'VALIDATING', 'WAITING_FOR_RUNTIME', 'SCORING', "
            "'PUBLISHING', 'FINALIZING', 'RECONCILING', 'COMPLETED', "
            "'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_classification_scoring_run_status",
        ),
        sa.CheckConstraint(
            "progress_completed >= 0 AND progress_total >= 0 AND progress_completed <= progress_total",
            name="ck_classification_scoring_run_progress",
        ),
        sa.CheckConstraint(
            "population_document_count >= 0 AND scored_document_count >= 0 "
            "AND abstained_document_count >= 0 AND excluded_document_count >= 0 "
            "AND failed_document_count >= 0 AND abstained_document_count <= scored_document_count",
            name="ck_classification_scoring_run_counts",
        ),
        sa.CheckConstraint(
            "(prediction_artifact_id IS NULL AND prediction_content_hash IS NULL) OR "
            "(prediction_artifact_id IS NOT NULL AND prediction_content_hash IS NOT NULL)",
            name="ck_classification_scoring_run_prediction_artifact",
        ),
        sa.CheckConstraint(
            "(result_manifest_artifact_id IS NULL AND result_manifest_content_hash IS NULL) OR "
            "(result_manifest_artifact_id IS NOT NULL AND result_manifest_content_hash IS NOT NULL)",
            name="ck_classification_scoring_run_manifest_artifact",
        ),
        sa.CheckConstraint(
            "status NOT IN ('COMPLETED', 'COMPLETED_WITH_ERRORS') OR "
            "(prediction_artifact_id IS NOT NULL AND result_manifest_artifact_id IS NOT NULL "
            "AND population_document_count = scored_document_count + excluded_document_count "
            "+ failed_document_count)",
            name="ck_classification_scoring_run_completed",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_classification_scoring_run_row_version"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["classification_model_id"], ["classification_model.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["model_version_id", "classification_model_id"],
            ["classification_model_version.id", "classification_model_version.classification_model_id"],
            name="fk_classification_scoring_run_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_snapshot_id"], ["classification_dataset_snapshot.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["execution_actor_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "classification_model_id", "idempotency_key", name="uq_classification_scoring_run_idempotency"
        ),
        sa.UniqueConstraint(
            "classification_model_id", "derivation_key", name="uq_classification_scoring_run_derivation"
        ),
        sa.UniqueConstraint("workflow_id", name="uq_classification_scoring_run_workflow"),
    )
    for column in (
        "tenant_id",
        "matter_id",
        "classification_model_id",
        "model_version_id",
        "dataset_snapshot_id",
        "derivation_key",
        "status",
        "created_by_user_id",
        "execution_actor_user_id",
    ):
        op.create_index(f"ix_classification_scoring_run_{column}", "classification_scoring_run", [column])
    op.create_index(
        "ix_classification_scoring_run_model_created",
        "classification_scoring_run",
        ["classification_model_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("classification_scoring_run")
