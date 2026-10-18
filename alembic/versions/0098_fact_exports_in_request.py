"""Produce fact exports within the request instead of as durable jobs.

Exports are now written, stored, and recorded in one request, so a row exists only for a completed export. Rows that
never completed (queued, running, failed, or canceled jobs) are deleted because they have no files; that deletion is
not restored by the downgrade.

Revision ID: 0098_fact_exports_in_request
Revises: 0097_fact_suggestions
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0098_fact_exports_in_request"
down_revision: str | None = "0097_fact_suggestions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_COLUMNS = (
    "status",
    "current_workflow_run_id",
    "workflow_id",
    "current_stage",
    "failure_code",
    "failure_summary",
    "failure_retriable",
    "started_at",
    "completed_at",
    "canceled_at",
)
REQUIRED_COLUMNS = (
    ("facts_artifact_id", sa.Uuid()),
    ("facts_content_hash", sa.String(length=64)),
    ("facts_byte_length", sa.BigInteger()),
    ("facts_filename", sa.String(length=255)),
    ("evidence_artifact_id", sa.Uuid()),
    ("evidence_content_hash", sa.String(length=64)),
    ("evidence_byte_length", sa.BigInteger()),
    ("evidence_filename", sa.String(length=255)),
    ("snapshot_started_at", sa.DateTime(timezone=True)),
    ("expires_at", sa.DateTime(timezone=True)),
)
INDEXED_COLUMNS = ("tenant_id", "matter_id", "created_by_user_id")


def upgrade() -> None:
    # Block writers from old code between the cleanup and the constraint changes.
    op.execute("LOCK TABLE matter_fact_export_job IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DELETE FROM matter_fact_export_job WHERE status <> 'COMPLETED' OR "
        + " OR ".join(f"{name} IS NULL" for name, _ in REQUIRED_COLUMNS)
    )
    op.drop_constraint("ck_matter_fact_export_status", "matter_fact_export_job", type_="check")
    op.drop_index("ix_matter_fact_export_job_current_workflow_run_id", table_name="matter_fact_export_job")
    for column in JOB_COLUMNS:
        op.drop_column("matter_fact_export_job", column)
    for name, type_ in REQUIRED_COLUMNS:
        op.alter_column("matter_fact_export_job", name, existing_type=type_, nullable=False)
    op.rename_table("matter_fact_export_job", "matter_fact_export")
    op.execute("ALTER TABLE matter_fact_export RENAME CONSTRAINT matter_fact_export_job_pkey TO matter_fact_export_pkey")
    for column in INDEXED_COLUMNS:
        op.execute(f"ALTER INDEX ix_matter_fact_export_job_{column} RENAME TO ix_matter_fact_export_{column}")


def downgrade() -> None:
    for column in INDEXED_COLUMNS:
        op.execute(f"ALTER INDEX ix_matter_fact_export_{column} RENAME TO ix_matter_fact_export_job_{column}")
    op.execute("ALTER TABLE matter_fact_export RENAME CONSTRAINT matter_fact_export_pkey TO matter_fact_export_job_pkey")
    op.rename_table("matter_fact_export", "matter_fact_export_job")
    for name, type_ in REQUIRED_COLUMNS:
        op.alter_column("matter_fact_export_job", name, existing_type=type_, nullable=True)
    op.add_column(
        "matter_fact_export_job",
        sa.Column("status", sa.String(length=30), nullable=False, server_default="COMPLETED"),
    )
    op.add_column(
        "matter_fact_export_job",
        sa.Column(
            "current_workflow_run_id",
            sa.Uuid(),
            sa.ForeignKey("workflow_run.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("matter_fact_export_job", sa.Column("workflow_id", sa.String(length=255), nullable=True))
    op.execute("UPDATE matter_fact_export_job SET workflow_id = 'matter-fact-export:' || id::text")
    op.alter_column("matter_fact_export_job", "workflow_id", existing_type=sa.String(length=255), nullable=False)
    op.create_unique_constraint("matter_fact_export_job_workflow_id_key", "matter_fact_export_job", ["workflow_id"])
    op.add_column("matter_fact_export_job", sa.Column("current_stage", sa.String(length=100), nullable=True))
    op.add_column("matter_fact_export_job", sa.Column("failure_code", sa.String(length=100), nullable=True))
    op.add_column("matter_fact_export_job", sa.Column("failure_summary", sa.Text(), nullable=True))
    op.add_column(
        "matter_fact_export_job",
        sa.Column("failure_retriable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    for column in ("started_at", "completed_at", "canceled_at"):
        op.add_column("matter_fact_export_job", sa.Column(column, sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE matter_fact_export_job SET started_at = created_at, completed_at = created_at")
    op.alter_column("matter_fact_export_job", "status", server_default=None)
    op.alter_column("matter_fact_export_job", "failure_retriable", server_default=None)
    op.create_check_constraint(
        "ck_matter_fact_export_status",
        "matter_fact_export_job",
        "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
    )
    op.create_index(
        "ix_matter_fact_export_job_current_workflow_run_id", "matter_fact_export_job", ["current_workflow_run_id"]
    )
