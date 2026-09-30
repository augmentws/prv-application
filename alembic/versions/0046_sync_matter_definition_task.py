"""Mirror existing Matter Definitions into analysis tasks.

Revision ID: 0046_sync_definition_task
Revises: 0045_matter_definition_task
"""

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0046_sync_definition_task"
down_revision: str | None = "0045_matter_definition_task"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    definitions = bind.execute(
        sa.text(
            "SELECT id, matter_id, current_revision, published_revision, created_by_user_id, "
            "created_at, updated_at FROM matter_definition ORDER BY created_at, id"
        )
    ).mappings()
    for definition in definitions:
        existing_task = bind.execute(
            sa.text(
                "SELECT id FROM matter_analysis_task "
                "WHERE matter_id = :matter_id AND task_type = 'MATTER_DEFINITION'"
            ),
            {"matter_id": definition["matter_id"]},
        ).scalar_one_or_none()
        if existing_task is not None:
            continue
        task_id = uuid.uuid4()
        bind.execute(
            sa.text(
                "INSERT INTO matter_analysis_task "
                "(id, matter_id, key, name, description, task_type, workflow_key, current_version, "
                "published_version, status, created_by_user_id, created_at, updated_at) VALUES "
                "(:id, :matter_id, 'matter_definition', 'Matter Definition', :description, "
                "'MATTER_DEFINITION', 'matter_definition_v1', :current_version, :published_version, "
                "'ACTIVE', :created_by_user_id, :created_at, :updated_at)"
            ),
            {
                "id": task_id,
                "matter_id": definition["matter_id"],
                "description": "Matter-wide reviewer guidance and responsiveness criteria.",
                "current_version": definition["current_revision"],
                "published_version": definition["published_revision"],
                "created_by_user_id": definition["created_by_user_id"],
                "created_at": definition["created_at"],
                "updated_at": definition["updated_at"],
            },
        )
        revisions = bind.execute(
            sa.text(
                "SELECT id, revision, content_markdown, source_kind, created_by_user_id, created_at "
                "FROM matter_definition_revision WHERE matter_definition_id = :definition_id "
                "ORDER BY revision"
            ),
            {"definition_id": definition["id"]},
        ).mappings()
        for source in revisions:
            status = (
                "PUBLISHED"
                if source["revision"] == definition["published_revision"]
                else "DRAFT"
                if source["revision"] == definition["current_revision"]
                else "RETIRED"
            )
            bind.execute(
                sa.text(
                    "INSERT INTO matter_analysis_task_version "
                    "(id, matter_analysis_task_id, version, status, compilation_status, "
                    "definition_markdown, definition_content_hash, decision_specification, "
                    "specification_content_hash, input_contract, output_contract, evidence_policy, "
                    "routing_policy, compiler_skill_definition_version_id, compiler_skill_run_id, "
                    "compiler_workflow_run_id, compiler_model_configuration, validation_report, "
                    "source_provenance, created_by_user_id, published_by_user_id, created_at, published_at) "
                    "VALUES (:id, :task_id, :version, :status, 'NOT_GENERATED', :markdown, :content_hash, "
                    "NULL, NULL, CAST('{}' AS json), CAST('{}' AS json), CAST('{}' AS json), "
                    "CAST('{}' AS json), NULL, NULL, NULL, CAST('{}' AS json), CAST('{}' AS json), "
                    "CAST(:source_provenance AS json), :created_by_user_id, :published_by_user_id, "
                    ":created_at, :published_at)"
                ),
                {
                    "id": uuid.uuid4(),
                    "task_id": task_id,
                    "version": source["revision"],
                    "status": status,
                    "markdown": source["content_markdown"],
                    "content_hash": hashlib.sha256(source["content_markdown"].encode("utf-8")).hexdigest(),
                    "source_provenance": json.dumps(
                        {
                            "origin": "MATTER_DEFINITION_REVISION",
                            "matter_definition_id": str(definition["id"]),
                            "matter_definition_revision_id": str(source["id"]),
                            "source_kind": source["source_kind"],
                        }
                    ),
                    "created_by_user_id": source["created_by_user_id"],
                    "published_by_user_id": (
                        source["created_by_user_id"] if status == "PUBLISHED" else None
                    ),
                    "created_at": source["created_at"],
                    "published_at": source["created_at"] if status == "PUBLISHED" else None,
                },
            )
    op.create_index(
        "uq_matter_analysis_task_matter_definition",
        "matter_analysis_task",
        ["matter_id"],
        unique=True,
        postgresql_where=sa.text("task_type = 'MATTER_DEFINITION'"),
    )


def downgrade() -> None:
    op.drop_index("uq_matter_analysis_task_matter_definition", table_name="matter_analysis_task")
    op.execute("DELETE FROM matter_analysis_task WHERE task_type = 'MATTER_DEFINITION'")
