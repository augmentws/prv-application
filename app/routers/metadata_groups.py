import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.matter_configuration import group_key
from app.models import Matter, MetadataDefinition, MetadataGroup, MetadataGroupField, MetadataGroupPreference
from app.schemas import MetadataGroupCreate, MetadataGroupRead, MetadataGroupVisibilityUpdate

router = APIRouter(prefix="/v1/matters/{matter_id}/metadata-groups", tags=["metadata groups"])


def authorized_matter(db: Session, principal: Principal, matter_id: uuid.UUID) -> Matter:
    matter = db.scalar(select(Matter).where(Matter.id == matter_id))
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if matter.status != "ACTIVE" or matter.client.status != "ACTIVE" or matter.client.tenant.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter, client, or tenant is not active")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    return matter


def visible_group(db: Session, principal: Principal, matter_id: uuid.UUID, group_id: uuid.UUID) -> MetadataGroup:
    group = db.scalar(
        select(MetadataGroup).where(
            MetadataGroup.id == group_id,
            MetadataGroup.matter_id == matter_id,
            MetadataGroup.status != "ARCHIVED",
            or_(MetadataGroup.scope != "PERSONAL", MetadataGroup.owner_user_id == principal.user.id),
        )
    )
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metadata group not found")
    return group


def group_reads(db: Session, principal: Principal, groups: list[MetadataGroup]) -> list[MetadataGroupRead]:
    group_ids = [group.id for group in groups]
    field_rows = (
        db.execute(
            select(MetadataGroupField.metadata_group_id, MetadataGroupField.metadata_definition_id)
            .where(MetadataGroupField.metadata_group_id.in_(group_ids))
            .order_by(MetadataGroupField.metadata_group_id, MetadataGroupField.sort_order)
        ).all()
        if group_ids
        else []
    )
    preference_rows = (
        list(
            db.scalars(
                select(MetadataGroupPreference).where(
                    MetadataGroupPreference.user_id == principal.user.id,
                    MetadataGroupPreference.metadata_group_id.in_(group_ids),
                )
            )
        )
        if group_ids
        else []
    )
    fields: dict[uuid.UUID, list[uuid.UUID]] = {group_id: [] for group_id in group_ids}
    for group_id, definition_id in field_rows:
        fields[group_id].append(definition_id)
    preferences = {(preference.metadata_group_id, preference.surface): preference.visible for preference in preference_rows}

    return [
        MetadataGroupRead(
            id=group.id,
            matter_id=group.matter_id,
            scope=group.scope,
            owner_user_id=group.owner_user_id,
            created_by_user_id=group.created_by_user_id,
            key=group.key,
            display_name=group.display_name,
            description=group.description,
            sort_order=group.sort_order,
            default_table_visible=group.default_table_visible,
            default_document_visible=group.default_document_visible,
            table_visible=preferences.get((group.id, "TABLE"), group.default_table_visible),
            document_visible=preferences.get((group.id, "DOCUMENT"), group.default_document_visible),
            definition_ids=fields[group.id],
            status=group.status,
            template_key=group.template_key,
            template_version=group.template_version,
            created_at=group.created_at,
        )
        for group in groups
    ]


@router.get("", response_model=list[MetadataGroupRead])
def list_metadata_groups(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MetadataGroupRead]:
    authorized_matter(db, principal, matter_id)
    groups = list(
        db.scalars(
            select(MetadataGroup)
            .where(
                MetadataGroup.matter_id == matter_id,
                MetadataGroup.status != "ARCHIVED",
                or_(MetadataGroup.scope != "PERSONAL", MetadataGroup.owner_user_id == principal.user.id),
            )
            .order_by(MetadataGroup.sort_order, MetadataGroup.display_name)
        )
    )
    return group_reads(db, principal, groups)


@router.post("", response_model=MetadataGroupRead, status_code=status.HTTP_201_CREATED)
def create_metadata_group(
    matter_id: uuid.UUID,
    payload: MetadataGroupCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MetadataGroupRead:
    matter = authorized_matter(db, principal, matter_id)
    definitions = list(
        db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter_id,
                MetadataDefinition.id.in_(payload.definition_ids),
                MetadataDefinition.status != "ARCHIVED",
            )
        )
    )
    if len(definitions) != len(payload.definition_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Every selected metadata definition must belong to this matter",
        )

    max_order = db.scalar(select(func.max(MetadataGroup.sort_order)).where(MetadataGroup.matter_id == matter_id)) or 0
    group = MetadataGroup(
        matter_id=matter_id,
        scope=payload.scope,
        owner_user_id=principal.user.id if payload.scope == "PERSONAL" else None,
        created_by_user_id=principal.user.id,
        key=group_key(payload.display_name),
        display_name=payload.display_name.strip(),
        description=payload.description,
        sort_order=max_order + 10,
        default_table_visible=payload.default_table_visible,
        default_document_visible=payload.default_document_visible,
        status="ACTIVE",
    )
    db.add(group)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A metadata group with this name already exists at the selected scope",
        ) from exc
    db.add_all(
        MetadataGroupField(metadata_group_id=group.id, metadata_definition_id=definition_id, sort_order=index * 10)
        for index, definition_id in enumerate(payload.definition_ids, start=1)
    )
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="metadata_group.created",
        target_type="metadata_group",
        target_id=group.id,
        details={"matter_id": str(matter.id), "scope": group.scope, "field_count": len(payload.definition_ids)},
    )
    db.commit()
    db.refresh(group)
    return group_reads(db, principal, [group])[0]


@router.put("/{group_id}/visibility", response_model=MetadataGroupRead)
def set_metadata_group_visibility(
    matter_id: uuid.UUID,
    group_id: uuid.UUID,
    payload: MetadataGroupVisibilityUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MetadataGroupRead:
    matter = authorized_matter(db, principal, matter_id)
    group = visible_group(db, principal, matter_id, group_id)
    preference = db.scalar(
        select(MetadataGroupPreference).where(
            MetadataGroupPreference.user_id == principal.user.id,
            MetadataGroupPreference.metadata_group_id == group.id,
            MetadataGroupPreference.surface == payload.surface,
        )
    )
    if preference is None:
        preference = MetadataGroupPreference(
            user_id=principal.user.id,
            matter_id=matter.id,
            metadata_group_id=group.id,
            surface=payload.surface,
            visible=payload.visible,
        )
        db.add(preference)
    else:
        preference.visible = payload.visible
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="metadata_group.visibility_changed",
        target_type="metadata_group",
        target_id=group.id,
        details={"matter_id": str(matter.id), "surface": payload.surface, "visible": payload.visible},
    )
    db.commit()
    db.refresh(group)
    return group_reads(db, principal, [group])[0]
