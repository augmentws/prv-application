"""Add matter-scoped saved searches and user shares.

Revision ID: 0015_matter_saved_searches
Revises: 0014_matter_topic_jobs
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_matter_saved_searches"
down_revision: str | None = "0014_matter_topic_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matter_saved_search",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("normalized_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("visibility", sa.String(length=20), nullable=False),
        sa.Column("search_definition", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "visibility IN ('PRIVATE', 'PUBLIC', 'SHARED')",
            name="ck_matter_saved_search_visibility",
        ),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "matter_id",
            "owner_user_id",
            "normalized_name",
            name="uq_matter_saved_search_owner_name",
        ),
    )
    op.create_index("ix_matter_saved_search_matter_id", "matter_saved_search", ["matter_id"])
    op.create_index("ix_matter_saved_search_owner_user_id", "matter_saved_search", ["owner_user_id"])
    op.create_index("ix_matter_saved_search_visibility", "matter_saved_search", ["visibility"])
    op.create_index(
        "ix_matter_saved_search_matter_updated",
        "matter_saved_search",
        ["matter_id", "updated_at"],
    )

    op.create_table(
        "matter_saved_search_user_share",
        sa.Column("saved_search_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["saved_search_id"], ["matter_saved_search.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("saved_search_id", "user_id"),
    )
    op.create_index(
        "ix_matter_saved_search_user_share_user_id",
        "matter_saved_search_user_share",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_table("matter_saved_search_user_share")
    op.drop_table("matter_saved_search")
