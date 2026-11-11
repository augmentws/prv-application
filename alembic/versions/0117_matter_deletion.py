"""Add durable permanent matter deletion.

Revision ID: 0117_matter_deletion
Revises: 0116_assessment_archiving
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0117_matter_deletion"
down_revision: str | None = "0116_assessment_archiving"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ACTIVE_STATUSES = (
    "QUEUED",
    "VALIDATING",
    "DELETING_SEARCH",
    "DELETING_ARTIFACTS",
    "DELETING_DATABASE_ROWS",
)


def upgrade() -> None:
    op.drop_constraint("ck_matter_status", "matter", type_="check")
    op.create_check_constraint(
        "ck_matter_status",
        "matter",
        "status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED', 'DELETING')",
    )
    op.create_table(
        "matter_deletion_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("matter_name", sa.String(length=200), nullable=False),
        sa.Column("previous_matter_status", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("document_count", sa.BigInteger(), nullable=False),
        sa.Column("artifact_count", sa.BigInteger(), nullable=False),
        sa.Column("deleted_index_count", sa.Integer(), nullable=False),
        sa.Column("deleted_artifact_count", sa.BigInteger(), nullable=False),
        sa.Column("deleted_blob_count", sa.BigInteger(), nullable=False),
        sa.Column("artifact_scopes", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'VALIDATING', 'DELETING_SEARCH', 'DELETING_ARTIFACTS', "
            "'DELETING_DATABASE_ROWS', 'COMPLETED', 'FAILED')",
            name="ck_matter_deletion_job_status",
        ),
        sa.CheckConstraint(
            "document_count >= 0 AND artifact_count >= 0 AND deleted_index_count >= 0 AND "
            "deleted_artifact_count >= 0 AND deleted_blob_count >= 0",
            name="ck_matter_deletion_job_counts",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
    )
    op.create_index("ix_matter_deletion_job_matter_id", "matter_deletion_job", ["matter_id"])
    op.create_index("ix_matter_deletion_job_tenant_id", "matter_deletion_job", ["tenant_id"])
    op.create_index("ix_matter_deletion_job_client_id", "matter_deletion_job", ["client_id"])
    op.create_index("ix_matter_deletion_job_status", "matter_deletion_job", ["status"])
    op.create_index(
        "ix_matter_deletion_job_requested_by_user_id",
        "matter_deletion_job",
        ["requested_by_user_id"],
    )
    op.create_index(
        "ix_matter_deletion_job_matter_created",
        "matter_deletion_job",
        ["matter_id", "created_at"],
    )
    active = ", ".join(f"'{value}'" for value in ACTIVE_STATUSES)
    op.create_index(
        "uq_matter_deletion_job_active",
        "matter_deletion_job",
        ["matter_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({active})"),
    )


def downgrade() -> None:
    op.drop_table("matter_deletion_job")
    op.drop_constraint("ck_matter_status", "matter", type_="check")
    op.create_check_constraint(
        "ck_matter_status",
        "matter",
        "status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')",
    )
