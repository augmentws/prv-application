import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.classification_models import (
    ClassificationModelCommandError,
    ClassificationModelNotFoundError,
    configuration_compatibility,
    target_field_read_data,
)
from app.classification_models import archive_classification_model as archive_classification_model_command
from app.classification_models import create_classification_model as create_classification_model_command
from app.classification_models import (
    create_classification_model_configuration as create_configuration_command,
)
from app.classification_models import get_classification_model as get_classification_model_record
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.models import ClassificationModel, ClassificationModelConfiguration, Matter
from app.schemas import (
    ClassificationModelCompatibilityRead,
    ClassificationModelConfigurationCreate,
    ClassificationModelConfigurationRead,
    ClassificationModelCreate,
    ClassificationModelRead,
)

router = APIRouter(
    prefix="/v1/matters/{matter_id}/classification-models",
    tags=["classification models"],
)


def get_authorized_matter(db: Session, principal: Principal, matter_id: uuid.UUID) -> Matter:
    matter = db.scalar(select(Matter).where(Matter.id == matter_id))
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if matter.status != "ACTIVE" or matter.client.status != "ACTIVE" or matter.client.tenant.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter, client, or tenant is not active")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    return matter


def configuration_read(
    configuration: ClassificationModelConfiguration,
) -> ClassificationModelConfigurationRead:
    return ClassificationModelConfigurationRead.model_validate(
        {
            "id": configuration.id,
            "classification_model_id": configuration.classification_model_id,
            "revision": configuration.revision,
            "target_field_snapshot": configuration.target_field_snapshot,
            "classification_strategy": configuration.classification_strategy,
            "ordered_class_keys": configuration.ordered_class_keys,
            "binary_mapping": configuration.binary_mapping,
            "label_policy": configuration.label_policy,
            "feature_contract": configuration.feature_contract,
            "prediction_resolution_policy": configuration.prediction_resolution_policy,
            "trainer_specification": configuration.trainer_specification,
            "configuration_hash": configuration.configuration_hash,
            "created_at": configuration.created_at,
            "created_by_user_id": configuration.created_by_user_id,
        }
    )


def model_read(model: ClassificationModel) -> ClassificationModelRead:
    return ClassificationModelRead.model_validate(
        {
            "id": model.id,
            "tenant_id": model.tenant_id,
            "matter_id": model.matter_id,
            "target_metadata_definition_id": model.target_metadata_definition_id,
            "name": model.name,
            "description": model.description,
            "status": model.status,
            "current_configuration_id": model.current_configuration_id,
            "target_field": target_field_read_data(model.target_metadata_definition),
            "current_configuration": (
                configuration_read(model.current_configuration) if model.current_configuration is not None else None
            ),
            "created_at": model.created_at,
            "created_by_user_id": model.created_by_user_id,
            "updated_at": model.updated_at,
            "updated_by_user_id": model.updated_by_user_id,
            "row_version": model.row_version,
        }
    )


def command_error(exc: ClassificationModelCommandError) -> HTTPException:
    error_status = (
        status.HTTP_404_NOT_FOUND
        if isinstance(exc, ClassificationModelNotFoundError)
        else status.HTTP_409_CONFLICT
    )
    return HTTPException(status_code=error_status, detail=str(exc))


