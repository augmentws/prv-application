"""Add lowercase normalization to metadata field definitions.

Revision ID: 0033_lowercase_metadata
Revises: 0032_agent_packages
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_lowercase_metadata"
down_revision: str | None = "0032_agent_packages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "metadata_definition",
        sa.Column("normalize_to_lowercase", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        sa.text(
            "UPDATE metadata_definition "
            "SET normalize_to_lowercase = true "
            "WHERE key IN ('email_from', 'email_to', 'email_cc', 'email_bcc')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE metadata_definition "
            "SET template_version = 2 "
            "WHERE template_key = 'edrm-core' AND template_version = 1"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE metadata_definition "
            "SET template_version = 1 "
            "WHERE template_key = 'edrm-core' AND template_version = 2"
        )
    )
    op.drop_column("metadata_definition", "normalize_to_lowercase")
