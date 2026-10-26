"""Add the reusable Review Program foundation.

Revision ID: 0111_review_program_foundation
Revises: 0110_notifications
"""

import json
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import context, op

revision: str = "0111_review_program_foundation"
down_revision: str = "0110_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RBAC_NAMESPACE = uuid.UUID("c803b7f5-9fe8-4af3-b68a-e9900dfbe192")
REVIEW_PROGRAM_PERMISSIONS = (
    ("review_program.view", "View", "Allows review program view.", False),
    ("review_program.manage", "Manage", "Allows review program manage.", True),
)
REVIEW_PROGRAM_ROLE_GRANTS = {
    "ROOT_ADMINISTRATOR": {"review_program.view", "review_program.manage"},
    "TENANT_ADMINISTRATOR": {"review_program.view", "review_program.manage"},
    "CLIENT_ADMINISTRATOR": {"review_program.view", "review_program.manage"},
    "MATTER_ADMINISTRATOR": {"review_program.view", "review_program.manage"},
    "REVIEWER": {"review_program.view"},
    "ANALYSIS_OPERATOR": {"review_program.view"},
    "CLASSIFICATION_MANAGER": {"review_program.view"},
    "READ_ONLY": {"review_program.view"},
}


def _catalog_id(kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(RBAC_NAMESPACE, f"{kind}:{key}")


def upgrade() -> None:
    op.add_column(
        "workflow_run",
        sa.Column("notification_policy", sa.String(length=30), server_default="DEFAULT", nullable=False),
    )
    op.add_column("workflow_run", sa.Column("notification_parent_type", sa.String(length=100), nullable=True))
    op.add_column("workflow_run", sa.Column("notification_parent_id", sa.Uuid(), nullable=True))
    op.create_index("ix_workflow_run_notification_parent_id", "workflow_run", ["notification_parent_id"])
    op.create_check_constraint(
        "ck_workflow_run_notification_policy",
        "workflow_run",
        "(notification_policy = 'DEFAULT' AND notification_parent_type IS NULL "
        "AND notification_parent_id IS NULL) OR "
        "(notification_policy = 'PARENT_MANAGED' AND notification_parent_type IS NOT NULL "
        "AND notification_parent_id IS NOT NULL)",
    )
    op.alter_column("workflow_run", "notification_policy", server_default=None)

    op.create_table(
        "review_program",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("template_key", sa.String(length=100), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("template_content_hash", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("matter_definition_id", sa.Uuid(), nullable=True),
        sa.Column("target_metadata_definition_id", sa.Uuid(), nullable=True),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("current_run_id", sa.Uuid(), nullable=True),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("row_version > 0", name="ck_review_program_row_version"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'SUSPENDED', 'ARCHIVED')",
            name="ck_review_program_status",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_definition_id"], ["matter_definition.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_metadata_definition_id"], ["metadata_definition.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("matter_id", "key", name="uq_review_program_matter_key"),
    )
    for name, columns in (
        ("ix_review_program_tenant_id", ["tenant_id"]),
        ("ix_review_program_matter_id", ["matter_id"]),
        ("ix_review_program_template_key", ["template_key"]),
        ("ix_review_program_status", ["status"]),
        ("ix_review_program_matter_definition_id", ["matter_definition_id"]),
        ("ix_review_program_target_metadata_definition_id", ["target_metadata_definition_id"]),
        ("ix_review_program_current_version_id", ["current_version_id"]),
        ("ix_review_program_current_run_id", ["current_run_id"]),
        ("ix_review_program_owner_user_id", ["owner_user_id"]),
        ("ix_review_program_created_by_user_id", ["created_by_user_id"]),
        ("ix_review_program_matter_status", ["matter_id", "status"]),
    ):
        op.create_index(name, "review_program", columns)

    op.create_table(
        "review_program_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("program_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("predecessor_version_id", sa.Uuid(), nullable=True),
        sa.Column("template_key", sa.String(length=100), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("template_content_hash", sa.String(length=64), nullable=False),
        sa.Column("configuration_stage", sa.String(length=100), nullable=False),
        sa.Column("configuration_snapshot", sa.JSON(), nullable=False),
        sa.Column("source_hashes", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("template_version > 0", name="ck_review_program_template_version_positive"),
        sa.CheckConstraint("version > 0", name="ck_review_program_version_positive"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["predecessor_version_id"], ["review_program_version.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["program_id"], ["review_program.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("program_id", "version", name="uq_review_program_version"),
    )
    for name, columns in (
        ("ix_review_program_version_program_id", ["program_id"]),
        ("ix_review_program_version_predecessor_version_id", ["predecessor_version_id"]),
        ("ix_review_program_version_created_by_user_id", ["created_by_user_id"]),
        ("ix_review_program_version_program_created", ["program_id", "created_at"]),
    ):
        op.create_index(name, "review_program_version", columns)

    op.create_table(
        "review_program_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("program_id", sa.Uuid(), nullable=False),
        sa.Column("starting_program_version_id", sa.Uuid(), nullable=False),
        sa.Column("current_program_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("current_stage", sa.String(length=100), nullable=False),
        sa.Column("current_stage_ordinal", sa.Integer(), nullable=False),
        sa.Column("current_discovery_round", sa.Integer(), nullable=False),
        sa.Column("current_training_round", sa.Integer(), nullable=False),
        sa.Column("pending_action_id", sa.Uuid(), nullable=True),
        sa.Column("progress_summary", sa.JSON(), nullable=False),
        sa.Column("frozen_configuration_hash", sa.String(length=64), nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("failure_retriable", sa.Boolean(), nullable=False),
        sa.Column("initiated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "current_discovery_round >= 0 AND current_training_round >= 0",
            name="ck_review_program_run_rounds",
        ),
        sa.CheckConstraint("current_stage_ordinal > 0", name="ck_review_program_run_stage_ordinal"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'WAITING_FOR_USER', 'COMPLETED', "
            "'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_review_program_run_status",
        ),
        sa.ForeignKeyConstraint(["current_program_version_id"], ["review_program_version.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["initiated_by_user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["program_id"], ["review_program.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["starting_program_version_id"], ["review_program_version.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in (
        ("ix_review_program_run_tenant_id", ["tenant_id"]),
        ("ix_review_program_run_matter_id", ["matter_id"]),
        ("ix_review_program_run_program_id", ["program_id"]),
        ("ix_review_program_run_starting_program_version_id", ["starting_program_version_id"]),
        ("ix_review_program_run_current_program_version_id", ["current_program_version_id"]),
        ("ix_review_program_run_status", ["status"]),
        ("ix_review_program_run_pending_action_id", ["pending_action_id"]),
        ("ix_review_program_run_initiated_by_user_id", ["initiated_by_user_id"]),
        ("ix_review_program_run_program_created", ["program_id", "created_at"]),
    ):
        op.create_index(name, "review_program_run", columns)
    op.create_index(
        "uq_review_program_one_nonterminal_run",
        "review_program_run",
        ["program_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING', 'WAITING_FOR_USER')"),
        sqlite_where=sa.text("status IN ('QUEUED', 'RUNNING', 'WAITING_FOR_USER')"),
    )

    op.create_table(
        "review_program_stage_attempt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("stage_key", sa.String(length=100), nullable=False),
        sa.Column("stage_ordinal", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=500), nullable=False),
        sa.Column("input_snapshot", sa.JSON(), nullable=False),
        sa.Column("policy_hashes", sa.JSON(), nullable=False),
        sa.Column("result_summary", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("stage_ordinal > 0 AND attempt > 0", name="ck_review_program_stage_positive"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'WAITING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_review_program_stage_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["review_program_run.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "idempotency_key", name="uq_review_program_stage_idempotency"),
        sa.UniqueConstraint("run_id", "stage_key", "attempt", name="uq_review_program_stage_attempt"),
    )
    op.create_index("ix_review_program_stage_attempt_run_id", "review_program_stage_attempt", ["run_id"])
    op.create_index("ix_review_program_stage_attempt_status", "review_program_stage_attempt", ["status"])
    op.create_index(
        "ix_review_program_stage_run_status", "review_program_stage_attempt", ["run_id", "status"]
    )

    op.create_table(
        "review_program_child",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("stage_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=100), nullable=False),
        sa.Column("child_type", sa.String(length=100), nullable=False),
        sa.Column("child_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("derivation_key", sa.String(length=500), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["review_program_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stage_attempt_id"], ["review_program_stage_attempt.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "child_type", "child_id", name="uq_review_program_child_source"),
        sa.UniqueConstraint("run_id", "derivation_key", name="uq_review_program_child_derivation"),
    )
    op.create_index("ix_review_program_child_run_id", "review_program_child", ["run_id"])
    op.create_index("ix_review_program_child_stage_attempt_id", "review_program_child", ["stage_attempt_id"])
    op.create_index("ix_review_program_child_child_id", "review_program_child", ["child_id"])
    op.create_index("ix_review_program_child_status", "review_program_child", ["status"])
    op.create_index("ix_review_program_child_stage", "review_program_child", ["stage_attempt_id", "status"])

    op.create_table(
        "review_program_action",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("stage_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("stage_key", sa.String(length=100), nullable=False),
        sa.Column("action_type", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("decision_context", sa.JSON(), nullable=False),
        sa.Column("preview_hash", sa.String(length=64), nullable=False),
        sa.Column("allowed_choices", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("resolution_payload", sa.JSON(), nullable=True),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("resolved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("authorization_context", sa.JSON(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('OPEN', 'RESOLVED', 'SUPERSEDED', 'CANCELED')",
            name="ck_review_program_action_status",
        ),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["app_user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["review_program_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stage_attempt_id"], ["review_program_stage_attempt.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_program_action_run_id", "review_program_action", ["run_id"])
    op.create_index("ix_review_program_action_stage_attempt_id", "review_program_action", ["stage_attempt_id"])
    op.create_index("ix_review_program_action_status", "review_program_action", ["status"])
    op.create_index("ix_review_program_action_resolved_by_user_id", "review_program_action", ["resolved_by_user_id"])
    op.create_index("ix_review_program_action_run_status", "review_program_action", ["run_id", "status"])
    op.create_index(
        "uq_review_program_one_open_action",
        "review_program_action",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
        sqlite_where=sa.text("status = 'OPEN'"),
    )

    op.create_table(
        "review_program_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("matter_id", sa.Uuid(), nullable=False),
        sa.Column("program_id", sa.Uuid(), nullable=False),
        sa.Column("program_version_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("stage_attempt_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=100), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("processing_attempt", sa.Integer(), nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_summary", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("processing_attempt >= 0", name="ck_review_program_event_processing_attempt"),
        sa.CheckConstraint("schema_version > 0", name="ck_review_program_event_schema_version"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'PROCESSED', 'FAILED')",
            name="ck_review_program_event_status",
        ),
        sa.ForeignKeyConstraint(["matter_id"], ["matter.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["program_id"], ["review_program.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["program_version_id"], ["review_program_version.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["review_program_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stage_attempt_id"], ["review_program_stage_attempt.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("program_id", "idempotency_key", name="uq_review_program_event_idempotency"),
    )
    for name, columns in (
        ("ix_review_program_event_tenant_id", ["tenant_id"]),
        ("ix_review_program_event_matter_id", ["matter_id"]),
        ("ix_review_program_event_program_id", ["program_id"]),
        ("ix_review_program_event_program_version_id", ["program_version_id"]),
        ("ix_review_program_event_run_id", ["run_id"]),
        ("ix_review_program_event_stage_attempt_id", ["stage_attempt_id"]),
        ("ix_review_program_event_event_type", ["event_type"]),
        ("ix_review_program_event_status", ["status"]),
        ("ix_review_program_event_run_status", ["run_id", "status", "created_at"]),
        ("ix_review_program_event_source", ["source_type", "source_id"]),
    ):
        op.create_index(name, "review_program_event", columns)

    op.create_foreign_key(
        "fk_review_program_current_version",
        "review_program",
        "review_program_version",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_review_program_current_run",
        "review_program",
        "review_program_run",
        ["current_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_review_program_run_pending_action",
        "review_program_run",
        "review_program_action",
        ["pending_action_id"],
        ["id"],
        ondelete="SET NULL",
    )

    connection = op.get_bind()
    now = datetime.now(timezone.utc)
    for key, display_name, description, dangerous in REVIEW_PROGRAM_PERMISSIONS:
        permission_id = _catalog_id("permission", key)
        if context.is_offline_mode():
            op.execute(
                "INSERT INTO permission "
                "(id, key, display_name, description, category, resource_type, dangerous, status, "
                "created_at, updated_at) "
                f"VALUES ('{permission_id}', '{key}', '{display_name}', '{description}', "
                f"'Review programs', 'MATTER', {'true' if dangerous else 'false'}, 'ACTIVE', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) ON CONFLICT (key) DO NOTHING"
            )
        else:
            connection.execute(
                sa.text(
                    "INSERT INTO permission "
                    "(id, key, display_name, description, category, resource_type, dangerous, status, "
                    "created_at, updated_at) "
                    "VALUES (:id, :key, :display_name, :description, 'Review programs', 'MATTER', :dangerous, "
                    "'ACTIVE', :now, :now) ON CONFLICT (key) DO NOTHING"
                ),
                {
                    "id": permission_id,
                    "key": key,
                    "display_name": display_name,
                    "description": description,
                    "dangerous": dangerous,
                    "now": now,
                },
            )
    for role_key, permission_keys in REVIEW_PROGRAM_ROLE_GRANTS.items():
        for permission_key in permission_keys:
            role_id = _catalog_id("system-role", role_key)
            permission_id = _catalog_id("permission", permission_key)
            if context.is_offline_mode():
                op.execute(
                    "INSERT INTO role_permission (role_id, permission_id, created_at) "
                    f"VALUES ('{role_id}', '{permission_id}', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING"
                )
            else:
                connection.execute(
                    sa.text(
                        "INSERT INTO role_permission (role_id, permission_id, created_at) "
                        "VALUES (:role_id, :permission_id, :now) ON CONFLICT DO NOTHING"
                    ),
                    {"role_id": role_id, "permission_id": permission_id, "now": now},
                )
    op.execute("UPDATE tenant SET authorization_revision = authorization_revision + 1")
    if not context.is_offline_mode():
        for tenant_id in list(connection.execute(sa.text("SELECT id FROM tenant")).scalars()):
            connection.execute(
                sa.text(
                    "INSERT INTO audit_record "
                    "(id, tenant_id, actor_user_id, action, target_type, target_id, details, created_at) "
                    "VALUES (:id, :tenant_id, NULL, 'authorization.permission.catalog_updated', "
                    "'tenant', :tenant_id, CAST(:details AS jsonb), :now)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "details": json.dumps(
                        {
                            "permission_keys": [item[0] for item in REVIEW_PROGRAM_PERMISSIONS],
                            "reason": "review_program_foundation",
                        }
                    ),
                    "now": now,
                },
            )


def downgrade() -> None:
    connection = op.get_bind()
    for key, *_ in REVIEW_PROGRAM_PERMISSIONS:
        permission_id = _catalog_id("permission", key)
        if context.is_offline_mode():
            op.execute(f"DELETE FROM role_permission WHERE permission_id = '{permission_id}'")
            op.execute(f"DELETE FROM permission WHERE id = '{permission_id}'")
        else:
            connection.execute(
                sa.text("DELETE FROM role_permission WHERE permission_id = :id"), {"id": permission_id}
            )
            connection.execute(sa.text("DELETE FROM permission WHERE id = :id"), {"id": permission_id})
    op.execute("UPDATE tenant SET authorization_revision = authorization_revision + 1")
    op.drop_constraint("fk_review_program_run_pending_action", "review_program_run", type_="foreignkey")
    op.drop_constraint("fk_review_program_current_run", "review_program", type_="foreignkey")
    op.drop_constraint("fk_review_program_current_version", "review_program", type_="foreignkey")
    op.drop_table("review_program_event")
    op.drop_table("review_program_action")
    op.drop_table("review_program_child")
    op.drop_table("review_program_stage_attempt")
    op.drop_table("review_program_run")
    op.drop_table("review_program_version")
    op.drop_table("review_program")
    op.drop_constraint("ck_workflow_run_notification_policy", "workflow_run", type_="check")
    op.drop_index("ix_workflow_run_notification_parent_id", table_name="workflow_run")
    op.drop_column("workflow_run", "notification_parent_id")
    op.drop_column("workflow_run", "notification_parent_type")
    op.drop_column("workflow_run", "notification_policy")
