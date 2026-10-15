"""Cache fact summary embeddings for agent recall.

Revision ID: 0091_fact_summary_embeddings
Revises: 0090_fact_exports
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0091_fact_summary_embeddings"
down_revision: str | None = "0090_fact_exports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("matter_fact", sa.Column("summary_embedding", sa.JSON(), nullable=True))
    op.add_column("matter_fact", sa.Column("summary_embedding_model", sa.String(length=300), nullable=True))
    op.add_column("matter_fact", sa.Column("summary_embedding_hash", sa.String(length=64), nullable=True))
    op.create_index("ix_matter_fact_issue_issue_key", "matter_fact_issue", ["issue_key"])


def downgrade() -> None:
    op.drop_index("ix_matter_fact_issue_issue_key", table_name="matter_fact_issue")
    op.drop_column("matter_fact", "summary_embedding_hash")
    op.drop_column("matter_fact", "summary_embedding_model")
    op.drop_column("matter_fact", "summary_embedding")
