import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.matter_definitions import MatterDefinitionError, append_matter_definition_revision
from app.models import Matter, MatterDefinition, MatterDefinitionRevision
from app.schemas import MatterDefinitionRead, MatterDefinitionRevisionCreate, MatterDefinitionRevisionRead

router = APIRouter(prefix="/v1/matters/{matter_id}/definition", tags=["matter definition"])


def _require_matter_admin(db: Session, principal: Principal, matter_id: uuid.UUID) -> Matter:
    matter = db.scalar(select(Matter).where(Matter.id == matter_id))
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    return matter


def _definition_read(
    definition: MatterDefinition,
    revision: MatterDefinitionRevision,
) -> MatterDefinitionRead:
    return MatterDefinitionRead(
        id=definition.id,
        matter_id=definition.matter_id,
        current_revision=definition.current_revision,
        published_revision=definition.published_revision,
        created_by_user_id=definition.created_by_user_id,
        created_at=definition.created_at,
        updated_at=definition.updated_at,
        revision=MatterDefinitionRevisionRead.model_validate(revision),
    )


@router.get("", response_model=MatterDefinitionRead | None)
def get_matter_definition(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionRead | None:
    _require_matter_admin(db, principal, matter_id)
    definition = db.scalar(select(MatterDefinition).where(MatterDefinition.matter_id == matter_id))
    if definition is None:
        return None
    revision = db.scalar(
        select(MatterDefinitionRevision).where(
            MatterDefinitionRevision.matter_definition_id == definition.id,
            MatterDefinitionRevision.revision == definition.current_revision,
        )
    )
    if revision is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter Definition has no current revision")
    return _definition_read(definition, revision)


@router.get("/revisions", response_model=list[MatterDefinitionRevisionRead])
def list_matter_definition_revisions(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterDefinitionRevision]:
    _require_matter_admin(db, principal, matter_id)
    definition = db.scalar(select(MatterDefinition).where(MatterDefinition.matter_id == matter_id))
    if definition is None:
        return []
    return list(
        db.scalars(
            select(MatterDefinitionRevision)
            .where(MatterDefinitionRevision.matter_definition_id == definition.id)
            .order_by(MatterDefinitionRevision.revision.desc())
        )
    )


@router.post("/revisions", response_model=MatterDefinitionRead, status_code=status.HTTP_201_CREATED)
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
    except MatterDefinitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.commit()
    db.refresh(definition)
    db.refresh(revision)
    return _definition_read(definition, revision)


@router.post("/revisions/{revision_number}/publish", response_model=MatterDefinitionRead)
def publish_matter_definition_revision(
    matter_id: uuid.UUID,
    revision_number: int,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionRead:
    matter = _require_matter_admin(db, principal, matter_id)
    definition = db.scalar(
        select(MatterDefinition).where(MatterDefinition.matter_id == matter_id).with_for_update()
    )
    if definition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter Definition not found")
    revision = db.scalar(
        select(MatterDefinitionRevision).where(
            MatterDefinitionRevision.matter_definition_id == definition.id,
            MatterDefinitionRevision.revision == revision_number,
        )
    )
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter Definition revision not found")
    definition.published_revision = revision_number
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_definition.revision.published",
        target_type="matter_definition",
        target_id=definition.id,
        details={"matter_id": str(matter.id), "revision": revision_number},
    )
    db.commit()
    db.refresh(definition)
    return _definition_read(definition, revision)
