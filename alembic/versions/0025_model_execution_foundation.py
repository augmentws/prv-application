"""Add structured model execution and evidence accounting records.

Revision ID: 0025_model_execution_foundation
Revises: 0024_managed_skills
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_model_execution_foundation"
down_revision: str | None = "0024_managed_skills"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _usage_columns() -> list[sa.Column]:
    return [
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("tool_call_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cached_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
    ]


def upgrade() -> None:
    for name, type_ in (
        ("request_count", sa.Integer()),
        ("tool_call_count", sa.Integer()),
        ("input_tokens", sa.BigInteger()),
        ("cached_input_tokens", sa.BigInteger()),
        ("cache_write_tokens", sa.BigInteger()),
        ("output_tokens", sa.BigInteger()),
    ):
        op.add_column("workflow_run", sa.Column(name, type_, nullable=False, server_default="0"))
        op.alter_column("workflow_run", name, server_default=None)

    op.create_table(
        "workflow_step_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=False),
        sa.Column("role_key", sa.String(length=100), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("fan_out_group", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("completed_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        *_usage_columns(),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("ordinal > 0", name="ck_workflow_step_run_ordinal_positive"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_workflow_step_run_status",
        ),
        sa.CheckConstraint(
            "request_count >= 0 AND tool_call_count >= 0 AND input_tokens >= 0 "
            "AND cached_input_tokens >= 0 AND cache_write_tokens >= 0 AND output_tokens >= 0",
            name="ck_workflow_step_run_usage",
        ),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_run_id", "ordinal", name="uq_workflow_step_run_ordinal"),
    )
    op.create_index("ix_workflow_step_run_workflow_run_id", "workflow_step_run", ["workflow_run_id"])
    op.create_index("ix_workflow_step_run_status", "workflow_step_run", ["status"])
    op.create_index(
        "ix_workflow_step_run_workflow_status",
        "workflow_step_run",
        ["workflow_run_id", "status"],
    )

    op.create_table(
        "skill_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_run_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_step_run_id", sa.Uuid(), nullable=False),
        sa.Column("skill_definition_version_id", sa.Uuid(), nullable=False),
        sa.Column("parent_skill_run_id", sa.Uuid(), nullable=True),
        sa.Column("root_skill_run_id", sa.Uuid(), nullable=True),
        sa.Column("scope_type", sa.String(length=80), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=True),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("configuration_hash", sa.String(length=64), nullable=False),
        sa.Column("cache_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("output_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        *_usage_columns(),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'SKIPPED', 'FAILED', 'CANCELED')",
            name="ck_skill_run_status",
        ),
        sa.CheckConstraint(
            "request_count >= 0 AND tool_call_count >= 0 AND input_tokens >= 0 "
            "AND cached_input_tokens >= 0 AND cache_write_tokens >= 0 AND output_tokens >= 0",
            name="ck_skill_run_usage",
        ),
        sa.ForeignKeyConstraint(["workflow_run_id"], ["workflow_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workflow_step_run_id"], ["workflow_step_run.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["skill_definition_version_id"],
            ["skill_definition_version.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["parent_skill_run_id"], ["skill_run.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["root_skill_run_id"], ["skill_run.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "workflow_run_id",
        "workflow_step_run_id",
        "skill_definition_version_id",
        "parent_skill_run_id",
        "root_skill_run_id",
        "cache_fingerprint",
        "status",
    ):
        op.create_index(f"ix_skill_run_{column}", "skill_run", [column])
    op.create_index("ix_skill_run_step_status", "skill_run", ["workflow_step_run_id", "status"])
    op.create_index("ix_skill_run_scope", "skill_run", ["scope_type", "scope_id"])

    op.create_table(
        "model_invocation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("skill_run_id", sa.Uuid(), nullable=True),
        sa.Column("provider_request_id", sa.String(length=500), nullable=True),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=500), nullable=False),
        sa.Column("model_configuration_hash", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("request_sequence", sa.Integer(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cached_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(agent_run_id IS NOT NULL AND skill_run_id IS NULL) OR "
            "(agent_run_id IS NULL AND skill_run_id IS NOT NULL)",
            name="ck_model_invocation_owner",
        ),
        sa.CheckConstraint("attempt > 0 AND request_sequence > 0", name="ck_model_invocation_sequence"),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_model_invocation_status",
        ),
        sa.CheckConstraint(
            "request_count >= 0 AND input_tokens >= 0 AND cached_input_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND output_tokens >= 0 AND latency_ms >= 0",
            name="ck_model_invocation_usage",
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["skill_run_id"], ["skill_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_run_id", "attempt", "request_sequence", name="uq_model_invocation_agent_request"
        ),
        sa.UniqueConstraint(
            "skill_run_id", "attempt", "request_sequence", name="uq_model_invocation_skill_request"
        ),
    )
    for column in ("agent_run_id", "skill_run_id", "provider", "status"):
        op.create_index(f"ix_model_invocation_{column}", "model_invocation", [column])
    op.create_index(
        "ix_model_invocation_agent_created", "model_invocation", ["agent_run_id", "created_at"]
    )
    op.create_index(
        "ix_model_invocation_skill_created", "model_invocation", ["skill_run_id", "created_at"]
    )

    op.add_column(
        "agent_run", sa.Column("cached_input_tokens", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.add_column(
        "agent_run", sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False, server_default="0")
    )
    op.alter_column("agent_run", "cached_input_tokens", server_default=None)
    op.alter_column("agent_run", "cache_write_tokens", server_default=None)
    op.alter_column("agent_run", "input_tokens", type_=sa.BigInteger(), existing_type=sa.Integer())
    op.alter_column("agent_run", "output_tokens", type_=sa.BigInteger(), existing_type=sa.Integer())

    op.drop_constraint("ck_external_provider_usage_counts", "external_provider_usage", type_="check")
    op.add_column(
        "external_provider_usage",
        sa.Column("cached_input_tokens", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "external_provider_usage",
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "external_provider_usage", sa.Column("model_invocation_id", sa.Uuid(), nullable=True)
    )
    op.alter_column("external_provider_usage", "cached_input_tokens", server_default=None)
    op.alter_column("external_provider_usage", "cache_write_tokens", server_default=None)
    op.create_foreign_key(
        "fk_external_provider_usage_model_invocation_id",
        "external_provider_usage",
        "model_invocation",
        ["model_invocation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_external_provider_usage_model_invocation_id",
        "external_provider_usage",
        ["model_invocation_id"],
        unique=True,
    )
    op.create_check_constraint(
        "ck_external_provider_usage_counts",
        "external_provider_usage",
        "request_count >= 0 AND input_tokens >= 0 AND cached_input_tokens >= 0 "
        "AND cache_write_tokens >= 0 AND output_tokens >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_external_provider_usage_counts", "external_provider_usage", type_="check")
    op.drop_index("ix_external_provider_usage_model_invocation_id", table_name="external_provider_usage")
    op.drop_constraint(
        "fk_external_provider_usage_model_invocation_id", "external_provider_usage", type_="foreignkey"
    )
    op.drop_column("external_provider_usage", "model_invocation_id")
    op.drop_column("external_provider_usage", "cache_write_tokens")
    op.drop_column("external_provider_usage", "cached_input_tokens")
    op.create_check_constraint(
        "ck_external_provider_usage_counts",
        "external_provider_usage",
        "request_count >= 0 AND input_tokens >= 0 AND output_tokens >= 0",
    )

    op.alter_column("agent_run", "output_tokens", type_=sa.Integer(), existing_type=sa.BigInteger())
    op.alter_column("agent_run", "input_tokens", type_=sa.Integer(), existing_type=sa.BigInteger())
    op.drop_column("agent_run", "cache_write_tokens")
    op.drop_column("agent_run", "cached_input_tokens")

    op.drop_index("ix_model_invocation_skill_created", table_name="model_invocation")
    op.drop_index("ix_model_invocation_agent_created", table_name="model_invocation")
    for column in ("status", "provider", "skill_run_id", "agent_run_id"):
        op.drop_index(f"ix_model_invocation_{column}", table_name="model_invocation")
    op.drop_table("model_invocation")

    op.drop_index("ix_skill_run_scope", table_name="skill_run")
    op.drop_index("ix_skill_run_step_status", table_name="skill_run")
    for column in (
        "status",
        "cache_fingerprint",
        "root_skill_run_id",
        "parent_skill_run_id",
        "skill_definition_version_id",
        "workflow_step_run_id",
        "workflow_run_id",
    ):
        op.drop_index(f"ix_skill_run_{column}", table_name="skill_run")
    op.drop_table("skill_run")

    op.drop_index("ix_workflow_step_run_workflow_status", table_name="workflow_step_run")
    op.drop_index("ix_workflow_step_run_status", table_name="workflow_step_run")
    op.drop_index("ix_workflow_step_run_workflow_run_id", table_name="workflow_step_run")
    op.drop_table("workflow_step_run")

    for name in (
        "output_tokens",
        "cache_write_tokens",
        "cached_input_tokens",
        "input_tokens",
        "tool_call_count",
        "request_count",
    ):
        op.drop_column("workflow_run", name)
