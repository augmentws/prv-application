"""Add the field-scoped statistical classification model registry.

Revision ID: 0053_classification_registry
Revises: 0052_scope_pre_guidance_chats
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0053_classification_registry"
down_revision: str | None = "0052_scope_pre_guidance_chats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_document = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "classification_model",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("target_metadata_definition_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("current_configuration_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')",
            name="ck_classification_model_status",
        ),
        sa.CheckConstraint("row_version > 0", name="ck_classification_model_row_version"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["target_metadata_definition_id"],
            ["metadata_definition.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["app_user.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["app_user.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_classification_model_tenant_id", "classification_model", ["tenant_id"])
    op.create_index("ix_classification_model_matter_id", "classification_model", ["matter_id"])
    op.create_index(
        "ix_classification_model_target_metadata_definition_id",
        "classification_model",
        ["target_metadata_definition_id"],
    )
    op.create_index("ix_classification_model_status", "classification_model", ["status"])
    op.create_index(
        "ix_classification_model_current_configuration_id",
        "classification_model",
        ["current_configuration_id"],
    )
    op.create_index(
        "ix_classification_model_created_by_user_id",
        "classification_model",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_classification_model_updated_by_user_id",
        "classification_model",
        ["updated_by_user_id"],
    )
    op.create_index(
        "ix_classification_model_matter_created",
        "classification_model",
        ["matter_id", "created_at"],
    )

    op.create_table(
        "classification_model_configuration",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("classification_model_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("target_field_snapshot", json_document, nullable=False),
        sa.Column("classification_strategy", sa.String(length=30), nullable=False),
        sa.Column("ordered_class_keys", json_document, nullable=False),
        sa.Column("binary_mapping", json_document, nullable=True),
        sa.Column("label_policy", json_document, nullable=False),
        sa.Column("feature_contract", json_document, nullable=False),
        sa.Column("prediction_resolution_policy", json_document, nullable=False),
        sa.Column("trainer_specification", json_document, nullable=False),
        sa.Column("configuration_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "classification_strategy IN ('BINARY', 'MULTICLASS', 'ONE_VS_REST')",
            name="ck_classification_model_configuration_strategy",
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_classification_model_configuration_revision",
        ),
        sa.ForeignKeyConstraint(
            ["classification_model_id"],
            ["classification_model.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["app_user.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "classification_model_id",
            "revision",
            name="uq_classification_model_configuration_revision",
        ),
        sa.UniqueConstraint(
            "classification_model_id",
            "configuration_hash",
            name="uq_classification_model_configuration_hash",
        ),
        sa.UniqueConstraint(
            "id",
            "classification_model_id",
            name="uq_classification_model_configuration_identity",
        ),
    )
    op.create_index(
        "ix_classification_model_configuration_classification_model_id",
        "classification_model_configuration",
        ["classification_model_id"],
    )
    op.create_index(
        "ix_classification_model_configuration_created_by_user_id",
        "classification_model_configuration",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_classification_model_configuration_model_created",
        "classification_model_configuration",
        ["classification_model_id", "created_at"],
    )
    op.create_foreign_key(
        "fk_classification_model_current_configuration",
        "classification_model",
        "classification_model_configuration",
        ["current_configuration_id", "id"],
        ["id", "classification_model_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_classification_model_current_configuration",
        "classification_model",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_classification_model_configuration_model_created",
        table_name="classification_model_configuration",
    )
    op.drop_index(
        "ix_classification_model_configuration_created_by_user_id",
        table_name="classification_model_configuration",
    )
    op.drop_index(
        "ix_classification_model_configuration_classification_model_id",
        table_name="classification_model_configuration",
    )
    op.drop_table("classification_model_configuration")
    op.drop_index("ix_classification_model_matter_created", table_name="classification_model")
    op.drop_index("ix_classification_model_updated_by_user_id", table_name="classification_model")
    op.drop_index("ix_classification_model_created_by_user_id", table_name="classification_model")
    op.drop_index("ix_classification_model_current_configuration_id", table_name="classification_model")
    op.drop_index("ix_classification_model_status", table_name="classification_model")
    op.drop_index(
        "ix_classification_model_target_metadata_definition_id",
        table_name="classification_model",
    )
    op.drop_index("ix_classification_model_matter_id", table_name="classification_model")
    op.drop_index("ix_classification_model_tenant_id", table_name="classification_model")
    op.drop_table("classification_model")
