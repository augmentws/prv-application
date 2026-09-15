"""Record the acting user for every agent run.

Revision ID: 0013_agent_run_actor
Revises: 0012_merge_agent_embeddings
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_agent_run_actor"
down_revision: str | None = "0012_merge_agent_embeddings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_run", sa.Column("actor_user_id", sa.Uuid(), nullable=True))
    op.execute(
        "UPDATE agent_run SET actor_user_id = agent_turn.created_by_user_id "
        "FROM agent_turn WHERE agent_run.turn_id = agent_turn.id"
    )
    op.alter_column("agent_run", "actor_user_id", nullable=False)
    op.create_foreign_key(
        "fk_agent_run_actor_user_id_app_user",
        "agent_run",
        "app_user",
        ["actor_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_agent_run_actor_user_id", "agent_run", ["actor_user_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_run_actor_user_id", table_name="agent_run")
    op.drop_constraint("fk_agent_run_actor_user_id_app_user", "agent_run", type_="foreignkey")
    op.drop_column("agent_run", "actor_user_id")
