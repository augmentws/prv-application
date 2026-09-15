"""Merge agent-runtime and matter-embedding migration heads.

Revision ID: 0012_merge_agent_embeddings
Revises: 0011_agent_runtime, 0011_matter_embedding_jobs
"""

from collections.abc import Sequence

revision: str = "0012_merge_agent_embeddings"
down_revision: tuple[str, str] = ("0011_agent_runtime", "0011_matter_embedding_jobs")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
