"""Add classification feature and split snapshot metadata.

Revision ID: 0055_classification_features
Revises: 0054_classification_datasets
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0055_classification_features"
down_revision: str | None = "0054_classification_datasets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TABLE = "classification_dataset_snapshot"


def _drop_extended_constraints() -> None:
    op.drop_constraint("ck_classification_dataset_snapshot_counts", TABLE, type_="check")
    op.drop_constraint("ck_classification_dataset_snapshot_feature_artifact", TABLE, type_="check")
    op.drop_constraint("ck_classification_dataset_snapshot_split_artifact", TABLE, type_="check")


def upgrade() -> None:
    _drop_extended_constraints()
    op.add_column(TABLE, sa.Column("feature_specification", json_document, nullable=True))
    op.add_column(TABLE, sa.Column("split_specification", json_document, nullable=True))
    op.add_column(TABLE, sa.Column("split_specification_hash", sa.String(length=64), nullable=True))
    op.add_column(TABLE, sa.Column("training_aggregate_hash", sa.String(length=64), nullable=True))
    op.add_column(
        TABLE,
        sa.Column("feature_ready_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        TABLE,
        sa.Column("feature_missing_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        TABLE,
        sa.Column("feature_failed_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        TABLE,
        sa.Column("training_eligible_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_classification_dataset_snapshot_counts",
        TABLE,
        "population_count >= 0 AND labeled_count >= 0 AND unlabeled_count >= 0 "
        "AND excluded_count >= 0 AND conflicting_count >= 0 "
        "AND family_grouped_count >= 0 AND exact_duplicate_grouped_count >= 0 "
        "AND missing_grouping_count >= 0 AND feature_ready_count >= 0 "
        "AND feature_missing_count >= 0 AND feature_failed_count >= 0 "
        "AND training_eligible_count >= 0",
    )
    op.create_check_constraint(
        "ck_classification_dataset_snapshot_feature_artifact",
        TABLE,
        "(feature_artifact_id IS NULL AND feature_content_hash IS NULL "
        "AND feature_specification IS NULL AND feature_specification_hash IS NULL) OR "
        "(feature_artifact_id IS NOT NULL AND feature_content_hash IS NOT NULL "
        "AND feature_specification IS NOT NULL AND feature_specification_hash IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_classification_dataset_snapshot_split_artifact",
        TABLE,
        "(split_artifact_id IS NULL AND split_content_hash IS NULL "
        "AND split_specification IS NULL AND split_specification_hash IS NULL "
        "AND training_aggregate_hash IS NULL) OR "
        "(split_artifact_id IS NOT NULL AND split_content_hash IS NOT NULL "
        "AND split_specification IS NOT NULL AND split_specification_hash IS NOT NULL "
        "AND training_aggregate_hash IS NOT NULL)",
    )


def downgrade() -> None:
    _drop_extended_constraints()
    op.drop_column(TABLE, "training_eligible_count")
    op.drop_column(TABLE, "feature_failed_count")
    op.drop_column(TABLE, "feature_missing_count")
    op.drop_column(TABLE, "feature_ready_count")
    op.drop_column(TABLE, "training_aggregate_hash")
    op.drop_column(TABLE, "split_specification_hash")
    op.drop_column(TABLE, "split_specification")
    op.drop_column(TABLE, "feature_specification")
    op.create_check_constraint(
        "ck_classification_dataset_snapshot_counts",
        TABLE,
        "population_count >= 0 AND labeled_count >= 0 AND unlabeled_count >= 0 "
        "AND excluded_count >= 0 AND conflicting_count >= 0 "
        "AND family_grouped_count >= 0 AND exact_duplicate_grouped_count >= 0 "
        "AND missing_grouping_count >= 0",
    )
    op.create_check_constraint(
        "ck_classification_dataset_snapshot_feature_artifact",
        TABLE,
        "(feature_artifact_id IS NULL AND feature_content_hash IS NULL "
        "AND feature_specification_hash IS NULL) OR "
        "(feature_artifact_id IS NOT NULL AND feature_content_hash IS NOT NULL "
        "AND feature_specification_hash IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_classification_dataset_snapshot_split_artifact",
        TABLE,
        "(split_artifact_id IS NULL AND split_content_hash IS NULL) OR "
        "(split_artifact_id IS NOT NULL AND split_content_hash IS NOT NULL)",
    )
