"""Archive superseded Matter Definition assessments.

Revision ID: 0116_assessment_archiving
Revises: 0115_search_error_promotion
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0116_assessment_archiving"
down_revision: str | None = "0115_search_error_promotion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("archived_by_user_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("archive_reason", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("superseded_by_assessment_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_definition_assessment_archived_by",
        "matter_definition_assessment_run",
        "app_user",
        ["archived_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_definition_assessment_superseded_by",
        "matter_definition_assessment_run",
        "matter_definition_assessment_run",
        ["superseded_by_assessment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_definition_assessment_archived_by_user_id",
        "matter_definition_assessment_run",
        ["archived_by_user_id"],
    )
    op.create_index(
        "ix_definition_assessment_superseded_by_assessment_id",
        "matter_definition_assessment_run",
        ["superseded_by_assessment_id"],
    )
    op.create_index(
        "ix_definition_assessment_matter_archived",
        "matter_definition_assessment_run",
        ["matter_id", "archived_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_definition_assessment_matter_archived",
        table_name="matter_definition_assessment_run",
    )
    op.drop_index(
        "ix_definition_assessment_superseded_by_assessment_id",
        table_name="matter_definition_assessment_run",
    )
    op.drop_index(
        "ix_definition_assessment_archived_by_user_id",
        table_name="matter_definition_assessment_run",
    )
    op.drop_constraint(
        "fk_definition_assessment_superseded_by",
        "matter_definition_assessment_run",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_definition_assessment_archived_by",
        "matter_definition_assessment_run",
        type_="foreignkey",
    )
    op.drop_column("matter_definition_assessment_run", "superseded_by_assessment_id")
    op.drop_column("matter_definition_assessment_run", "archive_reason")
    op.drop_column("matter_definition_assessment_run", "archived_by_user_id")
    op.drop_column("matter_definition_assessment_run", "archived_at")
