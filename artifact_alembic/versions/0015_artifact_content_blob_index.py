"""Index artifact content blob references.

Revision ID: 0015_artifact_content_blob_idx
Revises: 0014_artifact_lineage_src_idx
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_artifact_content_blob_idx"
down_revision: str | None = "0014_artifact_lineage_src_idx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEX_NAME = "ix_artifact_content_blob_id"


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        index_is_valid = op.get_bind().execute(
            sa.text(
                "SELECT ix.indisvalid "
                "FROM pg_index ix "
                "JOIN pg_class index_class ON index_class.oid = ix.indexrelid "
                "JOIN pg_class table_class ON table_class.oid = ix.indrelid "
                "WHERE table_class.relname = 'artifact' "
                "AND index_class.relname = :index_name"
            ),
            {"index_name": INDEX_NAME},
        ).scalar_one_or_none()
        if index_is_valid is True:
            return
        with op.get_context().autocommit_block():
            if index_is_valid is False:
                op.drop_index(INDEX_NAME, table_name="artifact", postgresql_concurrently=True)
            op.create_index(
                INDEX_NAME,
                "artifact",
                ["content_blob_id"],
                postgresql_concurrently=True,
            )
        return
    if INDEX_NAME in {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("artifact")}:
        return
    op.create_index(INDEX_NAME, "artifact", ["content_blob_id"])


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.drop_index(INDEX_NAME, table_name="artifact", postgresql_concurrently=True)
        return
    op.drop_index(INDEX_NAME, table_name="artifact")
