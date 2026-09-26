"""Add immutable review decision results and explicit confidence semantics.

Revision ID: 0041_decision_results
Revises: 0040_task_compiler
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0041_decision_results"
down_revision: str | None = "0040_task_compiler"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_decision_result",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_batch_run_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=False),
        sa.Column("matter_document_id", sa.Uuid(), nullable=False),
        sa.Column("matter_analysis_task_version_id", sa.Uuid(), nullable=False),
        sa.Column("definition_content_hash", sa.String(length=64), nullable=False),
        sa.Column("specification_content_hash", sa.String(length=64), nullable=False),
        sa.Column("source_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column("state_content_hash", sa.String(length=64), nullable=False),
        sa.Column("question_set_hash", sa.String(length=64), nullable=False),
        sa.Column("decision_policy_hash", sa.String(length=64), nullable=False),
        sa.Column("paragraph_map_version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("coverage", sa.JSON(), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("recommendations", sa.JSON(), nullable=False),
        sa.Column("routes", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("raw_answer_hash", sa.String(length=64), nullable=True),
        sa.Column("policy_evaluation_hash", sa.String(length=64), nullable=True),
        sa.Column("engine_key", sa.String(length=100), nullable=True),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=500), nullable=True),
        sa.Column("provider_request_id", sa.String(length=500), nullable=True),
        sa.Column("provider_metadata", sa.JSON(), nullable=False),
        sa.Column("evaluation_skill_run_id", sa.Uuid(), nullable=True),
        sa.Column("evidence_skill_run_id", sa.Uuid(), nullable=True),
        sa.Column("model_invocation_id", sa.Uuid(), nullable=True),
        sa.Column("reused_from_result_id", sa.Uuid(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('COMPLETED', 'PARTIAL', 'FAILED', 'SKIPPED')",
            name="ck_review_decision_result_status",
        ),
        sa.CheckConstraint(
            "attempts > 0 AND request_count >= 0 AND input_tokens >= 0 AND output_tokens >= 0 "
            "AND latency_ms >= 0",
            name="ck_review_decision_result_usage",
        ),
        sa.CheckConstraint(
            "status IN ('FAILED', 'SKIPPED') OR "
            "(engine_key IS NOT NULL AND provider IS NOT NULL AND model IS NOT NULL "
            "AND raw_answer_hash IS NOT NULL AND policy_evaluation_hash IS NOT NULL)",
            name="ck_review_decision_result_completed_shape",
        ),
        sa.ForeignKeyConstraint(
            ["review_batch_run_id"], ["review_batch_run.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_run.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_document_id"], ["matter_document.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["matter_analysis_task_version_id"],
            ["matter_analysis_task_version.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_skill_run_id"], ["skill_run.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["evidence_skill_run_id"], ["skill_run.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["model_invocation_id"], ["model_invocation.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["reused_from_result_id"], ["review_decision_result.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "review_batch_run_id",
            "matter_document_id",
            name="uq_review_decision_result_run_document",
        ),
    )
    op.create_index(
        "ix_review_decision_result_task_version",
        "review_decision_result",
        ["matter_analysis_task_version_id", "created_at"],
    )
    op.create_index(
        "ix_review_decision_result_document",
        "review_decision_result",
        ["matter_document_id", "created_at"],
    )
    op.create_index(
        "ix_review_decision_result_workflow",
        "review_decision_result",
        ["workflow_run_id", "status"],
    )
    op.create_index(
        "ix_review_decision_result_status",
        "review_decision_result",
        ["status"],
    )
    op.create_index(
        "ix_review_decision_result_review_batch_run_id",
        "review_decision_result",
        ["review_batch_run_id"],
    )
    op.create_index(
        "ix_review_decision_result_workflow_run_id",
        "review_decision_result",
        ["workflow_run_id"],
    )
    op.create_index(
        "ix_review_decision_result_matter_document_id",
        "review_decision_result",
        ["matter_document_id"],
    )
    op.create_index(
        "ix_review_decision_result_matter_analysis_task_version_id",
        "review_decision_result",
        ["matter_analysis_task_version_id"],
    )
    op.create_index(
        "ix_review_decision_result_evaluation_skill_run_id",
        "review_decision_result",
        ["evaluation_skill_run_id"],
    )
    op.create_index(
        "ix_review_decision_result_evidence_skill_run_id",
        "review_decision_result",
        ["evidence_skill_run_id"],
    )
    op.create_index(
        "ix_review_decision_result_model_invocation_id",
        "review_decision_result",
        ["model_invocation_id"],
        unique=True,
    )
    op.create_index(
        "ix_review_decision_result_reused_from_result_id",
        "review_decision_result",
        ["reused_from_result_id"],
    )
    op.execute(
        """
        CREATE FUNCTION prevent_review_decision_result_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'review_decision_result rows are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_review_decision_result_immutable
        BEFORE UPDATE OR DELETE ON review_decision_result
        FOR EACH ROW EXECUTE FUNCTION prevent_review_decision_result_mutation()
        """
    )

    op.add_column("review_batch_run_value", sa.Column("confidence_kind", sa.String(length=30), nullable=True))
    op.add_column(
        "review_batch_run_value", sa.Column("review_decision_result_id", sa.Uuid(), nullable=True)
    )
    op.add_column("review_batch_run_value", sa.Column("question_key", sa.String(length=500), nullable=True))
    op.create_foreign_key(
        "fk_review_batch_run_value_decision_result",
        "review_batch_run_value",
        "review_decision_result",
        ["review_decision_result_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_review_batch_run_value_confidence_kind",
        "review_batch_run_value",
        "confidence_kind IS NULL OR (confidence IS NULL AND confidence_kind = 'NONE') OR "
        "(confidence IS NOT NULL AND confidence_kind IN "
        "('PROVIDER_CONFIDENCE', 'SELECTED_PROBABILITY', 'DERIVED_PROBABILITY'))",
    )
    op.create_index(
        "ix_review_batch_run_value_decision_result",
        "review_batch_run_value",
        ["review_decision_result_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_review_batch_run_value_decision_result", table_name="review_batch_run_value")
    op.drop_constraint(
        "ck_review_batch_run_value_confidence_kind",
        "review_batch_run_value",
        type_="check",
    )
    op.drop_constraint(
        "fk_review_batch_run_value_decision_result",
        "review_batch_run_value",
        type_="foreignkey",
    )
    op.drop_column("review_batch_run_value", "question_key")
    op.drop_column("review_batch_run_value", "review_decision_result_id")
    op.drop_column("review_batch_run_value", "confidence_kind")
    op.execute("DROP TRIGGER IF EXISTS trg_review_decision_result_immutable ON review_decision_result")
    op.execute("DROP FUNCTION IF EXISTS prevent_review_decision_result_mutation()")
    op.drop_table("review_decision_result")
