"""Add metadata definition provenance for versioned matter profiles.

Revision ID: 0003_metadata_profile
Revises: 0002_custodians
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_metadata_profile"
down_revision: str | None = "0002_custodians"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "metadata_definition",
        sa.Column("value_source", sa.String(length=20), nullable=False, server_default="ASSERTED"),
    )
    op.add_column("metadata_definition", sa.Column("reference_target", sa.String(length=50), nullable=True))
    op.add_column("metadata_definition", sa.Column("template_key", sa.String(length=100), nullable=True))
    op.add_column("metadata_definition", sa.Column("template_version", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_metadata_definition_value_source",
        "metadata_definition",
        "value_source IN ('SYSTEM', 'IMPORTED', 'ASSERTED')",
    )
    op.create_check_constraint(
        "ck_metadata_definition_reference_target",
        "metadata_definition",
        "reference_target IS NULL OR reference_target IN ('CUSTODIAN')",
    )
    op.create_check_constraint(
        "ck_metadata_definition_template_identity",
        "metadata_definition",
        "(template_key IS NULL AND template_version IS NULL) OR "
        "(template_key IS NOT NULL AND template_version IS NOT NULL AND template_version > 0)",
    )
    op.alter_column("metadata_definition", "value_source", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_metadata_definition_template_identity", "metadata_definition", type_="check")
    op.drop_constraint("ck_metadata_definition_reference_target", "metadata_definition", type_="check")
    op.drop_constraint("ck_metadata_definition_value_source", "metadata_definition", type_="check")
    op.drop_column("metadata_definition", "template_version")
    op.drop_column("metadata_definition", "template_key")
    op.drop_column("metadata_definition", "reference_target")
    op.drop_column("metadata_definition", "value_source")
