"""Track trained and excluded classification classes.

Revision ID: 0062_class_exclusions
Revises: 0061_classification_training
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0062_class_exclusions"
down_revision: str | None = "0061_classification_training"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    for table_name in ("classification_dataset_snapshot", "classification_model_version"):
        op.add_column(
            table_name,
            sa.Column("trained_class_keys", json_document, nullable=False, server_default=sa.text("'[]'")),
        )
        op.add_column(
            table_name,
            sa.Column("excluded_classes", json_document, nullable=False, server_default=sa.text("'[]'")),
        )
        op.alter_column(table_name, "trained_class_keys", server_default=None)
        op.alter_column(table_name, "excluded_classes", server_default=None)


def downgrade() -> None:
    for table_name in ("classification_model_version", "classification_dataset_snapshot"):
        op.drop_column(table_name, "excluded_classes")
        op.drop_column(table_name, "trained_class_keys")
