"""Add workflow-owned review-batch runs.

Revision ID: 0023_workflow_run_foundation
Revises: 0022_external_provider_usage
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_workflow_run_foundation"
down_revision: str | None = "0022_external_provider_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("matter_id", sa.Uuid(), nullable=True),
        sa.Column("workflow_key", sa.String(length=100), nullable=False),
        sa.Column("code_version", sa.String(length=100), nullable=False),
        sa.Column("dbos_workflow_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("binding_snapshot", sa.JSON(), nullable=False),
        sa.Column("configuration_snapshot", sa.JSON(), nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("initiated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_workflow_run_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["client_id"], ["client.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["initiated_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dbos_workflow_id"),
    )
    op.create_index("ix_workflow_run_tenant_id", "workflow_run", ["tenant_id"])
    op.create_index("ix_workflow_run_client_id", "workflow_run", ["client_id"])
    op.create_index("ix_workflow_run_matter_id", "workflow_run", ["matter_id"])
    op.create_index("ix_workflow_run_workflow_key", "workflow_run", ["workflow_key"])
    op.create_index("ix_workflow_run_status", "workflow_run", ["status"])
    op.create_index("ix_workflow_run_initiated_by_user_id", "workflow_run", ["initiated_by_user_id"])
    op.create_index("ix_workflow_run_matter_created", "workflow_run", ["matter_id", "created_at"])

    op.alter_column("review_batch_run", "workflow_id", new_column_name="dbos_workflow_id")
    op.add_column("review_batch_run", sa.Column("workflow_run_record_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_review_batch_run_workflow_run_record_id",
        "review_batch_run",
        ["workflow_run_record_id"],
    )
    op.create_foreign_key(
        "fk_review_batch_run_workflow_run_record_id",
        "review_batch_run",
        "workflow_run",
        ["workflow_run_record_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.alter_column("review_batch_run", "status", type_=sa.String(length=30), existing_type=sa.String(length=20))
    op.drop_constraint("ck_review_batch_run_type", "review_batch_run", type_="check")
    op.drop_constraint("ck_review_batch_run_purpose", "review_batch_run", type_="check")
    op.drop_constraint("ck_review_batch_run_status", "review_batch_run", type_="check")
    op.drop_constraint("ck_review_batch_run_actor", "review_batch_run", type_="check")
    op.create_check_constraint(
        "ck_review_batch_run_type",
        "review_batch_run",
        "run_type IN ('HUMAN', 'AGENT', 'WORKFLOW')",
    )
    op.create_check_constraint(
        "ck_review_batch_run_purpose",
        "review_batch_run",
        "purpose IN ('REVIEW', 'REFERENCE', 'CANDIDATE', 'ASSESSMENT')",
    )
    op.create_check_constraint(
        "ck_review_batch_run_status",
        "review_batch_run",
        "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
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

    op.drop_constraint("ck_review_batch_run_document_status", "review_batch_run_document", type_="check")
    op.create_check_constraint(
        "ck_review_batch_run_document_status",
        "review_batch_run_document",
        "status IN ('QUEUED', 'IN_PROGRESS', 'COMPLETED', 'SKIPPED', 'FAILED')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_batch_run_document_status", "review_batch_run_document", type_="check")
    op.create_check_constraint(
        "ck_review_batch_run_document_status",
        "review_batch_run_document",
        "status IN ('IN_PROGRESS', 'COMPLETED', 'SKIPPED')",
    )

    op.drop_constraint("ck_review_batch_run_actor", "review_batch_run", type_="check")
    op.drop_constraint("ck_review_batch_run_status", "review_batch_run", type_="check")
    op.drop_constraint("ck_review_batch_run_purpose", "review_batch_run", type_="check")
    op.drop_constraint("ck_review_batch_run_type", "review_batch_run", type_="check")
    op.create_check_constraint(
        "ck_review_batch_run_type",
        "review_batch_run",
        "run_type IN ('HUMAN', 'AGENT')",
    )
    op.create_check_constraint(
        "ck_review_batch_run_purpose",
        "review_batch_run",
        "purpose IN ('REVIEW', 'REFERENCE', 'CANDIDATE')",
    )
    op.create_check_constraint(
        "ck_review_batch_run_status",
        "review_batch_run",
        "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
    )
    op.create_check_constraint(
        "ck_review_batch_run_actor",
        "review_batch_run",
        "(run_type = 'HUMAN' AND actor_user_id IS NOT NULL AND agent_definition_version_id IS NULL) OR "
        "(run_type = 'AGENT' AND actor_user_id IS NULL AND agent_definition_version_id IS NOT NULL)",
    )
    op.alter_column("review_batch_run", "status", type_=sa.String(length=20), existing_type=sa.String(length=30))
    op.drop_constraint(
        "fk_review_batch_run_workflow_run_record_id",
        "review_batch_run",
        type_="foreignkey",
    )
    op.drop_index("ix_review_batch_run_workflow_run_record_id", table_name="review_batch_run")
    op.drop_column("review_batch_run", "workflow_run_record_id")
    op.alter_column("review_batch_run", "dbos_workflow_id", new_column_name="workflow_id")

    op.drop_index("ix_workflow_run_matter_created", table_name="workflow_run")
    op.drop_index("ix_workflow_run_initiated_by_user_id", table_name="workflow_run")
    op.drop_index("ix_workflow_run_status", table_name="workflow_run")
    op.drop_index("ix_workflow_run_workflow_key", table_name="workflow_run")
    op.drop_index("ix_workflow_run_matter_id", table_name="workflow_run")
    op.drop_index("ix_workflow_run_client_id", table_name="workflow_run")
    op.drop_index("ix_workflow_run_tenant_id", table_name="workflow_run")
    op.drop_table("workflow_run")
