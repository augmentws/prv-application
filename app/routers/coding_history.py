import json
import uuid
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import Principal, get_principal
from app.document_metadata import derive_event_states, event_value
from app.models import (
    AgentDefinition,
    AgentDefinitionVersion,
    AgentRun,
    MetadataDefinition,
    MetadataEvent,
    ReviewBatch,
    ReviewBatchDocument,
    User,
)
from app.routers.document_metadata import _authorized_document
from app.routers.review_batches import review_batch_document_coding_history
from app.schemas import (
    DirectCodingHistoryEntryRead,
    DocumentCodingHistoryBatchRead,
    DocumentCodingHistoryRead,
)

router = APIRouter(prefix="/v1/matters/{matter_id}/documents", tags=["document coding history"])


def _value_label(value: Any, definition: MetadataDefinition) -> str:
    if definition.type == "ENUM" and isinstance(value, str):
        for option in definition.allowed_values or []:
            if isinstance(option, dict) and option.get("key") == value:
                label = option.get("label")
                if isinstance(label, str) and label.strip():
                    return label
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None or value == "":
        return "Not set"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


@router.get("/{document_id}/coding-history", response_model=DocumentCodingHistoryRead)
def get_document_coding_history(
    matter_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> DocumentCodingHistoryRead:
    """Return authoritative direct coding first, then isolated history grouped by batch."""

    _authorized_document(db, principal, matter_id, document_id)
    direct_rows = db.execute(
        select(MetadataEvent, MetadataDefinition, User)
        .join(MetadataDefinition, MetadataDefinition.id == MetadataEvent.metadata_definition_id)
        .outerjoin(User, User.id == MetadataEvent.actor_id)
        .where(
            MetadataEvent.matter_id == matter_id,
            MetadataEvent.matter_document_id == document_id,
        )
        .order_by(MetadataEvent.created_at, MetadataEvent.id)
    ).all()

    events_by_definition: dict[uuid.UUID, list[MetadataEvent]] = defaultdict(list)
    for event, _, _ in direct_rows:
        events_by_definition[event.metadata_definition_id].append(event)
    states_by_definition = {
        definition_id: derive_event_states(events, next(
            definition
            for event, definition, _ in direct_rows
            if event.metadata_definition_id == definition_id
        ))
        for definition_id, events in events_by_definition.items()
    }

    agent_run_ids = {event.agent_run_id for event, _, _ in direct_rows if event.agent_run_id is not None}
    agent_sources: dict[uuid.UUID, tuple[str, str]] = {}
    if agent_run_ids:
        agent_sources = {
            run_id: (name, model_key)
            for run_id, name, model_key in db.execute(
                select(AgentRun.id, AgentDefinition.name, AgentRun.model_key)
                .join(AgentDefinitionVersion, AgentDefinitionVersion.id == AgentRun.agent_definition_version_id)
                .join(AgentDefinition, AgentDefinition.id == AgentDefinitionVersion.agent_definition_id)
                .where(AgentRun.id.in_(agent_run_ids))
            )
        }

    event_by_id = {event.id: event for event, _, _ in direct_rows}
    definition_by_event = {event.id: definition for event, definition, _ in direct_rows}
    direct: list[DirectCodingHistoryEntryRead] = []
    for event, definition, user in reversed(direct_rows):
        states = states_by_definition[event.metadata_definition_id]
        if event.id in states.invalidated_ids:
            effective_status = "INVALIDATED"
        elif event.id in states.rejected_ids:
            effective_status = "REJECTED"
        elif event.id in states.superseded_ids:
            effective_status = "SUPERSEDED"
        else:
            effective_status = "ACTIVE"
        if event.id in states.rejected_ids:
            confirmation_state = "REJECTED"
        elif event.id in states.confirmed_ids:
            confirmation_state = "CONFIRMED"
        else:
            confirmation_state = "UNREVIEWED"

        raw_value = event_value(event)
        if raw_value is None and event.target_event_id is not None:
            target = event_by_id.get(event.target_event_id)
            target_definition = definition_by_event.get(event.target_event_id, definition)
            target_label = _value_label(event_value(target), target_definition) if target is not None else "prior value"
            value_label = f"{event.operation.title()}: {target_label}"
        elif event.operation == "CLEAR":
            value_label = "Cleared"
        else:
            value_label = _value_label(raw_value, definition)

        if event.source_type == "HUMAN":
            source_label = user.email if user is not None else "Unknown user"
            source_detail = user.display_name if user is not None else event.source_id
        elif event.source_type == "AGENT":
            source_label, source_detail = agent_sources.get(event.agent_run_id, ("Agent", event.source_id))
        else:
            source_label = event.source_type.title()
            source_detail = event.source_id

        direct.append(
            DirectCodingHistoryEntryRead(
                metadata_event_id=event.id,
                metadata_definition_id=event.metadata_definition_id,
                field_key=definition.key,
                field_display_name=definition.display_name,
                operation=event.operation,
                value=raw_value,
                value_label=value_label,
                recorded_at=event.created_at,
                source_kind=event.source_type,
                source_label=source_label,
                source_detail=source_detail,
                score=event.confidence,
                effective_status=effective_status,
                confirmation_state=confirmation_state,
            )
        )

    batches = list(
        db.scalars(
            select(ReviewBatch)
            .join(ReviewBatchDocument, ReviewBatchDocument.review_batch_id == ReviewBatch.id)
            .where(
                ReviewBatch.matter_id == matter_id,
                ReviewBatchDocument.matter_document_id == document_id,
            )
            .order_by(ReviewBatch.created_at.desc(), ReviewBatch.id)
        )
    )
    return DocumentCodingHistoryRead(
        direct=direct,
        batches=[
            DocumentCodingHistoryBatchRead(
                review_batch_id=batch.id,
                batch_name=batch.name,
                batch_status=batch.status,
                entries=review_batch_document_coding_history(db, matter_id, batch, document_id),
            )
            for batch in batches
        ],
    )
