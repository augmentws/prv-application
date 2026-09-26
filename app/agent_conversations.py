import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent_events import publish_agent_event
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
    ReviewBatch,
)
from app.workflows.dispatcher import enqueue_agent_run


class AgentConversationError(ValueError):
    pass


def create_conversation(
    db: Session,
    *,
    matter: Matter,
    agent: AgentDefinition,
    title: str | None,
    workflow_type: str,
    review_batch_id: uuid.UUID | None,
    actor_user_id: uuid.UUID,
) -> AgentConversation:
    if agent.status != "ACTIVE" or agent.published_version is None:
        raise AgentConversationError("Agent must have an active published version")
    if not agent_supports_workflow(agent, workflow_type):
        raise AgentConversationError("Agent is not compatible with this workflow")
    if agent.scope == "TENANT" and agent.owner_tenant_id != matter.client.tenant_id:
        raise AgentConversationError("Tenant agent is not available to this matter")
    if workflow_type == "BATCH_CHAT":
        batch = db.get(ReviewBatch, review_batch_id) if review_batch_id is not None else None
        if batch is None or batch.matter_id != matter.id:
            raise AgentConversationError("Review batch is not available to this matter")
        if batch.status != "READY":
            raise AgentConversationError("Review batch must be ready before starting a chat")
    elif review_batch_id is not None:
        raise AgentConversationError("Review batch scope is supported only for batch chat")
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
        review_batch_id=review_batch_id,
        agent_definition_id=agent.id,
        agent_definition_version_id=version.id,
        title=title,
        workflow_type=workflow_type,
        status="ACTIVE",
        initiated_by_user_id=actor_user_id,
    )
    db.add(conversation)
    db.flush()
    publish_agent_event(
        db,
        conversation,
        "conversation.created",
        payload={"title": conversation.title, "status": conversation.status},
    )
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
    # COMPLETED is accepted only while rolling deployments migrate legacy rows to ACTIVE.
    if conversation.status not in {"ACTIVE", "COMPLETED", "FAILED"}:
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
    publish_agent_event(db, conversation, "turn.created", turn_id=turn.id, payload={"status": turn.status})
    publish_agent_event(
        db,
        conversation,
        "message.created",
        turn_id=turn.id,
        message_id=user_message.id,
        payload={"role": user_message.role, "sequence": user_message.sequence},
    )
    publish_agent_event(
        db,
        conversation,
        "run.created",
        turn_id=turn.id,
        run_id=run.id,
        payload={"status": run.status, "sequence": run.sequence},
    )
    publish_agent_event(
        db,
        conversation,
        "conversation.updated",
        payload={"status": conversation.status},
    )
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
    conversation = db.get(AgentConversation, action_request.conversation_id)
    if conversation is None:
        raise AgentConversationError("Agent conversation is not available")
    publish_agent_event(
        db,
        conversation,
        "action_request.updated",
        turn_id=action_request.turn_id,
        run_id=action_request.agent_run_id,
        action_request_id=action_request.id,
        payload={"status": action_request.status},
    )

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
    if conversation is None:
        raise AgentConversationError("Agent conversation is not available")
    publish_agent_event(
        db,
        conversation,
        "run.created",
        turn_id=resumed_run.turn_id,
        run_id=resumed_run.id,
        payload={"status": resumed_run.status, "sequence": resumed_run.sequence},
    )
    if turn is not None:
        publish_agent_event(
            db,
            conversation,
            "turn.updated",
            turn_id=turn.id,
            payload={"status": turn.status},
        )
    publish_agent_event(
        db,
        conversation,
        "conversation.updated",
        payload={"status": conversation.status},
    )
    enqueue_agent_run(db, resumed_run.workflow_id, str(resumed_run.id))
    return decision, resumed_run
