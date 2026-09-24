"""Allow multiple field assignments in a bulk tag job.

Revision ID: 0037_bulk_tag_assignments
Revises: 0036_matter_bulk_tags
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037_bulk_tag_assignments"
down_revision: str | None = "0036_matter_bulk_tags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("matter_bulk_tag_job", sa.Column("assignments", sa.JSON(), nullable=True))
    op.execute(
        """
        UPDATE matter_bulk_tag_job
        SET assignments = json_build_array(
            json_build_object(
                'metadata_definition_id', metadata_definition_id::text,
                'value', value
            )
        )
        """
    )
    op.alter_column("matter_bulk_tag_job", "assignments", nullable=False)


def downgrade() -> None:
    op.drop_column("matter_bulk_tag_job", "assignments")
