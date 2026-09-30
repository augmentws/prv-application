"""Project one selected batch coding run into search.

Revision ID: 0043_batch_coding_search
Revises: 0042_saved_search_batches
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0043_batch_coding_search"
down_revision: str | None = "0042_saved_search_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_batch_search_coding_run",
        sa.Column("review_batch_id", sa.Uuid(), nullable=False),
        sa.Column("review_batch_run_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("selected_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'SYNCING', 'READY', 'FAILED', 'NOT_CONFIGURED')",
            name="ck_review_batch_search_coding_status",
        ),
        sa.ForeignKeyConstraint(
            ["review_batch_id"], ["review_batch.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["review_batch_run_id"], ["review_batch_run.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["selected_by_user_id"], ["app_user.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("review_batch_id"),
        sa.UniqueConstraint(
            "review_batch_run_id", name="uq_review_batch_search_coding_run"
        ),
    )
    op.create_index(
        "ix_review_batch_search_coding_run_id",
        "review_batch_search_coding_run",
        ["review_batch_run_id"],
    )
    op.create_index(
        "ix_review_batch_search_coding_status",
        "review_batch_search_coding_run",
        ["status"],
    )
    op.create_index(
        "ix_review_batch_search_coding_user",
        "review_batch_search_coding_run",
        ["selected_by_user_id"],
    )

    op.drop_constraint("ck_search_projection_kind", "search_projection_operation", type_="check")
    op.create_check_constraint(
        "ck_search_projection_kind",
        "search_projection_operation",
        "kind IN ('SCHEMA_SYNC', 'REBUILD', 'DOCUMENT_UPSERT', 'DOCUMENT_DELETE', "
        "'BATCH_CODING_SYNC')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_search_projection_kind", "search_projection_operation", type_="check")
    op.create_check_constraint(
        "ck_search_projection_kind",
        "search_projection_operation",
        "kind IN ('SCHEMA_SYNC', 'REBUILD', 'DOCUMENT_UPSERT', 'DOCUMENT_DELETE')",
    )
    op.drop_index(
        "ix_review_batch_search_coding_user",
        table_name="review_batch_search_coding_run",
    )
    op.drop_index(
        "ix_review_batch_search_coding_status",
        table_name="review_batch_search_coding_run",
    )
    op.drop_index(
        "ix_review_batch_search_coding_run_id",
        table_name="review_batch_search_coding_run",
    )
    op.drop_table("review_batch_search_coding_run")
