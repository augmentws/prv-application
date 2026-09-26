import asyncio
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.agent_conversations import AgentConversationError, create_conversation, create_turn, decide_action
from app.agent_events import agent_event_hub, publish_agent_event
from app.audit import record_audit
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.models import (
    AgentActionRequest,
    AgentConversation,
    AgentConversationEvent,
    AgentConversationEventCursor,
    AgentDefinition,
    AgentMessage,
    AgentRun,
    AgentTurn,
    Matter,
    ReviewBatch,
)
from app.schemas import (
    AgentActionDecisionCreate,
    AgentActionDecisionRead,
    AgentActionDecisionResult,
    AgentActionRequestRead,
    AgentConversationCreate,
    AgentConversationRead,
    AgentConversationUpdate,
    AgentConversationWorkflow,
    AgentMessageRead,
    AgentRunRead,
    AgentTurnCreate,
    AgentTurnCreated,
    AgentTurnRead,
)

router = APIRouter(prefix="/v1", tags=["agent conversations"])


def _matter_admin(db: Session, principal: Principal, matter_id: uuid.UUID) -> Matter:
    matter = db.scalar(select(Matter).where(Matter.id == matter_id))
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    return matter


def _conversation_admin(
    db: Session,
    principal: Principal,
    conversation_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> AgentConversation:
    query = select(AgentConversation).where(AgentConversation.id == conversation_id)
    if for_update:
        query = query.with_for_update()
    conversation = db.scalar(query)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent conversation not found")
    _matter_admin(db, principal, conversation.matter_id)
    return conversation


@router.post(
    "/matters/{matter_id}/agent-conversations",
    response_model=AgentConversationRead,
    status_code=status.HTTP_201_CREATED,
)
def start_agent_conversation(
    matter_id: uuid.UUID,
    payload: AgentConversationCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AgentConversation:
    matter = _matter_admin(db, principal, matter_id)
    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.id == payload.agent_definition_id))
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    try:
        conversation = create_conversation(
            db,
            matter=matter,
            agent=agent,
            title=payload.title,
            workflow_type=payload.workflow_type,
            review_batch_id=payload.review_batch_id,
            actor_user_id=principal.user.id,
        )
    except AgentConversationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="agent_conversation.created",
        target_type="agent_conversation",
        target_id=conversation.id,
        details={
            "matter_id": str(matter.id),
            "agent_definition_id": str(agent.id),
            "agent_definition_version_id": str(conversation.agent_definition_version_id),
            "title": conversation.title,
            "workflow_type": payload.workflow_type,
            "review_batch_id": str(payload.review_batch_id) if payload.review_batch_id else None,
        },
    )
    db.commit()
    return conversation


