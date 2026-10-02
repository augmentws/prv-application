import hashlib
import json
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.models import ClassificationModel, ClassificationModelConfiguration, Matter, MetadataDefinition
from app.schemas import ClassificationModelConfigurationCreate, ClassificationModelCreate
from classification_service.hashing import canonical_model_hash
from classification_service.schemas import (
    ClassificationModelConfigurationSpecification,
    ClassificationStrategy,
    TargetCardinality,
    TargetFieldSnapshot,
    TargetMetadataType,
)


class ClassificationModelCommandError(ValueError):
    pass


class ClassificationModelNotFoundError(ClassificationModelCommandError):
    pass


class ClassificationModelConflictError(ClassificationModelCommandError):
    pass


def active_value_keys(definition: MetadataDefinition) -> list[str]:
    if definition.type != "ENUM":
        return []
    return sorted(
        value["key"]
        for value in definition.allowed_values or []
        if value.get("active", True)
    )


def target_schema_fingerprint(definition: MetadataDefinition) -> str:
    payload = {
        "metadata_definition_id": str(definition.id),
        "stable_key": definition.key,
        "metadata_type": definition.type,
        "cardinality": definition.cardinality,
        "active_value_keys": active_value_keys(definition),
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def target_field_snapshot(
    *,
    matter: Matter,
    definition: MetadataDefinition,
) -> TargetFieldSnapshot:
    try:
        return TargetFieldSnapshot(
            tenant_id=matter.client.tenant_id,
            matter_id=matter.id,
            metadata_definition_id=definition.id,
            stable_key=definition.key,
            display_name=definition.display_name,
            metadata_type=TargetMetadataType(definition.type),
            cardinality=TargetCardinality(definition.cardinality),
            schema_fingerprint=target_schema_fingerprint(definition),
            active_value_keys=active_value_keys(definition),
        )
    except (ValueError, ValidationError) as exc:
        raise ClassificationModelConflictError(str(exc)) from exc


def target_field_read_data(definition: MetadataDefinition) -> dict[str, Any]:
    return {
        "metadata_definition_id": definition.id,
        "stable_key": definition.key,
        "display_name": definition.display_name,
        "metadata_type": definition.type,
        "cardinality": definition.cardinality,
        "status": definition.status,
        "reviewable": definition.reviewable,
        "active_value_keys": active_value_keys(definition),
        "schema_fingerprint": target_schema_fingerprint(definition),
    }


def eligible_target_definition(
    db: Session,
    *,
    matter: Matter,
    definition_id: uuid.UUID,
) -> MetadataDefinition:
    definition = db.scalar(
        select(MetadataDefinition).where(
            MetadataDefinition.id == definition_id,
            MetadataDefinition.matter_id == matter.id,
        )
    )
    if definition is None:
        raise ClassificationModelConflictError("Target metadata definition does not belong to this matter")
    if definition.status != "ACTIVE":
        raise ClassificationModelConflictError("Target metadata definition must be active")
    if not definition.reviewable:
        raise ClassificationModelConflictError("Target metadata definition must be reviewable")
    if definition.type not in {"BOOLEAN", "ENUM"}:
        raise ClassificationModelConflictError("Classification targets must be Boolean or enum fields")
    if definition.type == "BOOLEAN" and definition.cardinality != "SINGLE":
        raise ClassificationModelConflictError("Boolean classification targets must be single cardinality")
    return definition


def get_classification_model(
    db: Session,
    *,
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    for_update: bool = False,
) -> ClassificationModel:
    statement = select(ClassificationModel).where(
        ClassificationModel.id == classification_model_id,
        ClassificationModel.matter_id == matter_id,
    )
    if for_update:
        statement = statement.with_for_update()
    model = db.scalar(statement)
    if model is None:
        raise ClassificationModelNotFoundError("Classification model not found")
    return model


def create_classification_model(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    payload: ClassificationModelCreate,
) -> ClassificationModel:
    definition = eligible_target_definition(
        db,
        matter=matter,
        definition_id=payload.target_metadata_definition_id,
    )
    model = ClassificationModel(
        tenant_id=matter.client.tenant_id,
        matter_id=matter.id,
        target_metadata_definition_id=definition.id,
        name=payload.name,
        description=payload.description,
        status="ACTIVE",
        created_by_user_id=actor_user_id,
        updated_by_user_id=actor_user_id,
    )
    db.add(model)
    db.flush()
    record_audit(
        db,
        tenant_id=model.tenant_id,
        actor_user_id=actor_user_id,
        action="classification_model.created",
        target_type="classification_model",
        target_id=model.id,
        details={
            "matter_id": str(matter.id),
            "target_metadata_definition_id": str(definition.id),
            "target_field_key": definition.key,
        },
    )
    return model


def create_classification_model_configuration(
    db: Session,
    *,
    matter: Matter,
    classification_model_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    payload: ClassificationModelConfigurationCreate,
) -> ClassificationModelConfiguration:
    model = get_classification_model(
        db,
        matter_id=matter.id,
        classification_model_id=classification_model_id,
        for_update=True,
    )
    if model.tenant_id != matter.client.tenant_id:
        raise ClassificationModelConflictError("Classification model tenant does not match the matter tenant")
    if model.status != "ACTIVE":
        raise ClassificationModelConflictError("Archived or suspended classification models cannot be revised")
    definition = eligible_target_definition(
        db,
        matter=matter,
        definition_id=model.target_metadata_definition_id,
    )
    snapshot = target_field_snapshot(matter=matter, definition=definition)
    try:
        specification = ClassificationModelConfigurationSpecification(
            target_field_snapshot=snapshot,
            **payload.model_dump(),
        )
    except ValidationError as exc:
        raise ClassificationModelConflictError(str(exc)) from exc
    configuration_hash = canonical_model_hash(specification)
    existing = db.scalar(
        select(ClassificationModelConfiguration.id).where(
            ClassificationModelConfiguration.classification_model_id == model.id,
            ClassificationModelConfiguration.configuration_hash == configuration_hash,
        )
    )
    if existing is not None:
        raise ClassificationModelConflictError("An identical configuration revision already exists")
    last_revision = db.scalar(
        select(func.max(ClassificationModelConfiguration.revision)).where(
            ClassificationModelConfiguration.classification_model_id == model.id
        )
    )
    configuration = ClassificationModelConfiguration(
        classification_model_id=model.id,
        revision=(last_revision or 0) + 1,
        target_field_snapshot=snapshot.model_dump(mode="json"),
        classification_strategy=specification.classification_strategy.value,
        ordered_class_keys=list(specification.ordered_class_keys),
        binary_mapping=(
            specification.binary_mapping.model_dump(mode="json")
            if specification.binary_mapping is not None
            else None
        ),
        label_policy=specification.label_policy.model_dump(mode="json"),
        feature_contract=specification.feature_contract.model_dump(mode="json"),
        prediction_resolution_policy=specification.prediction_resolution_policy.model_dump(mode="json"),
        trainer_specification=specification.trainer_specification.model_dump(mode="json"),
        configuration_hash=configuration_hash,
        created_by_user_id=actor_user_id,
    )
    db.add(configuration)
    db.flush()
    model.current_configuration_id = configuration.id
    model.updated_by_user_id = actor_user_id
    db.flush()
    record_audit(
        db,
        tenant_id=model.tenant_id,
        actor_user_id=actor_user_id,
        action="classification_model.configuration_created",
        target_type="classification_model_configuration",
        target_id=configuration.id,
        details={
            "classification_model_id": str(model.id),
            "matter_id": str(matter.id),
            "revision": configuration.revision,
            "configuration_hash": configuration.configuration_hash,
            "target_metadata_definition_id": str(model.target_metadata_definition_id),
        },
    )
    return configuration


def configuration_compatibility(
    model: ClassificationModel,
    definition: MetadataDefinition,
) -> tuple[bool, bool | None, list[str]]:
    reasons: list[str] = []
    configuration = model.current_configuration
    if model.status != "ACTIVE":
        reasons.append("Classification model is not active")
    if definition.status != "ACTIVE":
        reasons.append("Target metadata definition is not active")
    if not definition.reviewable:
        reasons.append("Target metadata definition is not reviewable")
    if definition.type not in {"BOOLEAN", "ENUM"}:
        reasons.append("Target metadata definition is no longer Boolean or enum")
    if configuration is None:
        return not reasons, None, reasons

    snapshot = configuration.target_field_snapshot
    if str(definition.id) != snapshot["metadata_definition_id"]:
        reasons.append("Target metadata definition identity changed")
    if definition.key != snapshot["stable_key"]:
        reasons.append("Target field stable key changed")
    if definition.type != snapshot["metadata_type"]:
        reasons.append("Target field type changed")
    if definition.cardinality != snapshot["cardinality"]:
        reasons.append("Target field cardinality changed")

    available_values = set(active_value_keys(definition)) if definition.type == "ENUM" else {"false", "true"}
    if configuration.classification_strategy == ClassificationStrategy.BINARY.value:
        mapping = configuration.binary_mapping or {}
        referenced_values = set(mapping.get("negative_value_keys", [])) | set(mapping.get("positive_value_keys", []))
    else:
        referenced_values = set(configuration.ordered_class_keys)
    missing_values = sorted(referenced_values - available_values)
    if missing_values:
        reasons.append(f"Configured target values are inactive or missing: {', '.join(missing_values)}")

    fingerprint_matches = target_schema_fingerprint(definition) == snapshot["schema_fingerprint"]
    return not reasons, fingerprint_matches, reasons


def archive_classification_model(
    db: Session,
    *,
    matter: Matter,
    classification_model_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> ClassificationModel:
    model = get_classification_model(
        db,
        matter_id=matter.id,
        classification_model_id=classification_model_id,
        for_update=True,
    )
    if model.status == "ARCHIVED":
        return model
    model.status = "ARCHIVED"
    model.updated_by_user_id = actor_user_id
    db.flush()
    record_audit(
        db,
        tenant_id=model.tenant_id,
        actor_user_id=actor_user_id,
        action="classification_model.archived",
        target_type="classification_model",
        target_id=model.id,
        details={"matter_id": str(matter.id)},
    )
    return model
