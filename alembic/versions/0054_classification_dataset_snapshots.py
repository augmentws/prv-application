"""Add deterministic classification dataset snapshots.

Revision ID: 0054_classification_datasets
Revises: 0053_classification_registry
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0054_classification_datasets"
down_revision: str | None = "0053_classification_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "classification_dataset_snapshot",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("classification_model_id", sa.Uuid(), nullable=False),
        sa.Column("configuration_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("scope_type", sa.String(length=30), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("target_field_snapshot", json_document, nullable=False),
        sa.Column("label_policy_snapshot", json_document, nullable=False),
        sa.Column("population_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("population_content_hash", sa.String(length=64), nullable=True),
        sa.Column("label_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("label_content_hash", sa.String(length=64), nullable=True),
        sa.Column("feature_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("feature_content_hash", sa.String(length=64), nullable=True),
        sa.Column("feature_specification_hash", sa.String(length=64), nullable=True),
        sa.Column("split_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("split_content_hash", sa.String(length=64), nullable=True),
        sa.Column("aggregate_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("population_count", sa.Integer(), nullable=False),
        sa.Column("labeled_count", sa.Integer(), nullable=False),
        sa.Column("unlabeled_count", sa.Integer(), nullable=False),
        sa.Column("excluded_count", sa.Integer(), nullable=False),
        sa.Column("conflicting_count", sa.Integer(), nullable=False),
        sa.Column("family_grouped_count", sa.Integer(), nullable=False),
        sa.Column("exact_duplicate_grouped_count", sa.Integer(), nullable=False),
        sa.Column("missing_grouping_count", sa.Integer(), nullable=False),
        sa.Column("class_support", json_document, nullable=False),
        sa.Column("source_summary", json_document, nullable=False),
        sa.Column("warnings", json_document, nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "purpose = 'TRAINING'",
            name="ck_classification_dataset_snapshot_purpose",
        ),
        sa.CheckConstraint(
            "scope_type IN ('MATTER', 'REVIEW_BATCH')",
            name="ck_classification_dataset_snapshot_scope_type",
        ),
        sa.CheckConstraint(
            "status IN ('BUILDING', 'READY_FOR_FEATURES', 'READY_FOR_TRAINING', 'FAILED')",
            name="ck_classification_dataset_snapshot_status",
        ),
        sa.CheckConstraint(
            "population_count >= 0 AND labeled_count >= 0 AND unlabeled_count >= 0 "
            "AND excluded_count >= 0 AND conflicting_count >= 0 "
            "AND family_grouped_count >= 0 AND exact_duplicate_grouped_count >= 0 "
            "AND missing_grouping_count >= 0",
            name="ck_classification_dataset_snapshot_counts",
        ),
        sa.CheckConstraint(
            "(feature_artifact_id IS NULL AND feature_content_hash IS NULL "
            "AND feature_specification_hash IS NULL) OR "
            "(feature_artifact_id IS NOT NULL AND feature_content_hash IS NOT NULL "
            "AND feature_specification_hash IS NOT NULL)",
            name="ck_classification_dataset_snapshot_feature_artifact",
        ),
        sa.CheckConstraint(
            "(split_artifact_id IS NULL AND split_content_hash IS NULL) OR "
            "(split_artifact_id IS NOT NULL AND split_content_hash IS NOT NULL)",
            name="ck_classification_dataset_snapshot_split_artifact",
        ),
        sa.CheckConstraint(
            "status NOT IN ('READY_FOR_FEATURES', 'READY_FOR_TRAINING') OR "
            "(population_artifact_id IS NOT NULL AND population_content_hash IS NOT NULL "
            "AND label_artifact_id IS NOT NULL AND label_content_hash IS NOT NULL)",
            name="ck_classification_dataset_snapshot_base_artifacts",
        ),
        sa.CheckConstraint(
            "status != 'READY_FOR_TRAINING' OR "
            "(feature_artifact_id IS NOT NULL AND split_artifact_id IS NOT NULL)",
            name="ck_classification_dataset_snapshot_training_artifacts",
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
            name="fk_classification_dataset_snapshot_configuration",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["app_user.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "classification_model_id",
            "aggregate_hash",
            name="uq_classification_dataset_snapshot_aggregate",
        ),
    )
    op.create_index(
        "ix_classification_dataset_snapshot_tenant_id",
        "classification_dataset_snapshot",
        ["tenant_id"],
    )
    op.create_index(
        "ix_classification_dataset_snapshot_matter_id",
        "classification_dataset_snapshot",
        ["matter_id"],
    )
    op.create_index(
        "ix_classification_dataset_snapshot_classification_model_id",
        "classification_dataset_snapshot",
        ["classification_model_id"],
    )
    op.create_index(
        "ix_classification_dataset_snapshot_configuration_id",
        "classification_dataset_snapshot",
        ["configuration_id"],
    )
    op.create_index(
        "ix_classification_dataset_snapshot_scope_id",
        "classification_dataset_snapshot",
        ["scope_id"],
    )
    op.create_index(
        "ix_classification_dataset_snapshot_aggregate_hash",
        "classification_dataset_snapshot",
        ["aggregate_hash"],
    )
    op.create_index(
        "ix_classification_dataset_snapshot_status",
        "classification_dataset_snapshot",
        ["status"],
    )
    op.create_index(
        "ix_classification_dataset_snapshot_created_by_user_id",
        "classification_dataset_snapshot",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_classification_dataset_snapshot_model_created",
        "classification_dataset_snapshot",
        ["classification_model_id", "created_at"],
    )
    op.create_index(
        "ix_classification_dataset_snapshot_scope",
        "classification_dataset_snapshot",
        ["matter_id", "scope_type", "scope_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_classification_dataset_snapshot_scope",
        table_name="classification_dataset_snapshot",
    )
    op.drop_index(
        "ix_classification_dataset_snapshot_model_created",
        table_name="classification_dataset_snapshot",
    )
    op.drop_index(
        "ix_classification_dataset_snapshot_created_by_user_id",
        table_name="classification_dataset_snapshot",
    )
    op.drop_index(
        "ix_classification_dataset_snapshot_status",
        table_name="classification_dataset_snapshot",
    )
    op.drop_index(
        "ix_classification_dataset_snapshot_aggregate_hash",
        table_name="classification_dataset_snapshot",
    )
    op.drop_index(
        "ix_classification_dataset_snapshot_scope_id",
        table_name="classification_dataset_snapshot",
    )
    op.drop_index(
        "ix_classification_dataset_snapshot_configuration_id",
        table_name="classification_dataset_snapshot",
    )
    op.drop_index(
        "ix_classification_dataset_snapshot_classification_model_id",
        table_name="classification_dataset_snapshot",
    )
    op.drop_index(
        "ix_classification_dataset_snapshot_matter_id",
        table_name="classification_dataset_snapshot",
    )
    op.drop_index(
        "ix_classification_dataset_snapshot_tenant_id",
        table_name="classification_dataset_snapshot",
    )
    op.drop_table("classification_dataset_snapshot")
