"""Add generic analysis input manifests.

Revision ID: 0089_analysis_inputs
Revises: 0088_classifier_batches
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0089_analysis_inputs"
down_revision: str | None = "0088_classifier_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analysis_input_manifest",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("client_id", sa.Uuid(), sa.ForeignKey("client.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("matter_id", sa.Uuid(), sa.ForeignKey("matter.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_type", sa.String(length=80), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("scope_type", sa.String(length=40), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_schema", sa.String(length=100), nullable=False),
        sa.Column("manifest_schema_version", sa.Integer(), nullable=False),
        sa.Column("resolver_version", sa.String(length=100), nullable=False),
        sa.Column("input_contract", sa.JSON(), nullable=False),
        sa.Column("input_contract_hash", sa.String(length=64), nullable=False),
        sa.Column("derivation_key", sa.String(length=64), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=True),
        sa.Column("artifact_content_hash", sa.String(length=64), nullable=True),
        sa.Column("artifact_byte_length", sa.BigInteger(), nullable=True),
        sa.Column("item_count", sa.BigInteger(), nullable=False),
        sa.Column("manifest_status", sa.String(length=20), nullable=False),
        sa.Column("producer_kind", sa.String(length=50), nullable=False),
        sa.Column("producer_run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("app_user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "manifest_status IN ('BUILDING', 'FINALIZED', 'FAILED')",
            name="ck_analysis_input_manifest_status",
        ),
        sa.CheckConstraint("manifest_schema_version > 0", name="ck_analysis_input_manifest_schema_version"),
        sa.CheckConstraint("item_count >= 0", name="ck_analysis_input_manifest_item_count"),
        sa.CheckConstraint(
            "(artifact_id IS NULL AND artifact_content_hash IS NULL AND artifact_byte_length IS NULL) OR "
            "(artifact_id IS NOT NULL AND artifact_content_hash IS NOT NULL AND artifact_byte_length IS NOT NULL)",
            name="ck_analysis_input_manifest_artifact_tuple",
        ),
        sa.CheckConstraint(
            "manifest_status != 'FINALIZED' OR (artifact_id IS NOT NULL AND finalized_at IS NOT NULL)",
            name="ck_analysis_input_manifest_finalized",
        ),
        sa.UniqueConstraint("owner_type", "owner_id", name="uq_analysis_input_manifest_owner"),
    )
    op.create_index("ix_analysis_input_manifest_tenant_id", "analysis_input_manifest", ["tenant_id"])
    op.create_index("ix_analysis_input_manifest_client_id", "analysis_input_manifest", ["client_id"])
    op.create_index("ix_analysis_input_manifest_matter_id", "analysis_input_manifest", ["matter_id"])
    op.create_index("ix_analysis_input_manifest_scope_id", "analysis_input_manifest", ["scope_id"])
    op.create_index("ix_analysis_input_manifest_created_by_user_id", "analysis_input_manifest", ["created_by_user_id"])
    op.create_index(
        "ix_analysis_input_manifest_scope",
        "analysis_input_manifest",
        ["tenant_id", "client_id", "matter_id"],
    )
    op.create_index("ix_analysis_input_manifest_derivation", "analysis_input_manifest", ["derivation_key"])
    op.add_column(
        "classification_dataset_snapshot",
        sa.Column("analysis_input_manifest_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_classification_dataset_snapshot_analysis_input_manifest",
        "classification_dataset_snapshot",
        "analysis_input_manifest",
        ["analysis_input_manifest_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_classification_dataset_snapshot_analysis_input_manifest_id",
        "classification_dataset_snapshot",
        ["analysis_input_manifest_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_classification_dataset_snapshot_analysis_input_manifest_id",
        table_name="classification_dataset_snapshot",
    )
    op.drop_constraint(
        "fk_classification_dataset_snapshot_analysis_input_manifest",
        "classification_dataset_snapshot",
        type_="foreignkey",
    )
    op.drop_column("classification_dataset_snapshot", "analysis_input_manifest_id")
    op.drop_table("analysis_input_manifest")
