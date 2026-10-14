from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import AgentDefinition, AgentDefinitionVersion, AgentRun, MetadataEvent, User

USER_ACTOR_PREFIX = "user:"
AGENT_ACTOR_PREFIX = "agent:"

_AUTOMATED_AGENT_LABELS = {
    "analysis-task-promotion": "Jev analysis task",
    "topic-job": "Topic assignment agent",
    "automated": "Automated coding agent",
}


def _agent_actor_key(agent_definition_id: uuid.UUID | None, source_id: str | None) -> str:
    if agent_definition_id is not None:
        return f"{AGENT_ACTOR_PREFIX}{agent_definition_id}"
    source_kind = (source_id or "automated").partition(":")[0].strip().casefold() or "automated"
    return f"{AGENT_ACTOR_PREFIX}{source_kind}"


def coded_by_tokens_for_documents(
    db: Session,
    document_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[str]]:
    """Return stable coding actors for a document page in one database query."""

    tokens: dict[uuid.UUID, set[str]] = {document_id: set() for document_id in document_ids}
    if not document_ids:
        return {}
    rows = db.execute(
        select(
            MetadataEvent.matter_document_id,
            MetadataEvent.source_type,
            User.normalized_email,
            AgentDefinition.id,
            MetadataEvent.source_id,
        )
        .select_from(MetadataEvent)
        .outerjoin(User, User.id == MetadataEvent.actor_id)
        .outerjoin(AgentRun, AgentRun.id == MetadataEvent.agent_run_id)
        .outerjoin(
            AgentDefinitionVersion,
            AgentDefinitionVersion.id == AgentRun.agent_definition_version_id,
        )
        .outerjoin(
            AgentDefinition,
            AgentDefinition.id == AgentDefinitionVersion.agent_definition_id,
        )
        .where(
            MetadataEvent.matter_document_id.in_(document_ids),
            MetadataEvent.source_type.in_(["HUMAN", "AGENT"]),
        )
        .distinct()
    ).all()
    for document_id, source_type, normalized_email, agent_definition_id, source_id in rows:
        if source_type == "HUMAN" and normalized_email:
            tokens[document_id].add(f"{USER_ACTOR_PREFIX}{normalized_email}")
        elif source_type == "AGENT":
            tokens[document_id].add(_agent_actor_key(agent_definition_id, source_id))
    return {document_id: sorted(values) for document_id, values in tokens.items()}


def coded_by_tokens(db: Session, document_id: uuid.UUID) -> list[str]:
    """Return stable actors that have ever changed asserted coding on a document."""

    return coded_by_tokens_for_documents(db, [document_id])[document_id]


def coded_by_labels(db: Session, tokens: list[str]) -> dict[str, str]:
    user_emails = [token.removeprefix(USER_ACTOR_PREFIX) for token in tokens if token.startswith(USER_ACTOR_PREFIX)]
    agent_ids: list[uuid.UUID] = []
    for token in tokens:
        if not token.startswith(AGENT_ACTOR_PREFIX):
            continue
        try:
            agent_ids.append(uuid.UUID(token.removeprefix(AGENT_ACTOR_PREFIX)))
        except ValueError:
            pass

    labels = {
        f"{USER_ACTOR_PREFIX}{user.normalized_email}": f"{user.display_name} ({user.email})"
        for user in db.scalars(select(User).where(User.normalized_email.in_(user_emails)))
    } if user_emails else {}
    if agent_ids:
        labels.update(
            {
                f"{AGENT_ACTOR_PREFIX}{agent.id}": f"{agent.name} (Agent)"
                for agent in db.scalars(select(AgentDefinition).where(AgentDefinition.id.in_(agent_ids)))
            }
        )
    for token in tokens:
        if token.startswith(AGENT_ACTOR_PREFIX) and token not in labels:
            key = token.removeprefix(AGENT_ACTOR_PREFIX)
            labels[token] = f"{_AUTOMATED_AGENT_LABELS.get(key, key.replace('-', ' ').title())} (Agent)"
    return labels


def matching_coded_by_tokens(db: Session, tenant_id: uuid.UUID, query: str) -> list[str]:
    needle = query.strip().casefold()
    if not needle:
        return []
    pattern = f"%{needle}%"
    tokens = {
        f"{USER_ACTOR_PREFIX}{user.normalized_email}"
        for user in db.scalars(
            select(User)
            .where(
                User.tenant_id == tenant_id,
                or_(
                    func.lower(User.display_name).like(pattern),
                    func.lower(User.email).like(pattern),
                ),
            )
            .limit(500)
        )
    }
    tokens.update(
        f"{AGENT_ACTOR_PREFIX}{agent.id}"
        for agent in db.scalars(
            select(AgentDefinition)
            .where(func.lower(AgentDefinition.name).like(pattern))
            .limit(500)
        )
    )
    tokens.update(
        f"{AGENT_ACTOR_PREFIX}{key}"
        for key, label in _AUTOMATED_AGENT_LABELS.items()
        if needle in label.casefold()
    )
    return sorted(tokens)
