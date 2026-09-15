import json
import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import DocumentMetadataCurrent, MatterDocument, MetadataDefinition, MetadataEvent

ASSERTION_OPERATIONS = {"SET", "ADD", "CLEAR"}
VALUE_OPERATIONS = {"SET", "ADD"}
VALUE_COLUMNS = (
    "value_text",
    "value_long",
    "value_float",
    "value_boolean",
    "value_date",
    "value_datetime",
    "value_json",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ResolvedValue:
    event: MetadataEvent
    supporting_event_ids: list[uuid.UUID]


@dataclass(frozen=True)
class FieldResolution:
    state: str
    values: list[ResolvedValue]
    pending_events: list[MetadataEvent]
    conflicting_event_ids: list[uuid.UUID]


@dataclass(frozen=True)
class EventStates:
    rejected_ids: set[uuid.UUID]
    confirmed_ids: set[uuid.UUID]
    superseded_ids: set[uuid.UUID]
    invalidated_ids: set[uuid.UUID]


def event_value(event: MetadataEvent | DocumentMetadataCurrent) -> Any | None:
    for column in VALUE_COLUMNS:
        value = getattr(event, column)
        if value is not None:
            return value
    return None


def value_columns(value: Any, definition: MetadataDefinition) -> dict[str, Any]:
    columns = {column: None for column in VALUE_COLUMNS}
    if definition.type in {"TEXT", "LONG_TEXT", "ENUM"}:
        if not isinstance(value, str):
            raise ValueError(f"{definition.type} values must be strings")
        if definition.type == "TEXT" and len(value) > 4000:
            raise ValueError("TEXT values cannot exceed 4000 characters; use LONG_TEXT")
        if definition.type == "ENUM":
            allowed = {item["key"] for item in definition.allowed_values or [] if item.get("active", True)}
            if value not in allowed:
                raise ValueError("ENUM value must be an active allowed-value key")
        columns["value_text"] = value
    elif definition.type == "INTEGER":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("INTEGER values must be whole numbers")
        if not -(2**63) <= value < 2**63:
            raise ValueError("INTEGER value is outside the signed 64-bit range")
        columns["value_long"] = value
    elif definition.type == "DECIMAL":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("DECIMAL values must be numbers")
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("DECIMAL values must be finite")
        columns["value_float"] = converted
    elif definition.type == "BOOLEAN":
        if not isinstance(value, bool):
            raise ValueError("BOOLEAN values must be true or false")
        columns["value_boolean"] = value
    elif definition.type == "DATE":
        if isinstance(value, datetime):
            raise ValueError("DATE values cannot contain a time")
        if isinstance(value, str):
            try:
                value = date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("DATE values must use ISO YYYY-MM-DD format") from exc
        if not isinstance(value, date):
            raise ValueError("DATE values must use ISO YYYY-MM-DD format")
        columns["value_date"] = value
    elif definition.type == "DATETIME":
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("DATETIME values must use ISO 8601 format") from exc
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("DATETIME values must include a timezone")
        columns["value_datetime"] = value.astimezone(timezone.utc)
    elif definition.type == "JSON":
        if not isinstance(value, dict | list):
            raise ValueError("JSON values must be an object or array")
        columns["value_json"] = value
    else:
        raise ValueError(f"Unsupported metadata type: {definition.type}")
    return columns


def _event_order(event: MetadataEvent) -> tuple[datetime, str]:
    created_at = event.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at, str(event.id)


def _canonical(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        value = value.isoformat()
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def derive_event_states(events: list[MetadataEvent], definition: MetadataDefinition) -> EventStates:
    rejected_ids = {event.target_event_id for event in events if event.operation == "REJECT" and event.target_event_id}
    confirmed_ids = {
        event.target_event_id
        for event in events
        if event.operation == "CONFIRM" and event.target_event_id and event.target_event_id not in rejected_ids
    }
    superseded_ids = {event.supersedes_id for event in events if event.supersedes_id}
    removed_ids = {event.target_event_id for event in events if event.operation == "REMOVE" and event.target_event_id}
    active_assertions = [
        event
        for event in events
        if event.operation in ASSERTION_OPERATIONS
        and event.id not in rejected_ids
        and event.id not in superseded_ids
        and event.id not in removed_ids
    ]
    eligible_assertions = [
        event
        for event in active_assertions
        if definition.assertion_policy == "IMMEDIATE" or event.id in confirmed_ids
    ]
    clear_events = [event for event in eligible_assertions if event.operation == "CLEAR"]
    latest_clear = max(clear_events, key=_event_order) if clear_events else None
    invalidated_ids = set(removed_ids)
    if latest_clear is not None:
        invalidated_ids.update(
            event.id
            for event in events
            if event.operation in VALUE_OPERATIONS and _event_order(event) < _event_order(latest_clear)
        )
    return EventStates(
        rejected_ids=rejected_ids,
        confirmed_ids=confirmed_ids,
        superseded_ids=superseded_ids,
        invalidated_ids=invalidated_ids,
    )


def resolve_field(events: list[MetadataEvent], definition: MetadataDefinition) -> FieldResolution:
    states = derive_event_states(events, definition)
    active_assertions = [
        event
        for event in events
        if event.operation in ASSERTION_OPERATIONS
        and event.id not in states.rejected_ids
        and event.id not in states.superseded_ids
        and event.id not in states.invalidated_ids
    ]
    eligible = [
        event
        for event in active_assertions
        if definition.assertion_policy == "IMMEDIATE" or event.id in states.confirmed_ids
    ]
    pending = [
        event
        for event in active_assertions
        if definition.assertion_policy == "REQUIRES_CONFIRMATION" and event.id not in states.confirmed_ids
    ]
    eligible_clears = [event for event in eligible if event.operation == "CLEAR"]
    latest_clear = max(eligible_clears, key=_event_order) if eligible_clears else None
    candidates = [
        event
        for event in eligible
        if event.operation in VALUE_OPERATIONS
        and (latest_clear is None or _event_order(event) > _event_order(latest_clear))
    ]
    if not candidates:
        return FieldResolution(
            state="PENDING" if pending else "EMPTY",
            values=[ResolvedValue(event=event, supporting_event_ids=[event.id]) for event in pending],
            pending_events=pending,
            conflicting_event_ids=[],
        )

    if definition.cardinality == "MULTIPLE":
        grouped: dict[str, list[MetadataEvent]] = {}
        for event in candidates:
            grouped.setdefault(_canonical(event_value(event)), []).append(event)
        values = [
            ResolvedValue(
                event=max(supporting, key=_event_order),
                supporting_event_ids=[event.id for event in sorted(supporting, key=_event_order)],
            )
            for supporting in grouped.values()
        ]
        values.sort(key=lambda value: _canonical(event_value(value.event)))
        return FieldResolution(
            state="VALUE",
            values=values,
            pending_events=pending,
            conflicting_event_ids=[],
        )

    if definition.resolution_policy == "LATEST_VALID":
        chosen = max(candidates, key=_event_order)
        return FieldResolution(
            state="VALUE",
            values=[ResolvedValue(event=chosen, supporting_event_ids=[chosen.id])],
            pending_events=pending,
            conflicting_event_ids=[],
        )
    if definition.resolution_policy == "HUMAN_PRECEDENCE":
        human = [event for event in candidates if event.source_type == "HUMAN"]
        chosen = max(human or candidates, key=_event_order)
        return FieldResolution(
            state="VALUE",
            values=[ResolvedValue(event=chosen, supporting_event_ids=[chosen.id])],
            pending_events=pending,
            conflicting_event_ids=[],
        )

    grouped = {}
    for event in candidates:
        grouped.setdefault(_canonical(event_value(event)), []).append(event)
    values = [
        ResolvedValue(
            event=max(supporting, key=_event_order),
            supporting_event_ids=[event.id for event in sorted(supporting, key=_event_order)],
        )
        for supporting in grouped.values()
    ]
    values.sort(key=lambda value: _canonical(event_value(value.event)))
    if len(values) == 1:
        return FieldResolution(
            state="VALUE",
            values=values,
            pending_events=pending,
            conflicting_event_ids=[],
        )
    conflicting_ids = [event.id for event in sorted(candidates, key=_event_order)]
    return FieldResolution(
        state="CONFLICTED",
        values=values,
        pending_events=pending,
        conflicting_event_ids=conflicting_ids,
    )


def project_field(
    db: Session,
    document: MatterDocument,
    definition: MetadataDefinition,
) -> FieldResolution:
    events = list(
        db.scalars(
            select(MetadataEvent)
            .where(
                MetadataEvent.matter_document_id == document.id,
                MetadataEvent.metadata_definition_id == definition.id,
            )
            .order_by(MetadataEvent.created_at, MetadataEvent.id)
        )
    )
    resolution = resolve_field(events, definition)
    db.execute(
        delete(DocumentMetadataCurrent).where(
            DocumentMetadataCurrent.matter_document_id == document.id,
            DocumentMetadataCurrent.metadata_definition_id == definition.id,
        )
    )
    pending_ids = [str(event.id) for event in resolution.pending_events]
    conflicting_ids = [str(event_id) for event_id in resolution.conflicting_event_ids]
    projected = resolution.values
    if not projected:
        db.add(
            DocumentMetadataCurrent(
                matter_document_id=document.id,
                metadata_definition_id=definition.id,
                value_ordinal=0,
                matter_id=document.matter_id,
                resolution_state=resolution.state,
                source_event_id=None,
                supporting_event_ids=[],
                pending_event_ids=pending_ids,
                conflicting_event_ids=conflicting_ids,
            )
        )
    else:
        for ordinal, value in enumerate(projected):
            db.add(
                DocumentMetadataCurrent(
                    matter_document_id=document.id,
                    metadata_definition_id=definition.id,
                    value_ordinal=ordinal,
                    matter_id=document.matter_id,
                    resolution_state=resolution.state,
                    source_event_id=value.event.id,
                    supporting_event_ids=[str(event_id) for event_id in value.supporting_event_ids],
                    pending_event_ids=pending_ids,
                    conflicting_event_ids=conflicting_ids,
                    **{column: getattr(value.event, column) for column in VALUE_COLUMNS},
                )
            )
    db.flush()
    return resolution


def validate_event_relationships(
    db: Session,
    *,
    document: MatterDocument,
    definition: MetadataDefinition,
    operation: str,
    target_event_id: uuid.UUID | None,
    supersedes_id: uuid.UUID | None,
) -> None:
    if definition.value_source != "ASSERTED":
        raise ValueError("System and imported metadata fields are read-only")
    if definition.status != "ACTIVE":
        raise ValueError("Metadata definition is not active")
    if definition.cardinality == "SINGLE" and operation in {"ADD", "REMOVE"}:
        raise ValueError("Single-valued fields use SET and CLEAR")
    if definition.cardinality == "MULTIPLE" and operation == "SET":
        raise ValueError("Multi-valued fields use ADD, REMOVE, and CLEAR")

    if target_event_id is not None:
        target = db.get(MetadataEvent, target_event_id)
        if (
            target is None
            or target.matter_document_id != document.id
            or target.metadata_definition_id != definition.id
        ):
            raise ValueError("Target event must belong to this document and field")
        if operation == "REMOVE" and target.operation != "ADD":
            raise ValueError("REMOVE must target an ADD event")
        if operation in {"CONFIRM", "REJECT"} and target.operation not in ASSERTION_OPERATIONS:
            raise ValueError(f"{operation} must target a SET, ADD, or CLEAR event")
    if supersedes_id is not None:
        superseded = db.get(MetadataEvent, supersedes_id)
        if (
            superseded is None
            or superseded.matter_document_id != document.id
            or superseded.metadata_definition_id != definition.id
            or superseded.operation not in {"SET", "CLEAR"}
        ):
            raise ValueError("supersedes_id must reference a SET or CLEAR event for this document and field")


def add_metadata_event(
    db: Session,
    *,
    matter_id: uuid.UUID,
    document_id: uuid.UUID,
    definition_id: uuid.UUID,
    operation: str,
    value: Any | None,
    source_type: str,
    actor_id: uuid.UUID | None,
    source_id: str | None = None,
    agent_run_id: uuid.UUID | None = None,
    confidence: float | None = None,
    target_event_id: uuid.UUID | None = None,
    supersedes_id: uuid.UUID | None = None,
) -> tuple[MetadataEvent, FieldResolution]:
    document = db.scalar(
        select(MatterDocument)
        .where(MatterDocument.id == document_id, MatterDocument.matter_id == matter_id)
        .with_for_update()
    )
    if document is None:
        raise ValueError("Matter document not found")
    definition = db.scalar(
        select(MetadataDefinition).where(
            MetadataDefinition.id == definition_id,
            MetadataDefinition.matter_id == matter_id,
        )
    )
    if definition is None:
        raise ValueError("Metadata definition not found")
    validate_event_relationships(
        db,
        document=document,
        definition=definition,
        operation=operation,
        target_event_id=target_event_id,
        supersedes_id=supersedes_id,
    )
    columns = value_columns(value, definition) if operation in VALUE_OPERATIONS else {column: None for column in VALUE_COLUMNS}
    event = MetadataEvent(
        matter_id=matter_id,
        matter_document_id=document.id,
        metadata_definition_id=definition.id,
        operation=operation,
        source_type=source_type,
        source_id=source_id,
        actor_id=actor_id,
        agent_run_id=agent_run_id,
        confidence=confidence,
        target_event_id=target_event_id,
        supersedes_id=supersedes_id,
        **columns,
    )
    db.add(event)
    db.flush()
    resolution = project_field(db, document, definition)
    return event, resolution


def apply_metadata_values(
    db: Session,
    *,
    matter_id: uuid.UUID,
    document_id: uuid.UUID,
    definition_id: uuid.UUID,
    values: list[Any],
    replace: bool,
    source_type: str,
    actor_id: uuid.UUID | None,
    source_id: str,
    confidences: dict[str, float] | None = None,
) -> tuple[list[MetadataEvent], FieldResolution | None]:
    """Append a multi-value command and resolve the field once.

    This is the batch-writer equivalent of ``add_metadata_event``. A replacement
    remains auditable as CLEAR followed by ADD events, but avoids rebuilding the
    current projection after every individual value.
    """
    document = db.scalar(
        select(MatterDocument)
        .where(MatterDocument.id == document_id, MatterDocument.matter_id == matter_id)
        .with_for_update()
    )
    if document is None:
        raise ValueError("Matter document not found")
    definition = db.scalar(
        select(MetadataDefinition).where(
            MetadataDefinition.id == definition_id,
            MetadataDefinition.matter_id == matter_id,
        )
    )
    if definition is None:
        raise ValueError("Metadata definition not found")
    if definition.cardinality != "MULTIPLE":
        raise ValueError("Batch topic assignment requires a multi-valued field")
    if not replace and not values:
        return [], None

    events: list[MetadataEvent] = []
    base_time = utcnow()
    if replace:
        validate_event_relationships(
            db,
            document=document,
            definition=definition,
            operation="CLEAR",
            target_event_id=None,
            supersedes_id=None,
        )
        events.append(
            MetadataEvent(
                matter_id=matter_id,
                matter_document_id=document.id,
                metadata_definition_id=definition.id,
                operation="CLEAR",
                source_type=source_type,
                source_id=source_id,
                actor_id=actor_id,
                created_at=base_time,
            )
        )
    for index, value in enumerate(dict.fromkeys(values), start=1):
        validate_event_relationships(
            db,
            document=document,
            definition=definition,
            operation="ADD",
            target_event_id=None,
            supersedes_id=None,
        )
        events.append(
            MetadataEvent(
                matter_id=matter_id,
                matter_document_id=document.id,
                metadata_definition_id=definition.id,
                operation="ADD",
                source_type=source_type,
                source_id=source_id,
                actor_id=actor_id,
                confidence=(confidences or {}).get(str(value)),
                created_at=base_time + timedelta(microseconds=index),
                **value_columns(value, definition),
            )
        )
    db.add_all(events)
    db.flush()
    return events, project_field(db, document, definition)


def current_rows(
    db: Session,
    document_id: uuid.UUID,
    definition_id: uuid.UUID,
) -> list[DocumentMetadataCurrent]:
    return list(
        db.scalars(
            select(DocumentMetadataCurrent)
            .where(
                DocumentMetadataCurrent.matter_document_id == document_id,
                DocumentMetadataCurrent.metadata_definition_id == definition_id,
            )
            .order_by(DocumentMetadataCurrent.value_ordinal)
        )
    )


def current_metadata_values(
    db: Session,
    document_id: uuid.UUID,
    definitions: list[MetadataDefinition],
) -> dict[str, Any]:
    definitions_by_id = {definition.id: definition for definition in definitions}
    if not definitions_by_id:
        return {}
    rows_by_definition: dict[uuid.UUID, list[DocumentMetadataCurrent]] = {}
    for row in db.scalars(
        select(DocumentMetadataCurrent)
        .where(
            DocumentMetadataCurrent.matter_document_id == document_id,
            DocumentMetadataCurrent.metadata_definition_id.in_(definitions_by_id),
            DocumentMetadataCurrent.resolution_state == "VALUE",
        )
        .order_by(DocumentMetadataCurrent.metadata_definition_id, DocumentMetadataCurrent.value_ordinal)
    ):
        rows_by_definition.setdefault(row.metadata_definition_id, []).append(row)
    values = {}
    for definition_id, rows in rows_by_definition.items():
        definition = definitions_by_id[definition_id]
        resolved = [event_value(row) for row in rows]
        values[definition.key] = resolved if definition.cardinality == "MULTIPLE" else resolved[0]
    return values
