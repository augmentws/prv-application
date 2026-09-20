"""Add managed skills and workflow-role bindings.

Revision ID: 0024_managed_skills
Revises: 0023_workflow_run_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_managed_skills"
down_revision: str | None = "0023_workflow_run_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_definition",
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
        sa.CheckConstraint("scope IN ('SYSTEM', 'TENANT')", name="ck_skill_definition_scope"),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')",
            name="ck_skill_definition_status",
        ),
        sa.CheckConstraint("current_version > 0", name="ck_skill_definition_current_version"),
        sa.CheckConstraint(
            "published_version IS NULL OR (published_version > 0 AND published_version <= current_version)",
            name="ck_skill_definition_published_version",
        ),
        sa.ForeignKeyConstraint(["owner_tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_tenant_id", "key", name="uq_skill_definition_tenant_key"),
    )
    op.create_index("ix_skill_definition_owner_tenant_id", "skill_definition", ["owner_tenant_id"])
    op.create_index("ix_skill_definition_created_by_user_id", "skill_definition", ["created_by_user_id"])

    op.create_table(
        "skill_definition_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("skill_definition_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("input_schema_key", sa.String(length=150), nullable=False),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("output_schema_key", sa.String(length=150), nullable=False),
        sa.Column("output_schema", sa.JSON(), nullable=False),
        sa.Column("model_key", sa.String(length=200), nullable=False),
        sa.Column("model_policy", sa.JSON(), nullable=False),
        sa.Column("limits", sa.JSON(), nullable=False),
        sa.Column("required_capabilities", sa.JSON(), nullable=False),
        sa.Column("required_tools", sa.JSON(), nullable=False),
        sa.Column("cache_policy", sa.JSON(), nullable=False),
        sa.Column("evaluation_fixtures", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version > 0", name="ck_skill_definition_version_positive"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'RETIRED')",
            name="ck_skill_version_status",
        ),
        sa.ForeignKeyConstraint(["skill_definition_id"], ["skill_definition.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_definition_id", "version", name="uq_skill_definition_version"),
    )
    op.create_index(
        "ix_skill_definition_version_skill_definition_id",
        "skill_definition_version",
        ["skill_definition_id"],
    )
    op.create_index(
        "ix_skill_definition_version_created_by_user_id",
        "skill_definition_version",
        ["created_by_user_id"],
    )

    op.create_table(
        "workflow_skill_binding",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_key", sa.String(length=100), nullable=False),
        sa.Column("role_key", sa.String(length=100), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("owner_tenant_id", sa.Uuid(), nullable=False),
        sa.Column("skill_definition_id", sa.Uuid(), nullable=False),
        sa.Column("skill_definition_version_id", sa.Uuid(), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope IN ('SYSTEM', 'TENANT')", name="ck_workflow_skill_binding_scope"),
        sa.CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name="ck_workflow_skill_binding_status"),
        sa.ForeignKeyConstraint(["owner_tenant_id"], ["tenant.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["skill_definition_id"], ["skill_definition.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["skill_definition_version_id"],
            ["skill_definition_version.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_key",
            "role_key",
            "scope",
            "owner_tenant_id",
            name="uq_workflow_skill_binding_role_scope",
        ),
    )
    op.create_index("ix_workflow_skill_binding_workflow_key", "workflow_skill_binding", ["workflow_key"])
    op.create_index("ix_workflow_skill_binding_owner_tenant_id", "workflow_skill_binding", ["owner_tenant_id"])
    op.create_index(
        "ix_workflow_skill_binding_skill_definition_id",
        "workflow_skill_binding",
        ["skill_definition_id"],
    )
    op.create_index(
        "ix_workflow_skill_binding_skill_definition_version_id",
        "workflow_skill_binding",
        ["skill_definition_version_id"],
    )
    op.create_index(
        "ix_workflow_skill_binding_created_by_user_id",
        "workflow_skill_binding",
        ["created_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_skill_binding_created_by_user_id", table_name="workflow_skill_binding")
    op.drop_index("ix_workflow_skill_binding_skill_definition_version_id", table_name="workflow_skill_binding")
    op.drop_index("ix_workflow_skill_binding_skill_definition_id", table_name="workflow_skill_binding")
    op.drop_index("ix_workflow_skill_binding_owner_tenant_id", table_name="workflow_skill_binding")
    op.drop_index("ix_workflow_skill_binding_workflow_key", table_name="workflow_skill_binding")
    op.drop_table("workflow_skill_binding")

    op.drop_index("ix_skill_definition_version_created_by_user_id", table_name="skill_definition_version")
    op.drop_index("ix_skill_definition_version_skill_definition_id", table_name="skill_definition_version")
    op.drop_table("skill_definition_version")

    op.drop_index("ix_skill_definition_created_by_user_id", table_name="skill_definition")
    op.drop_index("ix_skill_definition_owner_tenant_id", table_name="skill_definition")
    op.drop_table("skill_definition")
