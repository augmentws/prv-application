"""Add temporary collection selections for durable imports.

Revision ID: 0002_collection_selections
Revises: 0001_artifact
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_collection_selections"
down_revision: str | None = "0001_artifact"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collection_selection",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("collection_id", sa.Uuid(), nullable=False),
        sa.Column("selection", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('READY', 'DISPATCHED')", name="ck_collection_selection_status"),
        sa.CheckConstraint("total_count >= 0", name="ck_collection_selection_total"),
        sa.ForeignKeyConstraint(["collection_id"], ["client_collection.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_collection_selection_request"),
    )
    op.create_index("ix_collection_selection_tenant_id", "collection_selection", ["tenant_id"])
    op.create_index("ix_collection_selection_client_id", "collection_selection", ["client_id"])
    op.create_index("ix_collection_selection_collection_id", "collection_selection", ["collection_id"])
    op.create_index(
        "ix_collection_selection_collection_created",
        "collection_selection",
        ["collection_id", "created_at"],
    )

    op.create_table(
        "collection_selection_item",
        sa.Column("selection_id", sa.Uuid(), nullable=False),
        sa.Column("collection_item_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["selection_id"], ["collection_selection.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["collection_item_id"], ["collection_item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("selection_id", "collection_item_id"),
        sa.UniqueConstraint("selection_id", "ordinal", name="uq_collection_selection_item_ordinal"),
    )


def downgrade() -> None:
    op.drop_table("collection_selection_item")
    op.drop_table("collection_selection")
