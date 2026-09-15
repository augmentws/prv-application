import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.audit import record_audit
from app.models import Matter, MetadataDefinition
from app.schemas import (
    MetadataDefinitionCreate,
    MetadataDefinitionUpdate,
    MetadataEnumValueCreate,
    MetadataEnumValueUpdate,
)
from app.search.operations import create_search_operation


class MetadataDefinitionCommandError(ValueError):
    pass


class MetadataDefinitionNotFoundError(MetadataDefinitionCommandError):
    pass


class MetadataDefinitionConflictError(MetadataDefinitionCommandError):
    pass


def _audit_details(
    matter: Matter,
    definition: MetadataDefinition,
    *,
    agent_run_id: uuid.UUID | None,
    changes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {"matter_id": str(matter.id), "key": definition.key}
    if changes:
        details["changes"] = changes
    if agent_run_id is not None:
        details["agent_run_id"] = str(agent_run_id)
    return details


def _enqueue_schema_sync(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
) -> None:
    create_search_operation(
        db,
        matter_id=matter.id,
        kind="SCHEMA_SYNC",
        created_by_user_id=actor_user_id,
    )


def _editable_definition(
    db: Session,
    *,
    matter: Matter,
    definition_id: uuid.UUID | None = None,
    definition_key: str | None = None,
) -> MetadataDefinition:
    conditions = [MetadataDefinition.matter_id == matter.id]
    if definition_id is not None:
        conditions.append(MetadataDefinition.id == definition_id)
    elif definition_key is not None:
        conditions.append(MetadataDefinition.key == definition_key)
    else:
        raise ValueError("A metadata definition identifier is required")
    definition = db.scalar(select(MetadataDefinition).where(*conditions).with_for_update())
    if definition is None:
        raise MetadataDefinitionNotFoundError("Metadata definition not found")
    if definition.value_source != "ASSERTED":
        raise MetadataDefinitionConflictError("Only matter-owned ASSERTED definitions are editable")
    return definition


def _enum_definition(
    db: Session,
    *,
    matter: Matter,
    definition_id: uuid.UUID | None = None,
    definition_key: str | None = None,
) -> MetadataDefinition:
    definition = _editable_definition(
        db,
        matter=matter,
        definition_id=definition_id,
        definition_key=definition_key,
    )
    if definition.type != "ENUM":
        raise MetadataDefinitionConflictError("Enum values can only be changed on ENUM definitions")
    return definition


def create_metadata_definition(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    payload: MetadataDefinitionCreate,
    agent_run_id: uuid.UUID | None = None,
) -> MetadataDefinition:
    existing = db.scalar(
        select(MetadataDefinition.id).where(
            MetadataDefinition.matter_id == matter.id,
            MetadataDefinition.key == payload.key,
        )
    )
    if existing is not None:
        raise MetadataDefinitionConflictError("Metadata definition key already exists in matter")
    definition = MetadataDefinition(
        matter_id=matter.id,
        key=payload.key,
        display_name=payload.display_name,
        description=payload.description,
        type=payload.type,
        cardinality=payload.cardinality,
        allowed_values=[item.model_dump() for item in payload.allowed_values] if payload.allowed_values else None,
        value_source="ASSERTED",
        reference_target=None,
        template_key=None,
        template_version=None,
        assertion_policy=payload.assertion_policy,
        resolution_policy=payload.resolution_policy,
        searchable=payload.searchable,
        facetable=payload.facetable,
        reviewable=payload.reviewable,
        ai_assignable=payload.ai_assignable,
        status="ACTIVE",
    )
    db.add(definition)
    db.flush()
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=actor_user_id,
        action="metadata_definition.created",
        target_type="metadata_definition",
        target_id=definition.id,
        details=_audit_details(matter, definition, agent_run_id=agent_run_id),
    )
    _enqueue_schema_sync(db, matter=matter, actor_user_id=actor_user_id)
    return definition


