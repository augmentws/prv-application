"""Add search index generations and durable projection operations.

Revision ID: 0008_search_projection
Revises: 0007_matter_document_custodians
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_search_projection"
down_revision: str | None = "0007_matter_document_custodians"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_index_generation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("index_name", sa.String(length=255), nullable=False),
        sa.Column("alias_name", sa.String(length=255), nullable=False),
        sa.Column("schema_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("schema_snapshot", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('CREATING', 'ACTIVE', 'RETIRED', 'FAILED')",
            name="ck_search_index_generation_status",
        ),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("index_name", name="uq_search_index_name"),
        sa.UniqueConstraint("matter_id", "generation", name="uq_search_index_matter_generation"),
    )
    op.create_index("ix_search_index_generation_matter_id", "search_index_generation", ["matter_id"])
    op.create_index("ix_search_index_generation_schema_hash", "search_index_generation", ["schema_hash"])
    op.create_index("ix_search_index_matter_status", "search_index_generation", ["matter_id", "status"])
    op.create_index(
        "uq_search_index_active_matter",
        "search_index_generation",
        ["matter_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
        sqlite_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "search_projection_operation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('SCHEMA_SYNC', 'REBUILD', 'DOCUMENT_UPSERT', 'DOCUMENT_DELETE')",
            name="ck_search_projection_kind",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="ck_search_projection_status",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", name="uq_search_projection_workflow"),
    )
    op.create_index("ix_search_projection_operation_matter_id", "search_projection_operation", ["matter_id"])
    op.create_index("ix_search_projection_operation_created_by_user_id", "search_projection_operation", ["created_by_user_id"])
    op.create_index("ix_search_projection_matter_created", "search_projection_operation", ["matter_id", "created_at"])
    op.create_index("ix_search_projection_status_created", "search_projection_operation", ["status", "created_at"])


def downgrade() -> None:
    op.drop_table("search_projection_operation")
    op.drop_table("search_index_generation")
