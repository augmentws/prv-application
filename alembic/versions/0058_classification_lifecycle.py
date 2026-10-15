"""Add governed classification model decisions.

Revision ID: 0058_classification_lifecycle
Revises: 0057_classification_retries
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0058_classification_lifecycle"
down_revision: str | None = "0057_classification_retries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "classification_model_decision",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("classification_model_id", sa.Uuid(), nullable=False),
        sa.Column("model_version_id", sa.Uuid(), nullable=False),
        sa.Column("decision_type", sa.String(length=30), nullable=False),
        sa.Column("workflow_id", sa.String(length=500), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("policy_snapshot", json_document, nullable=False),
        sa.Column("metric_snapshot", json_document, nullable=False),
        sa.Column("before_active_model_version_id", sa.Uuid(), nullable=True),
        sa.Column("after_active_model_version_id", sa.Uuid(), nullable=True),
        sa.Column("authorization_context", json_document, nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision_type IN ('APPROVAL', 'REJECTION', 'ACTIVATION', 'ROLLBACK', 'RETIREMENT')",
            name="ck_classification_model_decision_type",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_classification_model_decision_reason",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["classification_model_id"],
            ["classification_model.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id", "classification_model_id"],
            ["classification_model_version.id", "classification_model_version.classification_model_id"],
            name="fk_classification_model_decision_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["before_active_model_version_id", "classification_model_id"],
            ["classification_model_version.id", "classification_model_version.classification_model_id"],
            name="fk_classification_model_decision_before_active",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["after_active_model_version_id", "classification_model_id"],
            ["classification_model_version.id", "classification_model_version.classification_model_id"],
            name="fk_classification_model_decision_after_active",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id", name="uq_classification_model_decision_workflow"),
    )
    for column in (
        "tenant_id",
        "matter_id",
        "classification_model_id",
        "model_version_id",
        "decision_type",
        "before_active_model_version_id",
        "after_active_model_version_id",
        "actor_user_id",
    ):
        op.create_index(
            f"ix_classification_model_decision_{column}",
            "classification_model_decision",
            [column],
        )
    op.create_index(
        "ix_classification_model_decision_model_created",
        "classification_model_decision",
        ["classification_model_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("classification_model_decision")
