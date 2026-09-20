"""Add assessment synthesis and batch-scoped topics.

Revision ID: 0027_assessment_topics
Revises: 0026_definition_assessments
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_assessment_topics"
down_revision: str | None = "0026_definition_assessments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_matter_definition_assessment_counts",
        "matter_definition_assessment_run",
        type_="check",
    )
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("partial_coverage_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("invalid_result_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("coverage_snapshot", sa.JSON(), nullable=True),
    )
    op.add_column(
        "matter_definition_assessment_run",
        sa.Column("synthesis_result", sa.JSON(), nullable=True),
    )
    op.create_check_constraint(
        "ck_matter_definition_assessment_counts",
        "matter_definition_assessment_run",
        "candidate_count >= 0 AND selected_count >= 0 AND summarized_count >= 0 "
        "AND skipped_count >= 0 AND failed_count >= 0 AND partial_coverage_count >= 0 "
        "AND invalid_result_count >= 0",
    )

    op.create_table(
        "batch_topic_taxonomy",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_batch_id", sa.Uuid(), nullable=False),
        sa.Column("source_assessment_run_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_batch_topic_taxonomy_version_positive"),
        sa.CheckConstraint("status IN ('ACTIVE', 'RETIRED')", name="ck_batch_topic_taxonomy_status"),
        sa.ForeignKeyConstraint(["review_batch_id"], ["review_batch.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_assessment_run_id"],
            ["matter_definition_assessment_run.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("review_batch_id", "version", name="uq_batch_topic_taxonomy_version"),
        sa.UniqueConstraint("source_assessment_run_id"),
    )
    op.create_index(
        "ix_batch_topic_taxonomy_review_batch_id", "batch_topic_taxonomy", ["review_batch_id"]
    )
    op.create_index(
        "uq_batch_topic_taxonomy_active",
        "batch_topic_taxonomy",
        ["review_batch_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "batch_topic",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("taxonomy_id", sa.Uuid(), nullable=False),
        sa.Column("topic_key", sa.String(length=100), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("ordinal > 0", name="ck_batch_topic_ordinal_positive"),
        sa.ForeignKeyConstraint(["taxonomy_id"], ["batch_topic_taxonomy.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("taxonomy_id", "topic_key", name="uq_batch_topic_key"),
        sa.UniqueConstraint("taxonomy_id", "ordinal", name="uq_batch_topic_ordinal"),
    )
    op.create_index("ix_batch_topic_taxonomy_id", "batch_topic", ["taxonomy_id"])

    op.create_table(
        "batch_topic_assignment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_batch_id", sa.Uuid(), nullable=False),
        sa.Column("taxonomy_id", sa.Uuid(), nullable=False),
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.Column("matter_document_id", sa.Uuid(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_batch_topic_assignment_confidence"
        ),
        sa.ForeignKeyConstraint(["review_batch_id"], ["review_batch.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["taxonomy_id"], ["batch_topic_taxonomy.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["topic_id"], ["batch_topic.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matter_document_id"], ["matter_document.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "taxonomy_id", "matter_document_id", "topic_id", name="uq_batch_topic_assignment"
        ),
    )
    for name, column in (
        ("ix_batch_topic_assignment_review_batch_id", "review_batch_id"),
        ("ix_batch_topic_assignment_taxonomy_id", "taxonomy_id"),
        ("ix_batch_topic_assignment_topic_id", "topic_id"),
        ("ix_batch_topic_assignment_matter_document_id", "matter_document_id"),
    ):
        op.create_index(name, "batch_topic_assignment", [column])
    op.create_index(
        "ix_batch_topic_assignment_document",
        "batch_topic_assignment",
        ["matter_document_id", "taxonomy_id"],
    )


def downgrade() -> None:
    op.drop_table("batch_topic_assignment")
    op.drop_table("batch_topic")
    op.drop_table("batch_topic_taxonomy")
    op.drop_constraint(
        "ck_matter_definition_assessment_counts",
        "matter_definition_assessment_run",
        type_="check",
    )
    for column in (
        "synthesis_result",
        "coverage_snapshot",
        "invalid_result_count",
        "partial_coverage_count",
    ):
        op.drop_column("matter_definition_assessment_run", column)
    op.create_check_constraint(
        "ck_matter_definition_assessment_counts",
        "matter_definition_assessment_run",
        "candidate_count >= 0 AND selected_count >= 0 AND summarized_count >= 0 "
        "AND skipped_count >= 0 AND failed_count >= 0",
    )
