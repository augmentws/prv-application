"""Add the matter entity store, the fact ledger, and fact/entity permissions.

Revision ID: 0089_fact_ledger_entities
Revises: 0088_fact_development_task
"""

import json
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op

revision: str = "0089_fact_ledger_entities"
down_revision: str | None = "0088_fact_development_task"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RBAC_NAMESPACE = uuid.UUID("c803b7f5-9fe8-4af3-b68a-e9900dfbe192")
NEW_PERMISSIONS = (
    ("fact.view", "View", "Allows fact view.", "Facts", False),
    ("fact.review", "Review", "Allows fact review.", "Facts", False),
    ("fact.export", "Export", "Allows fact export.", "Facts", True),
    ("entity.view", "View", "Allows entity view.", "Entities", False),
    ("entity.manage", "Manage", "Allows entity manage.", "Entities", False),
)
ROLE_GRANTS = {
    "ROOT_ADMINISTRATOR": ("fact.view", "fact.review", "fact.export", "entity.view", "entity.manage"),
    "TENANT_ADMINISTRATOR": ("fact.view", "fact.review", "fact.export", "entity.view", "entity.manage"),
    "CLIENT_ADMINISTRATOR": ("fact.view", "fact.review", "fact.export", "entity.view", "entity.manage"),
    "MATTER_ADMINISTRATOR": ("fact.view", "fact.review", "fact.export", "entity.view", "entity.manage"),
    "REVIEWER": ("fact.view", "fact.review", "entity.view", "entity.manage"),
    "ANALYSIS_OPERATOR": ("fact.view", "entity.view"),
    "READ_ONLY": ("fact.view", "entity.view"),
}
DATE_PRECISIONS = "('DAY', 'MONTH', 'QUARTER', 'YEAR', 'RANGE', 'UNKNOWN')"
DATE_BASES = "('STATED_IN_TEXT', 'DOCUMENT_DATE', 'INFERRED')"


