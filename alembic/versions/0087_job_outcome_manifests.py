"""Add generic job outcome manifests and embedding reuse counters.

Revision ID: 0087_job_outcomes
Revises: 0086_reviewer_coding_history
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0087_job_outcomes"
down_revision: str | None = "0086_reviewer_coding_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("matter_embedding_job", sa.Column("reused_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("matter_embedding_batch", sa.Column("reused_count", sa.Integer(), nullable=False, server_default="0"))
    op.drop_constraint("ck_matter_embedding_job_counts", "matter_embedding_job", type_="check")
    op.create_check_constraint(
        "ck_matter_embedding_job_counts",
        "matter_embedding_job",
        "total_count >= 0 AND batch_count >= 0 AND processed_count >= 0 AND embedded_count >= 0 "
        "AND reused_count >= 0 AND skipped_count >= 0 AND failed_count >= 0 AND chunk_count >= 0",
    )
    op.drop_constraint("ck_matter_embedding_batch_counts", "matter_embedding_batch", type_="check")
    op.create_check_constraint(
        "ck_matter_embedding_batch_counts",
        "matter_embedding_batch",
        "item_count >= 0 AND processed_count >= 0 AND embedded_count >= 0 AND reused_count >= 0 "
        "AND skipped_count >= 0 AND failed_count >= 0 AND chunk_count >= 0",
    )

    op.create_table(
        "job_outcome_manifest",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("client_id", sa.Uuid(), sa.ForeignKey("client.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("matter_id", sa.Uuid(), sa.ForeignKey("matter.id", ondelete="CASCADE"), nullable=True),
        sa.Column("job_type", sa.String(length=80), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_status", sa.String(length=20), nullable=False),
        sa.Column("outcome_schema", sa.String(length=100), nullable=False),
        sa.Column("outcome_schema_version", sa.Integer(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=True),
        sa.Column("artifact_content_hash", sa.String(length=64), nullable=True),
        sa.Column("artifact_byte_length", sa.BigInteger(), nullable=True),
        sa.Column("derivation_key", sa.String(length=64), nullable=False),
        sa.Column("item_count", sa.BigInteger(), nullable=False),
        sa.Column("status_counts", sa.JSON(), nullable=False),
        sa.Column("reason_counts", sa.JSON(), nullable=False),
        sa.Column("producer_kind", sa.String(length=50), nullable=False),
        sa.Column("producer_run_id", sa.Uuid(), sa.ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("manifest_status IN ('BUILDING', 'FINALIZED', 'FAILED')", name="ck_job_outcome_manifest_status"),
        sa.CheckConstraint("outcome_schema_version > 0", name="ck_job_outcome_manifest_schema_version"),
        sa.CheckConstraint("item_count >= 0", name="ck_job_outcome_manifest_item_count"),
        sa.CheckConstraint(
            "(artifact_id IS NULL AND artifact_content_hash IS NULL AND artifact_byte_length IS NULL) OR "
            "(artifact_id IS NOT NULL AND artifact_content_hash IS NOT NULL AND artifact_byte_length IS NOT NULL)",
            name="ck_job_outcome_manifest_artifact_tuple",
        ),
        sa.CheckConstraint(
            "manifest_status != 'FINALIZED' OR (artifact_id IS NOT NULL AND finalized_at IS NOT NULL)",
            name="ck_job_outcome_manifest_finalized",
        ),
        sa.UniqueConstraint("job_type", "job_id", name="uq_job_outcome_manifest_job"),
    )
    op.create_index("ix_job_outcome_manifest_tenant_id", "job_outcome_manifest", ["tenant_id"])
    op.create_index("ix_job_outcome_manifest_client_id", "job_outcome_manifest", ["client_id"])
    op.create_index("ix_job_outcome_manifest_matter_id", "job_outcome_manifest", ["matter_id"])
    op.create_index("ix_job_outcome_manifest_producer_run_id", "job_outcome_manifest", ["producer_run_id"])
    op.create_index(
        "ix_job_outcome_manifest_scope",
        "job_outcome_manifest",
        ["tenant_id", "client_id", "matter_id"],
    )


def downgrade() -> None:
    op.drop_table("job_outcome_manifest")
    op.drop_constraint("ck_matter_embedding_batch_counts", "matter_embedding_batch", type_="check")
    op.create_check_constraint(
        "ck_matter_embedding_batch_counts",
        "matter_embedding_batch",
        "item_count >= 0 AND processed_count >= 0 AND embedded_count >= 0 AND skipped_count >= 0 "
        "AND failed_count >= 0 AND chunk_count >= 0",
    )
    op.drop_constraint("ck_matter_embedding_job_counts", "matter_embedding_job", type_="check")
    op.create_check_constraint(
        "ck_matter_embedding_job_counts",
        "matter_embedding_job",
        "total_count >= 0 AND batch_count >= 0 AND processed_count >= 0 AND embedded_count >= 0 "
        "AND skipped_count >= 0 AND failed_count >= 0 AND chunk_count >= 0",
    )
    op.drop_column("matter_embedding_batch", "reused_count")
    op.drop_column("matter_embedding_job", "reused_count")
