"""Add per-run review document progress.

Revision ID: 0017_review_batch_run_documents
Revises: 0016_review_batches
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_review_batch_run_documents"
down_revision: str | None = "0016_review_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_batch_run_document",
        sa.Column("review_batch_run_id", sa.Uuid(), nullable=False),
        sa.Column("matter_document_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('IN_PROGRESS', 'COMPLETED', 'SKIPPED')",
            name="ck_review_batch_run_document_status",
        ),
        sa.ForeignKeyConstraint(["review_batch_run_id"], ["review_batch_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matter_document_id"], ["matter_document.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("review_batch_run_id", "matter_document_id"),
    )
    op.create_index(
        "ix_review_batch_run_document_matter_document_id",
        "review_batch_run_document",
        ["matter_document_id"],
    )
    op.create_index(
        "ix_review_batch_run_document_run_status",
        "review_batch_run_document",
        ["review_batch_run_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("review_batch_run_document")
