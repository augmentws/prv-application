"""Add Core-owned matter document custodian associations.

Revision ID: 0007_matter_document_custodians
Revises: 0006_global_user_email
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_matter_document_custodians"
down_revision: str | None = "0006_global_user_email"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matter_document_custodian",
        sa.Column("matter_document_id", sa.Uuid(), nullable=False),
        sa.Column("custodian_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(length=20), nullable=False),
        sa.CheckConstraint(
            "relationship_type IN ('PRIMARY', 'COMMON')",
            name="ck_matter_document_custodian_type",
        ),
        sa.ForeignKeyConstraint(["matter_document_id"], ["matter_document.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["custodian_id"], ["custodian.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("matter_document_id", "custodian_id"),
    )
    op.create_index(
        "ix_matter_document_custodian_custodian_id",
        "matter_document_custodian",
        ["custodian_id"],
    )


def downgrade() -> None:
    op.drop_table("matter_document_custodian")
