"""Add durable classification model builds and candidate versions.

Revision ID: 0056_classification_builds
Revises: 0055_classification_features
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0056_classification_builds"
down_revision: str | None = "0055_classification_features"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "classification_training_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("classification_model_id", sa.Uuid(), nullable=False),
        sa.Column("configuration_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("workflow_id", sa.String(length=500), nullable=False),
        sa.Column("derivation_key", sa.String(length=64), nullable=False),
        sa.Column("trainer_specification", json_document, nullable=False),
        sa.Column("validation_specification", json_document, nullable=False),
        sa.Column("random_seed", sa.BigInteger(), nullable=False),
        sa.Column("package_schema_version", sa.Integer(), nullable=False),
        sa.Column("runtime_image_digest", sa.String(length=500), nullable=False),
        sa.Column("source_revision", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("current_stage", sa.String(length=100), nullable=False),
        sa.Column("progress_completed", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("model_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("model_content_hash", sa.String(length=64), nullable=True),
        sa.Column("prediction_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("prediction_content_hash", sa.String(length=64), nullable=True),
        sa.Column("evaluation_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("evaluation_content_hash", sa.String(length=64), nullable=True),
        sa.Column("result_manifest_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("result_manifest_content_hash", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("failure_retriable", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'SNAPSHOTTING', 'WAITING_FOR_RUNTIME', 'TRAINING', "
            "'PACKAGING', 'EVALUATING', 'PUBLISHING', 'FINALIZING', 'RECONCILING', "
            "'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_classification_training_run_status",
        ),
        sa.CheckConstraint(
            "progress_completed >= 0 AND progress_total >= 0 AND progress_completed <= progress_total",
            name="ck_classification_training_run_progress",
        ),
        sa.CheckConstraint(
            "((model_artifact_id IS NULL AND model_content_hash IS NULL) OR "
            "(model_artifact_id IS NOT NULL AND model_content_hash IS NOT NULL)) AND "
            "((prediction_artifact_id IS NULL AND prediction_content_hash IS NULL) OR "
            "(prediction_artifact_id IS NOT NULL AND prediction_content_hash IS NOT NULL)) AND "
            "((evaluation_artifact_id IS NULL AND evaluation_content_hash IS NULL) OR "
            "(evaluation_artifact_id IS NOT NULL AND evaluation_content_hash IS NOT NULL)) AND "
            "((result_manifest_artifact_id IS NULL AND result_manifest_content_hash IS NULL) OR "
            "(result_manifest_artifact_id IS NOT NULL AND result_manifest_content_hash IS NOT NULL))",
            name="ck_classification_training_run_artifact_pairs",
        ),
        sa.CheckConstraint(
            "status != 'COMPLETED' OR (model_artifact_id IS NOT NULL AND "
            "prediction_artifact_id IS NOT NULL AND evaluation_artifact_id IS NOT NULL AND "
            "result_manifest_artifact_id IS NOT NULL)",
            name="ck_classification_training_run_completed_outputs",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_classification_training_run_row_version"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["classification_model_id"], ["classification_model.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["configuration_id", "classification_model_id"],
            ["classification_model_configuration.id", "classification_model_configuration.classification_model_id"],
            name="fk_classification_training_run_configuration",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_snapshot_id"],
            ["classification_dataset_snapshot.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "classification_model_id",
            "idempotency_key",
            name="uq_classification_training_run_idempotency",
        ),
        sa.UniqueConstraint(
            "classification_model_id",
            "derivation_key",
            name="uq_classification_training_run_derivation",
        ),
        sa.UniqueConstraint("workflow_id", name="uq_classification_training_run_workflow"),
    )
    for column in (
        "tenant_id",
        "matter_id",
        "classification_model_id",
        "configuration_id",
        "dataset_snapshot_id",
        "derivation_key",
        "status",
        "created_by_user_id",
    ):
        op.create_index(f"ix_classification_training_run_{column}", "classification_training_run", [column])
    op.create_index(
        "ix_classification_training_run_model_created",
        "classification_training_run",
        ["classification_model_id", "created_at"],
    )

    op.create_table(
        "classification_model_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("classification_model_id", sa.Uuid(), nullable=False),
        sa.Column("configuration_id", sa.Uuid(), nullable=False),
        sa.Column("training_run_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("target_field_snapshot", json_document, nullable=False),
        sa.Column("training_aggregate_hash", sa.String(length=64), nullable=False),
        sa.Column("feature_specification_hash", sa.String(length=64), nullable=False),
        sa.Column("model_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("model_content_hash", sa.String(length=64), nullable=False),
        sa.Column("prediction_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("prediction_content_hash", sa.String(length=64), nullable=False),
        sa.Column("evaluation_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_content_hash", sa.String(length=64), nullable=False),
        sa.Column("result_manifest_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("result_manifest_content_hash", sa.String(length=64), nullable=False),
        sa.Column("package_schema_version", sa.Integer(), nullable=False),
        sa.Column("runtime_image_digest", sa.String(length=500), nullable=False),
        sa.Column("source_revision", sa.String(length=255), nullable=False),
        sa.Column("metric_summary", json_document, nullable=False),
        sa.Column("warnings", json_document, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('CANDIDATE', 'APPROVED', 'REJECTED', 'RETIRED')",
            name="ck_classification_model_version_status",
        ),
        sa.CheckConstraint("version_number > 0", name="ck_classification_model_version_number"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["classification_model_id"], ["classification_model.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["configuration_id", "classification_model_id"],
            ["classification_model_configuration.id", "classification_model_configuration.classification_model_id"],
            name="fk_classification_model_version_configuration",
        ),
        sa.ForeignKeyConstraint(["training_run_id"], ["classification_training_run.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["dataset_snapshot_id"],
            ["classification_dataset_snapshot.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("training_run_id", name="uq_classification_model_version_run"),
        sa.UniqueConstraint(
            "classification_model_id",
            "version_number",
            name="uq_classification_model_version_number",
        ),
        sa.UniqueConstraint("id", "classification_model_id", name="uq_classification_model_version_identity"),
    )
    for column in (
        "tenant_id",
        "matter_id",
        "classification_model_id",
        "configuration_id",
        "training_run_id",
        "dataset_snapshot_id",
        "status",
        "created_by_user_id",
    ):
        op.create_index(f"ix_classification_model_version_{column}", "classification_model_version", [column])
    op.create_index(
        "ix_classification_model_version_model_created",
        "classification_model_version",
        ["classification_model_id", "created_at"],
    )

    op.create_table(
        "classification_evaluation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("classification_model_id", sa.Uuid(), nullable=False),
        sa.Column("model_version_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_kind", sa.String(length=40), nullable=False),
        sa.Column("metric_policy_version", sa.Integer(), nullable=False),
        sa.Column("evaluation_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_content_hash", sa.String(length=64), nullable=False),
        sa.Column("metric_summary", json_document, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "evaluation_kind IN ('CROSS_FIT_DEVELOPMENT', 'FORMAL_VALIDATION', 'RANDOM_AUDIT', 'TARGETED_QA')",
            name="ck_classification_evaluation_kind",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["classification_model_id"], ["classification_model.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_version_id"], ["classification_model_version.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["dataset_snapshot_id"],
            ["classification_dataset_snapshot.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "model_version_id",
            "evaluation_kind",
            "dataset_snapshot_id",
            "metric_policy_version",
            name="uq_classification_evaluation_identity",
        ),
    )
    for column in (
        "tenant_id",
        "matter_id",
        "classification_model_id",
        "model_version_id",
        "dataset_snapshot_id",
    ):
        op.create_index(f"ix_classification_evaluation_{column}", "classification_evaluation", [column])
    op.create_index(
        "ix_classification_evaluation_model_created",
        "classification_evaluation",
        ["classification_model_id", "created_at"],
    )

    op.add_column("classification_model", sa.Column("active_model_version_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_classification_model_active_model_version_id",
        "classification_model",
        ["active_model_version_id"],
    )
    op.create_foreign_key(
        "fk_classification_model_active_version",
        "classification_model",
        "classification_model_version",
        ["active_model_version_id", "id"],
        ["id", "classification_model_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_classification_model_active_version", "classification_model", type_="foreignkey")
    op.drop_index("ix_classification_model_active_model_version_id", table_name="classification_model")
    op.drop_column("classification_model", "active_model_version_id")
    op.drop_table("classification_evaluation")
    op.drop_table("classification_model_version")
    op.drop_table("classification_training_run")
