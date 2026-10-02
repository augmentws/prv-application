import hashlib
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.models import AgentConversation, Matter, MatterDefinition, MatterDefinitionRevision

LEGACY_GUIDANCE_KEY = "general_review"
LEGACY_GUIDANCE_NAME = "General Review Guidance"


class MatterDefinitionError(ValueError):
    pass


def get_guidance(
    db: Session,
    *,
    matter_id: uuid.UUID,
    guidance_id: uuid.UUID,
    for_update: bool = False,
) -> MatterDefinition:
    statement = select(MatterDefinition).where(
        MatterDefinition.id == guidance_id,
        MatterDefinition.matter_id == matter_id,
    )
    if for_update:
        statement = statement.with_for_update()
    definition = db.scalar(statement)
    if definition is None:
        raise MatterDefinitionError("Review Guidance not found")
    return definition


def resolve_legacy_guidance(
    db: Session,
    *,
    matter_id: uuid.UUID,
    for_update: bool = False,
) -> MatterDefinition | None:
    statement = (
        select(MatterDefinition)
        .where(MatterDefinition.matter_id == matter_id)
        .order_by(
            (MatterDefinition.key == LEGACY_GUIDANCE_KEY).desc(),
            MatterDefinition.created_at,
            MatterDefinition.id,
        )
    )
    if for_update:
        statement = statement.with_for_update()
    definitions = list(db.scalars(statement))
    if not definitions:
        return None
    if definitions[0].key == LEGACY_GUIDANCE_KEY:
        return definitions[0]
    if len(definitions) == 1:
        return definitions[0]
    raise MatterDefinitionError(
        "This matter has multiple guidance profiles; select one explicitly"
    )


def current_guidance_revision(
    db: Session,
    definition: MatterDefinition,
) -> MatterDefinitionRevision:
    revision = db.scalar(
        select(MatterDefinitionRevision).where(
            MatterDefinitionRevision.matter_definition_id == definition.id,
            MatterDefinitionRevision.revision == definition.current_revision,
        )
    )
    if revision is None:
        raise MatterDefinitionError("Review Guidance has no current revision")
    return revision


def create_guidance(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    key: str,
    name: str,
    description: str | None,
    content_markdown: str,
    source_kind: str,
    source_filename: str | None = None,
    source_guidance_id: uuid.UUID | None = None,
    source_revision_id: uuid.UUID | None = None,
    source_content_hash: str | None = None,
) -> tuple[MatterDefinition, MatterDefinitionRevision]:
    existing = db.scalar(
        select(MatterDefinition.id).where(
            MatterDefinition.matter_id == matter.id,
            MatterDefinition.key == key,
        )
    )
    if existing is not None:
        raise MatterDefinitionError(f"Review Guidance key '{key}' already exists")
    is_first_guidance = db.scalar(
        select(MatterDefinition.id)
        .where(MatterDefinition.matter_id == matter.id)
        .limit(1)
    ) is None
    definition = MatterDefinition(
        matter_id=matter.id,
        key=key,
        name=name,
        description=description,
        status="ACTIVE",
        current_revision=1,
        created_by_user_id=actor_user_id,
    )
    db.add(definition)
    db.flush()
    revision = MatterDefinitionRevision(
        matter_definition_id=definition.id,
        revision=1,
        content_markdown=content_markdown,
        source_kind=source_kind,
        source_filename=source_filename,
        based_on_revision=None,
        created_by_user_id=actor_user_id,
        source_guidance_id=source_guidance_id,
        source_revision_id=source_revision_id,
        source_content_hash=source_content_hash,
    )
    db.add(revision)
    claimed_conversation_count = 0
    if is_first_guidance:
        result = db.execute(
            update(AgentConversation)
            .where(
                AgentConversation.matter_id == matter.id,
                AgentConversation.workflow_type == "MATTER_DEFINITION_SETUP",
                AgentConversation.matter_definition_id.is_(None),
            )
            .values(matter_definition_id=definition.id)
        )
        claimed_conversation_count = result.rowcount
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=actor_user_id,
        action="matter_definition.guidance.created",
        target_type="matter_definition",
        target_id=definition.id,
        details={
            "matter_id": str(matter.id),
            "guidance_id": str(definition.id),
            "guidance_key": definition.key,
            "revision": 1,
            "source_kind": source_kind,
            "source_guidance_id": str(source_guidance_id) if source_guidance_id else None,
            "source_revision_id": str(source_revision_id) if source_revision_id else None,
            "claimed_conversation_count": claimed_conversation_count,
        },
    )
    db.flush()
    return definition, revision


def append_matter_definition_revision(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    content_markdown: str,
    source_kind: str,
    based_on_revision: int | None,
    matter_definition_id: uuid.UUID | None = None,
    source_artifact_id: uuid.UUID | None = None,
    source_filename: str | None = None,
    agent_run_id: uuid.UUID | None = None,
    source_skill_run_id: uuid.UUID | None = None,
) -> tuple[MatterDefinition, MatterDefinitionRevision]:
    if matter_definition_id is None:
        definition = resolve_legacy_guidance(db, matter_id=matter.id, for_update=True)
    else:
        definition = get_guidance(
            db,
            matter_id=matter.id,
            guidance_id=matter_definition_id,
            for_update=True,
        )
    if definition is None:
        if based_on_revision is not None:
            raise MatterDefinitionError("The Matter Definition does not have a prior revision")
        return create_guidance(
            db,
            matter=matter,
            actor_user_id=actor_user_id,
            key=LEGACY_GUIDANCE_KEY,
            name=LEGACY_GUIDANCE_NAME,
            description="Primary reviewer guidance for this matter.",
            content_markdown=content_markdown,
            source_kind=source_kind,
            source_filename=source_filename,
        )
    if definition.status != "ACTIVE":
        raise MatterDefinitionError("Archived Review Guidance cannot be edited")
    if based_on_revision is not None and based_on_revision != definition.current_revision:
        raise MatterDefinitionError(
            f"Review Guidance changed; current revision is {definition.current_revision}"
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
        source_skill_run_id=source_skill_run_id,
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
            "guidance_id": str(definition.id),
            "guidance_key": definition.key,
            "revision": next_revision,
            "source_kind": source_kind,
            "agent_run_id": str(agent_run_id) if agent_run_id else None,
        },
    )
    db.flush()
    return definition, revision


def clone_guidance(
    db: Session,
    *,
    matter: Matter,
    source: MatterDefinition,
    actor_user_id: uuid.UUID,
    key: str,
    name: str,
    description: str | None,
    source_revision_number: int | None,
) -> tuple[MatterDefinition, MatterDefinitionRevision]:
    revision_number = source_revision_number or source.current_revision
    source_revision = db.scalar(
        select(MatterDefinitionRevision).where(
            MatterDefinitionRevision.matter_definition_id == source.id,
            MatterDefinitionRevision.revision == revision_number,
        )
    )
    if source_revision is None:
        raise MatterDefinitionError("Source guidance revision not found")
    return create_guidance(
        db,
        matter=matter,
        actor_user_id=actor_user_id,
        key=key,
        name=name,
        description=description,
        content_markdown=source_revision.content_markdown,
        source_kind="CLONE",
        source_guidance_id=source.id,
        source_revision_id=source_revision.id,
        source_content_hash=hashlib.sha256(source_revision.content_markdown.encode()).hexdigest(),
    )