def _catalog_id(kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(RBAC_NAMESPACE, f"{kind}:{key}")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _user_fk(name: str) -> sa.Column:
    return sa.Column(name, sa.Uuid(), sa.ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True)


def _workflow_fk(name: str) -> sa.Column:
    return sa.Column(name, sa.Uuid(), sa.ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=True)


def upgrade() -> None:
    op.create_table(
        "matter_entity",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("matter_id", sa.Uuid(), sa.ForeignKey("matter.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", sa.Uuid(), sa.ForeignKey("matter_entity.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("display_name", sa.String(length=300), nullable=False),
        sa.Column("normalized_name", sa.String(length=300), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("normalized_email", sa.String(length=320), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column(
            "organization_id", sa.Uuid(), sa.ForeignKey("matter_entity.id", ondelete="RESTRICT"), nullable=True
        ),
        sa.Column("custodian_id", sa.Uuid(), sa.ForeignKey("custodian.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        _user_fk("created_by_user_id"),
        _workflow_fk("created_by_workflow_run_id"),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("entity_type IN ('PERSON', 'ORGANIZATION')", name="ck_matter_entity_type"),
        sa.CheckConstraint(
            "(parent_id IS NULL AND status IN ('PROPOSED', 'CONFIRMED', 'REJECTED')) OR "
            "(parent_id IS NOT NULL AND status IS NULL AND title IS NULL AND organization_id IS NULL "
            "AND custodian_id IS NULL)",
            name="ck_matter_entity_canonical_shape",
        ),
        sa.CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_matter_entity_not_own_parent"),
        sa.CheckConstraint(
            "(created_by_user_id IS NULL) <> (created_by_workflow_run_id IS NULL)",
            name="ck_matter_entity_creator",
        ),
        sa.CheckConstraint("(email IS NULL) = (normalized_email IS NULL)", name="ck_matter_entity_email_pair"),
        sa.CheckConstraint("lock_version > 0", name="ck_matter_entity_lock_version"),
    )
    op.create_index(
        "uq_matter_entity_matter_email",
        "matter_entity",
        ["matter_id", "normalized_email"],
        unique=True,
        postgresql_where=sa.text("normalized_email IS NOT NULL"),
    )
    op.create_index("ix_matter_entity_matter_name", "matter_entity", ["matter_id", "normalized_name"])
    op.create_index("ix_matter_entity_matter_type_status", "matter_entity", ["matter_id", "entity_type", "status"])
    for column in ("matter_id", "parent_id", "organization_id", "custodian_id", "created_by_user_id", "created_by_workflow_run_id"):
        op.create_index(f"ix_matter_entity_{column}", "matter_entity", [column])

    op.create_table(
        "matter_entity_event",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("matter_id", sa.Uuid(), sa.ForeignKey("matter.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", sa.Uuid(), sa.ForeignKey("matter_entity.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        _user_fk("actor_user_id"),
        _workflow_fk("workflow_run_id"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("actor_type IN ('HUMAN', 'AGENT', 'SYSTEM')", name="ck_matter_entity_event_actor_type"),
    )
    op.create_index("ix_matter_entity_event_entity_created", "matter_entity_event", ["entity_id", "created_at"])
    for column in ("matter_id", "actor_user_id", "workflow_run_id"):
        op.create_index(f"ix_matter_entity_event_{column}", "matter_entity_event", [column])

    op.create_table(
        "matter_fact",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("matter_id", sa.Uuid(), sa.ForeignKey("matter.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("event_date_start", sa.Date(), nullable=True),
        sa.Column("event_date_end", sa.Date(), nullable=True),
        sa.Column("date_precision", sa.String(length=20), nullable=False),
        sa.Column("date_basis", sa.String(length=20), nullable=True),
        sa.Column(
            "owner_entity_id", sa.Uuid(), sa.ForeignKey("matter_entity.id", ondelete="RESTRICT"), nullable=True
        ),
        sa.Column("key_fact", sa.Boolean(), nullable=False),
        sa.Column("has_contradiction", sa.Boolean(), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column(
            "merged_into_fact_id", sa.Uuid(), sa.ForeignKey("matter_fact.id", ondelete="RESTRICT"), nullable=True
        ),
        _user_fk("confirmed_by_user_id"),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk("created_by_user_id"),
        _workflow_fk("origin_workflow_run_id"),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("status IN ('WORKING', 'CONFIRMED', 'REJECTED', 'MERGED')", name="ck_matter_fact_status"),
        sa.CheckConstraint(f"date_precision IN {DATE_PRECISIONS}", name="ck_matter_fact_date_precision"),
        sa.CheckConstraint(f"date_basis IS NULL OR date_basis IN {DATE_BASES}", name="ck_matter_fact_date_basis"),
        sa.CheckConstraint(
            "(date_precision = 'UNKNOWN' AND event_date_start IS NULL AND event_date_end IS NULL) OR "
            "(date_precision = 'RANGE' AND event_date_start IS NOT NULL AND event_date_end IS NOT NULL "
            "AND event_date_end >= event_date_start) OR "
            "(date_precision IN ('DAY', 'MONTH', 'QUARTER', 'YEAR') AND event_date_start IS NOT NULL "
            "AND event_date_end IS NULL)",
            name="ck_matter_fact_date_shape",
        ),
        sa.CheckConstraint("(status = 'MERGED') = (merged_into_fact_id IS NOT NULL)", name="ck_matter_fact_merged_target"),
        sa.CheckConstraint("(status = 'CONFIRMED') = (confirmed_at IS NOT NULL)", name="ck_matter_fact_confirmation"),
        sa.CheckConstraint("current_revision > 0 AND lock_version > 0", name="ck_matter_fact_versions"),
        sa.CheckConstraint("evidence_count >= 0 AND document_count >= 0", name="ck_matter_fact_counts"),
    )
    op.create_index("ix_matter_fact_matter_status_date", "matter_fact", ["matter_id", "status", "event_date_start"])
    for column in (
        "matter_id",
        "owner_entity_id",
        "merged_into_fact_id",
        "confirmed_by_user_id",
        "created_by_user_id",
        "origin_workflow_run_id",
    ):
        op.create_index(f"ix_matter_fact_{column}", "matter_fact", [column])

    op.create_table(
        "matter_fact_revision",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("fact_id", sa.Uuid(), sa.ForeignKey("matter_fact.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("event_date_start", sa.Date(), nullable=True),
        sa.Column("event_date_end", sa.Date(), nullable=True),
        sa.Column("date_precision", sa.String(length=20), nullable=False),
        sa.Column("date_basis", sa.String(length=20), nullable=True),
        sa.Column("owner_entity_id", sa.Uuid(), nullable=True),
        sa.Column("issues", sa.JSON(), nullable=False),
        sa.Column("participants", sa.JSON(), nullable=False),
        sa.Column("author_type", sa.String(length=20), nullable=False),
        _user_fk("author_user_id"),
        _workflow_fk("workflow_run_id"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("fact_id", "revision", name="uq_matter_fact_revision"),
        sa.CheckConstraint("revision > 0", name="ck_matter_fact_revision_positive"),
        sa.CheckConstraint("author_type IN ('HUMAN', 'AGENT')", name="ck_matter_fact_revision_author_type"),
        sa.CheckConstraint(f"date_precision IN {DATE_PRECISIONS}", name="ck_matter_fact_revision_precision"),
    )
    for column in ("fact_id", "author_user_id", "workflow_run_id"):
        op.create_index(f"ix_matter_fact_revision_{column}", "matter_fact_revision", [column])

    op.create_table(
        "matter_fact_issue",
        sa.Column("fact_id", sa.Uuid(), sa.ForeignKey("matter_fact.id", ondelete="CASCADE"), primary_key=True),
        sa.Column(
            "matter_analysis_task_id",
            sa.Uuid(),
            sa.ForeignKey("matter_analysis_task.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("issue_key", sa.String(length=100), primary_key=True),
    )
    op.create_index(
        "ix_matter_fact_issue_matter_analysis_task_id", "matter_fact_issue", ["matter_analysis_task_id"]
    )

    op.create_table(
        "matter_fact_entity",
        sa.Column("fact_id", sa.Uuid(), sa.ForeignKey("matter_fact.id", ondelete="CASCADE"), primary_key=True),
        sa.Column(
            "entity_id", sa.Uuid(), sa.ForeignKey("matter_entity.id", ondelete="RESTRICT"), primary_key=True
        ),
        sa.Column("role", sa.String(length=30), primary_key=True),
        sa.CheckConstraint(
            "role IN ('PARTICIPANT_ACTOR', 'PARTICIPANT_RECIPIENT')", name="ck_matter_fact_entity_role"
        ),
    )
    op.create_index("ix_matter_fact_entity_entity_id", "matter_fact_entity", ["entity_id"])

    op.create_table(
        "matter_fact_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("fact_id", sa.Uuid(), sa.ForeignKey("matter_fact.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "matter_document_id",
            sa.Uuid(),
            sa.ForeignKey("matter_document.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column("paragraph_map_version", sa.String(length=100), nullable=False),
        sa.Column("paragraph_id", sa.String(length=50), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("quote_hash", sa.String(length=64), nullable=False),
        sa.Column("stance", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        _user_fk("added_by_user_id"),
        _workflow_fk("workflow_run_id"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("stance IN ('SUPPORTS', 'CONTRADICTS')", name="ck_matter_fact_evidence_stance"),
        sa.CheckConstraint("status IN ('ACTIVE', 'REMOVED')", name="ck_matter_fact_evidence_status"),
        sa.CheckConstraint("char_end > char_start AND char_start >= 0", name="ck_matter_fact_evidence_offsets"),
        sa.CheckConstraint(
            "(added_by_user_id IS NULL) <> (workflow_run_id IS NULL)", name="ck_matter_fact_evidence_author"
        ),
    )
    op.create_index("ix_matter_fact_evidence_fact_status", "matter_fact_evidence", ["fact_id", "status"])
    op.create_index(
        "ix_matter_fact_evidence_document_status", "matter_fact_evidence", ["matter_document_id", "status"]
    )
    for column in ("added_by_user_id", "workflow_run_id"):
        op.create_index(f"ix_matter_fact_evidence_{column}", "matter_fact_evidence", [column])

    op.create_table(
        "matter_fact_event",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("matter_id", sa.Uuid(), sa.ForeignKey("matter.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fact_id", sa.Uuid(), sa.ForeignKey("matter_fact.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        _user_fk("actor_user_id"),
        _workflow_fk("workflow_run_id"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("actor_type IN ('HUMAN', 'AGENT', 'SYSTEM')", name="ck_matter_fact_event_actor_type"),
    )
    op.create_index("ix_matter_fact_event_fact_created", "matter_fact_event", ["fact_id", "created_at"])
    op.create_index("ix_matter_fact_event_matter_created", "matter_fact_event", ["matter_id", "created_at"])
    for column in ("actor_user_id", "workflow_run_id"):
        op.create_index(f"ix_matter_fact_event_{column}", "matter_fact_event", [column])

    _seed_permissions()


def _seed_permissions() -> None:
    connection = op.get_bind()
    now = datetime.now(timezone.utc)
    for key, display_name, description, category, dangerous in NEW_PERMISSIONS:
        connection.execute(
            sa.text(
                "INSERT INTO permission "
                "(id, key, display_name, description, category, resource_type, dangerous, status, "
                "created_at, updated_at) "
                "VALUES (:id, :key, :display_name, :description, :category, 'MATTER', :dangerous, 'ACTIVE', "
                ":now, :now) ON CONFLICT (key) DO NOTHING"
            ),
            {
                "id": _catalog_id("permission", key),
                "key": key,
                "display_name": display_name,
                "description": description,
                "category": category,
                "dangerous": dangerous,
                "now": now,
            },
        )
    existing_roles = set(connection.execute(sa.text("SELECT key FROM role WHERE role_kind = 'SYSTEM'")).scalars())
    for role_key, permission_keys in ROLE_GRANTS.items():
        if role_key not in existing_roles:
            continue
        for permission_key in permission_keys:
            connection.execute(
                sa.text(
                    "INSERT INTO role_permission (role_id, permission_id, created_at) "
                    "VALUES (:role_id, :permission_id, :now) ON CONFLICT DO NOTHING"
                ),
                {
                    "role_id": _catalog_id("system-role", role_key),
                    "permission_id": _catalog_id("permission", permission_key),
                    "now": now,
                },
            )
    connection.execute(sa.text("UPDATE tenant SET authorization_revision = authorization_revision + 1"))
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
                    {"permission_keys": [item[0] for item in NEW_PERMISSIONS], "reason": "fact_ledger_entities"}
                ),
                "now": now,
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    permission_ids = [_catalog_id("permission", item[0]) for item in NEW_PERMISSIONS]
    for permission_id in permission_ids:
        connection.execute(sa.text("DELETE FROM role_permission WHERE permission_id = :id"), {"id": permission_id})
        connection.execute(sa.text("DELETE FROM permission WHERE id = :id"), {"id": permission_id})
    connection.execute(sa.text("UPDATE tenant SET authorization_revision = authorization_revision + 1"))
    op.drop_table("matter_fact_event")
    op.drop_table("matter_fact_evidence")
    op.drop_table("matter_fact_entity")
    op.drop_table("matter_fact_issue")
    op.drop_table("matter_fact_revision")
    op.drop_table("matter_fact")
    op.drop_table("matter_entity_event")
    op.drop_table("matter_entity")
