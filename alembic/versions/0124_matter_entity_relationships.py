"""Add generic matter entity relationships.

Revision ID: 0124_entity_relationships
Revises: 0123_identity_domain_recs
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0124_entity_relationships"
down_revision: str | None = "0123_identity_domain_recs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matter_entity_relationship",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("source_entity_id", sa.Uuid(), nullable=False),
        sa.Column("target_entity_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_workflow_run_id", sa.Uuid(), nullable=True),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_entity_id <> target_entity_id",
            name="ck_matter_entity_relationship_distinct",
        ),
        sa.CheckConstraint(
            "status IN ('PROPOSED', 'CONFIRMED', 'REJECTED')",
            name="ck_matter_entity_relationship_status",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_matter_entity_relationship_confidence",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name="ck_matter_entity_relationship_dates",
        ),
        sa.CheckConstraint(
            "(created_by_user_id IS NULL) <> (created_by_workflow_run_id IS NULL)",
            name="ck_matter_entity_relationship_creator",
        ),
        sa.CheckConstraint(
            "(status = 'PROPOSED' AND decided_by_user_id IS NULL AND decided_at IS NULL) OR "
            "(status IN ('CONFIRMED', 'REJECTED') AND decided_by_user_id IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_matter_entity_relationship_decision",
        ),
        sa.CheckConstraint("lock_version > 0", name="ck_matter_entity_relationship_lock_version"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_workflow_run_id"], ["workflow_run.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_entity_id"], ["matter_entity.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_entity_id"], ["matter_entity.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_matter_entity_relationship_active",
        "matter_entity_relationship",
        ["matter_id", "source_entity_id", "relationship_type", "target_entity_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PROPOSED', 'CONFIRMED')"),
        sqlite_where=sa.text("status IN ('PROPOSED', 'CONFIRMED')"),
    )
    op.create_index(
        "ix_matter_entity_relationship_source_status",
        "matter_entity_relationship",
        ["source_entity_id", "status"],
    )
    op.create_index(
        "ix_matter_entity_relationship_target_status",
        "matter_entity_relationship",
        ["target_entity_id", "status"],
    )
    op.create_index(
        "ix_matter_entity_relationship_matter_type",
        "matter_entity_relationship",
        ["matter_id", "relationship_type"],
    )
    for column in (
        "matter_id",
        "created_by_user_id",
        "created_by_workflow_run_id",
        "decided_by_user_id",
    ):
        op.create_index(
            f"ix_matter_entity_relationship_{column}",
            "matter_entity_relationship",
            [column],
        )


def downgrade() -> None:
    op.drop_table("matter_entity_relationship")
