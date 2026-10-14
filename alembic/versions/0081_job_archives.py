"""add generic soft-delete records for jobs

Revision ID: 0081_job_archives
Revises: 0080_analysis_input_retry
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0081_job_archives"
down_revision: str | None = "0080_analysis_input_retry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_archive",
        sa.Column("job_type", sa.String(length=80), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("archived_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["archived_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_type", "job_id"),
    )
    op.create_index("ix_job_archive_archived_by_user_id", "job_archive", ["archived_by_user_id"])
    op.create_index("ix_job_archive_matter_archived", "job_archive", ["matter_id", "archived_at"])
    op.create_index("ix_job_archive_matter_id", "job_archive", ["matter_id"])
    op.create_index("ix_job_archive_tenant_id", "job_archive", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_job_archive_tenant_id", table_name="job_archive")
    op.drop_index("ix_job_archive_matter_id", table_name="job_archive")
    op.drop_index("ix_job_archive_matter_archived", table_name="job_archive")
    op.drop_index("ix_job_archive_archived_by_user_id", table_name="job_archive")
    op.drop_table("job_archive")
