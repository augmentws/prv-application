"""Add versioned agent definitions and Matter Definitions.

Revision ID: 0010_agents_matter_definitions
Revises: 0009_document_metadata_values
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_agents_matter_definitions"
down_revision: str | None = "0009_document_metadata_values"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_definition",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_tenant_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("published_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope IN ('SYSTEM', 'TENANT')", name="ck_agent_definition_scope"),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_agent_definition_status"
        ),
        sa.CheckConstraint("current_version > 0", name="ck_agent_definition_current_version"),
        sa.CheckConstraint(
            "published_version IS NULL OR (published_version > 0 AND published_version <= current_version)",
            name="ck_agent_definition_published_version",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_tenant_id", "key", name="uq_agent_definition_tenant_key"),
    )
    op.create_index("ix_agent_definition_owner_tenant_id", "agent_definition", ["owner_tenant_id"])
    op.create_index("ix_agent_definition_created_by_user_id", "agent_definition", ["created_by_user_id"])

    op.create_table(
        "agent_definition_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_definition_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("model_key", sa.String(length=200), nullable=False),
        sa.Column("model_policy", sa.JSON(), nullable=False),
        sa.Column("output_schema", sa.JSON(), nullable=False),
        sa.Column("limits", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version > 0", name="ck_agent_definition_version_positive"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'RETIRED')", name="ck_agent_version_status"
        ),
        sa.ForeignKeyConstraint(["agent_definition_id"], ["agent_definition.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_definition_id", "version", name="uq_agent_definition_version"),
    )
    op.create_index(
        "ix_agent_definition_version_agent_definition_id",
        "agent_definition_version",
        ["agent_definition_id"],
    )
    op.create_index(
        "ix_agent_definition_version_created_by_user_id",
        "agent_definition_version",
        ["created_by_user_id"],
    )

    op.create_table(
        "agent_version_tool",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_definition_version_id", sa.Uuid(), nullable=False),
        sa.Column("tool_key", sa.String(length=150), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_definition_version_id"], ["agent_definition_version.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_definition_version_id", "tool_key", name="uq_agent_version_tool"),
    )
    op.create_index(
        "ix_agent_version_tool_agent_definition_version_id",
        "agent_version_tool",
        ["agent_definition_version_id"],
    )

    op.create_table(
        "matter_definition",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("published_revision", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("current_revision > 0", name="ck_matter_definition_current_revision"),
        sa.CheckConstraint(
            "published_revision IS NULL OR (published_revision > 0 AND published_revision <= current_revision)",
            name="ck_matter_definition_published_revision",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("matter_id", name="uq_matter_definition_matter"),
    )
    op.create_index("ix_matter_definition_matter_id", "matter_definition", ["matter_id"])
    op.create_index(
        "ix_matter_definition_created_by_user_id", "matter_definition", ["created_by_user_id"]
    )

    op.create_table(
        "matter_definition_revision",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("matter_definition_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content_markdown", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(length=20), nullable=False),
        sa.Column("source_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("source_filename", sa.String(length=500), nullable=True),
        sa.Column("based_on_revision", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_matter_definition_revision_positive"),
        sa.CheckConstraint(
            "source_kind IN ('PASTE', 'MARKDOWN', 'TEXT', 'DOCX', 'AGENT_EDIT', 'USER_EDIT')",
            name="ck_matter_definition_revision_source_kind",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["matter_definition_id"], ["matter_definition.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("matter_definition_id", "revision", name="uq_matter_definition_revision"),
    )
    op.create_index(
        "ix_matter_definition_revision_matter_definition_id",
        "matter_definition_revision",
        ["matter_definition_id"],
    )
    op.create_index(
        "ix_matter_definition_revision_source_artifact_id",
        "matter_definition_revision",
        ["source_artifact_id"],
    )
    op.create_index(
        "ix_matter_definition_revision_created_by_user_id",
        "matter_definition_revision",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_matter_definition_revision_agent_run_id",
        "matter_definition_revision",
        ["agent_run_id"],
    )


def downgrade() -> None:
    op.drop_table("matter_definition_revision")
    op.drop_table("matter_definition")
    op.drop_table("agent_version_tool")
    op.drop_table("agent_definition_version")
    op.drop_table("agent_definition")
