"""Persist Voyage batch state for durable embedding workflows.

Revision ID: 0021_voyage_embedding_batches
Revises: 0020_topic_proposal_review
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021_voyage_embedding_batches"
down_revision: str | None = "0020_topic_proposal_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("matter_embedding_batch", sa.Column("provider_input_file_id", sa.String(255), nullable=True))
    op.add_column("matter_embedding_batch", sa.Column("provider_batch_id", sa.String(255), nullable=True))
    op.add_column("matter_embedding_batch", sa.Column("provider_output_file_id", sa.String(255), nullable=True))
    op.add_column("matter_embedding_batch", sa.Column("provider_error_file_id", sa.String(255), nullable=True))
    op.add_column("matter_embedding_batch", sa.Column("provider_status", sa.String(40), nullable=True))
    op.add_column(
        "matter_embedding_batch",
        sa.Column("provider_request_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("matter_embedding_batch", sa.Column("provider_manifest", sa.JSON(), nullable=True))
    op.add_column(
        "matter_embedding_batch",
        sa.Column("provider_last_polled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_matter_embedding_batch_provider_request_count",
        "matter_embedding_batch",
        "provider_request_count >= 0",
    )
    op.create_unique_constraint(
        "uq_matter_embedding_batch_provider_batch_id",
        "matter_embedding_batch",
        ["provider_batch_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_matter_embedding_batch_provider_batch_id",
        "matter_embedding_batch",
        type_="unique",
    )
    op.drop_constraint(
        "ck_matter_embedding_batch_provider_request_count",
        "matter_embedding_batch",
        type_="check",
    )
    for column in (
        "provider_last_polled_at",
        "provider_manifest",
        "provider_request_count",
        "provider_status",
        "provider_error_file_id",
        "provider_output_file_id",
        "provider_batch_id",
        "provider_input_file_id",
    ):
        op.drop_column("matter_embedding_batch", column)
