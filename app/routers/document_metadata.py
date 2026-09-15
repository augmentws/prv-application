import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.document_metadata import add_metadata_event, derive_event_states, event_value
from app.models import DocumentMetadataCurrent, Matter, MatterDocument, MetadataDefinition, MetadataEvent
from app.schemas import (
    DocumentMetadataFieldRead,
    DocumentMetadataValueRead,
    MetadataEventCreate,
    MetadataEventRead,
    MetadataMutationRead,
)
from app.search.operations import create_search_operation

router = APIRouter(
    prefix="/v1/matters/{matter_id}/documents/{document_id}/metadata-values",
    tags=["document metadata values"],
)


def _authorized_document(
    db: Session,
    principal: Principal,
    matter_id: uuid.UUID,
    document_id: uuid.UUID,
) -> tuple[Matter, MatterDocument]:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if matter.status != "ACTIVE" or matter.client.status != "ACTIVE" or matter.client.tenant.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter, client, or tenant is not active")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    document = db.scalar(
        select(MatterDocument).where(
            MatterDocument.id == document_id,
            MatterDocument.matter_id == matter_id,
        )
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter document not found")
    return matter, document


def _definition(db: Session, matter_id: uuid.UUID, definition_id: uuid.UUID) -> MetadataDefinition:
    definition = db.scalar(
        select(MetadataDefinition).where(
            MetadataDefinition.id == definition_id,
            MetadataDefinition.matter_id == matter_id,
        )
    )
    if definition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metadata definition not found")
    if definition.value_source != "ASSERTED":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="System and imported fields resolve from their authoritative source, not the asserted-values API",
        )
    return definition


def _row_value(row: DocumentMetadataCurrent):
    for column in (
        "value_text",
        "value_long",
        "value_float",
        "value_boolean",
        "value_date",
        "value_datetime",
        "value_json",
    ):
        value = getattr(row, column)
        if value is not None:
            return value
    return None


def _field_read(
    document: MatterDocument,
    definition: MetadataDefinition,
    rows: list[DocumentMetadataCurrent],
) -> DocumentMetadataFieldRead:
    first = rows[0] if rows else None
    return DocumentMetadataFieldRead(
        matter_document_id=document.id,
        metadata_definition_id=definition.id,
        key=definition.key,
        display_name=definition.display_name,
        type=definition.type,
        cardinality=definition.cardinality,
        resolution_state=first.resolution_state if first else "EMPTY",
        values=[
            DocumentMetadataValueRead(
                value=_row_value(row),
                source_event_id=row.source_event_id,
                supporting_event_ids=[uuid.UUID(value) for value in row.supporting_event_ids],
            )
            for row in rows
            if row.source_event_id is not None
        ],
        pending_event_ids=[uuid.UUID(value) for value in first.pending_event_ids] if first else [],
        conflicting_event_ids=[uuid.UUID(value) for value in first.conflicting_event_ids] if first else [],
        updated_at=max((row.updated_at for row in rows), default=None),
    )


def _event_reads(events: list[MetadataEvent], definition: MetadataDefinition) -> list[MetadataEventRead]:
    states = derive_event_states(events, definition)
    result = []
    for event in events:
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
        result.append(
            MetadataEventRead(
                id=event.id,
                matter_id=event.matter_id,
                matter_document_id=event.matter_document_id,
                metadata_definition_id=event.metadata_definition_id,
                operation=event.operation,
                value=event_value(event),
                source_type=event.source_type,
                source_id=event.source_id,
                actor_id=event.actor_id,
                agent_run_id=event.agent_run_id,
                confidence=event.confidence,
                target_event_id=event.target_event_id,
                supersedes_id=event.supersedes_id,
                effective_status=effective_status,
                confirmation_state=confirmation_state,
                created_at=event.created_at,
            )
        )
    return result


def _events(db: Session, document_id: uuid.UUID, definition_id: uuid.UUID) -> list[MetadataEvent]:
    return list(
        db.scalars(
            select(MetadataEvent)
            .where(
                MetadataEvent.matter_document_id == document_id,
                MetadataEvent.metadata_definition_id == definition_id,
            )
            .order_by(MetadataEvent.created_at, MetadataEvent.id)
        )
    )


