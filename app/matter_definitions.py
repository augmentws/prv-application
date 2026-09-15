import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.models import Matter, MatterDefinition, MatterDefinitionRevision


class MatterDefinitionError(ValueError):
    pass


def append_matter_definition_revision(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    content_markdown: str,
    source_kind: str,
    based_on_revision: int | None,
    source_artifact_id: uuid.UUID | None = None,
    source_filename: str | None = None,
    agent_run_id: uuid.UUID | None = None,
) -> tuple[MatterDefinition, MatterDefinitionRevision]:
    definition = db.scalar(
        select(MatterDefinition).where(MatterDefinition.matter_id == matter.id).with_for_update()
    )
    if definition is None:
        if based_on_revision is not None:
            raise MatterDefinitionError("The Matter Definition does not have a prior revision")
        definition = MatterDefinition(
            matter_id=matter.id,
            current_revision=1,
            created_by_user_id=actor_user_id,
        )
        db.add(definition)
        db.flush()
        next_revision = 1
    else:
        if based_on_revision is not None and based_on_revision != definition.current_revision:
            raise MatterDefinitionError(
                f"Matter Definition changed; current revision is {definition.current_revision}"
            )
        next_revision = definition.current_revision + 1
        definition.current_revision = next_revision

    revision = MatterDefinitionRevision(
        matter_definition_id=definition.id,
        revision=next_revision,
        content_markdown=content_markdown,
        source_kind=source_kind,
        source_artifact_id=source_artifact_id,
        source_filename=source_filename,
        based_on_revision=based_on_revision,
        created_by_user_id=actor_user_id,
        agent_run_id=agent_run_id,
    )
    db.add(revision)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=actor_user_id,
        action="matter_definition.revision.created",
        target_type="matter_definition",
        target_id=definition.id,
        details={
            "matter_id": str(matter.id),
            "revision": next_revision,
            "source_kind": source_kind,
            "agent_run_id": str(agent_run_id) if agent_run_id else None,
        },
    )
    db.flush()
    return definition, revision
