import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.metadata_definitions import (
    MetadataDefinitionCommandError,
    MetadataDefinitionConflictError,
    MetadataDefinitionNotFoundError,
    add_metadata_enum_value,
    deactivate_metadata_enum_value,
    update_metadata_enum_value,
)
from app.metadata_definitions import (
    create_metadata_definition as create_metadata_definition_command,
)
from app.metadata_definitions import (
    update_metadata_definition as update_metadata_definition_command,
)
from app.models import Matter, MetadataDefinition
from app.schemas import (
    MetadataDefinitionCreate,
    MetadataDefinitionRead,
    MetadataDefinitionUpdate,
    MetadataEnumValueCreate,
    MetadataEnumValueUpdate,
)

router = APIRouter(prefix="/v1/matters/{matter_id}/metadata-definitions", tags=["metadata definitions"])


def get_authorized_matter(db: Session, principal: Principal, matter_id: uuid.UUID) -> Matter:
    matter = db.scalar(select(Matter).where(Matter.id == matter_id))
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if matter.status != "ACTIVE" or matter.client.status != "ACTIVE" or matter.client.tenant.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter, client, or tenant is not active")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    return matter


@router.post("", response_model=MetadataDefinitionRead, status_code=status.HTTP_201_CREATED)
def create_metadata_definition(
    matter_id: uuid.UUID,
    payload: MetadataDefinitionCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MetadataDefinition:
    matter = get_authorized_matter(db, principal, matter_id)
    try:
        definition = create_metadata_definition_command(
            db,
            matter=matter,
            actor_user_id=principal.user.id,
            payload=payload,
        )
        db.commit()
    except (MetadataDefinitionConflictError, IntegrityError) as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Metadata definition key already exists in matter",
        ) from exc
    return definition


def _command_error(exc: MetadataDefinitionCommandError) -> HTTPException:
    error_status = (
        status.HTTP_404_NOT_FOUND
        if isinstance(exc, MetadataDefinitionNotFoundError)
        else status.HTTP_409_CONFLICT
    )
    return HTTPException(status_code=error_status, detail=str(exc))


@router.patch("/{definition_id}", response_model=MetadataDefinitionRead)
def update_metadata_definition(
    matter_id: uuid.UUID,
    definition_id: uuid.UUID,
    payload: MetadataDefinitionUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MetadataDefinition:
    matter = get_authorized_matter(db, principal, matter_id)
    try:
        definition = update_metadata_definition_command(
            db,
            matter=matter,
            actor_user_id=principal.user.id,
            definition_id=definition_id,
            payload=payload,
        )
        db.commit()
        return definition
    except MetadataDefinitionCommandError as exc:
        db.rollback()
        raise _command_error(exc) from exc


@router.post(
    "/{definition_id}/enum-values",
    response_model=MetadataDefinitionRead,
    status_code=status.HTTP_201_CREATED,
)
def add_enum_value(
    matter_id: uuid.UUID,
    definition_id: uuid.UUID,
    payload: MetadataEnumValueCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MetadataDefinition:
    matter = get_authorized_matter(db, principal, matter_id)
    try:
        definition = add_metadata_enum_value(
            db,
            matter=matter,
            actor_user_id=principal.user.id,
            definition_id=definition_id,
            payload=payload,
        )
        db.commit()
        return definition
    except MetadataDefinitionCommandError as exc:
        db.rollback()
        raise _command_error(exc) from exc


@router.patch("/{definition_id}/enum-values/{value_key}", response_model=MetadataDefinitionRead)
def update_enum_value(
    matter_id: uuid.UUID,
    definition_id: uuid.UUID,
    value_key: str,
    payload: MetadataEnumValueUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MetadataDefinition:
    matter = get_authorized_matter(db, principal, matter_id)
    try:
        definition = update_metadata_enum_value(
            db,
            matter=matter,
            actor_user_id=principal.user.id,
            definition_id=definition_id,
            value_key=value_key,
            payload=payload,
        )
        db.commit()
        return definition
    except MetadataDefinitionCommandError as exc:
        db.rollback()
        raise _command_error(exc) from exc


@router.post(
    "/{definition_id}/enum-values/{value_key}/deactivate",
    response_model=MetadataDefinitionRead,
)
def deactivate_enum_value(
    matter_id: uuid.UUID,
    definition_id: uuid.UUID,
    value_key: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MetadataDefinition:
    matter = get_authorized_matter(db, principal, matter_id)
    try:
        definition = deactivate_metadata_enum_value(
            db,
            matter=matter,
            actor_user_id=principal.user.id,
            definition_id=definition_id,
            value_key=value_key,
        )
        db.commit()
        return definition
    except MetadataDefinitionCommandError as exc:
        db.rollback()
        raise _command_error(exc) from exc


@router.get("", response_model=list[MetadataDefinitionRead])
def list_metadata_definitions(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MetadataDefinition]:
    get_authorized_matter(db, principal, matter_id)
    return list(
        db.scalars(
            select(MetadataDefinition)
            .where(MetadataDefinition.matter_id == matter_id)
            .order_by(MetadataDefinition.key)
        )
    )


@router.get("/{definition_id}", response_model=MetadataDefinitionRead)
def get_metadata_definition(
    matter_id: uuid.UUID,
    definition_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MetadataDefinition:
    get_authorized_matter(db, principal, matter_id)
    definition = db.scalar(
        select(MetadataDefinition).where(
            MetadataDefinition.id == definition_id,
            MetadataDefinition.matter_id == matter_id,
        )
    )
    if definition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metadata definition not found")
    return definition
