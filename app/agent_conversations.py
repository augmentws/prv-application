import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent_workflows import agent_supports_workflow
from app.models import (
    AgentActionDecision,
    AgentActionRequest,
    AgentConversation,
    AgentDefinition,
    AgentDefinitionVersion,
    AgentMessage,
    AgentRun,
    AgentTurn,
    Matter,
)
from app.workflows.dispatcher import enqueue_agent_run


class AgentConversationError(ValueError):
    pass


def create_conversation(
    db: Session,
    *,
    matter: Matter,
    agent: AgentDefinition,
    workflow_type: str,
    actor_user_id: uuid.UUID,
) -> AgentConversation:
    if agent.status != "ACTIVE" or agent.published_version is None:
        raise AgentConversationError("Agent must have an active published version")
    if not agent_supports_workflow(agent, workflow_type):
        raise AgentConversationError("Agent is not compatible with this workflow")
    if agent.scope == "TENANT" and agent.owner_tenant_id != matter.client.tenant_id:
        raise AgentConversationError("Tenant agent is not available to this matter")
    version = db.scalar(
        select(AgentDefinitionVersion).where(
            AgentDefinitionVersion.agent_definition_id == agent.id,
            AgentDefinitionVersion.version == agent.published_version,
            AgentDefinitionVersion.status == "PUBLISHED",
        )
    )
    if version is None:
        raise AgentConversationError("Published agent version is not available")
    conversation = AgentConversation(
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        matter_id=matter.id,
        agent_definition_id=agent.id,
        agent_definition_version_id=version.id,
        workflow_type=workflow_type,
        status="ACTIVE",
        initiated_by_user_id=actor_user_id,
    )
    db.add(conversation)
    db.flush()
    return conversation


def create_turn(
    db: Session,
    *,
    conversation: AgentConversation,
    message: str,
    actor_user_id: uuid.UUID,
) -> tuple[AgentTurn, AgentMessage, AgentRun]:
    if conversation.status == "WAITING_APPROVAL":
        raise AgentConversationError("Resolve pending agent actions before sending another message")
    if conversation.status not in {"ACTIVE", "COMPLETED"}:
        raise AgentConversationError("Conversation is not available for a new turn")

    turn_sequence = (
        db.scalar(select(func.max(AgentTurn.sequence)).where(AgentTurn.conversation_id == conversation.id)) or 0
    ) + 1
    message_sequence = (
        db.scalar(select(func.max(AgentMessage.sequence)).where(AgentMessage.conversation_id == conversation.id)) or 0
    ) + 1
    parent_run = db.scalar(
        select(AgentRun)
        .where(AgentRun.conversation_id == conversation.id)
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(1)
    )
    version = db.get(AgentDefinitionVersion, conversation.agent_definition_version_id)
    if version is None:
        raise AgentConversationError("Pinned agent version is not available")

    turn = AgentTurn(
        conversation_id=conversation.id,
        sequence=turn_sequence,
        status="QUEUED",
        created_by_user_id=actor_user_id,
    )
    db.add(turn)
    db.flush()
    user_message = AgentMessage(
        conversation_id=conversation.id,
        turn_id=turn.id,
        sequence=message_sequence,
        role="USER",
        content=message,
        created_by_user_id=actor_user_id,
    )
    db.add(user_message)
    run_id = uuid.uuid4()
    run = AgentRun(
        id=run_id,
        conversation_id=conversation.id,
        turn_id=turn.id,
        sequence=1,
        workflow_id=f"agent-run:{run_id}",
        parent_run_id=parent_run.id if parent_run else None,
        agent_definition_version_id=conversation.agent_definition_version_id,
        actor_user_id=actor_user_id,
        model_key=version.model_key,
        status="QUEUED",
    )
    db.add(run)
    conversation.status = "ACTIVE"
    db.flush()
    enqueue_agent_run(db, run.workflow_id, str(run.id))
    return turn, user_message, run


def decide_action(
    db: Session,
    *,
    action_request: AgentActionRequest,
    decision_value: str,
    reason: str | None,
    actor_user_id: uuid.UUID,
) -> tuple[AgentActionDecision, AgentRun | None]:
    if action_request.status != "PENDING":
        raise AgentConversationError("Agent action has already been decided")
    existing = db.scalar(
        select(AgentActionDecision).where(AgentActionDecision.action_request_id == action_request.id)
    )
    if existing is not None:
        raise AgentConversationError("Agent action has already been decided")
    decision = AgentActionDecision(
        action_request_id=action_request.id,
        decision=decision_value,
        reason=reason,
        decided_by_user_id=actor_user_id,
    )
    db.add(decision)
    action_request.status = "APPROVED" if decision_value == "APPROVE" else "REJECTED"
    db.flush()

    pending_count = db.scalar(
        select(func.count())
        .select_from(AgentActionRequest)
        .where(
            AgentActionRequest.agent_run_id == action_request.agent_run_id,
            AgentActionRequest.status == "PENDING",
        )
    )
    if pending_count:
        return decision, None

    source_run = db.get(AgentRun, action_request.agent_run_id)
    if source_run is None:
        raise AgentConversationError("Source agent run is not available")
    existing_resume = db.scalar(
        select(AgentRun).where(AgentRun.deferred_from_run_id == source_run.id)
    )
    if existing_resume is not None:
        return decision, existing_resume
    run_id = uuid.uuid4()
    resumed_run = AgentRun(
        id=run_id,
        conversation_id=source_run.conversation_id,
        turn_id=source_run.turn_id,
        sequence=source_run.sequence + 1,
        workflow_id=f"agent-run:{run_id}",
        parent_run_id=source_run.id,
        deferred_from_run_id=source_run.id,
        agent_definition_version_id=source_run.agent_definition_version_id,
        actor_user_id=actor_user_id,
        model_key=source_run.model_key,
        status="QUEUED",
    )
    db.add(resumed_run)
    turn = db.get(AgentTurn, source_run.turn_id)
    conversation = db.get(AgentConversation, source_run.conversation_id)
    if turn is not None:
        turn.status = "QUEUED"
    if conversation is not None:
        conversation.status = "ACTIVE"
    db.flush()
    enqueue_agent_run(db, resumed_run.workflow_id, str(resumed_run.id))
    return decision, resumed_run
