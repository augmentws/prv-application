"""Add client-scoped custodians.

Revision ID: 0002_custodians
Revises: 0001_core
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_custodians"
down_revision: str | None = "0001_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "custodian",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=300), nullable=False),
        sa.Column("normalized_name", sa.String(length=300), nullable=False),
        sa.Column("email_addresses", sa.JSON(), nullable=False),
        sa.Column("external_reference", sa.String(length=1000), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_custodian_status"),
        sa.ForeignKeyConstraint(["client_id"], ["client.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "normalized_name", name="uq_custodian_client_name"),
    )
    op.create_index("ix_custodian_client_id", "custodian", ["client_id"])


def downgrade() -> None:
    op.drop_table("custodian")
