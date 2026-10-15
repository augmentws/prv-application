"""Add classification build attempt tracking.

Revision ID: 0057_classification_retries
Revises: 0056_classification_builds
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0057_classification_retries"
down_revision: str | None = "0056_classification_builds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "classification_training_run",
        sa.Column("attempt_number", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_check_constraint(
        "ck_classification_training_run_attempt_number",
        "classification_training_run",
        "attempt_number > 0",
    )
    op.alter_column(
        "classification_training_run",
        "attempt_number",
        server_default=None,
    )
    op.add_column(
        "classification_training_run",
        sa.Column("execution_actor_user_id", sa.Uuid(), nullable=True),
    )
    op.execute(
        "UPDATE classification_training_run "
        "SET execution_actor_user_id = created_by_user_id"
    )
    op.alter_column(
        "classification_training_run",
        "execution_actor_user_id",
        nullable=False,
    )
    op.create_foreign_key(
        "fk_classification_training_run_execution_actor",
        "classification_training_run",
        "app_user",
        ["execution_actor_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_classification_training_run_execution_actor_user_id",
        "classification_training_run",
        ["execution_actor_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_classification_training_run_execution_actor_user_id",
        table_name="classification_training_run",
    )
    op.drop_constraint(
        "fk_classification_training_run_execution_actor",
        "classification_training_run",
        type_="foreignkey",
    )
    op.drop_column("classification_training_run", "execution_actor_user_id")
    op.drop_constraint(
        "ck_classification_training_run_attempt_number",
        "classification_training_run",
        type_="check",
    )
    op.drop_column("classification_training_run", "attempt_number")
