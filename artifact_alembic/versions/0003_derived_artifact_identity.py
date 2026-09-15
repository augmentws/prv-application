"""Add derived artifact metadata and deterministic identity.

Revision ID: 0003_derived_artifact_identity
Revises: 0002_collection_selections
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_derived_artifact_identity"
down_revision: str | None = "0002_collection_selections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("artifact", sa.Column("metadata", sa.JSON(), nullable=True))
    op.execute(sa.text("UPDATE artifact SET metadata = '{}' WHERE metadata IS NULL"))
    op.alter_column("artifact", "metadata", nullable=False)
    op.add_column(
        "collection_item_artifact",
        sa.Column("derivation_key", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_collection_item_artifact_derivation_key",
        "collection_item_artifact",
        ["derivation_key"],
    )
    op.create_unique_constraint(
        "uq_item_artifact_derivation",
        "collection_item_artifact",
        ["collection_item_id", "artifact_role", "derivation_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_item_artifact_derivation", "collection_item_artifact", type_="unique")
    op.drop_index("ix_collection_item_artifact_derivation_key", table_name="collection_item_artifact")
    op.drop_column("collection_item_artifact", "derivation_key")
    op.drop_column("artifact", "metadata")
