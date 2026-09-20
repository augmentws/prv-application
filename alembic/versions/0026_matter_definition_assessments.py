"""Add Matter Definition assessment workflow records.

Revision ID: 0026_definition_assessments
Revises: 0025_model_execution_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_definition_assessments"
down_revision: str | None = "0025_model_execution_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH', "
        "'DEFINITION_ASSESSMENT')",
    )
    op.create_table(
        "matter_definition_assessment_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("matter_definition_revision_id", sa.Uuid(), nullable=False),
        sa.Column("definition_content_hash", sa.String(length=64), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=False),
        sa.Column("search_index_generation_id", sa.Uuid(), nullable=True),
        sa.Column("review_batch_id", sa.Uuid(), nullable=True),
        sa.Column("review_batch_run_id", sa.Uuid(), nullable=True),
        sa.Column("configuration_snapshot", sa.JSON(), nullable=False),
        sa.Column("binding_snapshot", sa.JSON(), nullable=False),
        sa.Column("requested_document_count", sa.Integer(), nullable=False),
        sa.Column("control_sample_size", sa.Integer(), nullable=False),
        sa.Column("large_run_warning_acknowledged", sa.Boolean(), nullable=False),
        sa.Column("warning_acknowledged_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("warning_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("estimated_output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("token_estimator", sa.String(length=100), nullable=True),
        sa.Column("token_estimator_version", sa.String(length=100), nullable=True),
        sa.Column("estimation_model", sa.String(length=500), nullable=True),
        sa.Column("estimate_source_hashes", sa.JSON(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("selected_count", sa.Integer(), nullable=False),
        sa.Column("summarized_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("initiated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'PLANNING', 'RETRIEVING', 'BUILDING_BATCH', 'SUMMARIZING', "
            "'SYNTHESIZING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_matter_definition_assessment_status",
        ),
        sa.CheckConstraint(
            "requested_document_count > 0", name="ck_matter_definition_assessment_requested_count"
        ),
        sa.CheckConstraint(
            "candidate_count >= 0 AND selected_count >= 0 AND summarized_count >= 0 "
            "AND skipped_count >= 0 AND failed_count >= 0",
            name="ck_matter_definition_assessment_counts",
        ),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["matter_definition_revision_id"], ["matter_definition_revision.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_run.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["search_index_generation_id"], ["search_index_generation.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["review_batch_id"], ["review_batch.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["review_batch_run_id"], ["review_batch_run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["warning_acknowledged_by_user_id"], ["app_user.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["initiated_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_run_id"),
        sa.UniqueConstraint("review_batch_id"),
        sa.UniqueConstraint("review_batch_run_id"),
    )
    for name, column in (
        ("ix_definition_assessment_matter", "matter_id"),
        ("ix_definition_assessment_revision", "matter_definition_revision_id"),
        ("ix_definition_assessment_generation", "search_index_generation_id"),
        ("ix_definition_assessment_status", "status"),
        ("ix_definition_assessment_initiator", "initiated_by_user_id"),
    ):
        op.create_index(name, "matter_definition_assessment_run", [column])
    op.create_index(
        "ix_definition_assessment_matter_created",
        "matter_definition_assessment_run",
        ["matter_id", "created_at"],
    )

    op.create_table(
        "matter_definition_assessment_query",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assessment_run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("criterion_key", sa.String(length=200), nullable=False),
        sa.Column("criterion_label", sa.String(length=500), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("search_request", sa.JSON(), nullable=False),
        sa.Column("quota", sa.Integer(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ordinal > 0 AND quota > 0 AND result_count >= 0", name="ck_definition_assessment_query_counts"
        ),
        sa.ForeignKeyConstraint(
            ["assessment_run_id"], ["matter_definition_assessment_run.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("assessment_run_id", "ordinal", name="uq_definition_assessment_query_ordinal"),
    )
    op.create_index(
        "ix_matter_definition_assessment_query_assessment_run_id",
        "matter_definition_assessment_query",
        ["assessment_run_id"],
    )

    op.create_table(
        "matter_definition_assessment_candidate",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assessment_run_id", sa.Uuid(), nullable=False),
        sa.Column("matter_document_id", sa.Uuid(), nullable=False),
        sa.Column("retrieval_provenance", sa.JSON(), nullable=False),
        sa.Column("fused_score", sa.Float(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("selection_order", sa.Integer(), nullable=True),
        sa.Column("selection_reason", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "selection_order IS NULL OR selection_order > 0", name="ck_definition_assessment_selection_order"
        ),
        sa.ForeignKeyConstraint(
            ["assessment_run_id"], ["matter_definition_assessment_run.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["matter_document_id"], ["matter_document.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assessment_run_id", "matter_document_id", name="uq_definition_assessment_candidate_document"
        ),
    )
    op.create_index(
        "ix_matter_definition_assessment_candidate_assessment_run_id",
        "matter_definition_assessment_candidate",
        ["assessment_run_id"],
    )
    op.create_index(
        "ix_matter_definition_assessment_candidate_matter_document_id",
        "matter_definition_assessment_candidate",
        ["matter_document_id"],
    )
    op.create_index(
        "ix_definition_assessment_candidate_selected",
        "matter_definition_assessment_candidate",
        ["assessment_run_id", "selected"],
    )

    op.create_table(
        "matter_definition_assessment_question",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assessment_run_id", sa.Uuid(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(length=20), nullable=False),
        sa.Column("blocking", sa.Boolean(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("answered_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "priority IN ('HIGH', 'MEDIUM', 'LOW')", name="ck_definition_assessment_question_priority"
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'ANSWERED', 'DISMISSED')", name="ck_definition_assessment_question_status"
        ),
        sa.ForeignKeyConstraint(
            ["assessment_run_id"], ["matter_definition_assessment_run.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["answered_by_user_id"], ["app_user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_matter_definition_assessment_question_assessment_run_id",
        "matter_definition_assessment_question",
        ["assessment_run_id"],
    )
    op.create_index(
        "ix_definition_assessment_question_status",
        "matter_definition_assessment_question",
        ["assessment_run_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("matter_definition_assessment_question")
    op.drop_table("matter_definition_assessment_candidate")
    op.drop_table("matter_definition_assessment_query")
    op.drop_table("matter_definition_assessment_run")
    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH')",
    )
