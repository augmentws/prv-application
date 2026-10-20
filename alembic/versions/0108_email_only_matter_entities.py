"""Allow matter entities identified only by email.

Revision ID: 0108_email_only_entities
Revises: 0107_review_run_source
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0108_email_only_entities"
down_revision: str = "0107_review_run_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("matter_entity", "display_name", existing_type=sa.String(length=300), nullable=True)
    op.alter_column("matter_entity", "normalized_name", existing_type=sa.String(length=300), nullable=True)
    op.create_check_constraint(
        "ck_matter_entity_name_or_email",
        "matter_entity",
        "display_name IS NOT NULL OR email IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_matter_entity_name_pair",
        "matter_entity",
        "(display_name IS NULL) = (normalized_name IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_matter_entity_name_pair", "matter_entity", type_="check")
    op.drop_constraint("ck_matter_entity_name_or_email", "matter_entity", type_="check")
    op.execute(
        sa.text(
            "UPDATE matter_entity SET display_name = email, normalized_name = normalized_email "
            "WHERE display_name IS NULL"
        )
    )
    op.alter_column("matter_entity", "normalized_name", existing_type=sa.String(length=300), nullable=False)
    op.alter_column("matter_entity", "display_name", existing_type=sa.String(length=300), nullable=False)
