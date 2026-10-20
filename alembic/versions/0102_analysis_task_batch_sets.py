"""Add task-scoped partitioned Review Batch sets.

Revision ID: 0102_task_batch_sets
Revises: 0101_comparison_result_batches, 0101_search_chat_conversations
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0102_task_batch_sets"
down_revision: tuple[str, str] = (
    "0101_comparison_result_batches",
    "0101_search_chat_conversations",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("matter_analysis_task_version", sa.Column("scope_policy", sa.JSON(), nullable=True))
    op.execute(
        """
        UPDATE matter_analysis_task_version AS version
        SET scope_policy = CASE
            WHEN task.task_type = 'CLASSIFICATION'
                THEN '{"allowed_modes":["AD_HOC_BATCH","WHOLE_MATTER"],"default_mode":"AD_HOC_BATCH"}'::json
            ELSE '{"allowed_modes":["AD_HOC_BATCH"],"default_mode":"AD_HOC_BATCH"}'::json
        END
        FROM matter_analysis_task AS task
        WHERE task.id = version.matter_analysis_task_id
        """
    )
    op.alter_column("matter_analysis_task_version", "scope_policy", nullable=False)

    op.create_table(
        "analysis_task_batch_set",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("matter_analysis_task_id", sa.Uuid(), nullable=False),
        sa.Column("matter_analysis_task_version_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("population_source_type", sa.String(length=30), nullable=False),
        sa.Column("selection_definition", sa.JSON(), nullable=False),
        sa.Column("source_review_batch_id", sa.Uuid(), nullable=True),
        sa.Column("target_batch_size", sa.Integer(), nullable=False),
        sa.Column("allocation_policy", sa.JSON(), nullable=False),
        sa.Column("random_seed", sa.String(length=100), nullable=False),
        sa.Column("allocation_algorithm_version", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("current_workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("population_count", sa.Integer(), nullable=False),
        sa.Column("batch_count", sa.Integer(), nullable=False),
        sa.Column("population_hash", sa.String(length=64), nullable=True),
        sa.Column("warning_details", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "population_source_type IN ('ALL_MATTER', 'SOURCE_BATCH')",
            name="ck_analysis_task_batch_set_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'BUILDING', 'READY', 'FAILED', 'CANCELED', 'ARCHIVED')",
            name="ck_analysis_task_batch_set_status",
        ),
        sa.CheckConstraint("target_batch_size > 0", name="ck_analysis_task_batch_set_target_size"),
        sa.CheckConstraint(
            "population_count >= 0 AND batch_count >= 0", name="ck_analysis_task_batch_set_counts"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matter_analysis_task_id"], ["matter_analysis_task.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["matter_analysis_task_version_id"], ["matter_analysis_task_version.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["source_review_batch_id"], ["review_batch.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["current_workflow_run_id"], ["workflow_run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", name="uq_analysis_task_batch_set_workflow_id"),
    )
    op.create_index(
        "ix_analysis_task_batch_set_task_created",
        "analysis_task_batch_set",
        ["matter_analysis_task_id", "created_at"],
    )
    for column in (
        "tenant_id",
        "matter_id",
        "matter_analysis_task_id",
        "matter_analysis_task_version_id",
        "source_review_batch_id",
        "current_workflow_run_id",
        "created_by_user_id",
        "status",
    ):
        op.create_index(f"ix_analysis_task_batch_set_{column}", "analysis_task_batch_set", [column])

    op.add_column("review_batch", sa.Column("analysis_task_batch_set_id", sa.Uuid(), nullable=True))
    op.add_column("review_batch", sa.Column("batch_set_ordinal", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_review_batch_analysis_task_batch_set",
        "review_batch",
        "analysis_task_batch_set",
        ["analysis_task_batch_set_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_review_batch_analysis_task_batch_set_id", "review_batch", ["analysis_task_batch_set_id"])
    op.create_unique_constraint(
        "uq_review_batch_id_batch_set", "review_batch", ["id", "analysis_task_batch_set_id"]
    )
    op.create_unique_constraint(
        "uq_review_batch_batch_set_ordinal",
        "review_batch",
        ["analysis_task_batch_set_id", "batch_set_ordinal"],
    )

    op.add_column("review_batch_document", sa.Column("analysis_task_batch_set_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_review_batch_document_analysis_task_batch_set",
        "review_batch_document",
        "analysis_task_batch_set",
        ["analysis_task_batch_set_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_review_batch_document_batch_set",
        "review_batch_document",
        "review_batch",
        ["review_batch_id", "analysis_task_batch_set_id"],
        ["id", "analysis_task_batch_set_id"],
    )
    op.create_index(
        "ix_review_batch_document_analysis_task_batch_set_id",
        "review_batch_document",
        ["analysis_task_batch_set_id"],
    )
    op.create_unique_constraint(
        "uq_review_batch_document_batch_set_document",
        "review_batch_document",
        ["analysis_task_batch_set_id", "matter_document_id"],
    )

    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH', "
        "'RANDOM_SAVED_SEARCH', 'RANDOM_ASSESSMENT', 'DEFINITION_ASSESSMENT', "
        "'CLASSIFICATION_PREDICTIONS', 'COMPARISON_RESULT', 'TASK_BATCH_PARTITION')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_batch_selection_type", "review_batch", type_="check")
    op.create_check_constraint(
        "ck_review_batch_selection_type",
        "review_batch",
        "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH', "
        "'RANDOM_SAVED_SEARCH', 'RANDOM_ASSESSMENT', 'DEFINITION_ASSESSMENT', "
        "'CLASSIFICATION_PREDICTIONS', 'COMPARISON_RESULT')",
    )
    op.drop_constraint("uq_review_batch_document_batch_set_document", "review_batch_document", type_="unique")
    op.drop_index("ix_review_batch_document_analysis_task_batch_set_id", table_name="review_batch_document")
    op.drop_constraint("fk_review_batch_document_batch_set", "review_batch_document", type_="foreignkey")
    op.drop_constraint(
        "fk_review_batch_document_analysis_task_batch_set", "review_batch_document", type_="foreignkey"
    )
    op.drop_column("review_batch_document", "analysis_task_batch_set_id")
    op.drop_constraint("uq_review_batch_batch_set_ordinal", "review_batch", type_="unique")
    op.drop_constraint("uq_review_batch_id_batch_set", "review_batch", type_="unique")
    op.drop_index("ix_review_batch_analysis_task_batch_set_id", table_name="review_batch")
    op.drop_constraint("fk_review_batch_analysis_task_batch_set", "review_batch", type_="foreignkey")
    op.drop_column("review_batch", "batch_set_ordinal")
    op.drop_column("review_batch", "analysis_task_batch_set_id")
    op.drop_table("analysis_task_batch_set")
    op.drop_column("matter_analysis_task_version", "scope_policy")
