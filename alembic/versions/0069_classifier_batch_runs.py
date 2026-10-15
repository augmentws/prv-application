"""Add classifier projection review-batch runs.

Revision ID: 0069_classifier_batch_runs
Revises: 0068_human_batch_promotions
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0069_classifier_batch_runs"
down_revision: str | None = "0068_human_batch_promotions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_review_batch_run_type", "review_batch_run", type_="check")
    op.drop_constraint("ck_review_batch_run_actor", "review_batch_run", type_="check")
    op.add_column(
        "review_batch_run",
        sa.Column("classification_scoring_run_id", sa.Uuid(), nullable=True),
    )
    op.add_column("review_batch_run", sa.Column("derivation_key", sa.String(length=64), nullable=True))
    op.add_column("review_batch_run", sa.Column("idempotency_key", sa.String(length=200), nullable=True))
    op.add_column("review_batch_run", sa.Column("result_summary", sa.JSON(), nullable=True))
    op.create_foreign_key(
        "fk_review_batch_run_classification_scoring_run",
        "review_batch_run",
        "classification_scoring_run",
        ["classification_scoring_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_review_batch_run_classification_scoring_run_id",
        "review_batch_run",
        ["classification_scoring_run_id"],
    )
    op.create_index("ix_review_batch_run_derivation_key", "review_batch_run", ["derivation_key"])
    op.create_index(
        "uq_review_batch_classifier_idempotency",
        "review_batch_run",
        ["review_batch_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("run_type = 'CLASSIFIER'"),
        sqlite_where=sa.text("run_type = 'CLASSIFIER'"),
    )
    op.create_index(
        "uq_review_batch_classifier_derivation",
        "review_batch_run",
        ["review_batch_id", "classification_scoring_run_id", "derivation_key"],
        unique=True,
        postgresql_where=sa.text("run_type = 'CLASSIFIER'"),
        sqlite_where=sa.text("run_type = 'CLASSIFIER'"),
    )
    op.create_check_constraint(
        "ck_review_batch_run_type",
        "review_batch_run",
        "run_type IN ('HUMAN', 'AGENT', 'WORKFLOW', 'CLASSIFIER')",
    )
    op.create_check_constraint(
        "ck_review_batch_run_actor",
        "review_batch_run",
        "(run_type = 'HUMAN' AND actor_user_id IS NOT NULL "
        "AND agent_definition_version_id IS NULL AND workflow_run_record_id IS NULL "
        "AND classification_scoring_run_id IS NULL) OR "
        "(run_type = 'AGENT' AND actor_user_id IS NULL "
        "AND agent_definition_version_id IS NOT NULL AND workflow_run_record_id IS NULL "
        "AND classification_scoring_run_id IS NULL) OR "
        "(run_type = 'WORKFLOW' AND actor_user_id IS NULL "
        "AND agent_definition_version_id IS NULL AND workflow_run_record_id IS NOT NULL "
        "AND classification_scoring_run_id IS NULL) OR "
        "(run_type = 'CLASSIFIER' AND actor_user_id IS NULL "
        "AND agent_definition_version_id IS NULL AND workflow_run_record_id IS NULL "
        "AND classification_scoring_run_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute("DELETE FROM review_batch_run WHERE run_type = 'CLASSIFIER'")
    op.drop_constraint("ck_review_batch_run_actor", "review_batch_run", type_="check")
    op.drop_constraint("ck_review_batch_run_type", "review_batch_run", type_="check")
    op.create_check_constraint(
        "ck_review_batch_run_type",
        "review_batch_run",
        "run_type IN ('HUMAN', 'AGENT', 'WORKFLOW')",
    )
    op.create_check_constraint(
        "ck_review_batch_run_actor",
        "review_batch_run",
        "(run_type = 'HUMAN' AND actor_user_id IS NOT NULL "
        "AND agent_definition_version_id IS NULL AND workflow_run_record_id IS NULL) OR "
        "(run_type = 'AGENT' AND actor_user_id IS NULL "
        "AND agent_definition_version_id IS NOT NULL AND workflow_run_record_id IS NULL) OR "
        "(run_type = 'WORKFLOW' AND actor_user_id IS NULL "
        "AND agent_definition_version_id IS NULL AND workflow_run_record_id IS NOT NULL)",
    )
    op.drop_index("uq_review_batch_classifier_derivation", table_name="review_batch_run")
    op.drop_index("uq_review_batch_classifier_idempotency", table_name="review_batch_run")
    op.drop_index("ix_review_batch_run_derivation_key", table_name="review_batch_run")
    op.drop_index(
        "ix_review_batch_run_classification_scoring_run_id",
        table_name="review_batch_run",
    )
    op.drop_constraint(
        "fk_review_batch_run_classification_scoring_run",
        "review_batch_run",
        type_="foreignkey",
    )
    op.drop_column("review_batch_run", "result_summary")
    op.drop_column("review_batch_run", "idempotency_key")
    op.drop_column("review_batch_run", "derivation_key")
    op.drop_column("review_batch_run", "classification_scoring_run_id")