def update_metadata_definition(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    payload: MetadataDefinitionUpdate,
    definition_id: uuid.UUID | None = None,
    definition_key: str | None = None,
    agent_run_id: uuid.UUID | None = None,
) -> MetadataDefinition:
    definition = _editable_definition(
        db,
        matter=matter,
        definition_id=definition_id,
        definition_key=definition_key,
    )
    changes = payload.model_dump(exclude_unset=True)
    resulting_facetable = changes.get("facetable", definition.facetable)
    if resulting_facetable and definition.type in {"LONG_TEXT", "JSON"}:
        raise MetadataDefinitionConflictError(
            "LONG_TEXT and JSON fields cannot be facetable in phase one"
        )
    for field, value in changes.items():
        setattr(definition, field, value)
    db.flush()
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=actor_user_id,
        action="metadata_definition.updated",
        target_type="metadata_definition",
        target_id=definition.id,
        details=_audit_details(
            matter,
            definition,
            agent_run_id=agent_run_id,
            changes=changes,
        ),
    )
    _enqueue_schema_sync(db, matter=matter, actor_user_id=actor_user_id)
    return definition


def add_metadata_enum_value(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    payload: MetadataEnumValueCreate,
    definition_id: uuid.UUID | None = None,
    definition_key: str | None = None,
    agent_run_id: uuid.UUID | None = None,
) -> MetadataDefinition:
    definition = _enum_definition(
        db,
        matter=matter,
        definition_id=definition_id,
        definition_key=definition_key,
    )
    values = [dict(value) for value in definition.allowed_values or []]
    if any(value["key"] == payload.key for value in values):
        raise MetadataDefinitionConflictError("Enum value key already exists")
    enum_value = payload.model_dump()
    enum_value["active"] = True
    values.append(enum_value)
    definition.allowed_values = values
    flag_modified(definition, "allowed_values")
    db.flush()
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=actor_user_id,
        action="metadata_definition.enum_value_added",
        target_type="metadata_definition",
        target_id=definition.id,
        details=_audit_details(
            matter,
            definition,
            agent_run_id=agent_run_id,
            changes={"enum_value": enum_value},
        ),
    )
    _enqueue_schema_sync(db, matter=matter, actor_user_id=actor_user_id)
    return definition


def update_metadata_enum_value(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    value_key: str,
    payload: MetadataEnumValueUpdate,
    definition_id: uuid.UUID | None = None,
    definition_key: str | None = None,
    agent_run_id: uuid.UUID | None = None,
) -> MetadataDefinition:
    definition = _enum_definition(
        db,
        matter=matter,
        definition_id=definition_id,
        definition_key=definition_key,
    )
    values = [dict(value) for value in definition.allowed_values or []]
    value = next((item for item in values if item["key"] == value_key), None)
    if value is None:
        raise MetadataDefinitionNotFoundError("Enum value not found")
    changes = payload.model_dump(exclude_unset=True)
    value.update(changes)
    definition.allowed_values = values
    flag_modified(definition, "allowed_values")
    db.flush()
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=actor_user_id,
        action="metadata_definition.enum_value_updated",
        target_type="metadata_definition",
        target_id=definition.id,
        details=_audit_details(
            matter,
            definition,
            agent_run_id=agent_run_id,
            changes={"enum_value_key": value_key, **changes},
        ),
    )
    _enqueue_schema_sync(db, matter=matter, actor_user_id=actor_user_id)
    return definition


def deactivate_metadata_enum_value(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    value_key: str,
    definition_id: uuid.UUID | None = None,
    definition_key: str | None = None,
    agent_run_id: uuid.UUID | None = None,
) -> MetadataDefinition:
    definition = _enum_definition(
        db,
        matter=matter,
        definition_id=definition_id,
        definition_key=definition_key,
    )
    values = [dict(value) for value in definition.allowed_values or []]
    value = next((item for item in values if item["key"] == value_key), None)
    if value is None:
        raise MetadataDefinitionNotFoundError("Enum value not found")
    value["active"] = False
    definition.allowed_values = values
    flag_modified(definition, "allowed_values")
    db.flush()
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=actor_user_id,
        action="metadata_definition.enum_value_deactivated",
        target_type="metadata_definition",
        target_id=definition.id,
        details=_audit_details(
            matter,
            definition,
            agent_run_id=agent_run_id,
            changes={"enum_value_key": value_key, "active": False},
        ),
    )
    _enqueue_schema_sync(db, matter=matter, actor_user_id=actor_user_id)
    return definition