@router.get("/matters/{matter_id}/agent-conversations", response_model=list[AgentConversationRead])
def list_agent_conversations(
    matter_id: uuid.UUID,
    workflow_type: AgentConversationWorkflow | None = Query(default=None),
    review_batch_id: uuid.UUID | None = Query(default=None),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[AgentConversation]:
    _matter_admin(db, principal, matter_id)
    query = select(AgentConversation).where(AgentConversation.matter_id == matter_id)
    if workflow_type is not None:
        query = query.where(AgentConversation.workflow_type == workflow_type)
    if review_batch_id is not None:
        query = query.where(AgentConversation.review_batch_id == review_batch_id)
    return list(db.scalars(query.order_by(AgentConversation.created_at.desc())))


@router.get("/agent-conversations/{conversation_id}", response_model=AgentConversationRead)
def get_agent_conversation(
    conversation_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AgentConversation:
    return _conversation_admin(db, principal, conversation_id)


@router.patch("/agent-conversations/{conversation_id}", response_model=AgentConversationRead)
def update_agent_conversation(
    conversation_id: uuid.UUID,
    payload: AgentConversationUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AgentConversation:
    conversation = _conversation_admin(db, principal, conversation_id, for_update=True)
    previous_title = conversation.title
    conversation.title = payload.title
    publish_agent_event(
        db,
        conversation,
        "conversation.updated",
        payload={"title": conversation.title, "status": conversation.status},
    )
    record_audit(
        db,
        tenant_id=conversation.tenant_id,
        actor_user_id=principal.user.id,
        action="agent_conversation.renamed",
        target_type="agent_conversation",
        target_id=conversation.id,
        details={"previous_title": previous_title, "title": conversation.title},
    )
    db.commit()
    return conversation


def _sse_frame(event: str, data: dict[str, object], *, event_id: int | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    encoded = json.dumps(data, separators=(",", ":"), default=str)
    lines.extend(f"data: {line}" for line in encoded.splitlines() or [""])
    return "\n".join(lines) + "\n\n"


@router.get(
    "/matters/{matter_id}/agent-events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream_agent_events(
    matter_id: uuid.UUID,
    request: Request,
    workflow_type: AgentConversationWorkflow = Query(),
    review_batch_id: uuid.UUID | None = Query(default=None),
    after: int | None = Query(default=None, ge=0),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    if not settings.agent_streaming_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent event streaming is disabled")
    _matter_admin(db, principal, matter_id)
    if workflow_type == "BATCH_CHAT":
        batch = db.get(ReviewBatch, review_batch_id) if review_batch_id is not None else None
        if batch is None or batch.matter_id != matter_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review batch not found")
    elif review_batch_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="review_batch_id is supported only for BATCH_CHAT streams",
        )

    header_cursor = request.headers.get("last-event-id")
    try:
        parsed_header_cursor = int(header_cursor) if header_cursor is not None else None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Last-Event-ID must be an integer") from exc
    if parsed_header_cursor is not None and parsed_header_cursor < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Last-Event-ID must not be negative")
    if after is not None and parsed_header_cursor is not None and after != parsed_header_cursor:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Conflicting stream cursors")
    requested_cursor = after if after is not None else parsed_header_cursor
    stream_session = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)

    def cursor_state() -> tuple[int, int]:
        with stream_session() as stream_db:
            cursor = stream_db.get(AgentConversationEventCursor, matter_id)
            return (cursor.newest_sequence, cursor.oldest_sequence) if cursor is not None else (0, 1)

    def read_events(start: int, through: int) -> list[AgentConversationEvent]:
        with stream_session() as stream_db:
            query = select(AgentConversationEvent).where(
                AgentConversationEvent.matter_id == matter_id,
                AgentConversationEvent.workflow_type == workflow_type,
                AgentConversationEvent.matter_sequence > start,
                AgentConversationEvent.matter_sequence <= through,
            )
            if review_batch_id is None:
                query = query.where(AgentConversationEvent.review_batch_id.is_(None))
            else:
                query = query.where(AgentConversationEvent.review_batch_id == review_batch_id)
            return list(
                stream_db.scalars(
                    query.order_by(AgentConversationEvent.matter_sequence).limit(settings.agent_stream_replay_page_size)
                )
            )

    async def generate():
        subscription = agent_event_hub.subscribe(matter_id)
        cursor = requested_cursor
        replayed = 0
        deadline = time.monotonic() + settings.agent_stream_max_lifetime_seconds
        try:
            high_water, oldest = await asyncio.to_thread(cursor_state)
            if cursor is None:
                cursor = high_water
                yield _sse_frame("stream.ready", {"schema_version": 1, "matter_sequence": cursor})
            elif cursor > high_water:
                yield _sse_frame(
                    "snapshot.required",
                    {"schema_version": 1, "matter_sequence": high_water, "reason": "cursor_ahead"},
                )
                return
            elif cursor < oldest - 1:
                yield _sse_frame(
                    "snapshot.required",
                    {"schema_version": 1, "matter_sequence": high_water, "reason": "cursor_expired"},
                )
                return

            while True:
                high_water, oldest = await asyncio.to_thread(cursor_state)
                if cursor < oldest - 1:
                    yield _sse_frame(
                        "snapshot.required",
                        {"schema_version": 1, "matter_sequence": high_water, "reason": "cursor_expired"},
                    )
                    return
                while cursor < high_water:
                    events = await asyncio.to_thread(read_events, cursor, high_water)
                    if not events:
                        cursor = high_water
                        break
                    for event in events:
                        replayed += 1
                        if replayed > settings.agent_stream_replay_limit:
                            yield _sse_frame(
                                "snapshot.required",
                                {"schema_version": 1, "matter_sequence": high_water, "reason": "replay_limit"},
                            )
                            return
                        cursor = event.matter_sequence
                        yield _sse_frame(
                            event.event_type,
                            {
                                "schema_version": event.schema_version,
                                "matter_sequence": event.matter_sequence,
                                "conversation_id": str(event.conversation_id),
                                "turn_id": str(event.turn_id) if event.turn_id else None,
                                "run_id": str(event.agent_run_id) if event.agent_run_id else None,
                                "message_id": str(event.message_id) if event.message_id else None,
                                "action_request_id": str(event.action_request_id) if event.action_request_id else None,
                                "payload": event.payload,
                            },
                            event_id=event.matter_sequence,
                        )
                    if len(events) < settings.agent_stream_replay_page_size:
                        cursor = high_water
                        break
                yield _sse_frame("stream.checkpoint", {"schema_version": 1, "matter_sequence": cursor})
                if await request.is_disconnected():
                    return
                if time.monotonic() >= deadline:
                    yield _sse_frame("stream.rotate", {"schema_version": 1, "matter_sequence": cursor})
                    return
                try:
                    await asyncio.wait_for(
                        subscription.queue.get(),
                        timeout=min(
                            settings.agent_stream_heartbeat_seconds,
                            max(0.1, deadline - time.monotonic()),
                        ),
                    )
                except TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            agent_event_hub.unsubscribe(subscription)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/agent-conversations/{conversation_id}/turns",
    response_model=AgentTurnCreated,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_agent_turn(
    conversation_id: uuid.UUID,
    payload: AgentTurnCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AgentTurnCreated:
    if settings.dbos_enabled and settings.agent_default_model is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent execution is unavailable until AGENT_DEFAULT_MODEL is configured",
        )
    conversation = _conversation_admin(db, principal, conversation_id, for_update=True)
    try:
        turn, message, run = create_turn(
            db,
            conversation=conversation,
            message=payload.message,
            actor_user_id=principal.user.id,
        )
    except AgentConversationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=conversation.tenant_id,
        actor_user_id=principal.user.id,
        action="agent_turn.queued",
        target_type="agent_turn",
        target_id=turn.id,
        details={"conversation_id": str(conversation.id), "run_id": str(run.id)},
    )
    db.commit()
    return AgentTurnCreated(
        turn=AgentTurnRead.model_validate(turn),
        message=AgentMessageRead.model_validate(message),
        run=AgentRunRead.model_validate(run),
    )


@router.get("/agent-conversations/{conversation_id}/turns", response_model=list[AgentTurnRead])
def list_agent_turns(
    conversation_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[AgentTurn]:
    conversation = _conversation_admin(db, principal, conversation_id)
    return list(
        db.scalars(select(AgentTurn).where(AgentTurn.conversation_id == conversation.id).order_by(AgentTurn.sequence))
    )


@router.get("/agent-conversations/{conversation_id}/messages", response_model=list[AgentMessageRead])
def list_agent_messages(
    conversation_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[AgentMessage]:
    conversation = _conversation_admin(db, principal, conversation_id)
    return list(
        db.scalars(
            select(AgentMessage)
            .where(AgentMessage.conversation_id == conversation.id)
            .order_by(AgentMessage.sequence)
        )
    )


@router.get("/agent-conversations/{conversation_id}/runs", response_model=list[AgentRunRead])
def list_agent_runs(
    conversation_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[AgentRun]:
    conversation = _conversation_admin(db, principal, conversation_id)
    return list(
        db.scalars(
            select(AgentRun)
            .where(AgentRun.conversation_id == conversation.id)
            .order_by(AgentRun.created_at, AgentRun.sequence)
        )
    )


@router.get(
    "/agent-conversations/{conversation_id}/action-requests",
    response_model=list[AgentActionRequestRead],
)
def list_agent_action_requests(
    conversation_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[AgentActionRequest]:
    conversation = _conversation_admin(db, principal, conversation_id)
    return list(
        db.scalars(
            select(AgentActionRequest)
            .where(AgentActionRequest.conversation_id == conversation.id)
            .order_by(AgentActionRequest.requested_at)
        )
    )


@router.post("/agent-action-requests/{action_request_id}/decision", response_model=AgentActionDecisionResult)
def submit_agent_action_decision(
    action_request_id: uuid.UUID,
    payload: AgentActionDecisionCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AgentActionDecisionResult:
    action_request = db.scalar(
        select(AgentActionRequest).where(AgentActionRequest.id == action_request_id).with_for_update()
    )
    if action_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent action request not found")
    conversation = _conversation_admin(db, principal, action_request.conversation_id, for_update=True)
    try:
        decision, resumed_run = decide_action(
            db,
            action_request=action_request,
            decision_value=payload.decision,
            reason=payload.reason,
            actor_user_id=principal.user.id,
        )
    except AgentConversationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=conversation.tenant_id,
        actor_user_id=principal.user.id,
        action="agent_action.decided",
        target_type="agent_action_request",
        target_id=action_request.id,
        details={
            "decision": payload.decision,
            "tool_key": action_request.tool_key,
            "resumed_run_id": str(resumed_run.id) if resumed_run else None,
        },
    )
    db.commit()
    return AgentActionDecisionResult(
        action_request=AgentActionRequestRead.model_validate(action_request),
        decision=AgentActionDecisionRead.model_validate(decision),
        resumed_run=AgentRunRead.model_validate(resumed_run) if resumed_run else None,
    )
