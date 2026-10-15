"""Add tenant-scoped audit log lookup indexes.

Revision ID: 0066_audit_log
Revises: 0065_random_assessment
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0066_audit_log"
down_revision: str | None = "0065_random_assessment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_audit_record_tenant_created_id",
        "audit_record",
        ["tenant_id", "created_at", "id"],
    )
    op.create_index(
        "ix_audit_record_tenant_actor_created",
        "audit_record",
        ["tenant_id", "actor_user_id", "created_at"],
    )
    op.create_index(
        "ix_audit_record_tenant_action_created",
        "audit_record",
        ["tenant_id", "action", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_record_tenant_action_created", table_name="audit_record")
    op.drop_index("ix_audit_record_tenant_actor_created", table_name="audit_record")
    op.drop_index("ix_audit_record_tenant_created_id", table_name="audit_record")
