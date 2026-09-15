import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.database import get_db
from app.dependencies import (
    Principal,
    can_admin_client,
    can_admin_matter,
    get_principal,
)
from app.matter_configuration import instantiate_configuration_snapshot, snapshot_matter_configuration
from app.metadata_profiles import DEFAULT_MATTER_METADATA_PROFILE, instantiate_default_configuration
from app.models import Client, Matter, MatterMembership, MatterTemplate, MatterTemplateVersion
from app.schemas import MatterCreate, MatterRead
from app.search.operations import create_search_operation

router = APIRouter(prefix="/v1", tags=["matters"])


@router.post("/clients/{client_id}/matters", response_model=MatterRead, status_code=status.HTTP_201_CREATED)
def create_matter(
    client_id: uuid.UUID,
    payload: MatterCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Matter:
    client = db.scalar(select(Client).where(Client.id == client_id))
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    if client.status != "ACTIVE" or client.tenant.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Client or tenant is not active")
    if not can_admin_client(db, principal, client):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client ADMIN required")

    matter = Matter(client_id=client.id, name=payload.name, status="ACTIVE")
    db.add(matter)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter name already exists in client") from exc
    if principal.user.tenant_id == client.tenant_id:
        db.add(MatterMembership(matter_id=matter.id, user_id=principal.user.id, role="ADMIN"))

    configuration_details: dict[str, str | int] = {}
    if payload.template_id:
        template = db.scalar(select(MatterTemplate).where(MatterTemplate.id == payload.template_id))
        if template is None or template.status != "ACTIVE":
            db.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter template not found")
        if template.tenant_id != client.tenant_id or (template.client_id and template.client_id != client.id):
            db.rollback()
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter template is not available to this client")
        version = db.scalar(
            select(MatterTemplateVersion).where(
                MatterTemplateVersion.matter_template_id == template.id,
                MatterTemplateVersion.version == template.current_version,
            )
        )
        if version is None:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter template has no current version")
        configuration = instantiate_configuration_snapshot(matter.id, principal.user.id, version.configuration)
        configuration_details = {
            "matter_template_id": str(template.id),
            "matter_template_version": version.version,
        }
    elif payload.clone_from_matter_id:
        source_matter = db.scalar(select(Matter).where(Matter.id == payload.clone_from_matter_id))
        if source_matter is None:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source matter not found")
        if source_matter.client.tenant_id != client.tenant_id or not can_admin_matter(db, principal, source_matter):
            db.rollback()
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Source matter is not available")
        snapshot = snapshot_matter_configuration(db, source_matter.id)
        configuration = instantiate_configuration_snapshot(matter.id, principal.user.id, snapshot)
        configuration_details = {"cloned_from_matter_id": str(source_matter.id)}
    else:
        configuration = instantiate_default_configuration(matter.id, principal.user.id)
        configuration_details = {"metadata_profile": DEFAULT_MATTER_METADATA_PROFILE.identifier}

    db.add_all(configuration.definitions)
    db.add_all(configuration.groups)
    db.add_all(configuration.group_fields)
    create_search_operation(
        db,
        matter_id=matter.id,
        kind="SCHEMA_SYNC",
        created_by_user_id=principal.user.id,
    )
    record_audit(
        db,
        tenant_id=client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter.created",
        target_type="matter",
        target_id=matter.id,
        details={
            **configuration_details,
            "metadata_definition_count": len(configuration.definitions),
            "metadata_group_count": len(configuration.groups),
        },
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Matter and default metadata profile could not be created",
        ) from exc
    return matter


@router.get("/clients/{client_id}/matters", response_model=list[MatterRead])
def list_matters(
    client_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[Matter]:
    client = db.scalar(select(Client).where(Client.id == client_id))
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    if not can_admin_client(db, principal, client):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client ADMIN required")
    return list(db.scalars(select(Matter).where(Matter.client_id == client_id).order_by(Matter.name)))


@router.get("/matters/{matter_id}", response_model=MatterRead)
def get_matter(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Matter:
    matter = db.scalar(select(Matter).where(Matter.id == matter_id))
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    return matter
