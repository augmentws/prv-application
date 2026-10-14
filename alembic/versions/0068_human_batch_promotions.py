"""Allow human review runs to use durable batch promotion.

Revision ID: 0068_human_batch_promotions
Revises: 0067_rbac_foundation
"""

from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op
from app.rbac_manifest import PERMISSION_BY_KEY, SYSTEM_ROLES

revision: str = "0068_human_batch_promotions"
down_revision: str | None = "0067_rbac_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "review_batch_run_promotion",
        "matter_analysis_task_version_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
    op.drop_index(
        "ix_review_batch_run_promotion_task_version_id",
        table_name="review_batch_run_promotion",
    )
    op.create_index(
        "ix_review_batch_run_promotion_matter_analysis_task_version_id",
        "review_batch_run_promotion",
        ["matter_analysis_task_version_id"],
    )
    op.drop_constraint("permission_key_key", "permission", type_="unique")
    op.drop_index("ix_permission_key", table_name="permission")
    op.create_index("ix_permission_key", "permission", ["key"], unique=True)
    bind = op.get_bind()
    permission = PERMISSION_BY_KEY["batch.publish_coding"]
    if bind.execute(
        sa.text("SELECT 1 FROM permission WHERE id = :id"), {"id": permission.id}
    ).scalar_one_or_none() is None:
        now = datetime.now(timezone.utc)
        bind.execute(
            sa.text(
                "INSERT INTO permission "
                "(id, key, display_name, description, category, resource_type, dangerous, status, created_at, updated_at) "
                "VALUES (:id, :key, :display_name, :description, :category, :resource_type, :dangerous, 'ACTIVE', :created_at, :updated_at)"
            ),
            {
                "id": permission.id,
                "key": permission.key,
                "display_name": permission.display_name,
                "description": permission.description,
                "category": permission.category,
                "resource_type": permission.resource_type,
                "dangerous": permission.dangerous,
                "created_at": now,
                "updated_at": now,
            },
        )
    for role in SYSTEM_ROLES:
        if permission.key not in role.permission_keys:
            continue
        if bind.execute(
            sa.text(
                "SELECT 1 FROM role_permission "
                "WHERE role_id = :role_id AND permission_id = :permission_id"
            ),
            {"role_id": role.id, "permission_id": permission.id},
        ).scalar_one_or_none() is None:
            bind.execute(
                sa.text(
                    "INSERT INTO role_permission (role_id, permission_id, created_at) "
                    "VALUES (:role_id, :permission_id, :created_at)"
                ),
                {
                    "role_id": role.id,
                    "permission_id": permission.id,
                    "created_at": datetime.now(timezone.utc),
                },
            )


def downgrade() -> None:
    permission = PERMISSION_BY_KEY["batch.publish_coding"]
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM role_permission WHERE permission_id = :permission_id"),
        {"permission_id": permission.id},
    )
    bind.execute(
        sa.text("DELETE FROM permission WHERE id = :permission_id"),
        {"permission_id": permission.id},
    )
    op.execute(
        "DELETE FROM review_batch_run_promotion "
        "WHERE matter_analysis_task_version_id IS NULL"
    )
    op.alter_column(
        "review_batch_run_promotion",
        "matter_analysis_task_version_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
    op.drop_index(
        "ix_review_batch_run_promotion_matter_analysis_task_version_id",
        table_name="review_batch_run_promotion",
    )
    op.create_index(
        "ix_review_batch_run_promotion_task_version_id",
        "review_batch_run_promotion",
        ["matter_analysis_task_version_id"],
    )
    op.drop_index("ix_permission_key", table_name="permission")
    op.create_index("ix_permission_key", "permission", ["key"])
    op.create_unique_constraint("permission_key_key", "permission", ["key"])
