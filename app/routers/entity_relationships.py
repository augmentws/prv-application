import uuid
from typing import Literal, Never

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.authorization import ResourceRef, can
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.entity_relationships import (
    RELATIONSHIP_DEFINITIONS,
    EntityRelationshipConflict,
    EntityRelationshipError,
    EntityRelationshipNotFound,
    create_entity_relationship,
    decide_entity_relationship,
    get_entity_relationship,
    list_entity_relationships,
    relationship_definition,
)
from app.models import Matter, MatterEntity, MatterEntityRelationship
from app.schemas import (
    MatterEntityRelationshipAction,
    MatterEntityRelationshipCreate,
    MatterEntityRelationshipDefinitionRead,
    MatterEntityRelationshipPartyRead,
    MatterEntityRelationshipRead,
)

router = APIRouter(prefix="/v1/matters/{matter_id}/entity-relationships", tags=["matter entity relationships"])


def _matter(db: Session, principal: Principal, matter_id: uuid.UUID, permission_key: str) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can(db, principal, permission_key, ResourceRef.matter(matter.id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission required: {permission_key}")
    return matter


def _raise(exc: EntityRelationshipError) -> Never:
    if isinstance(exc, EntityRelationshipNotFound):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, EntityRelationshipConflict):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


def _read(db: Session, relationship: MatterEntityRelationship) -> MatterEntityRelationshipRead:
    source = db.get(MatterEntity, relationship.source_entity_id)
    target = db.get(MatterEntity, relationship.target_entity_id)
    if source is None or target is None:
        raise EntityRelationshipError("Relationship entity is unavailable")
    definition = relationship_definition(relationship.relationship_type)
    return MatterEntityRelationshipRead(
        **{
            key: getattr(relationship, key)
            for key in MatterEntityRelationshipRead.model_fields
            if key not in {"source", "target", "forward_label", "inverse_label"}
        },
        source=MatterEntityRelationshipPartyRead.model_validate(source),
        target=MatterEntityRelationshipPartyRead.model_validate(target),
        forward_label=definition.forward_label,
        inverse_label=definition.inverse_label,
    )


def _audit(
    db: Session,
    *,
    matter: Matter,
    principal: Principal,
    action: str,
    relationship: MatterEntityRelationship,
) -> None:
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action=f"matter_entity_relationship.{action}",
        target_type="matter_entity_relationship",
        target_id=relationship.id,
        details={
            "matter_id": str(matter.id),
            "relationship_type": relationship.relationship_type,
            "source_entity_id": str(relationship.source_entity_id),
            "target_entity_id": str(relationship.target_entity_id),
            "status": relationship.status,
        },
    )


@router.get("/types", response_model=list[MatterEntityRelationshipDefinitionRead])
def list_relationship_types(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterEntityRelationshipDefinitionRead]:
    _matter(db, principal, matter_id, "entity.view")
    return [
        MatterEntityRelationshipDefinitionRead(
            key=definition.key,
            forward_label=definition.forward_label,
            inverse_label=definition.inverse_label,
            source_entity_types=sorted(definition.source_entity_types),
            source_organization_categories=(
                sorted(definition.source_organization_categories)
                if definition.source_organization_categories is not None
                else None
            ),
            target_entity_types=sorted(definition.target_entity_types),
        )
        for definition in RELATIONSHIP_DEFINITIONS.values()
    ]


@router.get("", response_model=list[MatterEntityRelationshipRead])
def list_relationships(
    matter_id: uuid.UUID,
    entity_id: uuid.UUID | None = None,
    relationship_status: list[str] = Query(default=[], alias="status"),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterEntityRelationshipRead]:
    _matter(db, principal, matter_id, "entity.view")
    statuses = tuple(relationship_status) if relationship_status else ("PROPOSED", "CONFIRMED")
    if any(value not in {"PROPOSED", "CONFIRMED", "REJECTED"} for value in statuses):
        raise HTTPException(status_code=422, detail="Unsupported relationship status")
    return [
        _read(db, relationship)
        for relationship in list_entity_relationships(
            db, matter_id=matter_id, entity_id=entity_id, statuses=statuses
        )
    ]


@router.post("", response_model=MatterEntityRelationshipRead, status_code=status.HTTP_201_CREATED)
def create_relationship(
    matter_id: uuid.UUID,
    payload: MatterEntityRelationshipCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEntityRelationshipRead:
    matter = _matter(db, principal, matter_id, "entity.manage")
    try:
        relationship = create_entity_relationship(
            db,
            matter_id=matter.id,
            source_entity_id=payload.source_entity_id,
            target_entity_id=payload.target_entity_id,
            relationship_type=payload.relationship_type,
            status=payload.status,
            actor_user_id=principal.user.id,
            confidence=payload.confidence,
            rationale=payload.rationale,
            effective_from=payload.effective_from,
            effective_to=payload.effective_to,
        )
    except (EntityRelationshipError, IntegrityError) as exc:
        db.rollback()
        if isinstance(exc, IntegrityError):
            _raise(EntityRelationshipConflict("This active entity relationship already exists"))
        _raise(exc)
    _audit(db, matter=matter, principal=principal, action="created", relationship=relationship)
    db.commit()
    db.refresh(relationship)
    return _read(db, relationship)


def _decide(
    *,
    matter_id: uuid.UUID,
    relationship_id: uuid.UUID,
    payload: MatterEntityRelationshipAction,
    decision: Literal["CONFIRMED", "REJECTED"],
    principal: Principal,
    db: Session,
) -> MatterEntityRelationshipRead:
    matter = _matter(db, principal, matter_id, "entity.manage")
    try:
        relationship = get_entity_relationship(
            db, matter_id=matter.id, relationship_id=relationship_id, for_update=True
        )
        decide_entity_relationship(
            db,
            relationship=relationship,
            decision=decision,
            lock_version=payload.lock_version,
            actor_user_id=principal.user.id,
        )
    except EntityRelationshipError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter=matter, principal=principal, action=decision.lower(), relationship=relationship)
    db.commit()
    db.refresh(relationship)
    return _read(db, relationship)


@router.post("/{relationship_id}/confirm", response_model=MatterEntityRelationshipRead)
def confirm_relationship(
    matter_id: uuid.UUID,
    relationship_id: uuid.UUID,
    payload: MatterEntityRelationshipAction,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEntityRelationshipRead:
    return _decide(
        matter_id=matter_id,
        relationship_id=relationship_id,
        payload=payload,
        decision="CONFIRMED",
        principal=principal,
        db=db,
    )


@router.post("/{relationship_id}/reject", response_model=MatterEntityRelationshipRead)
def reject_relationship(
    matter_id: uuid.UUID,
    relationship_id: uuid.UUID,
    payload: MatterEntityRelationshipAction,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEntityRelationshipRead:
    return _decide(
        matter_id=matter_id,
        relationship_id=relationship_id,
        payload=payload,
        decision="REJECTED",
        principal=principal,
        db=db,
    )
