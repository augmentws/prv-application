"""Add identity-domain recommendations and organization categories.

Revision ID: 0123_identity_domain_recs
Revises: 0122_matter_identity_analysis
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0123_identity_domain_recs"
down_revision: str | None = "0122_matter_identity_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORGANIZATION_CATEGORIES = (
    "LAW_FIRM",
    "CORPORATION",
    "GOVERNMENT",
    "ACCOUNTING_FIRM",
    "CONSULTING_FIRM",
    "INSURER",
    "EMAIL_PROVIDER",
    "OTHER",
    "UNKNOWN",
)


def upgrade() -> None:
    op.add_column("matter_entity", sa.Column("organization_category", sa.String(length=40), nullable=True))
    allowed = ", ".join(f"'{value}'" for value in _ORGANIZATION_CATEGORIES)
    op.create_check_constraint(
        "ck_matter_entity_organization_category",
        "matter_entity",
        f"organization_category IS NULL OR (entity_type = 'ORGANIZATION' AND organization_category IN ({allowed}))",
    )
    op.create_index(
        "ix_matter_entity_matter_organization_category",
        "matter_entity",
        ["matter_id", "organization_category"],
    )

    op.create_table(
        "matter_identity_recommendation_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("identity_analysis_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("current_workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("current_stage", sa.String(length=100), nullable=True),
        sa.Column("domain_count", sa.Integer(), nullable=False),
        sa.Column("processed_domain_count", sa.Integer(), nullable=False),
        sa.Column("recommendation_count", sa.Integer(), nullable=False),
        sa.Column("result_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("result_content_hash", sa.String(length=64), nullable=True),
        sa.Column("result_byte_length", sa.BigInteger(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("failure_retriable", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_matter_identity_recommendation_status",
        ),
        sa.CheckConstraint(
            "domain_count >= 0 AND processed_domain_count >= 0 "
            "AND processed_domain_count <= domain_count AND recommendation_count >= 0",
            name="ck_matter_identity_recommendation_counts",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["current_workflow_run_id"], ["workflow_run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["identity_analysis_id"], ["matter_identity_analysis.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_id"),
    )
    op.create_index(
        "ix_matter_identity_recommendation_matter_created",
        "matter_identity_recommendation_run",
        ["matter_id", "created_at"],
    )
    op.create_index(
        "uq_matter_identity_recommendation_active",
        "matter_identity_recommendation_run",
        ["identity_analysis_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
        sqlite_where=sa.text("status IN ('QUEUED', 'RUNNING')"),
    )
    for column in (
        "tenant_id",
        "matter_id",
        "identity_analysis_id",
        "status",
        "current_workflow_run_id",
        "result_artifact_id",
        "created_by_user_id",
    ):
        op.create_index(
            f"ix_matter_identity_recommendation_run_{column}",
            "matter_identity_recommendation_run",
            [column],
        )

    op.create_table(
        "matter_entity_domain",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("normalized_domain", sa.String(length=253), nullable=False),
        sa.Column("source_recommendation_run_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["entity_id"], ["matter_entity.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_recommendation_run_id"],
            ["matter_identity_recommendation_run.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("matter_id", "normalized_domain", name="uq_matter_entity_domain_matter_domain"),
    )
    op.create_index("ix_matter_entity_domain_entity_id", "matter_entity_domain", ["entity_id"])
    op.create_index("ix_matter_entity_domain_matter_id", "matter_entity_domain", ["matter_id"])

    op.create_table(
        "matter_identity_domain_decision",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recommendation_run_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("organization_name", sa.String(length=300), nullable=False),
        sa.Column("organization_category", sa.String(length=40), nullable=False),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("decision IN ('ACCEPTED', 'REJECTED')", name="ck_identity_domain_decision"),
        sa.CheckConstraint(
            "(decision = 'ACCEPTED' AND entity_id IS NOT NULL) OR "
            "(decision = 'REJECTED' AND entity_id IS NULL)",
            name="ck_identity_domain_decision_entity",
        ),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["entity_id"], ["matter_entity.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["recommendation_run_id"], ["matter_identity_recommendation_run.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recommendation_run_id", "domain", name="uq_identity_domain_decision_run_domain"),
    )
    op.create_index(
        "ix_matter_identity_domain_decision_run",
        "matter_identity_domain_decision",
        ["recommendation_run_id"],
    )


def downgrade() -> None:
    op.drop_table("matter_identity_domain_decision")
    op.drop_table("matter_entity_domain")
    op.drop_table("matter_identity_recommendation_run")
    op.drop_index("ix_matter_entity_matter_organization_category", table_name="matter_entity")
    op.drop_constraint("ck_matter_entity_organization_category", "matter_entity", type_="check")
    op.drop_column("matter_entity", "organization_category")