@router.post("", response_model=ClassificationModelRead, status_code=status.HTTP_201_CREATED)
def create_classification_model(
    matter_id: uuid.UUID,
    payload: ClassificationModelCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ClassificationModelRead:
    matter = get_authorized_matter(db, principal, matter_id)
    try:
        model = create_classification_model_command(
            db,
            matter=matter,
            actor_user_id=principal.user.id,
            payload=payload,
        )
        db.commit()
        db.refresh(model)
        return model_read(model)
    except ClassificationModelCommandError as exc:
        db.rollback()
        raise command_error(exc) from exc


@router.get("", response_model=list[ClassificationModelRead])
def list_classification_models(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ClassificationModelRead]:
    get_authorized_matter(db, principal, matter_id)
    models = db.scalars(
        select(ClassificationModel)
        .options(
            selectinload(ClassificationModel.target_metadata_definition),
            selectinload(ClassificationModel.current_configuration),
        )
        .where(ClassificationModel.matter_id == matter_id)
        .order_by(ClassificationModel.created_at, ClassificationModel.id)
    ).all()
    return [model_read(model) for model in models]


@router.get("/{classification_model_id}", response_model=ClassificationModelRead)
def get_classification_model(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ClassificationModelRead:
    get_authorized_matter(db, principal, matter_id)
    try:
        model = get_classification_model_record(
            db,
            matter_id=matter_id,
            classification_model_id=classification_model_id,
        )
        return model_read(model)
    except ClassificationModelCommandError as exc:
        raise command_error(exc) from exc


@router.post(
    "/{classification_model_id}/configurations",
    response_model=ClassificationModelConfigurationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_classification_model_configuration(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    payload: ClassificationModelConfigurationCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ClassificationModelConfigurationRead:
    matter = get_authorized_matter(db, principal, matter_id)
    try:
        configuration = create_configuration_command(
            db,
            matter=matter,
            classification_model_id=classification_model_id,
            actor_user_id=principal.user.id,
            payload=payload,
        )
        db.commit()
        db.refresh(configuration)
        return configuration_read(configuration)
    except ClassificationModelCommandError as exc:
        db.rollback()
        raise command_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Classification model configuration revision conflicts with an existing revision",
        ) from exc


@router.get(
    "/{classification_model_id}/configurations",
    response_model=list[ClassificationModelConfigurationRead],
)
def list_classification_model_configurations(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ClassificationModelConfigurationRead]:
    get_authorized_matter(db, principal, matter_id)
    try:
        get_classification_model_record(
            db,
            matter_id=matter_id,
            classification_model_id=classification_model_id,
        )
    except ClassificationModelCommandError as exc:
        raise command_error(exc) from exc
    configurations = db.scalars(
        select(ClassificationModelConfiguration)
        .where(ClassificationModelConfiguration.classification_model_id == classification_model_id)
        .order_by(ClassificationModelConfiguration.revision)
    ).all()
    return [configuration_read(configuration) for configuration in configurations]


@router.get(
    "/{classification_model_id}/compatibility",
    response_model=ClassificationModelCompatibilityRead,
)
def get_classification_model_compatibility(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ClassificationModelCompatibilityRead:
    get_authorized_matter(db, principal, matter_id)
    try:
        model = get_classification_model_record(
            db,
            matter_id=matter_id,
            classification_model_id=classification_model_id,
        )
    except ClassificationModelCommandError as exc:
        raise command_error(exc) from exc
    compatible, fingerprint_matches, reasons = configuration_compatibility(
        model,
        model.target_metadata_definition,
    )
    return ClassificationModelCompatibilityRead(
        classification_model_id=model.id,
        configuration_id=model.current_configuration_id,
        compatible=compatible,
        schema_fingerprint_matches=fingerprint_matches,
        current_target_field=target_field_read_data(model.target_metadata_definition),
        configured_target_field_snapshot=(
            model.current_configuration.target_field_snapshot if model.current_configuration is not None else None
        ),
        reasons=reasons,
    )


@router.post("/{classification_model_id}/archive", response_model=ClassificationModelRead)
def archive_classification_model(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ClassificationModelRead:
    matter = get_authorized_matter(db, principal, matter_id)
    try:
        model = archive_classification_model_command(
            db,
            matter=matter,
            classification_model_id=classification_model_id,
            actor_user_id=principal.user.id,
        )
        db.commit()
        db.refresh(model)
        return model_read(model)
    except ClassificationModelCommandError as exc:
        db.rollback()
        raise command_error(exc) from exc
