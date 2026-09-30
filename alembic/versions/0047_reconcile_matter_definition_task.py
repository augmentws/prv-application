"""Reconcile pre-existing Matter Definition analysis tasks.

Revision ID: 0047_reconcile_definition_task
Revises: 0046_sync_definition_task
"""

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0047_reconcile_definition_task"
down_revision: str | None = "0046_sync_definition_task"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def _provenance(definition: sa.RowMapping, source: sa.RowMapping, existing: object = None) -> str:
    value = dict(existing) if isinstance(existing, dict) else {}
    value.update(
        {
            "origin": "MATTER_DEFINITION_REVISION",
            "matter_definition_id": str(definition["id"]),
            "matter_definition_revision_id": str(source["id"]),
            "source_kind": source["source_kind"],
        }
    )
    return json.dumps(value)


def upgrade() -> None:
    bind = op.get_bind()
    definitions = list(
        bind.execute(
            sa.text(
                "SELECT id, matter_id, current_revision, published_revision, created_by_user_id, "
                "created_at, updated_at FROM matter_definition ORDER BY created_at, id"
            )
        ).mappings()
    )
    for definition in definitions:
        task = bind.execute(
            sa.text(
                "SELECT id FROM matter_analysis_task "
                "WHERE matter_id = :matter_id AND task_type = 'MATTER_DEFINITION'"
            ),
            {"matter_id": definition["matter_id"]},
        ).mappings().one_or_none()
        if task is None:
            raise RuntimeError(f"Matter Definition {definition['id']} has no mirrored analysis task")

        revisions = list(
            bind.execute(
                sa.text(
                    "SELECT id, revision, content_markdown, source_kind, created_by_user_id, created_at "
                    "FROM matter_definition_revision WHERE matter_definition_id = :definition_id "
                    "ORDER BY revision"
                ),
                {"definition_id": definition["id"]},
            ).mappings()
        )
        revisions_by_number = {source["revision"]: source for source in revisions}
        revisions_by_hash = {_hash(source["content_markdown"]): source for source in revisions}
        versions = list(
            bind.execute(
                sa.text(
                    "SELECT id, version, definition_content_hash, source_provenance "
                    "FROM matter_analysis_task_version WHERE matter_analysis_task_id = :task_id "
                    "ORDER BY version"
                ),
                {"task_id": task["id"]},
            ).mappings()
        )
        occupied_versions = {version["version"] for version in versions}

        for version in versions:
            source = revisions_by_number.get(version["version"])
            if source is not None and version["definition_content_hash"] == _hash(source["content_markdown"]):
                continue
            source = revisions_by_hash.get(version["definition_content_hash"])
            if source is None:
                raise RuntimeError(
                    f"Matter Definition task version {version['id']} does not match an authoritative revision"
                )
            if source["revision"] in occupied_versions:
                raise RuntimeError(
                    f"Matter Definition task version {version['id']} conflicts with revision {source['revision']}"
                )
            occupied_versions.remove(version["version"])
            occupied_versions.add(source["revision"])
            bind.execute(
                sa.text(
                    "UPDATE matter_analysis_task_version SET version = :version, "
                    "source_provenance = CAST(:source_provenance AS json) WHERE id = :id"
                ),
                {
                    "id": version["id"],
                    "version": source["revision"],
                    "source_provenance": _provenance(
                        definition,
                        source,
                        version["source_provenance"],
                    ),
                },
            )

        reconciled_versions = list(
            bind.execute(
                sa.text(
                    "SELECT version, source_provenance FROM matter_analysis_task_version "
                    "WHERE matter_analysis_task_id = :task_id"
                ),
                {"task_id": task["id"]},
            ).mappings()
        )
        existing_versions = {version["version"] for version in reconciled_versions}
        provenance_by_version = {
            version["version"]: version["source_provenance"] for version in reconciled_versions
        }
        for source in revisions:
            if source["revision"] in existing_versions:
                bind.execute(
                    sa.text(
                        "UPDATE matter_analysis_task_version SET "
                        "source_provenance = CAST(:source_provenance AS json) "
                        "WHERE matter_analysis_task_id = :task_id AND version = :version"
                    ),
                    {
                        "task_id": task["id"],
                        "version": source["revision"],
                        "source_provenance": _provenance(
                            definition,
                            source,
                            provenance_by_version[source["revision"]],
                        ),
                    },
                )
                continue
            bind.execute(
                sa.text(
                    "INSERT INTO matter_analysis_task_version "
                    "(id, matter_analysis_task_id, version, status, compilation_status, "
                    "definition_markdown, definition_content_hash, decision_specification, "
                    "specification_content_hash, input_contract, output_contract, evidence_policy, "
                    "routing_policy, compiler_skill_definition_version_id, compiler_skill_run_id, "
                    "compiler_workflow_run_id, compiler_model_configuration, validation_report, "
                    "source_provenance, created_by_user_id, published_by_user_id, created_at, published_at) "
                    "VALUES (:id, :task_id, :version, 'DRAFT', 'NOT_GENERATED', :markdown, :content_hash, "
                    "NULL, NULL, CAST('{}' AS json), CAST('{}' AS json), CAST('{}' AS json), "
                    "CAST('{}' AS json), NULL, NULL, NULL, CAST('{}' AS json), CAST('{}' AS json), "
                    "CAST(:source_provenance AS json), :created_by_user_id, NULL, :created_at, NULL)"
                ),
                {
                    "id": uuid.uuid4(),
                    "task_id": task["id"],
                    "version": source["revision"],
                    "markdown": source["content_markdown"],
                    "content_hash": _hash(source["content_markdown"]),
                    "source_provenance": _provenance(definition, source),
                    "created_by_user_id": source["created_by_user_id"],
                    "created_at": source["created_at"],
                },
            )

        bind.execute(
            sa.text(
                "UPDATE matter_analysis_task_version SET status = CASE "
                "WHEN version = :published_revision THEN 'PUBLISHED' "
                "WHEN version = :current_revision THEN 'DRAFT' ELSE 'RETIRED' END, "
                "published_by_user_id = CASE WHEN version = :published_revision "
                "THEN created_by_user_id ELSE NULL END, "
                "published_at = CASE WHEN version = :published_revision THEN created_at ELSE NULL END "
                "WHERE matter_analysis_task_id = :task_id"
            ),
            {
                "task_id": task["id"],
                "current_revision": definition["current_revision"],
                "published_revision": definition["published_revision"],
            },
        )
        bind.execute(
            sa.text(
                "UPDATE matter_analysis_task SET key = 'matter_definition', name = 'Matter Definition', "
                "description = :description, workflow_key = 'matter_definition_v1', "
                "current_version = :current_revision, published_version = :published_revision, "
                "status = 'ACTIVE', updated_at = :updated_at WHERE id = :task_id"
            ),
            {
                "task_id": task["id"],
                "description": "Matter-wide reviewer guidance and responsiveness criteria.",
                "current_revision": definition["current_revision"],
                "published_revision": definition["published_revision"],
                "updated_at": definition["updated_at"],
            },
        )


def downgrade() -> None:
    # Reconciliation preserves authoritative history and is intentionally not reversed.
    pass
