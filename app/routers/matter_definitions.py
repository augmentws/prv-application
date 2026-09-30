import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.matter_definitions import (
    MatterDefinitionError,
    append_matter_definition_revision,
    clone_guidance,
    create_guidance,
    current_guidance_revision,
    get_guidance,
    resolve_legacy_guidance,
)
from app.models import Matter, MatterDefinition, MatterDefinitionRevision
from app.schemas import (
    MatterDefinitionCloneCreate,
    MatterDefinitionCreate,
    MatterDefinitionRead,
    MatterDefinitionRevisionCreate,
    MatterDefinitionRevisionRead,
    MatterDefinitionUpdate,
)

router = APIRouter(tags=["review guidance"])


def _require_matter_admin(db: Session, principal: Principal, matter_id: uuid.UUID) -> Matter:
    matter = db.scalar(select(Matter).where(Matter.id == matter_id))
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    return matter


def _not_found_or_conflict(exc: MatterDefinitionError) -> HTTPException:
    code = status.HTTP_404_NOT_FOUND if "not found" in str(exc).lower() else status.HTTP_409_CONFLICT
    return HTTPException(status_code=code, detail=str(exc))


def _definition_read(
    definition: MatterDefinition,
    revision: MatterDefinitionRevision,
) -> MatterDefinitionRead:
    return MatterDefinitionRead(
        id=definition.id,
        matter_id=definition.matter_id,
        key=definition.key,
        name=definition.name,
        description=definition.description,
        status=definition.status,
        current_revision=definition.current_revision,
        published_revision=definition.published_revision,
        created_by_user_id=definition.created_by_user_id,
        created_at=definition.created_at,
        updated_at=definition.updated_at,
        revision=MatterDefinitionRevisionRead.model_validate(revision),
    )


def _read_current(db: Session, definition: MatterDefinition) -> MatterDefinitionRead:
    try:
        return _definition_read(definition, current_guidance_revision(db, definition))
    except MatterDefinitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/v1/matters/{matter_id}/guidance", response_model=list[MatterDefinitionRead])
def list_guidance(
    matter_id: uuid.UUID,
    include_archived: bool = True,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterDefinitionRead]:
    _require_matter_admin(db, principal, matter_id)
    statement = select(MatterDefinition).where(MatterDefinition.matter_id == matter_id)
    if not include_archived:
        statement = statement.where(MatterDefinition.status == "ACTIVE")
    definitions = list(
        db.scalars(statement.order_by(MatterDefinition.created_at, MatterDefinition.id))
    )
    return [_read_current(db, definition) for definition in definitions]