@router.get("", response_model=list[DocumentMetadataFieldRead])
def list_document_metadata_values(
    matter_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[DocumentMetadataFieldRead]:
    _, document = _authorized_document(db, principal, matter_id, document_id)
    definitions = list(
        db.scalars(
            select(MetadataDefinition)
            .where(
                MetadataDefinition.matter_id == matter_id,
                MetadataDefinition.value_source == "ASSERTED",
            )
            .order_by(MetadataDefinition.key)
        )
    )
    rows_by_definition: dict[uuid.UUID, list[DocumentMetadataCurrent]] = defaultdict(list)
    if definitions:
        for row in db.scalars(
            select(DocumentMetadataCurrent)
            .where(
                DocumentMetadataCurrent.matter_document_id == document.id,
                DocumentMetadataCurrent.metadata_definition_id.in_([definition.id for definition in definitions]),
            )
            .order_by(DocumentMetadataCurrent.metadata_definition_id, DocumentMetadataCurrent.value_ordinal)
        ):
            rows_by_definition[row.metadata_definition_id].append(row)
    return [_field_read(document, definition, rows_by_definition[definition.id]) for definition in definitions]


@router.get("/{definition_id}", response_model=DocumentMetadataFieldRead)
def get_document_metadata_value(
    matter_id: uuid.UUID,
    document_id: uuid.UUID,
    definition_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> DocumentMetadataFieldRead:
    _, document = _authorized_document(db, principal, matter_id, document_id)
    definition = _definition(db, matter_id, definition_id)
    rows = list(
        db.scalars(
            select(DocumentMetadataCurrent)
            .where(
                DocumentMetadataCurrent.matter_document_id == document.id,
                DocumentMetadataCurrent.metadata_definition_id == definition.id,
            )
            .order_by(DocumentMetadataCurrent.value_ordinal)
        )
    )
    return _field_read(document, definition, rows)


@router.get("/{definition_id}/events", response_model=list[MetadataEventRead])
def list_document_metadata_events(
    matter_id: uuid.UUID,
    document_id: uuid.UUID,
    definition_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MetadataEventRead]:
    _authorized_document(db, principal, matter_id, document_id)
    definition = _definition(db, matter_id, definition_id)
    return _event_reads(_events(db, document_id, definition.id), definition)


@router.post("/{definition_id}/events", response_model=MetadataMutationRead, status_code=status.HTTP_201_CREATED)
def create_document_metadata_event(
    matter_id: uuid.UUID,
    document_id: uuid.UUID,
    definition_id: uuid.UUID,
    payload: MetadataEventCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MetadataMutationRead:
    matter, document = _authorized_document(db, principal, matter_id, document_id)
    definition = _definition(db, matter_id, definition_id)
    search_operation_id = None
    try:
        event, _ = add_metadata_event(
            db,
            matter_id=matter.id,
            document_id=document.id,
            definition_id=definition.id,
            operation=payload.operation,
            value=payload.value,
            source_type="HUMAN",
            actor_id=principal.user.id,
            source_id=payload.source_id,
            confidence=payload.confidence,
            target_event_id=payload.target_event_id,
            supersedes_id=payload.supersedes_id,
        )
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=principal.user.id,
            action="document.metadata_event.created",
            target_type="metadata_event",
            target_id=event.id,
            details={
                "matter_id": str(matter.id),
                "document_id": str(document.id),
                "metadata_definition_id": str(definition.id),
                "operation": event.operation,
            },
        )
        if definition.searchable:
            search_operation = create_search_operation(
                db,
                matter_id=matter.id,
                kind="DOCUMENT_UPSERT",
                payload={"document_ids": [str(document.id)]},
                created_by_user_id=principal.user.id,
            )
            search_operation_id = search_operation.id
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    events = _events(db, document.id, definition.id)
    rows = list(
        db.scalars(
            select(DocumentMetadataCurrent)
            .where(
                DocumentMetadataCurrent.matter_document_id == document.id,
                DocumentMetadataCurrent.metadata_definition_id == definition.id,
            )
            .order_by(DocumentMetadataCurrent.value_ordinal)
        )
    )
    event_read = next(item for item in _event_reads(events, definition) if item.id == event.id)
    return MetadataMutationRead(
        event=event_read,
        current=_field_read(document, definition, rows),
        search_operation_id=search_operation_id,
    )
