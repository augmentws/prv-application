"""Add metadata groups, preferences, and matter templates.

Revision ID: 0004_matter_configuration
Revises: 0003_metadata_profile
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_matter_configuration"
down_revision: str | None = "0003_metadata_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "metadata_group",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("default_table_visible", sa.Boolean(), nullable=False),
        sa.Column("default_document_visible", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("template_key", sa.String(length=100), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=True),
        *timestamps(),
        sa.CheckConstraint("scope IN ('SYSTEM', 'MATTER', 'PERSONAL')", name="ck_metadata_group_scope"),
        sa.CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_metadata_group_status"),
        sa.CheckConstraint(
            "(scope = 'PERSONAL' AND owner_user_id IS NOT NULL) OR "
            "(scope IN ('SYSTEM', 'MATTER') AND owner_user_id IS NULL)",
            name="ck_metadata_group_owner",
        ),
        sa.CheckConstraint(
            "(template_key IS NULL AND template_version IS NULL) OR "
            "(template_key IS NOT NULL AND template_version IS NOT NULL AND template_version > 0)",
            name="ck_metadata_group_template_identity",
        ),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_metadata_group_matter_id", "metadata_group", ["matter_id"])
    op.create_index("ix_metadata_group_owner_user_id", "metadata_group", ["owner_user_id"])
    op.create_index("ix_metadata_group_created_by_user_id", "metadata_group", ["created_by_user_id"])
    op.create_index(
        "uq_metadata_group_shared_key",
        "metadata_group",
        ["matter_id", "key"],
        unique=True,
        postgresql_where=sa.text("scope IN ('SYSTEM', 'MATTER')"),
        sqlite_where=sa.text("scope IN ('SYSTEM', 'MATTER')"),
    )
    op.create_index(
        "uq_metadata_group_personal_key",
        "metadata_group",
        ["matter_id", "owner_user_id", "key"],
        unique=True,
        postgresql_where=sa.text("scope = 'PERSONAL'"),
        sqlite_where=sa.text("scope = 'PERSONAL'"),
    )

    op.create_table(
        "metadata_group_field",
        sa.Column("metadata_group_id", sa.Uuid(), nullable=False),
        sa.Column("metadata_definition_id", sa.Uuid(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["metadata_group_id"], ["metadata_group.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["metadata_definition_id"], ["metadata_definition.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("metadata_group_id", "metadata_definition_id"),
        sa.UniqueConstraint("metadata_group_id", "sort_order", name="uq_metadata_group_field_order"),
    )

    op.create_table(
        "metadata_group_preference",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("metadata_group_id", sa.Uuid(), nullable=False),
        sa.Column("surface", sa.String(length=20), nullable=False),
        sa.Column("visible", sa.Boolean(), nullable=False),
        *timestamps(),
        sa.CheckConstraint("surface IN ('TABLE', 'DOCUMENT')", name="ck_metadata_group_preference_surface"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["metadata_group_id"], ["metadata_group.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "metadata_group_id", "surface", name="uq_metadata_group_preference"),
    )
    op.create_index("ix_metadata_group_preference_user_id", "metadata_group_preference", ["user_id"])
    op.create_index("ix_metadata_group_preference_matter_id", "metadata_group_preference", ["matter_id"])
    op.create_index(
        "ix_metadata_group_preference_metadata_group_id",
        "metadata_group_preference",
        ["metadata_group_id"],
    )

    op.create_table(
        "matter_template",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("normalized_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        *timestamps(),
        sa.CheckConstraint("scope IN ('TENANT', 'CLIENT')", name="ck_matter_template_scope"),
        sa.CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_matter_template_status"),
        sa.CheckConstraint(
            "(scope = 'TENANT' AND client_id IS NULL) OR (scope = 'CLIENT' AND client_id IS NOT NULL)",
            name="ck_matter_template_client_scope",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["client_id"], ["client.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_matter_template_tenant_id", "matter_template", ["tenant_id"])
    op.create_index("ix_matter_template_client_id", "matter_template", ["client_id"])
    op.create_index("ix_matter_template_created_by_user_id", "matter_template", ["created_by_user_id"])
    op.create_index(
        "uq_matter_template_tenant_name",
        "matter_template",
        ["tenant_id", "normalized_name"],
        unique=True,
        postgresql_where=sa.text("scope = 'TENANT'"),
        sqlite_where=sa.text("scope = 'TENANT'"),
    )
    op.create_index(
        "uq_matter_template_client_name",
        "matter_template",
        ["client_id", "normalized_name"],
        unique=True,
        postgresql_where=sa.text("scope = 'CLIENT'"),
        sqlite_where=sa.text("scope = 'CLIENT'"),
    )

    op.create_table(
        "matter_template_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_template_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("base_profile_key", sa.String(length=100), nullable=False),
        sa.Column("base_profile_version", sa.Integer(), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("source_matter_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_matter_template_version_positive"),
        sa.CheckConstraint("base_profile_version > 0", name="ck_matter_template_base_version_positive"),
        sa.ForeignKeyConstraint(["matter_template_id"], ["matter_template.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_matter_id"], ["matter.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("matter_template_id", "version", name="uq_matter_template_version"),
    )
    op.create_index(
        "ix_matter_template_version_matter_template_id",
        "matter_template_version",
        ["matter_template_id"],
    )
    op.create_index("ix_matter_template_version_source_matter_id", "matter_template_version", ["source_matter_id"])
    op.create_index(
        "ix_matter_template_version_created_by_user_id",
        "matter_template_version",
        ["created_by_user_id"],
    )


def downgrade() -> None:
    op.drop_table("matter_template_version")
    op.drop_table("matter_template")
    op.drop_table("metadata_group_preference")
    op.drop_table("metadata_group_field")
    op.drop_table("metadata_group")
