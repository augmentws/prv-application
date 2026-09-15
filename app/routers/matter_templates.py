import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.database import get_db
from app.dependencies import Principal, can_admin_client, can_admin_matter, get_principal
from app.matter_configuration import configuration_counts, normalize_name, snapshot_matter_configuration
from app.metadata_profiles import DEFAULT_MATTER_METADATA_PROFILE
from app.models import Client, Matter, MatterTemplate, MatterTemplateVersion
from app.schemas import MatterTemplateCreate, MatterTemplateRead

router = APIRouter(prefix="/v1", tags=["matter templates"])


def template_read(template: MatterTemplate, version: MatterTemplateVersion) -> MatterTemplateRead:
    definition_count, group_count = configuration_counts(version.configuration)
    return MatterTemplateRead(
        id=template.id,
        tenant_id=template.tenant_id,
        client_id=template.client_id,
        scope=template.scope,
        name=template.name,
        description=template.description,
        current_version=template.current_version,
        source_matter_id=version.source_matter_id,
        definition_count=definition_count,
        group_count=group_count,
        status=template.status,
        created_by_user_id=template.created_by_user_id,
        created_at=template.created_at,
    )


@router.post("/matters/{matter_id}/templates", response_model=MatterTemplateRead, status_code=status.HTTP_201_CREATED)
def create_matter_template(
    matter_id: uuid.UUID,
    payload: MatterTemplateCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterTemplateRead:
    matter = db.scalar(select(Matter).where(Matter.id == matter_id))
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    configuration = snapshot_matter_configuration(db, matter.id)
    template = MatterTemplate(
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id if payload.scope == "CLIENT" else None,
        scope=payload.scope,
        name=payload.name.strip(),
        normalized_name=normalize_name(payload.name),
        description=payload.description,
        current_version=1,
        status="ACTIVE",
        created_by_user_id=principal.user.id,
    )
    db.add(template)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A matter template with this name already exists at the selected scope",
        ) from exc
    version = MatterTemplateVersion(
        matter_template_id=template.id,
        version=1,
        base_profile_key=DEFAULT_MATTER_METADATA_PROFILE.key,
        base_profile_version=DEFAULT_MATTER_METADATA_PROFILE.version,
        configuration=configuration,
        source_matter_id=matter.id,
        created_by_user_id=principal.user.id,
    )
    db.add(version)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_template.created",
        target_type="matter_template",
        target_id=template.id,
        details={"source_matter_id": str(matter.id), "scope": template.scope, "version": 1},
    )
    db.commit()
    db.refresh(template)
    db.refresh(version)
    return template_read(template, version)


@router.get("/clients/{client_id}/matter-templates", response_model=list[MatterTemplateRead])
def list_matter_templates(
    client_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterTemplateRead]:
    client = db.scalar(select(Client).where(Client.id == client_id))
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    if not can_admin_client(db, principal, client):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client ADMIN required")
    templates = list(
        db.scalars(
            select(MatterTemplate)
            .where(
                MatterTemplate.tenant_id == client.tenant_id,
                MatterTemplate.status == "ACTIVE",
                or_(MatterTemplate.scope == "TENANT", MatterTemplate.client_id == client.id),
            )
            .order_by(MatterTemplate.name)
        )
    )
    reads: list[MatterTemplateRead] = []
    for template in templates:
        version = db.scalar(
            select(MatterTemplateVersion).where(
                MatterTemplateVersion.matter_template_id == template.id,
                MatterTemplateVersion.version == template.current_version,
            )
        )
        if version is not None:
            reads.append(template_read(template, version))
    return reads
