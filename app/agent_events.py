import asyncio
import contextlib
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models import AgentConversation, AgentConversationEvent, AgentConversationEventCursor, utcnow

logger = logging.getLogger(__name__)
NOTIFY_CHANNEL = "agent_conversation_events"


def _bounded_payload(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, default=str, separators=(",", ":")).encode()
    if len(encoded) > get_settings().agent_stream_event_payload_max_bytes:
        raise ValueError("Agent conversation event payload exceeds the configured limit")
    return payload


def publish_agent_event(
    db: Session,
    conversation: AgentConversation,
    event_type: str,
    *,
    turn_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
    action_request_id: uuid.UUID | None = None,
    attempt_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AgentConversationEvent:
    """Append one lifecycle event using a transactionally serialized matter cursor."""

    db.flush()
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            postgresql_insert(AgentConversationEventCursor)
            .values(matter_id=conversation.matter_id, newest_sequence=0, oldest_sequence=1)
            .on_conflict_do_nothing(index_elements=[AgentConversationEventCursor.matter_id])
        )
    elif db.get(AgentConversationEventCursor, conversation.matter_id) is None:
        db.add(
            AgentConversationEventCursor(
                matter_id=conversation.matter_id,
                newest_sequence=0,
                oldest_sequence=1,
            )
        )
        db.flush()

    cursor = db.scalar(
        select(AgentConversationEventCursor)
        .where(AgentConversationEventCursor.matter_id == conversation.matter_id)
        .with_for_update()
    )
    if cursor is None:
        raise RuntimeError("Agent conversation event cursor could not be allocated")
    cursor.newest_sequence += 1
    event = AgentConversationEvent(
        tenant_id=conversation.tenant_id,
        matter_id=conversation.matter_id,
        conversation_id=conversation.id,
        workflow_type=conversation.workflow_type,
        review_batch_id=conversation.review_batch_id,
        matter_sequence=cursor.newest_sequence,
        event_type=event_type,
        schema_version=1,
        turn_id=turn_id,
        agent_run_id=run_id,
        message_id=message_id,
        action_request_id=action_request_id,
        attempt_id=attempt_id,
        payload=_bounded_payload(payload or {}),
    )
    db.add(event)
    db.flush()
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {
                "channel": NOTIFY_CHANNEL,
                "payload": json.dumps(
                    {"matter_id": str(conversation.matter_id), "sequence": cursor.newest_sequence},
                    separators=(",", ":"),
                ),
            },
        )
    return event


def cleanup_agent_events(retention_days: int) -> int:
    """Remove expired delivery records and advance each matter's retained-sequence floor atomically."""

    cutoff = utcnow() - timedelta(days=retention_days)
    deleted = 0
    with SessionLocal() as db:
        cursors = list(db.scalars(select(AgentConversationEventCursor).with_for_update()))
        for cursor in cursors:
            result = db.execute(
                delete(AgentConversationEvent).where(
                    AgentConversationEvent.matter_id == cursor.matter_id,
                    AgentConversationEvent.created_at < cutoff,
                )
            )
            deleted += result.rowcount or 0
            first_retained = db.scalar(
                select(func.min(AgentConversationEvent.matter_sequence)).where(
                    AgentConversationEvent.matter_id == cursor.matter_id
                )
            )
            cursor.oldest_sequence = int(first_retained or cursor.newest_sequence + 1)
        db.commit()
    return deleted


@dataclass(eq=False)
class AgentEventSubscription:
    matter_id: uuid.UUID
    queue: asyncio.Queue[None]


class AgentEventHub:
    """One PostgreSQL listener per API process, with local wake-up fan-out."""

    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, set[AgentEventSubscription]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._settings: Settings | None = None

    async def start(self, settings: Settings) -> None:
        self._settings = settings
        if not settings.agent_streaming_enabled or not settings.database_url.startswith("postgresql"):
            return
        if self._task is None:
            self._task = asyncio.create_task(self._listen(), name="agent-event-listener")
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup(), name="agent-event-cleanup")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        cleanup_task, self._cleanup_task = self._cleanup_task, None
        if cleanup_task is not None:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task

    def subscribe(self, matter_id: uuid.UUID) -> AgentEventSubscription:
        size = (self._settings or get_settings()).agent_stream_subscriber_queue_size
        subscription = AgentEventSubscription(matter_id=matter_id, queue=asyncio.Queue(maxsize=size))
        self._subscribers[matter_id].add(subscription)
        return subscription

    def unsubscribe(self, subscription: AgentEventSubscription) -> None:
        subscriptions = self._subscribers.get(subscription.matter_id)
        if subscriptions is None:
            return
        subscriptions.discard(subscription)
        if not subscriptions:
            self._subscribers.pop(subscription.matter_id, None)

    def wake(self, matter_id: uuid.UUID) -> None:
        for subscription in tuple(self._subscribers.get(matter_id, ())):
            if subscription.queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    subscription.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                subscription.queue.put_nowait(None)

    async def _listen(self) -> None:
        assert self._settings is not None
        database_url = make_url(self._settings.database_url).set(drivername="postgresql")
        conninfo = database_url.render_as_string(hide_password=False)
        backoff = 1.0
        while True:
            try:
                connection = await psycopg.AsyncConnection.connect(conninfo, autocommit=True)
                async with connection:
                    async with connection.cursor() as cursor:
                        await cursor.execute(f"LISTEN {NOTIFY_CHANNEL}")
                    backoff = 1.0
                    for matter_id in tuple(self._subscribers):
                        self.wake(matter_id)
                    async for notification in connection.notifies():
                        try:
                            payload = json.loads(notification.payload)
                            self.wake(uuid.UUID(payload["matter_id"]))
                        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                            logger.warning("Ignored malformed agent event notification")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Agent event listener disconnected; retrying")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _cleanup(self) -> None:
        assert self._settings is not None
        while True:
            await asyncio.sleep(self._settings.agent_stream_cleanup_interval_seconds)
            try:
                deleted = await asyncio.to_thread(
                    cleanup_agent_events,
                    self._settings.agent_stream_event_retention_days,
                )
                if deleted:
                    logger.info("Removed expired agent conversation events count=%s", deleted)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Agent conversation event cleanup failed")


agent_event_hub = AgentEventHub()


@contextlib.asynccontextmanager
async def agent_event_lifespan(_: Any = None) -> AsyncIterator[None]:
    await agent_event_hub.start(get_settings())
    try:
        yield
    finally:
        await agent_event_hub.stop()
