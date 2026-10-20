"""Add native whole-matter manifest support.

Revision ID: 0106_whole_matter_manifests
Revises: 0105_batch_set_runs
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0106_whole_matter_manifests"
down_revision: str = "0105_batch_set_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("review_batch_run", "review_batch_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column(
        "review_batch_run",
        sa.Column(
            "analysis_input_manifest_id",
            sa.Uuid(),
            sa.ForeignKey("analysis_input_manifest.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_review_batch_run_input_manifest",
        "review_batch_run",
        ["analysis_input_manifest_id"],
    )
    op.add_column(
        "review_batch_run_document",
        sa.Column("input_manifest_ordinal", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_review_batch_run_document_manifest_ordinal",
        "review_batch_run_document",
        "input_manifest_ordinal IS NULL OR input_manifest_ordinal > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_review_batch_run_document_manifest_ordinal",
        "review_batch_run_document",
        type_="check",
    )
    op.drop_column("review_batch_run_document", "input_manifest_ordinal")
    op.drop_index("ix_review_batch_run_input_manifest", table_name="review_batch_run")
    op.drop_column("review_batch_run", "analysis_input_manifest_id")
    op.alter_column("review_batch_run", "review_batch_id", existing_type=sa.Uuid(), nullable=False)
