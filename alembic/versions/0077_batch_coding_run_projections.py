"""retain batch coding search projections by run

Revision ID: 0077_batch_coding_runs
Revises: 0076_analysis_task_provider
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0077_batch_coding_runs"
down_revision: str | None = "0076_analysis_task_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_batch_search_coding_projection",
        sa.Column("review_batch_id", sa.Uuid(), nullable=False),
        sa.Column("review_batch_run_id", sa.Uuid(), nullable=False),
        sa.Column("search_index_generation_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'SYNCING', 'READY', 'FAILED', 'NOT_CONFIGURED')",
            name="ck_review_batch_search_projection_status",
        ),
        sa.ForeignKeyConstraint(
            ["review_batch_id"],
            ["review_batch.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["review_batch_run_id"],
            ["review_batch_run.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["search_index_generation_id"],
            ["search_index_generation.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["app_user.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "review_batch_id",
            "review_batch_run_id",
            name="pk_review_batch_search_coding_projection",
        ),
    )
    op.create_index(
        "ix_review_batch_search_projection_run",
        "review_batch_search_coding_projection",
        ["review_batch_run_id"],
    )
    op.create_index(
        "ix_review_batch_search_projection_status",
        "review_batch_search_coding_projection",
        ["status"],
    )
    op.create_index(
        "ix_review_batch_search_projection_generation",
        "review_batch_search_coding_projection",
        ["search_index_generation_id"],
    )
    op.create_index(
        "ix_review_batch_search_projection_user",
        "review_batch_search_coding_projection",
        ["requested_by_user_id"],
    )

    # Preserve the projection represented by the legacy single-selection row.
    op.execute(
        sa.text(
            """
            INSERT INTO review_batch_search_coding_projection (
                review_batch_id,
                review_batch_run_id,
                search_index_generation_id,
                status,
                requested_by_user_id,
                projected_at,
                error_message,
                created_at,
                updated_at
            )
            SELECT
                selection.review_batch_id,
                selection.review_batch_run_id,
                batch.search_index_generation_id,
                selection.status,
                selection.selected_by_user_id,
                selection.projected_at,
                selection.error_message,
                selection.created_at,
                selection.updated_at
            FROM review_batch_search_coding_run AS selection
            JOIN review_batch AS batch ON batch.id = selection.review_batch_id
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_review_batch_search_projection_user",
        table_name="review_batch_search_coding_projection",
    )
    op.drop_index(
        "ix_review_batch_search_projection_generation",
        table_name="review_batch_search_coding_projection",
    )
    op.drop_index(
        "ix_review_batch_search_projection_status",
        table_name="review_batch_search_coding_projection",
    )
    op.drop_index(
        "ix_review_batch_search_projection_run",
        table_name="review_batch_search_coding_projection",
    )
    op.drop_table("review_batch_search_coding_projection")