@router.post(
    "/v1/matters/{matter_id}/guidance",
    response_model=MatterDefinitionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_review_guidance(
    matter_id: uuid.UUID,
    payload: MatterDefinitionCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionRead:
    matter = _require_matter_admin(db, principal, matter_id)
    try:
        definition, revision = create_guidance(
            db,
            matter=matter,
            actor_user_id=principal.user.id,
            key=payload.key,
            name=payload.name,
            description=payload.description,
            content_markdown=payload.content_markdown,
            source_kind=payload.source_kind,
            source_filename=payload.source_filename,
        )
        db.commit()
    except MatterDefinitionError as exc:
        db.rollback()
        raise _not_found_or_conflict(exc) from exc
    db.refresh(definition)
    db.refresh(revision)
    return _definition_read(definition, revision)


@router.get(
    "/v1/matters/{matter_id}/guidance/{guidance_id}",
    response_model=MatterDefinitionRead,
)
def get_review_guidance(
    matter_id: uuid.UUID,
    guidance_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionRead:
    _require_matter_admin(db, principal, matter_id)
    try:
        definition = get_guidance(db, matter_id=matter_id, guidance_id=guidance_id)
    except MatterDefinitionError as exc:
        raise _not_found_or_conflict(exc) from exc
    return _read_current(db, definition)


@router.patch(
    "/v1/matters/{matter_id}/guidance/{guidance_id}",
    response_model=MatterDefinitionRead,
)
def update_review_guidance(
    matter_id: uuid.UUID,
    guidance_id: uuid.UUID,
    payload: MatterDefinitionUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionRead:
    matter = _require_matter_admin(db, principal, matter_id)
    try:
        definition = get_guidance(
            db, matter_id=matter_id, guidance_id=guidance_id, for_update=True
        )
    except MatterDefinitionError as exc:
        raise _not_found_or_conflict(exc) from exc
    before = {"name": definition.name, "description": definition.description}
    if "name" in payload.model_fields_set:
        definition.name = payload.name  # type: ignore[assignment]
    if "description" in payload.model_fields_set:
        definition.description = payload.description
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_definition.guidance.updated",
        target_type="matter_definition",
        target_id=definition.id,
        details={
            "matter_id": str(matter.id),
            "guidance_id": str(definition.id),
            "guidance_key": definition.key,
            "before": before,
            "after": {"name": definition.name, "description": definition.description},
        },
    )
    db.commit()
    db.refresh(definition)
    return _read_current(db, definition)


def _set_guidance_status(
    *,
    db: Session,
    matter: Matter,
    guidance_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    next_status: str,
) -> MatterDefinitionRead:
    try:
        definition = get_guidance(
            db, matter_id=matter.id, guidance_id=guidance_id, for_update=True
        )
    except MatterDefinitionError as exc:
        raise _not_found_or_conflict(exc) from exc
    definition.status = next_status
    action = "archived" if next_status == "ARCHIVED" else "restored"
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=actor_user_id,
        action=f"matter_definition.guidance.{action}",
        target_type="matter_definition",
        target_id=definition.id,
        details={
            "matter_id": str(matter.id),
            "guidance_id": str(definition.id),
            "guidance_key": definition.key,
        },
    )
    db.commit()
    db.refresh(definition)
    return _read_current(db, definition)


@router.post(
    "/v1/matters/{matter_id}/guidance/{guidance_id}/archive",
    response_model=MatterDefinitionRead,
)
def archive_review_guidance(
    matter_id: uuid.UUID,
    guidance_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionRead:
    matter = _require_matter_admin(db, principal, matter_id)
    return _set_guidance_status(
        db=db,
        matter=matter,
        guidance_id=guidance_id,
        actor_user_id=principal.user.id,
        next_status="ARCHIVED",
    )


@router.post(
    "/v1/matters/{matter_id}/guidance/{guidance_id}/restore",
    response_model=MatterDefinitionRead,
)
def restore_review_guidance(
    matter_id: uuid.UUID,
    guidance_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionRead:
    matter = _require_matter_admin(db, principal, matter_id)
    return _set_guidance_status(
        db=db,
        matter=matter,
        guidance_id=guidance_id,
        actor_user_id=principal.user.id,
        next_status="ACTIVE",
    )


@router.post(
    "/v1/matters/{matter_id}/guidance/{guidance_id}/clone",
    response_model=MatterDefinitionRead,
    status_code=status.HTTP_201_CREATED,
)
def clone_review_guidance(
    matter_id: uuid.UUID,
    guidance_id: uuid.UUID,
    payload: MatterDefinitionCloneCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionRead:
    matter = _require_matter_admin(db, principal, matter_id)
    try:
        source = get_guidance(db, matter_id=matter_id, guidance_id=guidance_id)
        definition, revision = clone_guidance(
            db,
            matter=matter,
            source=source,
            actor_user_id=principal.user.id,
            key=payload.key,
            name=payload.name,
            description=payload.description,
            source_revision_number=payload.source_revision,
        )
        db.commit()
    except MatterDefinitionError as exc:
        db.rollback()
        raise _not_found_or_conflict(exc) from exc
    db.refresh(definition)
    db.refresh(revision)
    return _definition_read(definition, revision)


@router.get(
    "/v1/matters/{matter_id}/guidance/{guidance_id}/revisions",
    response_model=list[MatterDefinitionRevisionRead],
)
def list_review_guidance_revisions(
    matter_id: uuid.UUID,
    guidance_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterDefinitionRevision]:
    _require_matter_admin(db, principal, matter_id)
    try:
        definition = get_guidance(db, matter_id=matter_id, guidance_id=guidance_id)
    except MatterDefinitionError as exc:
        raise _not_found_or_conflict(exc) from exc
    return list(
        db.scalars(
            select(MatterDefinitionRevision)
            .where(MatterDefinitionRevision.matter_definition_id == definition.id)
            .order_by(MatterDefinitionRevision.revision.desc())
        )
    )


@router.post(
    "/v1/matters/{matter_id}/guidance/{guidance_id}/revisions",
    response_model=MatterDefinitionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_review_guidance_revision(
    matter_id: uuid.UUID,
    guidance_id: uuid.UUID,
    payload: MatterDefinitionRevisionCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionRead:
    matter = _require_matter_admin(db, principal, matter_id)
    try:
        definition, revision = append_matter_definition_revision(
            db,
            matter=matter,
            matter_definition_id=guidance_id,
            actor_user_id=principal.user.id,
            content_markdown=payload.content_markdown,
            source_kind=payload.source_kind,
            based_on_revision=payload.based_on_revision,
            source_filename=payload.source_filename,
        )
        db.commit()
    except MatterDefinitionError as exc:
        db.rollback()
        raise _not_found_or_conflict(exc) from exc
    db.refresh(definition)
    db.refresh(revision)
    return _definition_read(definition, revision)


def _publish(
    *,
    db: Session,
    matter: Matter,
    definition: MatterDefinition,
    revision_number: int,
    actor_user_id: uuid.UUID,
) -> MatterDefinitionRead:
    if definition.status != "ACTIVE":
        raise HTTPException(status_code=409, detail="Archived Review Guidance cannot be published")
    revision = db.scalar(
        select(MatterDefinitionRevision).where(
            MatterDefinitionRevision.matter_definition_id == definition.id,
            MatterDefinitionRevision.revision == revision_number,
        )
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="Review Guidance revision not found")
    definition.published_revision = revision_number
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=actor_user_id,
        action="matter_definition.revision.published",
        target_type="matter_definition",
        target_id=definition.id,
        details={
            "matter_id": str(matter.id),
            "guidance_id": str(definition.id),
            "guidance_key": definition.key,
            "revision": revision_number,
            "revision_id": str(revision.id),
        },
    )
    db.commit()
    db.refresh(definition)
    return _definition_read(definition, revision)


@router.post(
    "/v1/matters/{matter_id}/guidance/{guidance_id}/revisions/{revision_number}/publish",
    response_model=MatterDefinitionRead,
)
def publish_review_guidance_revision(
    matter_id: uuid.UUID,
    guidance_id: uuid.UUID,
    revision_number: int,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionRead:
    matter = _require_matter_admin(db, principal, matter_id)
    try:
        definition = get_guidance(
            db, matter_id=matter_id, guidance_id=guidance_id, for_update=True
        )
    except MatterDefinitionError as exc:
        raise _not_found_or_conflict(exc) from exc
    return _publish(
        db=db,
        matter=matter,
        definition=definition,
        revision_number=revision_number,
        actor_user_id=principal.user.id,
    )


# Compatibility routes for callers that still expect one Matter Definition.
@router.get("/v1/matters/{matter_id}/definition", response_model=MatterDefinitionRead | None)
def get_matter_definition(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionRead | None:
    _require_matter_admin(db, principal, matter_id)
    try:
        definition = resolve_legacy_guidance(db, matter_id=matter_id)
    except MatterDefinitionError as exc:
        raise _not_found_or_conflict(exc) from exc
    return _read_current(db, definition) if definition else None


@router.get(
    "/v1/matters/{matter_id}/definition/revisions",
    response_model=list[MatterDefinitionRevisionRead],
)
def list_matter_definition_revisions(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterDefinitionRevision]:
    _require_matter_admin(db, principal, matter_id)
    try:
        definition = resolve_legacy_guidance(db, matter_id=matter_id)
    except MatterDefinitionError as exc:
        raise _not_found_or_conflict(exc) from exc
    if definition is None:
        return []
    return list(
        db.scalars(
            select(MatterDefinitionRevision)
            .where(MatterDefinitionRevision.matter_definition_id == definition.id)
            .order_by(MatterDefinitionRevision.revision.desc())
        )
    )


@router.post(
    "/v1/matters/{matter_id}/definition/revisions",
    response_model=MatterDefinitionRead,
    status_code=status.HTTP_201_CREATED,
)
def create_matter_definition_revision(
    matter_id: uuid.UUID,
    payload: MatterDefinitionRevisionCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionRead:
    matter = _require_matter_admin(db, principal, matter_id)
    try:
        definition, revision = append_matter_definition_revision(
            db,
            matter=matter,
            actor_user_id=principal.user.id,
            content_markdown=payload.content_markdown,
            source_kind=payload.source_kind,
            based_on_revision=payload.based_on_revision,
            source_filename=payload.source_filename,
        )
        db.commit()
    except MatterDefinitionError as exc:
        db.rollback()
        raise _not_found_or_conflict(exc) from exc
    db.refresh(definition)
    db.refresh(revision)
    return _definition_read(definition, revision)


@router.post(
    "/v1/matters/{matter_id}/definition/revisions/{revision_number}/publish",
    response_model=MatterDefinitionRead,
)
def publish_matter_definition_revision(
    matter_id: uuid.UUID,
    revision_number: int,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionRead:
    matter = _require_matter_admin(db, principal, matter_id)
    try:
        definition = resolve_legacy_guidance(db, matter_id=matter_id, for_update=True)
    except MatterDefinitionError as exc:
        raise _not_found_or_conflict(exc) from exc
    if definition is None:
        raise HTTPException(status_code=404, detail="Matter Definition not found")
    return _publish(
        db=db,
        matter=matter,
        definition=definition,
        revision_number=revision_number,
        actor_user_id=principal.user.id,
    )
