import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.audit import record_audit
from app.classification_builds import (
    ClassificationBuildError,
    ClassificationBuildNotFoundError,
    build_preview,
    cancel_training_run,
    create_training_run,
    get_training_run,
    prepare_build,
    reconcile_training_run,
)
from app.classification_datasets import (
    ClassificationDatasetError,
    ClassificationDatasetNotFoundError,
    begin_dataset_snapshot,
    fail_dataset_snapshot,
    get_dataset_snapshot,
    materialize_dataset_snapshot,
    prepare_dataset_snapshot,
)
from app.classification_features import (
    ClassificationFeatureSplitError,
    begin_feature_split,
    materialize_feature_split,
    prepare_feature_split,
    request_matches_materialized_snapshot,
)
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
from app.models import (
    ClassificationDatasetSnapshot,
    ClassificationModel,
    ClassificationModelConfiguration,
    ClassificationModelVersion,
    ClassificationTrainingRun,
    Matter,
)
from app.schemas import (
    ClassificationDatasetSnapshotCreateResponse,
    ClassificationDatasetSnapshotPreview,
    ClassificationDatasetSnapshotRead,
    ClassificationDatasetSnapshotRequest,
    ClassificationFeatureSplitCreateResponse,
    ClassificationFeatureSplitRequest,
    ClassificationModelBuildCreate,
    ClassificationModelBuildCreateResponse,
    ClassificationModelBuildPreview,
    ClassificationModelBuildPreviewRequest,
    ClassificationModelCompatibilityRead,
    ClassificationModelConfigurationCreate,
    ClassificationModelConfigurationRead,
    ClassificationModelCreate,
    ClassificationModelRead,
    ClassificationModelVersionRead,
    ClassificationTrainingRunRead,
)
from app.workflows.dispatcher import (
    cancel_classification_model_build,
    enqueue_classification_model_build,
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
            "active_model_version_id": model.active_model_version_id,
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


def model_version_read(version: ClassificationModelVersion) -> ClassificationModelVersionRead:
    return ClassificationModelVersionRead.model_validate(
        {
            "id": version.id,
            "tenant_id": version.tenant_id,
            "matter_id": version.matter_id,
            "classification_model_id": version.classification_model_id,
            "configuration_id": version.configuration_id,
            "training_run_id": version.training_run_id,
            "dataset_snapshot_id": version.dataset_snapshot_id,
            "version_number": version.version_number,
            "status": version.status,
            "target_field_snapshot": version.target_field_snapshot,
            "training_aggregate_hash": version.training_aggregate_hash,
            "feature_specification_hash": version.feature_specification_hash,
            "model_artifact_id": version.model_artifact_id,
            "model_content_hash": version.model_content_hash,
            "prediction_artifact_id": version.prediction_artifact_id,
            "prediction_content_hash": version.prediction_content_hash,
            "evaluation_artifact_id": version.evaluation_artifact_id,
            "evaluation_content_hash": version.evaluation_content_hash,
            "result_manifest_artifact_id": version.result_manifest_artifact_id,
            "result_manifest_content_hash": version.result_manifest_content_hash,
            "package_schema_version": version.package_schema_version,
            "runtime_image_digest": version.runtime_image_digest,
            "source_revision": version.source_revision,
            "metric_summary": version.metric_summary,
            "warnings": version.warnings,
            "created_at": version.created_at,
            "created_by_user_id": version.created_by_user_id,
        }
    )


def training_run_read(db: Session, run: ClassificationTrainingRun) -> ClassificationTrainingRunRead:
    version = db.scalar(
        select(ClassificationModelVersion).where(ClassificationModelVersion.training_run_id == run.id)
    )
    return ClassificationTrainingRunRead.model_validate(
        {
            "id": run.id,
            "tenant_id": run.tenant_id,
            "matter_id": run.matter_id,
            "classification_model_id": run.classification_model_id,
            "configuration_id": run.configuration_id,
            "dataset_snapshot_id": run.dataset_snapshot_id,
            "idempotency_key": run.idempotency_key,
            "workflow_id": run.workflow_id,
            "derivation_key": run.derivation_key,
            "trainer_specification": run.trainer_specification,
            "validation_specification": run.validation_specification,
            "random_seed": run.random_seed,
            "package_schema_version": run.package_schema_version,
            "runtime_image_digest": run.runtime_image_digest,
            "source_revision": run.source_revision,
            "status": run.status,
            "current_stage": run.current_stage,
            "progress_completed": run.progress_completed,
            "progress_total": run.progress_total,
            "model_artifact_id": run.model_artifact_id,
            "model_content_hash": run.model_content_hash,
            "prediction_artifact_id": run.prediction_artifact_id,
            "prediction_content_hash": run.prediction_content_hash,
            "evaluation_artifact_id": run.evaluation_artifact_id,
            "evaluation_content_hash": run.evaluation_content_hash,
            "result_manifest_artifact_id": run.result_manifest_artifact_id,
            "result_manifest_content_hash": run.result_manifest_content_hash,
            "failure_code": run.failure_code,
            "failure_summary": run.failure_summary,
            "failure_retriable": run.failure_retriable,
            "model_version": model_version_read(version) if version is not None else None,
            "created_by_user_id": run.created_by_user_id,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "canceled_at": run.canceled_at,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "row_version": run.row_version,
        }
    )


def dataset_snapshot_read(
    snapshot: ClassificationDatasetSnapshot,
) -> ClassificationDatasetSnapshotRead:
    return ClassificationDatasetSnapshotRead.model_validate(
        {
            "id": snapshot.id,
            "tenant_id": snapshot.tenant_id,
            "matter_id": snapshot.matter_id,
            "classification_model_id": snapshot.classification_model_id,
            "configuration_id": snapshot.configuration_id,
            "purpose": snapshot.purpose,
            "training_scope": {
                "scope_type": snapshot.scope_type,
                "scope_id": snapshot.scope_id,
            },
            "target_field_snapshot": snapshot.target_field_snapshot,
            "label_policy_snapshot": snapshot.label_policy_snapshot,
            "population_artifact_id": snapshot.population_artifact_id,
            "population_content_hash": snapshot.population_content_hash,
            "label_artifact_id": snapshot.label_artifact_id,
            "label_content_hash": snapshot.label_content_hash,
            "feature_artifact_id": snapshot.feature_artifact_id,
            "feature_content_hash": snapshot.feature_content_hash,
            "feature_specification": snapshot.feature_specification,
            "feature_specification_hash": snapshot.feature_specification_hash,
            "split_artifact_id": snapshot.split_artifact_id,
            "split_content_hash": snapshot.split_content_hash,
            "split_specification": snapshot.split_specification,
            "split_specification_hash": snapshot.split_specification_hash,
            "training_aggregate_hash": snapshot.training_aggregate_hash,
            "aggregate_hash": snapshot.aggregate_hash,
            "status": snapshot.status,
            "population_count": snapshot.population_count,
            "labeled_count": snapshot.labeled_count,
            "unlabeled_count": snapshot.unlabeled_count,
            "excluded_count": snapshot.excluded_count,
            "conflicting_count": snapshot.conflicting_count,
            "family_grouped_count": snapshot.family_grouped_count,
            "exact_duplicate_grouped_count": snapshot.exact_duplicate_grouped_count,
            "missing_grouping_count": snapshot.missing_grouping_count,
            "feature_ready_count": snapshot.feature_ready_count,
            "feature_missing_count": snapshot.feature_missing_count,
            "feature_failed_count": snapshot.feature_failed_count,
            "training_eligible_count": snapshot.training_eligible_count,
            "class_support": snapshot.class_support or {},
            "source_summary": snapshot.source_summary or [],
            "warnings": snapshot.warnings or [],
            "failure_code": snapshot.failure_code,
            "failure_summary": snapshot.failure_summary,
            "created_by_user_id": snapshot.created_by_user_id,
            "created_at": snapshot.created_at,
            "completed_at": snapshot.completed_at,
        }
    )


def command_error(exc: ClassificationModelCommandError) -> HTTPException:
    error_status = (
        status.HTTP_404_NOT_FOUND
        if isinstance(exc, ClassificationModelNotFoundError)
        else status.HTTP_409_CONFLICT
    )
    return HTTPException(status_code=error_status, detail=str(exc))


def dataset_error(exc: ClassificationDatasetError) -> HTTPException:
    error_status = (
        status.HTTP_404_NOT_FOUND
        if isinstance(exc, ClassificationDatasetNotFoundError)
        else status.HTTP_409_CONFLICT
    )
    return HTTPException(status_code=error_status, detail=str(exc))


def build_error(exc: ClassificationBuildError) -> HTTPException:
    error_status = (
        status.HTTP_404_NOT_FOUND
        if isinstance(exc, ClassificationBuildNotFoundError)
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


@router.post(
    "/{classification_model_id}/dataset-snapshots/preview",
    response_model=ClassificationDatasetSnapshotPreview,
)
def preview_classification_dataset_snapshot(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    payload: ClassificationDatasetSnapshotRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ClassificationDatasetSnapshotPreview:
    matter = get_authorized_matter(db, principal, matter_id)
    try:
        prepared = prepare_dataset_snapshot(
            db,
            matter=matter,
            classification_model_id=classification_model_id,
            actor_user_id=principal.user.id,
            request=payload,
        )
        return prepared.preview
    except ClassificationModelCommandError as exc:
        raise command_error(exc) from exc
    except ClassificationDatasetError as exc:
        raise dataset_error(exc) from exc


@router.post(
    "/{classification_model_id}/dataset-snapshots",
    response_model=ClassificationDatasetSnapshotCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_classification_dataset_snapshot(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    payload: ClassificationDatasetSnapshotRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ClassificationDatasetSnapshotCreateResponse:
    matter = get_authorized_matter(db, principal, matter_id)
    try:
        prepared = prepare_dataset_snapshot(
            db,
            matter=matter,
            classification_model_id=classification_model_id,
            actor_user_id=principal.user.id,
            request=payload,
        )
        snapshot, should_materialize, created = begin_dataset_snapshot(
            db,
            prepared=prepared,
            actor_user_id=principal.user.id,
        )
        if not should_materialize:
            return ClassificationDatasetSnapshotCreateResponse(
                snapshot=dataset_snapshot_read(snapshot),
                created=False,
            )
        db.commit()
    except ClassificationModelCommandError as exc:
        db.rollback()
        raise command_error(exc) from exc
    except ClassificationDatasetError as exc:
        db.rollback()
        raise dataset_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An identical classification dataset snapshot already exists",
        ) from exc

    try:
        materialize_dataset_snapshot(
            db,
            snapshot=snapshot,
            prepared=prepared,
            actor_user_id=principal.user.id,
        )
        db.commit()
        return ClassificationDatasetSnapshotCreateResponse(
            snapshot=dataset_snapshot_read(snapshot),
            created=created,
        )
    except Exception as exc:
        db.rollback()
        persisted = db.get(ClassificationDatasetSnapshot, snapshot.id)
        if persisted is not None:
            fail_dataset_snapshot(
                persisted,
                failure_code="ARTIFACT_MATERIALIZATION_FAILED",
                failure_summary=str(exc),
            )
            db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Classification dataset artifact materialization failed",
        ) from exc


@router.get(
    "/{classification_model_id}/dataset-snapshots",
    response_model=list[ClassificationDatasetSnapshotRead],
)
def list_classification_dataset_snapshots(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ClassificationDatasetSnapshotRead]:
    get_authorized_matter(db, principal, matter_id)
    try:
        get_classification_model_record(
            db,
            matter_id=matter_id,
            classification_model_id=classification_model_id,
        )
    except ClassificationModelCommandError as exc:
        raise command_error(exc) from exc
    snapshots = db.scalars(
        select(ClassificationDatasetSnapshot)
        .where(
            ClassificationDatasetSnapshot.matter_id == matter_id,
            ClassificationDatasetSnapshot.classification_model_id == classification_model_id,
        )
        .order_by(ClassificationDatasetSnapshot.created_at, ClassificationDatasetSnapshot.id)
    ).all()
    return [dataset_snapshot_read(snapshot) for snapshot in snapshots]


@router.post(
    "/{classification_model_id}/dataset-snapshots/{snapshot_id}/features-and-splits",
    response_model=ClassificationFeatureSplitCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def materialize_classification_features_and_splits(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    payload: ClassificationFeatureSplitRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ClassificationFeatureSplitCreateResponse:
    get_authorized_matter(db, principal, matter_id)
    try:
        snapshot = get_dataset_snapshot(
            db,
            matter_id=matter_id,
            classification_model_id=classification_model_id,
            snapshot_id=snapshot_id,
        )
        if snapshot.status == "READY_FOR_TRAINING":
            if request_matches_materialized_snapshot(snapshot, payload):
                return ClassificationFeatureSplitCreateResponse(
                    snapshot=dataset_snapshot_read(snapshot),
                    materialized=False,
                )
            raise ClassificationFeatureSplitError(
                "A ready dataset snapshot is immutable; create a new snapshot for different feature or split inputs",
                code="DATASET_SNAPSHOT_IMMUTABLE",
            )
        begin_feature_split(snapshot)
        db.commit()
    except ClassificationDatasetError as exc:
        db.rollback()
        raise dataset_error(exc) from exc

    try:
        prepared = prepare_feature_split(
            db,
            snapshot=snapshot,
            request=payload,
            actor_user_id=principal.user.id,
        )
        materialize_feature_split(
            db,
            snapshot=snapshot,
            prepared=prepared,
            actor_user_id=principal.user.id,
        )
        db.commit()
        return ClassificationFeatureSplitCreateResponse(
            snapshot=dataset_snapshot_read(snapshot),
            materialized=True,
        )
    except ClassificationFeatureSplitError as exc:
        db.rollback()
        persisted = db.get(ClassificationDatasetSnapshot, snapshot_id)
        if persisted is not None:
            fail_dataset_snapshot(
                persisted,
                failure_code=exc.code,
                failure_summary=str(exc),
            )
            db.commit()
        raise dataset_error(exc) from exc
    except Exception as exc:
        db.rollback()
        persisted = db.get(ClassificationDatasetSnapshot, snapshot_id)
        if persisted is not None:
            fail_dataset_snapshot(
                persisted,
                failure_code="FEATURE_SPLIT_MATERIALIZATION_FAILED",
                failure_summary=str(exc),
            )
            db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Classification feature and split materialization failed",
        ) from exc


@router.get(
    "/{classification_model_id}/dataset-snapshots/{snapshot_id}",
    response_model=ClassificationDatasetSnapshotRead,
)
def read_classification_dataset_snapshot(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ClassificationDatasetSnapshotRead:
    get_authorized_matter(db, principal, matter_id)
    try:
        snapshot = get_dataset_snapshot(
            db,
            matter_id=matter_id,
            classification_model_id=classification_model_id,
            snapshot_id=snapshot_id,
        )
        return dataset_snapshot_read(snapshot)
    except ClassificationDatasetError as exc:
        raise dataset_error(exc) from exc


@router.post(
    "/{classification_model_id}/builds/preview",
    response_model=ClassificationModelBuildPreview,
)
def preview_classification_model_build(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    payload: ClassificationModelBuildPreviewRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ClassificationModelBuildPreview:
    get_authorized_matter(db, principal, matter_id)
    try:
        prepared = prepare_build(
            db,
            matter_id=matter_id,
            classification_model_id=classification_model_id,
            dataset_snapshot_id=payload.dataset_snapshot_id,
        )
        return build_preview(prepared)
    except ClassificationBuildError as exc:
        raise build_error(exc) from exc


@router.post(
    "/{classification_model_id}/builds",
    response_model=ClassificationModelBuildCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_classification_model_build(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    payload: ClassificationModelBuildCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ClassificationModelBuildCreateResponse:
    get_authorized_matter(db, principal, matter_id)
    try:
        prepared = prepare_build(
            db,
            matter_id=matter_id,
            classification_model_id=classification_model_id,
            dataset_snapshot_id=payload.dataset_snapshot_id,
        )
        run, created = create_training_run(
            db,
            prepared=prepared,
            idempotency_key=payload.idempotency_key,
            actor_user_id=principal.user.id,
        )
        if created:
            enqueue_classification_model_build(db, run.workflow_id, str(run.id))
        db.commit()
        db.refresh(run)
        return ClassificationModelBuildCreateResponse(
            training_run=training_run_read(db, run),
            created=created,
        )
    except ClassificationBuildError as exc:
        db.rollback()
        raise build_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A conflicting classification model build already exists",
        ) from exc


@router.get(
    "/{classification_model_id}/builds",
    response_model=list[ClassificationTrainingRunRead],
)
def list_classification_model_builds(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ClassificationTrainingRunRead]:
    get_authorized_matter(db, principal, matter_id)
    try:
        get_classification_model_record(
            db,
            matter_id=matter_id,
            classification_model_id=classification_model_id,
        )
    except ClassificationModelCommandError as exc:
        raise command_error(exc) from exc
    runs = db.scalars(
        select(ClassificationTrainingRun)
        .where(
            ClassificationTrainingRun.matter_id == matter_id,
            ClassificationTrainingRun.classification_model_id == classification_model_id,
        )
        .order_by(ClassificationTrainingRun.created_at.desc(), ClassificationTrainingRun.id)
    ).all()
    return [training_run_read(db, run) for run in runs]


@router.get(
    "/{classification_model_id}/builds/{training_run_id}",
    response_model=ClassificationTrainingRunRead,
)
def read_classification_model_build(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    training_run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ClassificationTrainingRunRead:
    get_authorized_matter(db, principal, matter_id)
    try:
        run = get_training_run(
            db,
            matter_id=matter_id,
            classification_model_id=classification_model_id,
            training_run_id=training_run_id,
        )
        return training_run_read(db, run)
    except ClassificationBuildError as exc:
        raise build_error(exc) from exc


@router.post(
    "/{classification_model_id}/builds/{training_run_id}/cancel",
    response_model=ClassificationTrainingRunRead,
)
def cancel_classification_model_build_run(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    training_run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ClassificationTrainingRunRead:
    get_authorized_matter(db, principal, matter_id)
    try:
        run = get_training_run(
            db,
            matter_id=matter_id,
            classification_model_id=classification_model_id,
            training_run_id=training_run_id,
            for_update=True,
        )
        canceled = cancel_training_run(run)
        if canceled:
            record_audit(
                db,
                tenant_id=run.tenant_id,
                actor_user_id=principal.user.id,
                action="classification_training_run.canceled",
                target_type="classification_training_run",
                target_id=run.id,
                details={"classification_model_id": str(run.classification_model_id)},
            )
        db.commit()
        if canceled:
            cancel_classification_model_build(run.workflow_id)
        db.refresh(run)
        return training_run_read(db, run)
    except ClassificationBuildError as exc:
        db.rollback()
        raise build_error(exc) from exc


@router.post(
    "/{classification_model_id}/builds/{training_run_id}/reconcile",
    response_model=ClassificationTrainingRunRead,
)
def reconcile_classification_model_build(
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    training_run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ClassificationTrainingRunRead:
    get_authorized_matter(db, principal, matter_id)
    try:
        run = get_training_run(
            db,
            matter_id=matter_id,
            classification_model_id=classification_model_id,
            training_run_id=training_run_id,
            for_update=True,
        )
        reconcile_training_run(db, run=run)
        db.refresh(run)
        return training_run_read(db, run)
    except ClassificationBuildError as exc:
        db.rollback()
        raise build_error(exc) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Classification build reconciliation failed",
        ) from exc


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
