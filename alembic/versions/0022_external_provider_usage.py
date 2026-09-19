"""Add the external provider token-usage ledger.

Revision ID: 0022_external_provider_usage
Revises: 0021_voyage_embedding_batches
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_external_provider_usage"
down_revision: str | None = "0021_voyage_embedding_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_provider_usage",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("matter_id", sa.Uuid(), nullable=True),
        sa.Column("started_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=80), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("job_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=500), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=500), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "request_count >= 0 AND input_tokens >= 0 AND output_tokens >= 0",
            name="ck_external_provider_usage_counts",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["client.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["started_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_external_provider_usage_idempotency_key"),
    )
    op.create_index("ix_external_provider_usage_client_id", "external_provider_usage", ["client_id"])
    op.create_index("ix_external_provider_usage_job", "external_provider_usage", ["job_type", "job_id"])
    op.create_index("ix_external_provider_usage_job_created_at", "external_provider_usage", ["job_created_at"])
    op.create_index("ix_external_provider_usage_job_id", "external_provider_usage", ["job_id"])
    op.create_index("ix_external_provider_usage_job_type", "external_provider_usage", ["job_type"])
    op.create_index("ix_external_provider_usage_matter_id", "external_provider_usage", ["matter_id"])
    op.create_index("ix_external_provider_usage_provider", "external_provider_usage", ["provider"])
    op.create_index(
        "ix_external_provider_usage_provider_date",
        "external_provider_usage",
        ["provider", "job_created_at"],
    )
    op.create_index(
        "ix_external_provider_usage_started_by_user_id",
        "external_provider_usage",
        ["started_by_user_id"],
    )
    op.create_index("ix_external_provider_usage_tenant_id", "external_provider_usage", ["tenant_id"])
    op.create_index(
        "ix_external_provider_usage_tenant_job_date",
        "external_provider_usage",
        ["tenant_id", "job_created_at"],
    )


def downgrade() -> None:
    op.drop_table("external_provider_usage")
