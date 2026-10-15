import uuid
from typing import Never

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.authorization import ResourceRef, can
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.matter_entities import (
    MatterEntityConflict,
    MatterEntityError,
    MatterEntityNotFound,
    add_variant,
    canonical_entity,
    confirm_entity,
    create_entity,
    get_entity,
    merge_entities,
    normalize_entity_name,
    reject_entity,
    unmerge_entity,
    update_entity,
)
from app.models import Matter, MatterEntity, MatterFact, MatterFactEntity
from app.schemas import (
    MatterEntityAction,
    MatterEntityCreate,
    MatterEntityMerge,
    MatterEntityPage,
    MatterEntityRead,
    MatterEntityUpdate,
    MatterEntityVariantCreate,
    MatterEntityVariantRead,
)

router = APIRouter(prefix="/v1/matters/{matter_id}/entities", tags=["matter entities"])


def _matter(db: Session, principal: Principal, matter_id: uuid.UUID, permission_key: str) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can(db, principal, permission_key, ResourceRef.matter(matter.id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission required: {permission_key}")
    return matter


def _raise(exc: MatterEntityError) -> Never:
    if isinstance(exc, MatterEntityNotFound):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, MatterEntityConflict):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


def _fact_counts(db: Session, canonical_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Count non-merged facts that reference each canonical entity through any of its rows."""

    if not canonical_ids:
        return {}
    group = func.coalesce(MatterEntity.parent_id, MatterEntity.id)
    participant = (
        select(group.label("canonical_id"), MatterFactEntity.fact_id.label("fact_id"))
        .join(MatterFactEntity, MatterFactEntity.entity_id == MatterEntity.id)
        .join(MatterFact, MatterFact.id == MatterFactEntity.fact_id)
        .where(group.in_(canonical_ids), MatterFact.status != "MERGED")
    )
    owner = (
        select(group.label("canonical_id"), MatterFact.id.label("fact_id"))
        .join(MatterFact, MatterFact.owner_entity_id == MatterEntity.id)
        .where(group.in_(canonical_ids), MatterFact.status != "MERGED")
    )
    union = participant.union(owner).subquery()
    return {
        canonical_id: int(count)
        for canonical_id, count in db.execute(
            select(union.c.canonical_id, func.count(func.distinct(union.c.fact_id))).group_by(union.c.canonical_id)
        )
    }


def _read(db: Session, entity: MatterEntity, *, fact_count: int | None = None) -> MatterEntityRead:
    variants = list(
        db.scalars(
            select(MatterEntity)
            .where(MatterEntity.parent_id == entity.id)
            .order_by(MatterEntity.created_at, MatterEntity.id)
        )
    )
    count = fact_count if fact_count is not None else _fact_counts(db, [entity.id]).get(entity.id, 0)
    return MatterEntityRead.model_validate(entity).model_copy(
        update={
            "variants": [MatterEntityVariantRead.model_validate(variant) for variant in variants],
            "fact_count": count,
        }
    )


def _audit(db: Session, matter: Matter, principal: Principal, action: str, entity: MatterEntity, details: dict) -> None:
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action=f"matter_entity.{action}",
        target_type="matter_entity",
        target_id=entity.id,
        details={"matter_id": str(matter.id), **details},
    )


@router.get("", response_model=MatterEntityPage)
def list_entities(
    matter_id: uuid.UUID,
    q: str | None = Query(default=None, max_length=300),
    entity_type: str | None = Query(default=None, pattern="^(PERSON|ORGANIZATION)$"),
    entity_status: list[str] = Query(default=[], alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEntityPage:
    """Canonical entities; a name or email search also matches their variants."""

    _matter(db, principal, matter_id, "entity.view")
    statement = select(MatterEntity).where(MatterEntity.matter_id == matter_id, MatterEntity.parent_id.is_(None))
    if entity_type:
        statement = statement.where(MatterEntity.entity_type == entity_type)
    if entity_status:
        statement = statement.where(MatterEntity.status.in_(entity_status))
    if q and q.strip():
        needle = f"%{normalize_entity_name(q)}%"
        matching_groups = select(func.coalesce(MatterEntity.parent_id, MatterEntity.id)).where(
            MatterEntity.matter_id == matter_id,
            or_(MatterEntity.normalized_name.like(needle), MatterEntity.normalized_email.like(needle)),
        )
        statement = statement.where(MatterEntity.id.in_(matching_groups))
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    entities = list(
        db.scalars(
            statement.order_by(MatterEntity.normalized_name, MatterEntity.id).limit(limit).offset(offset)
        )
    )
    counts = _fact_counts(db, [entity.id for entity in entities])
    return MatterEntityPage(
        items=[_read(db, entity, fact_count=counts.get(entity.id, 0)) for entity in entities],
        total=total,
    )


@router.post("", response_model=MatterEntityRead, status_code=status.HTTP_201_CREATED)
def create_matter_entity(
    matter_id: uuid.UUID,
    payload: MatterEntityCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEntityRead:
    matter = _matter(db, principal, matter_id, "entity.manage")
    try:
        entity = create_entity(
            db,
            matter=matter,
            entity_type=payload.entity_type,
            display_name=payload.display_name,
            email=payload.email,
            title=payload.title,
            organization_id=payload.organization_id,
            custodian_id=payload.custodian_id,
            description=payload.description,
            status=payload.status,
            actor_user_id=principal.user.id,
        )
    except MatterEntityError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "created", entity, {"entity_type": entity.entity_type})
    db.commit()
    db.refresh(entity)
    return _read(db, entity)


@router.get("/{entity_id}", response_model=MatterEntityRead)
def get_matter_entity(
    matter_id: uuid.UUID,
    entity_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEntityRead:
    """Return the canonical entity, even when ``entity_id`` names one of its variants."""

    _matter(db, principal, matter_id, "entity.view")
    try:
        entity = canonical_entity(db, get_entity(db, matter_id=matter_id, entity_id=entity_id))
    except MatterEntityError as exc:
        _raise(exc)
    return _read(db, entity)


@router.patch("/{entity_id}", response_model=MatterEntityRead)
def update_matter_entity(
    matter_id: uuid.UUID,
    entity_id: uuid.UUID,
    payload: MatterEntityUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEntityRead:
    matter = _matter(db, principal, matter_id, "entity.manage")
    changes = {key: getattr(payload, key) for key in payload.model_fields_set - {"lock_version"}}
    try:
        entity = get_entity(db, matter_id=matter_id, entity_id=entity_id, for_update=True)
        update_entity(
            db,
            matter=matter,
            entity=entity,
            lock_version=payload.lock_version,
            changes=changes,
            actor_user_id=principal.user.id,
        )
    except MatterEntityError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "updated", entity, {"fields": sorted(changes)})
    db.commit()
    db.refresh(entity)
    return _read(db, entity)


@router.post("/{entity_id}/variants", response_model=MatterEntityRead, status_code=status.HTTP_201_CREATED)
def add_matter_entity_variant(
    matter_id: uuid.UUID,
    entity_id: uuid.UUID,
    payload: MatterEntityVariantCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEntityRead:
    matter = _matter(db, principal, matter_id, "entity.manage")
    try:
        entity = get_entity(db, matter_id=matter_id, entity_id=entity_id, for_update=True)
        if entity.lock_version != payload.lock_version:
            raise MatterEntityConflict("Entity changed since it was loaded")
        variant = add_variant(
            db,
            canonical=entity,
            display_name=payload.display_name,
            email=payload.email,
            actor_user_id=principal.user.id,
        )
    except MatterEntityError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "variant_added", entity, {"variant_id": str(variant.id)})
    db.commit()
    db.refresh(entity)
    return _read(db, entity)


@router.post("/{entity_id}/confirm", response_model=MatterEntityRead)
def confirm_matter_entity(
    matter_id: uuid.UUID,
    entity_id: uuid.UUID,
    payload: MatterEntityAction,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEntityRead:
    matter = _matter(db, principal, matter_id, "entity.manage")
    try:
        entity = get_entity(db, matter_id=matter_id, entity_id=entity_id, for_update=True)
        confirm_entity(db, entity=entity, lock_version=payload.lock_version, actor_user_id=principal.user.id)
    except MatterEntityError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "confirmed", entity, {})
    db.commit()
    db.refresh(entity)
    return _read(db, entity)


@router.post("/{entity_id}/reject", response_model=MatterEntityRead)
def reject_matter_entity(
    matter_id: uuid.UUID,
    entity_id: uuid.UUID,
    payload: MatterEntityAction,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEntityRead:
    """Reject an entity; it is removed from every fact's owner and participants as a new fact revision."""

    matter = _matter(db, principal, matter_id, "entity.manage")
    if not can(db, principal, "fact.review", ResourceRef.matter(matter.id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission required: fact.review")
    try:
        entity = get_entity(db, matter_id=matter_id, entity_id=entity_id, for_update=True)
        reject_entity(
            db,
            entity=entity,
            lock_version=payload.lock_version,
            actor_user_id=principal.user.id,
            reason=payload.reason,
        )
    except MatterEntityError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "rejected", entity, {"reason": payload.reason})
    db.commit()
    db.refresh(entity)
    return _read(db, entity)


@router.post("/{entity_id}/merge", response_model=MatterEntityRead)
def merge_matter_entity(
    matter_id: uuid.UUID,
    entity_id: uuid.UUID,
    payload: MatterEntityMerge,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEntityRead:
    """Merge this canonical entity (and its variants) into the target; returns the target."""

    matter = _matter(db, principal, matter_id, "entity.manage")
    try:
        source = get_entity(db, matter_id=matter_id, entity_id=entity_id, for_update=True)
        target = get_entity(db, matter_id=matter_id, entity_id=payload.target_entity_id, for_update=True)
        merge_entities(
            db,
            source=source,
            target=target,
            source_lock_version=payload.lock_version,
            actor_user_id=principal.user.id,
        )
    except MatterEntityError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "merged", source, {"target_entity_id": str(target.id)})
    db.commit()
    db.refresh(target)
    return _read(db, target)


@router.post("/{entity_id}/unmerge", response_model=MatterEntityRead)
def unmerge_matter_entity(
    matter_id: uuid.UUID,
    entity_id: uuid.UUID,
    payload: MatterEntityAction,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEntityRead:
    """Split a variant off as its own canonical entity; returns the new canonical entity."""

    matter = _matter(db, principal, matter_id, "entity.manage")
    try:
        variant = get_entity(db, matter_id=matter_id, entity_id=entity_id, for_update=True)
        unmerge_entity(db, variant=variant, lock_version=payload.lock_version, actor_user_id=principal.user.id)
    except MatterEntityError as exc:
        db.rollback()
        _raise(exc)
    _audit(db, matter, principal, "unmerged", variant, {})
    db.commit()
    db.refresh(variant)
    return _read(db, variant)
