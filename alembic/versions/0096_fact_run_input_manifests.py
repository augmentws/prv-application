"""Freeze fact-development populations in analysis input manifests instead of chunk rows.

Revision ID: 0096_fact_run_input_manifests
Revises: 0095_fact_run_fingerprints
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0096_fact_run_input_manifests"
down_revision: str | None = "0095_fact_run_fingerprints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Chunk rows from before this revision describe populations no manifest records; they cannot be resumed.
    op.execute("DELETE FROM matter_analysis_task_run_chunk")
    op.add_column(
        "matter_analysis_task_run",
        sa.Column("analysis_input_manifest_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_matter_analysis_task_run_input_manifest",
        "matter_analysis_task_run",
        "analysis_input_manifest",
        ["analysis_input_manifest_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_matter_analysis_task_run_analysis_input_manifest_id"),
        "matter_analysis_task_run",
        ["analysis_input_manifest_id"],
        unique=False,
    )
    op.drop_column("matter_analysis_task_run_chunk", "prior_fingerprints")
    op.drop_column("matter_analysis_task_run_chunk", "issue_keys")
    op.drop_column("matter_analysis_task_run_chunk", "document_ids")


def downgrade() -> None:
    op.execute("DELETE FROM matter_analysis_task_run_chunk")
    op.add_column(
        "matter_analysis_task_run_chunk",
        sa.Column("document_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "matter_analysis_task_run_chunk",
        sa.Column("issue_keys", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        "matter_analysis_task_run_chunk",
        sa.Column("prior_fingerprints", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.drop_index(
        op.f("ix_matter_analysis_task_run_analysis_input_manifest_id"), table_name="matter_analysis_task_run"
    )
    op.drop_constraint("fk_matter_analysis_task_run_input_manifest", "matter_analysis_task_run", type_="foreignkey")
    op.drop_column("matter_analysis_task_run", "analysis_input_manifest_id")
