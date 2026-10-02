"""Add immutable workflow artifacts for classification inputs and outputs.

Revision ID: 0008_workflow_artifacts
Revises: 0007_run_rule_selection
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_workflow_artifacts"
down_revision: str | None = "0007_run_rule_selection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_artifact",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("core_scope_kind", sa.String(length=50), nullable=False),
        sa.Column("core_scope_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_type", sa.String(length=50), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("derivation_key", sa.String(length=64), nullable=False),
        sa.Column("content_blob_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("byte_length", sa.BigInteger(), nullable=False),
        sa.Column("producer_kind", sa.String(length=40), nullable=False),
        sa.Column("producer_run_id", sa.Uuid(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "core_scope_kind IN ('MATTER', 'REVIEW_BATCH', 'CLASSIFICATION_MODEL', "
            "'CLASSIFICATION_CONFIGURATION', 'CLASSIFICATION_DATASET_SNAPSHOT', "
            "'CLASSIFICATION_TRAINING_RUN', 'CLASSIFICATION_SCORING_RUN')",
            name="ck_workflow_artifact_scope_kind",
        ),
        sa.CheckConstraint(
            "artifact_type IN ('CLASSIFICATION_POPULATION', 'CLASSIFICATION_FEATURES', "
            "'CLASSIFICATION_LABELS', 'CLASSIFICATION_SPLITS', 'CLASSIFICATION_MODEL_PACKAGE', "
            "'CLASSIFICATION_PREDICTIONS', 'CLASSIFICATION_EVALUATION', "
            "'CLASSIFICATION_RESULT_MANIFEST')",
            name="ck_workflow_artifact_type",
        ),
        sa.CheckConstraint("schema_version > 0", name="ck_workflow_artifact_schema_version"),
        sa.CheckConstraint("byte_length > 0", name="ck_workflow_artifact_byte_length"),
        sa.CheckConstraint(
            "producer_kind IN ('CORE', 'CLASSIFICATION_WORKER', 'CLASSIFICATION_RUNTIME')",
            name="ck_workflow_artifact_producer_kind",
        ),
        sa.ForeignKeyConstraint(["content_blob_id"], ["content_blob.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "client_id",
            "artifact_type",
            "derivation_key",
            name="uq_workflow_artifact_derivation",
        ),
    )
    op.create_index("ix_workflow_artifact_tenant_id", "workflow_artifact", ["tenant_id"])
    op.create_index("ix_workflow_artifact_client_id", "workflow_artifact", ["client_id"])
    op.create_index("ix_workflow_artifact_core_scope_id", "workflow_artifact", ["core_scope_id"])
    op.create_index("ix_workflow_artifact_artifact_type", "workflow_artifact", ["artifact_type"])
    op.create_index("ix_workflow_artifact_derivation_key", "workflow_artifact", ["derivation_key"])
    op.create_index("ix_workflow_artifact_content_blob_id", "workflow_artifact", ["content_blob_id"])
    op.create_index("ix_workflow_artifact_content_hash", "workflow_artifact", ["content_hash"])
    op.create_index("ix_workflow_artifact_producer_run_id", "workflow_artifact", ["producer_run_id"])
    op.create_index(
        "ix_workflow_artifact_created_by_user_id",
        "workflow_artifact",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_workflow_artifact_scope_created",
        "workflow_artifact",
        ["tenant_id", "client_id", "core_scope_kind", "core_scope_id", "created_at"],
    )

    op.create_table(
        "workflow_artifact_lineage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("source_workflow_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("source_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("relationship", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(source_workflow_artifact_id IS NOT NULL AND source_artifact_id IS NULL) OR "
            "(source_workflow_artifact_id IS NULL AND source_artifact_id IS NOT NULL)",
            name="ck_workflow_artifact_lineage_source",
        ),
        sa.CheckConstraint(
            "relationship = 'DERIVED_FROM'",
            name="ck_workflow_artifact_lineage_relationship",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_artifact_id"],
            ["workflow_artifact.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_workflow_artifact_id"],
            ["workflow_artifact.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_id"],
            ["artifact.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_artifact_id",
            "source_workflow_artifact_id",
            name="uq_workflow_artifact_lineage_workflow_source",
        ),
        sa.UniqueConstraint(
            "workflow_artifact_id",
            "source_artifact_id",
            name="uq_workflow_artifact_lineage_artifact_source",
        ),
    )
    op.create_index(
        "ix_workflow_artifact_lineage_workflow_artifact_id",
        "workflow_artifact_lineage",
        ["workflow_artifact_id"],
    )
    op.create_index(
        "ix_workflow_artifact_lineage_source_workflow_artifact_id",
        "workflow_artifact_lineage",
        ["source_workflow_artifact_id"],
    )
    op.create_index(
        "ix_workflow_artifact_lineage_source_artifact_id",
        "workflow_artifact_lineage",
        ["source_artifact_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_artifact_lineage_source_artifact_id",
        table_name="workflow_artifact_lineage",
    )
    op.drop_index(
        "ix_workflow_artifact_lineage_source_workflow_artifact_id",
        table_name="workflow_artifact_lineage",
    )
    op.drop_index(
        "ix_workflow_artifact_lineage_workflow_artifact_id",
        table_name="workflow_artifact_lineage",
    )
    op.drop_table("workflow_artifact_lineage")
    op.drop_index("ix_workflow_artifact_scope_created", table_name="workflow_artifact")
    op.drop_index("ix_workflow_artifact_created_by_user_id", table_name="workflow_artifact")
    op.drop_index("ix_workflow_artifact_producer_run_id", table_name="workflow_artifact")
    op.drop_index("ix_workflow_artifact_content_hash", table_name="workflow_artifact")
    op.drop_index("ix_workflow_artifact_content_blob_id", table_name="workflow_artifact")
    op.drop_index("ix_workflow_artifact_derivation_key", table_name="workflow_artifact")
    op.drop_index("ix_workflow_artifact_artifact_type", table_name="workflow_artifact")
    op.drop_index("ix_workflow_artifact_core_scope_id", table_name="workflow_artifact")
    op.drop_index("ix_workflow_artifact_client_id", table_name="workflow_artifact")
    op.drop_index("ix_workflow_artifact_tenant_id", table_name="workflow_artifact")
    op.drop_table("workflow_artifact")
