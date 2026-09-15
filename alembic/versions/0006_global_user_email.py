"""Make user email globally unique for tenant-free login.

Revision ID: 0006_global_user_email
Revises: 0005_matter_documents
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_global_user_email"
down_revision: str | None = "0005_matter_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_user_normalized_email", "app_user", ["normalized_email"])
    op.drop_constraint("uq_user_tenant_email", "app_user", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_user_tenant_email",
        "app_user",
        ["tenant_id", "normalized_email"],
    )
    op.drop_constraint("uq_user_normalized_email", "app_user", type_="unique")
