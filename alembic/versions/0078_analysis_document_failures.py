"""persist analysis task document failures

Revision ID: 0078_analysis_doc_failures
Revises: 0077_batch_coding_runs
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0078_analysis_doc_failures"
down_revision: str | None = "0077_batch_coding_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_batch_run_document",
        sa.Column("last_workflow_run_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "review_batch_run_document",
        sa.Column("failure_code", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "review_batch_run_document",
        sa.Column("failure_message", sa.Text(), nullable=True),
    )
    op.add_column(
        "review_batch_run_document",
        sa.Column("failure_retriable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_foreign_key(
        "fk_review_batch_run_document_last_workflow",
        "review_batch_run_document",
        "workflow_run",
        ["last_workflow_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_review_batch_run_document_last_workflow",
        "review_batch_run_document",
        ["last_workflow_run_id"],
    )

    op.execute(
        sa.text(
            """
            UPDATE review_batch_run_document AS document
            SET last_workflow_run_id = run.workflow_run_record_id
            FROM review_batch_run AS run
            WHERE run.id = document.review_batch_run_id
              AND run.run_type = 'WORKFLOW'
              AND run.workflow_run_record_id IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE review_batch_run_document
            SET failure_code = 'LEGACY_FAILURE_DETAILS_UNAVAILABLE',
                failure_message = 'This document failed before per-document failure details were retained.'
            WHERE status = 'FAILED'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE review_batch_run_document AS document
            SET failure_code = COALESCE(latest.error_code, 'MODEL_FAILURE'),
                failure_message = COALESCE(latest.error_message, document.failure_message)
            FROM (
                SELECT DISTINCT ON (run.id, skill.scope_id)
                       run.id AS review_batch_run_id,
                       skill.scope_id AS matter_document_id,
                       skill.error_code,
                       skill.error_message
                FROM skill_run AS skill
                JOIN review_batch_run AS run
                  ON run.workflow_run_record_id = skill.workflow_run_id
                WHERE skill.status = 'FAILED'
                  AND skill.scope_id IS NOT NULL
                ORDER BY run.id,
                         skill.scope_id,
                         skill.completed_at DESC NULLS LAST,
                         skill.created_at DESC,
                         skill.id DESC
            ) AS latest
            WHERE document.status = 'FAILED'
              AND latest.review_batch_run_id = document.review_batch_run_id
              AND latest.matter_document_id = document.matter_document_id
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_review_batch_run_document_last_workflow",
        table_name="review_batch_run_document",
    )
    op.drop_constraint(
        "fk_review_batch_run_document_last_workflow",
        "review_batch_run_document",
        type_="foreignkey",
    )
    op.drop_column("review_batch_run_document", "failure_retriable")
    op.drop_column("review_batch_run_document", "failure_message")
    op.drop_column("review_batch_run_document", "failure_code")
    op.drop_column("review_batch_run_document", "last_workflow_run_id")
