import hashlib
import io
import json
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.artifact_gateway import copy_workflow_artifact_content, find_workflow_artifact_reference
from app.audit import record_audit
from app.classification_gateway import (
    ClassificationArtifactAccess,
    ClassificationGateway,
    ClassificationGatewayError,
)
from app.classification_models import (
    CLASSIFICATION_TARGET_TYPES,
    effective_binary_mapping,
    effective_prediction_policy,
    target_schema_fingerprint,
    trained_class_keys,
)
from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models import (
    ClassificationDatasetSnapshot,
    ClassificationEvaluation,
    ClassificationModel,
    ClassificationModelConfiguration,
    ClassificationModelVersion,
    ClassificationTrainingRun,
    ClientMembership,
    Matter,
    MatterMembership,
    User,
)
from app.schemas import (
    ClassificationDatasetIssue,
    ClassificationModelBuildPreview,
)
from classification_service.hashing import (
    canonical_value_hash,
    classification_output_artifact_derivation_key,
)
from classification_service.schemas import (
    ArtifactReference,
    ArtifactType,
    ClassificationStrategy,
    ClassificationTrainingRequest,
    ClassificationTrainingResult,
    FeatureContract,
    LabelPolicy,
    PackageValidationResult,
    TargetFieldSnapshot,
    TrainerSpecification,
    TrainingScopeSpecification,
    TrainingScopeType,
    ValidationPolicy,
)

PACKAGE_SCHEMA_VERSION = 1
METRIC_POLICY_VERSION = 1


class ClassificationBuildError(ValueError):
    def __init__(self, message: str, *, code: str = "CLASSIFICATION_BUILD_ERROR") -> None:
        super().__init__(message)
        self.code = code


class ClassificationBuildNotFoundError(ClassificationBuildError):
    pass


class ClassificationBuildConflictError(ClassificationBuildError):
    pass


@dataclass(frozen=True)
class PreparedBuild:
    model: ClassificationModel
    configuration: ClassificationModelConfiguration
    snapshot: ClassificationDatasetSnapshot
    validation_policy: ValidationPolicy
    random_seed: int
    derivation_key: str
    blocking_errors: list[ClassificationDatasetIssue]
    warnings: list[ClassificationDatasetIssue]
    existing_run_id: uuid.UUID | None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _artifact_reference(
    artifact_id: uuid.UUID | None,
    artifact_type: ArtifactType,
    content_hash: str | None,
    media_type: str,
) -> ArtifactReference:
    if artifact_id is None or content_hash is None:
        raise ClassificationBuildConflictError(
            f"Dataset snapshot is missing {artifact_type.value}",
            code="DATASET_ARTIFACT_MISSING",
        )
    return ArtifactReference(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        schema_version=1,
        content_hash=content_hash,
        media_type=media_type,
    )


def _configuration_issues(
    model: ClassificationModel,
    configuration: ClassificationModelConfiguration,
) -> list[ClassificationDatasetIssue]:
    issues: list[ClassificationDatasetIssue] = []
    definition = model.target_metadata_definition
    snapshot = configuration.target_field_snapshot
    if model.status != "ACTIVE":
        issues.append(ClassificationDatasetIssue(code="MODEL_NOT_ACTIVE", message="Classification model is not active"))
    if (
        definition.status != "ACTIVE"
        or definition.value_source != "ASSERTED"
        or definition.type not in CLASSIFICATION_TARGET_TYPES
    ):
        issues.append(
            ClassificationDatasetIssue(
                code="TARGET_FIELD_NOT_ELIGIBLE",
                message="Target field must be an active Boolean, enum, text, or long-text coding field",
            )
        )
    identity_matches = (
        snapshot.get("metadata_definition_id") == str(definition.id)
        and snapshot.get("stable_key") == definition.key
        and snapshot.get("metadata_type") == definition.type
        and snapshot.get("cardinality") == definition.cardinality
    )
    if not identity_matches:
        issues.append(
            ClassificationDatasetIssue(
                code="TARGET_FIELD_IDENTITY_CHANGED",
                message="Configured target field identity is no longer compatible",
            )
        )
    return issues


def _build_derivation(
    *,
    configuration: ClassificationModelConfiguration,
    snapshot: ClassificationDatasetSnapshot,
    validation_policy: ValidationPolicy,
    random_seed: int,
    settings: Settings,
) -> str:
    return canonical_value_hash(
        {
            "contract_version": 1,
            "configuration_hash": configuration.configuration_hash,
            "training_aggregate_hash": snapshot.training_aggregate_hash,
            "feature_specification_hash": snapshot.feature_specification_hash,
            "trainer_specification": configuration.trainer_specification,
            "validation_policy": validation_policy.model_dump(mode="json"),
            "random_seed": random_seed,
            "runtime_image_digest": settings.classification_runtime_image_digest,
            "source_revision": settings.classification_source_revision,
            "package_schema_version": PACKAGE_SCHEMA_VERSION,
        }
    )


def prepare_build(
    db: Session,
    *,
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    dataset_snapshot_id: uuid.UUID,
    settings: Settings | None = None,
) -> PreparedBuild:
    model = db.scalar(
        select(ClassificationModel).where(
            ClassificationModel.id == classification_model_id,
            ClassificationModel.matter_id == matter_id,
        )
    )
    if model is None:
        raise ClassificationBuildNotFoundError("Classification model not found")
    snapshot = db.scalar(
        select(ClassificationDatasetSnapshot).where(
            ClassificationDatasetSnapshot.id == dataset_snapshot_id,
            ClassificationDatasetSnapshot.classification_model_id == model.id,
            ClassificationDatasetSnapshot.matter_id == matter_id,
        )
    )
    if snapshot is None:
        raise ClassificationBuildNotFoundError("Classification dataset snapshot not found")
    configuration = db.scalar(
        select(ClassificationModelConfiguration).where(
            ClassificationModelConfiguration.id == snapshot.configuration_id,
            ClassificationModelConfiguration.classification_model_id == model.id,
        )
    )
    if configuration is None:
        raise ClassificationBuildConflictError("Dataset configuration is unavailable")

    issues = _configuration_issues(model, configuration)
    required_values = (
        snapshot.training_aggregate_hash,
        snapshot.feature_specification_hash,
        snapshot.feature_artifact_id,
        snapshot.feature_content_hash,
        snapshot.label_artifact_id,
        snapshot.label_content_hash,
        snapshot.split_artifact_id,
        snapshot.split_content_hash,
    )
    if snapshot.status != "READY_FOR_TRAINING" or any(value is None for value in required_values):
        issues.append(
            ClassificationDatasetIssue(
                code="DATASET_NOT_READY_FOR_TRAINING",
                message="Dataset snapshot must have complete frozen training artifacts",
            )
        )
    configured_target = configuration.target_field_snapshot
    frozen_target = snapshot.target_field_snapshot
    if any(
        frozen_target.get(key) != configured_target.get(key)
        for key in ("tenant_id", "matter_id", "metadata_definition_id", "stable_key", "metadata_type", "cardinality")
    ):
        issues.append(
            ClassificationDatasetIssue(
                code="DATASET_CONFIGURATION_MISMATCH",
                message="Dataset target field snapshot does not match its configuration",
            )
        )
    split_specification = snapshot.split_specification or {}
    try:
        validation_policy = ValidationPolicy.model_validate(split_specification["validation_policy"])
        random_seed = int(split_specification["random_seed"])
    except (KeyError, TypeError, ValueError):
        validation_policy = ValidationPolicy()
        random_seed = 42
        issues.append(
            ClassificationDatasetIssue(
                code="SPLIT_SPECIFICATION_INVALID",
                message="Dataset split specification is missing or invalid",
            )
        )
    resolved_settings = settings or get_settings()
    derivation_key = _build_derivation(
        configuration=configuration,
        snapshot=snapshot,
        validation_policy=validation_policy,
        random_seed=random_seed,
        settings=resolved_settings,
    )
    existing_id = db.scalar(
        select(ClassificationTrainingRun.id).where(
            ClassificationTrainingRun.classification_model_id == model.id,
            ClassificationTrainingRun.derivation_key == derivation_key,
        )
    )
    warnings = [
        ClassificationDatasetIssue.model_validate(value)
        for value in snapshot.warnings or []
    ]
    if target_schema_fingerprint(model.target_metadata_definition) != configuration.target_field_snapshot.get(
        "schema_fingerprint"
    ):
        warnings.append(
            ClassificationDatasetIssue(
                code="TARGET_SCHEMA_FINGERPRINT_CHANGED",
                message="The target schema fingerprint changed; compatibility checks still passed",
            )
        )
    return PreparedBuild(
        model=model,
        configuration=configuration,
        snapshot=snapshot,
        validation_policy=validation_policy,
        random_seed=random_seed,
        derivation_key=derivation_key,
        blocking_errors=issues,
        warnings=warnings,
        existing_run_id=existing_id,
    )


def build_preview(prepared: PreparedBuild, settings: Settings | None = None) -> ClassificationModelBuildPreview:
    resolved_settings = settings or get_settings()
    return ClassificationModelBuildPreview(
        classification_model_id=prepared.model.id,
        configuration_id=prepared.configuration.id,
        dataset_snapshot_id=prepared.snapshot.id,
        configuration_hash=prepared.configuration.configuration_hash,
        training_aggregate_hash=prepared.snapshot.training_aggregate_hash,
        feature_specification_hash=prepared.snapshot.feature_specification_hash,
        validation_specification=prepared.validation_policy,
        random_seed=prepared.random_seed,
        runtime_image_digest=resolved_settings.classification_runtime_image_digest,
        source_revision=resolved_settings.classification_source_revision,
        package_schema_version=PACKAGE_SCHEMA_VERSION,
        derivation_key=prepared.derivation_key,
        training_eligible_count=prepared.snapshot.training_eligible_count,
        can_launch=not prepared.blocking_errors,
        blocking_errors=prepared.blocking_errors,
        warnings=prepared.warnings,
        existing_training_run_id=prepared.existing_run_id,
    )


def create_training_run(
    db: Session,
    *,
    prepared: PreparedBuild,
    idempotency_key: str,
    actor_user_id: uuid.UUID,
    settings: Settings | None = None,
) -> tuple[ClassificationTrainingRun, bool]:
    existing = db.scalar(
        select(ClassificationTrainingRun).where(
            ClassificationTrainingRun.classification_model_id == prepared.model.id,
            ClassificationTrainingRun.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.derivation_key != prepared.derivation_key:
            raise ClassificationBuildConflictError(
                "Idempotency key was already used for different build inputs",
                code="IDEMPOTENCY_KEY_REUSED",
            )
        return existing, False
    existing = db.scalar(
        select(ClassificationTrainingRun).where(
            ClassificationTrainingRun.classification_model_id == prepared.model.id,
            ClassificationTrainingRun.derivation_key == prepared.derivation_key,
        )
    )
    if existing is not None:
        return existing, False
    if prepared.blocking_errors:
        raise ClassificationBuildConflictError(
            "Classification build preview contains blocking errors",
            code="BUILD_BLOCKED",
        )
    resolved_settings = settings or get_settings()
    run_id = uuid.uuid5(prepared.model.id, prepared.derivation_key)
    run = ClassificationTrainingRun(
        id=run_id,
        tenant_id=prepared.model.tenant_id,
        matter_id=prepared.model.matter_id,
        classification_model_id=prepared.model.id,
        configuration_id=prepared.configuration.id,
        dataset_snapshot_id=prepared.snapshot.id,
        idempotency_key=idempotency_key,
        workflow_id=f"classification-model-build:{run_id}",
        derivation_key=prepared.derivation_key,
        trainer_specification=prepared.configuration.trainer_specification,
        validation_specification=prepared.validation_policy.model_dump(mode="json"),
        random_seed=prepared.random_seed,
        package_schema_version=PACKAGE_SCHEMA_VERSION,
        runtime_image_digest=resolved_settings.classification_runtime_image_digest,
        source_revision=resolved_settings.classification_source_revision,
        status="QUEUED",
        current_stage="QUEUED",
        progress_completed=0,
        progress_total=5,
        created_by_user_id=actor_user_id,
        execution_actor_user_id=actor_user_id,
    )
    db.add(run)
    db.flush()
    record_audit(
        db,
        tenant_id=run.tenant_id,
        actor_user_id=actor_user_id,
        action="classification_training_run.created",
        target_type="classification_training_run",
        target_id=run.id,
        details={
            "matter_id": str(run.matter_id),
            "classification_model_id": str(run.classification_model_id),
            "configuration_id": str(run.configuration_id),
            "dataset_snapshot_id": str(run.dataset_snapshot_id),
            "derivation_key": run.derivation_key,
        },
    )
    return run, True


def resolve_existing_training_run(
    db: Session,
    *,
    classification_model_id: uuid.UUID,
    idempotency_key: str,
    derivation_key: str,
) -> ClassificationTrainingRun:
    existing = db.scalar(
        select(ClassificationTrainingRun).where(
            ClassificationTrainingRun.classification_model_id == classification_model_id,
            ClassificationTrainingRun.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.derivation_key != derivation_key:
            raise ClassificationBuildConflictError(
                "Idempotency key was already used for different build inputs",
                code="IDEMPOTENCY_KEY_REUSED",
            )
        return existing
    existing = db.scalar(
        select(ClassificationTrainingRun).where(
            ClassificationTrainingRun.classification_model_id == classification_model_id,
            ClassificationTrainingRun.derivation_key == derivation_key,
        )
    )
    if existing is None:
        raise ClassificationBuildConflictError(
            "Concurrent classification build could not be resolved",
            code="IDEMPOTENCY_RACE_UNRESOLVED",
        )
    return existing


def retry_training_run(
    db: Session,
    *,
    run: ClassificationTrainingRun,
    actor_user_id: uuid.UUID,
) -> ClassificationTrainingRun:
    if run.status not in {"FAILED", "CANCELED"}:
        raise ClassificationBuildConflictError(
            "Only failed or canceled classification builds can be retried",
            code="BUILD_NOT_FAILED",
        )
    if run.status == "FAILED" and not run.failure_retriable:
        raise ClassificationBuildConflictError(
            "Classification build failure is not retriable",
            code="BUILD_FAILURE_NOT_RETRIABLE",
        )
    version = db.scalar(
        select(ClassificationModelVersion.id).where(
            ClassificationModelVersion.training_run_id == run.id
        )
    )
    if version is not None:
        raise ClassificationBuildConflictError(
            "Classification build already produced a model version",
            code="BUILD_ALREADY_FINALIZED",
        )
    run.attempt_number += 1
    run.workflow_id = f"classification-model-build:{run.id}:attempt:{run.attempt_number}"
    run.execution_actor_user_id = actor_user_id
    run.status = "QUEUED"
    run.current_stage = "QUEUED"
    run.progress_completed = 0
    run.failure_code = None
    run.failure_summary = None
    run.failure_retriable = False
    run.started_at = None
    run.completed_at = None
    run.canceled_at = None
    record_audit(
        db,
        tenant_id=run.tenant_id,
        actor_user_id=actor_user_id,
        action="classification_training_run.retried",
        target_type="classification_training_run",
        target_id=run.id,
        details={
            "classification_model_id": str(run.classification_model_id),
            "attempt_number": run.attempt_number,
            "workflow_id": run.workflow_id,
        },
    )
    db.flush()
    return run


def get_training_run(
    db: Session,
    *,
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    training_run_id: uuid.UUID,
    for_update: bool = False,
) -> ClassificationTrainingRun:
    statement = select(ClassificationTrainingRun).where(
        ClassificationTrainingRun.id == training_run_id,
        ClassificationTrainingRun.matter_id == matter_id,
        ClassificationTrainingRun.classification_model_id == classification_model_id,
    )
    if for_update:
        statement = statement.with_for_update()
    run = db.scalar(statement)
    if run is None:
        raise ClassificationBuildNotFoundError("Classification training run not found")
    return run


def _actor_is_authorized(db: Session, run: ClassificationTrainingRun) -> bool:
    user = db.get(User, run.execution_actor_user_id)
    matter = db.get(Matter, run.matter_id)
    if user is None or matter is None or user.status != "ACTIVE" or user.tenant.status != "ACTIVE":
        return False
    if matter.status != "ACTIVE" or matter.client.status != "ACTIVE" or matter.client.tenant.status != "ACTIVE":
        return False
    if user.tenant.is_root and user.tenant_role == "ADMIN":
        return True
    if user.tenant_id == matter.client.tenant_id and user.tenant_role == "ADMIN":
        return True
    if db.scalar(
        select(ClientMembership.id).where(
            ClientMembership.client_id == matter.client_id,
            ClientMembership.user_id == user.id,
            ClientMembership.role == "ADMIN",
        )
    ):
        return True
    return (
        db.scalar(
            select(MatterMembership.id).where(
                MatterMembership.matter_id == matter.id,
                MatterMembership.user_id == user.id,
                MatterMembership.role == "ADMIN",
            )
        )
        is not None
    )


def training_request(run: ClassificationTrainingRun) -> ClassificationTrainingRequest:
    configuration = run.configuration
    snapshot = run.dataset_snapshot
    ordered_class_keys = trained_class_keys(snapshot, configuration)
    return ClassificationTrainingRequest(
        training_run_id=run.id,
        tenant_id=run.tenant_id,
        matter_id=run.matter_id,
        classification_model_id=run.classification_model_id,
        configuration_id=run.configuration_id,
        configuration_hash=configuration.configuration_hash,
        training_scope=TrainingScopeSpecification(
            scope_type=TrainingScopeType(snapshot.scope_type),
            scope_id=snapshot.scope_id,
        ),
        target_field_snapshot=TargetFieldSnapshot.model_validate(snapshot.target_field_snapshot),
        classification_strategy=ClassificationStrategy(configuration.classification_strategy),
        ordered_class_keys=ordered_class_keys,
        binary_mapping=effective_binary_mapping(snapshot, configuration),
        label_policy=LabelPolicy.model_validate(configuration.label_policy),
        feature_contract=FeatureContract.model_validate(configuration.feature_contract),
        trainer_specification=TrainerSpecification.model_validate(configuration.trainer_specification),
        feature_artifact=_artifact_reference(
            snapshot.feature_artifact_id,
            ArtifactType.CLASSIFICATION_FEATURES,
            snapshot.feature_content_hash,
            "application/vnd.apache.parquet",
        ),
        label_artifact=_artifact_reference(
            snapshot.label_artifact_id,
            ArtifactType.CLASSIFICATION_LABELS,
            snapshot.label_content_hash,
            "application/vnd.apache.parquet",
        ),
        split_artifact=_artifact_reference(
            snapshot.split_artifact_id,
            ArtifactType.CLASSIFICATION_SPLITS,
            snapshot.split_content_hash,
            "application/vnd.apache.parquet",
        ),
        validation_policy=ValidationPolicy.model_validate(run.validation_specification),
        prediction_resolution_policy=effective_prediction_policy(snapshot, configuration),
        random_seed=run.random_seed,
        output_derivation_key=run.derivation_key,
    )


def prepare_runtime_invocation(
    training_run_id: uuid.UUID,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> dict[str, Any] | None:
    with session_factory() as db:
        run = db.scalar(
            select(ClassificationTrainingRun)
            .where(ClassificationTrainingRun.id == training_run_id)
            .with_for_update()
        )
        if run is None:
            raise ClassificationBuildNotFoundError("Classification training run not found")
        if run.status == "CANCELED":
            return None
        if run.status == "COMPLETED":
            return {"completed": True}
        if not _actor_is_authorized(db, run):
            raise ClassificationBuildConflictError(
                "Build creator is no longer authorized for the matter",
                code="BUILD_AUTHORIZATION_REVOKED",
            )
        prepared = prepare_build(
            db,
            matter_id=run.matter_id,
            classification_model_id=run.classification_model_id,
            dataset_snapshot_id=run.dataset_snapshot_id,
        )
        if prepared.blocking_errors or prepared.derivation_key != run.derivation_key:
            raise ClassificationBuildConflictError(
                "Frozen build inputs are no longer valid",
                code="FROZEN_BUILD_INPUT_INVALID",
            )
        run.status = "WAITING_FOR_RUNTIME"
        run.current_stage = "DISPATCHING_RUNTIME"
        run.progress_completed = 1
        run.started_at = run.started_at or utcnow()
        run.failure_code = None
        run.failure_summary = None
        run.failure_retriable = False
        request = training_request(run)
        client_id = run.classification_model.matter.client_id
        db.commit()
        return {
            "completed": False,
            "request": request.model_dump(mode="json"),
            "access": {
                "tenant_id": str(run.tenant_id),
                "client_id": str(client_id),
                "actor_user_id": str(run.execution_actor_user_id),
            },
        }


def invoke_runtime(
    invocation: dict[str, Any],
    *,
    gateway: ClassificationGateway | None = None,
    published_result_verifier: Callable[
        [ClassificationTrainingResult, ClassificationArtifactAccess],
        dict[str, Any],
    ]
    | None = None,
) -> dict[str, Any]:
    if invocation.get("completed"):
        return invocation
    request = ClassificationTrainingRequest.model_validate(invocation["request"])
    access = ClassificationArtifactAccess(
        tenant_id=uuid.UUID(invocation["access"]["tenant_id"]),
        client_id=uuid.UUID(invocation["access"]["client_id"]),
        actor_user_id=uuid.UUID(invocation["access"]["actor_user_id"]),
    )
    resolved_gateway = gateway or ClassificationGateway()
    result = resolved_gateway.train(request, access)
    result_manifest = (published_result_verifier or verify_published_training_result)(result, access)
    package = resolved_gateway.validate_package(result.model_artifact, access)
    return {
        "completed": False,
        "result": result.model_dump(mode="json"),
        "result_manifest": result_manifest,
        "package": package.model_dump(mode="json"),
    }


def mark_training_runtime_started(
    training_run_id: uuid.UUID,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    with session_factory() as db:
        run = db.scalar(
            select(ClassificationTrainingRun)
            .where(ClassificationTrainingRun.id == training_run_id)
            .with_for_update()
        )
        if run is None:
            raise ClassificationBuildNotFoundError("Classification training run not found")
        if run.status in {"CANCELED", "COMPLETED"}:
            return
        run.status = "TRAINING"
        run.current_stage = "FITTING_AND_EVALUATING_MODEL"
        run.progress_completed = 2
        db.commit()


def mark_training_runtime_completed(
    training_run_id: uuid.UUID,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    with session_factory() as db:
        run = db.scalar(
            select(ClassificationTrainingRun)
            .where(ClassificationTrainingRun.id == training_run_id)
            .with_for_update()
        )
        if run is None:
            raise ClassificationBuildNotFoundError("Classification training run not found")
        if run.status in {"CANCELED", "COMPLETED"}:
            return
        run.status = "FINALIZING"
        run.current_stage = "VALIDATING_RUNTIME_OUTPUTS"
        run.progress_completed = 3
        db.commit()


def _validate_runtime_result(
    run: ClassificationTrainingRun,
    result: ClassificationTrainingResult,
    package: PackageValidationResult,
) -> None:
    configuration = run.configuration
    snapshot = run.dataset_snapshot
    manifest = package.manifest
    signature = package.signature
    feature_contract = FeatureContract.model_validate(configuration.feature_contract)
    ordered_class_keys = trained_class_keys(snapshot, configuration)
    prediction_policy = effective_prediction_policy(snapshot, configuration)
    if not package.valid:
        raise ClassificationBuildConflictError("Runtime package validation did not succeed")
    if result.training_run_id != run.id or result.output_derivation_key != run.derivation_key:
        raise ClassificationBuildConflictError("Runtime result does not match the training run")
    if result.model_content_hash != result.model_artifact.content_hash:
        raise ClassificationBuildConflictError("Runtime model content hash is inconsistent")
    if package.package_content_hash != result.model_artifact.content_hash:
        raise ClassificationBuildConflictError("Validated package hash does not match the runtime result")
    if (
        manifest.training_run_id != run.id
        or manifest.tenant_id != run.tenant_id
        or manifest.matter_id != run.matter_id
        or manifest.classification_model_id != run.classification_model_id
        or manifest.configuration_id != run.configuration_id
        or manifest.configuration_hash != configuration.configuration_hash
        or manifest.package_schema_version != run.package_schema_version
        or manifest.package_derivation_key != run.derivation_key
        or manifest.runtime_image_digest != run.runtime_image_digest
        or manifest.source_revision != run.source_revision
        or manifest.feature_specification_hash != snapshot.feature_specification_hash
        or manifest.target_field_snapshot.model_dump(mode="json") != snapshot.target_field_snapshot
        or manifest.classification_strategy.value != configuration.classification_strategy
        or manifest.ordered_class_keys != ordered_class_keys
        or manifest.trainer_specification.model_dump(mode="json") != run.trainer_specification
        or (
            manifest.prediction_resolution_policy is None
            or manifest.prediction_resolution_policy.model_dump(mode="json")
            != prediction_policy.model_dump(mode="json")
        )
    ):
        raise ClassificationBuildConflictError("Validated package identity does not match frozen build inputs")
    if (
        signature.package_schema_version != run.package_schema_version
        or signature.feature_dimensions != feature_contract.dimensions
        or signature.feature_data_type != feature_contract.data_type
        or signature.feature_specification_hash != snapshot.feature_specification_hash
        or signature.classification_strategy.value != configuration.classification_strategy
        or signature.ordered_class_keys != ordered_class_keys
    ):
        raise ClassificationBuildConflictError("Validated package signature does not match frozen build inputs")
    expected_hashes = {
        "features": snapshot.feature_content_hash,
        "labels": snapshot.label_content_hash,
        "splits": snapshot.split_content_hash,
    }
    if manifest.input_artifact_hashes != expected_hashes:
        raise ClassificationBuildConflictError("Validated package input hashes do not match the dataset snapshot")
    if result.runtime_image_digest != run.runtime_image_digest or result.package_schema_version != run.package_schema_version:
        raise ClassificationBuildConflictError("Runtime identity does not match the training run")


def finalize_training_run(
    training_run_id: uuid.UUID,
    runtime_output: dict[str, Any],
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    db: Session | None = None,
) -> uuid.UUID | None:
    if runtime_output.get("completed"):
        return None
    result = ClassificationTrainingResult.model_validate(runtime_output["result"])
    result_manifest = runtime_output.get("result_manifest")
    if not isinstance(result_manifest, dict):
        raise ClassificationBuildConflictError(
            "Verified runtime result manifest is missing",
            code="RESULT_MANIFEST_MISSING",
        )
    package = PackageValidationResult.model_validate(runtime_output["package"])
    with (nullcontext(db) if db is not None else session_factory()) as active_db:
        run = active_db.scalar(
            select(ClassificationTrainingRun)
            .where(ClassificationTrainingRun.id == training_run_id)
            .with_for_update()
        )
        if run is None:
            raise ClassificationBuildNotFoundError("Classification training run not found")
        if run.status == "CANCELED":
            return None
        existing = active_db.scalar(
            select(ClassificationModelVersion).where(
                ClassificationModelVersion.training_run_id == run.id
            )
        )
        if existing is not None:
            run.status = "COMPLETED"
            run.current_stage = "COMPLETED"
            run.progress_completed = run.progress_total
            active_db.commit()
            return existing.id
        _validate_training_result_manifest(
            run,
            result_manifest,
            _result_artifact_references(result),
        )
        _validate_runtime_result(run, result, package)
        model = active_db.scalar(
            select(ClassificationModel)
            .where(ClassificationModel.id == run.classification_model_id)
            .with_for_update()
        )
        if model is None:
            raise ClassificationBuildConflictError("Classification model disappeared during finalization")
        run.status = "FINALIZING"
        run.current_stage = "CREATING_CANDIDATE"
        run.progress_completed = 4
        version_number = (
            active_db.scalar(
                select(func.max(ClassificationModelVersion.version_number)).where(
                    ClassificationModelVersion.classification_model_id == model.id
                )
            )
            or 0
        ) + 1
        version = ClassificationModelVersion(
            id=uuid.uuid5(run.id, "classification-model-version-v1"),
            tenant_id=run.tenant_id,
            matter_id=run.matter_id,
            classification_model_id=run.classification_model_id,
            configuration_id=run.configuration_id,
            training_run_id=run.id,
            dataset_snapshot_id=run.dataset_snapshot_id,
            version_number=version_number,
            status="CANDIDATE",
            target_field_snapshot=run.dataset_snapshot.target_field_snapshot,
            trained_class_keys=trained_class_keys(run.dataset_snapshot, run.configuration),
            excluded_classes=run.dataset_snapshot.excluded_classes or [],
            training_aggregate_hash=run.dataset_snapshot.training_aggregate_hash,
            feature_specification_hash=run.dataset_snapshot.feature_specification_hash,
            model_artifact_id=result.model_artifact.artifact_id,
            model_content_hash=result.model_artifact.content_hash,
            prediction_artifact_id=result.prediction_artifact.artifact_id,
            prediction_content_hash=result.prediction_artifact.content_hash,
            evaluation_artifact_id=result.evaluation_artifact.artifact_id,
            evaluation_content_hash=result.evaluation_artifact.content_hash,
            result_manifest_artifact_id=result.result_manifest_artifact.artifact_id,
            result_manifest_content_hash=result.result_manifest_artifact.content_hash,
            package_schema_version=result.package_schema_version,
            runtime_image_digest=result.runtime_image_digest,
            source_revision=package.manifest.source_revision,
            metric_summary=result.metrics,
            warnings=result.warnings,
            created_by_user_id=run.execution_actor_user_id,
        )
        active_db.add(version)
        active_db.flush()
        active_db.add(
            ClassificationEvaluation(
                id=uuid.uuid5(version.id, "cross-fit-development-v1"),
                tenant_id=run.tenant_id,
                matter_id=run.matter_id,
                classification_model_id=run.classification_model_id,
                model_version_id=version.id,
                dataset_snapshot_id=run.dataset_snapshot_id,
                evaluation_kind="CROSS_FIT_DEVELOPMENT",
                metric_policy_version=METRIC_POLICY_VERSION,
                evaluation_artifact_id=result.evaluation_artifact.artifact_id,
                evaluation_content_hash=result.evaluation_artifact.content_hash,
                metric_summary=result.metrics,
            )
        )
        run.model_artifact_id = result.model_artifact.artifact_id
        run.model_content_hash = result.model_artifact.content_hash
        run.prediction_artifact_id = result.prediction_artifact.artifact_id
        run.prediction_content_hash = result.prediction_artifact.content_hash
        run.evaluation_artifact_id = result.evaluation_artifact.artifact_id
        run.evaluation_content_hash = result.evaluation_artifact.content_hash
        run.result_manifest_artifact_id = result.result_manifest_artifact.artifact_id
        run.result_manifest_content_hash = result.result_manifest_artifact.content_hash
        run.status = "COMPLETED"
        run.current_stage = "COMPLETED"
        run.progress_completed = run.progress_total
        run.failure_code = None
        run.failure_summary = None
        run.failure_retriable = False
        run.completed_at = utcnow()
        record_audit(
            active_db,
            tenant_id=run.tenant_id,
            actor_user_id=run.execution_actor_user_id,
            action="classification_training_run.completed",
            target_type="classification_training_run",
            target_id=run.id,
            details={
                "classification_model_id": str(run.classification_model_id),
                "model_version_id": str(version.id),
                "version_number": version.version_number,
                "model_content_hash": version.model_content_hash,
            },
        )
        active_db.commit()
        return version.id


def fail_training_run(
    training_run_id: uuid.UUID,
    message: str,
    *,
    code: str = "CLASSIFICATION_BUILD_FAILED",
    retriable: bool = True,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    with session_factory() as db:
        run = db.get(ClassificationTrainingRun, training_run_id)
        if run is None or run.status in {"COMPLETED", "CANCELED"}:
            return
        run.status = "FAILED"
        run.current_stage = "FAILED"
        run.failure_code = code[:100]
        run.failure_summary = message[:4000]
        run.failure_retriable = retriable
        run.completed_at = utcnow()
        db.commit()


def training_failure_details(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, ClassificationBuildError):
        retriable_codes = {"PUBLISHED_OUTPUT_NOT_VISIBLE"}
        return exc.code, exc.code in retriable_codes
    if isinstance(exc, ClassificationGatewayError):
        return "CLASSIFICATION_RUNTIME_UNAVAILABLE", True
    return "CLASSIFICATION_BUILD_FAILED", True


def cancel_training_run(run: ClassificationTrainingRun) -> bool:
    if run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELED"}:
        return False
    if run.status == "FAILED":
        raise ClassificationBuildConflictError("Failed builds cannot be canceled")
    run.status = "CANCELED"
    run.current_stage = "CANCELED"
    run.canceled_at = utcnow()
    run.completed_at = run.canceled_at
    run.failure_retriable = False
    return True


def _workflow_reference(reference) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=reference.artifact_id,
        artifact_type=reference.artifact_type,
        schema_version=reference.schema_version,
        content_hash=reference.content_hash,
        media_type=reference.media_type,
    )


def _result_artifact_references(
    result: ClassificationTrainingResult,
) -> dict[ArtifactType, ArtifactReference]:
    return {
        ArtifactType.CLASSIFICATION_MODEL_PACKAGE: result.model_artifact,
        ArtifactType.CLASSIFICATION_PREDICTIONS: result.prediction_artifact,
        ArtifactType.CLASSIFICATION_EVALUATION: result.evaluation_artifact,
        ArtifactType.CLASSIFICATION_RESULT_MANIFEST: result.result_manifest_artifact,
    }


def _validate_published_manifest(
    *,
    training_run_id: uuid.UUID,
    derivation_key: str,
    manifest: dict[str, Any],
    references: dict[ArtifactType, ArtifactReference],
) -> None:
    if (
        manifest.get("status") != "COMPLETED"
        or manifest.get("training_run_id") != str(training_run_id)
        or manifest.get("output_derivation_key") != derivation_key
    ):
        raise ClassificationBuildConflictError(
            "Published result manifest does not match the training run",
            code="RESULT_MANIFEST_IDENTITY_MISMATCH",
        )
    expected_outputs = {
        "model": references[ArtifactType.CLASSIFICATION_MODEL_PACKAGE],
        "predictions": references[ArtifactType.CLASSIFICATION_PREDICTIONS],
        "evaluation": references[ArtifactType.CLASSIFICATION_EVALUATION],
    }
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise ClassificationBuildConflictError(
            "Published result manifest is missing output references",
            code="RESULT_MANIFEST_OUTPUTS_MISSING",
        )
    try:
        output_matches = all(
            ArtifactReference.model_validate(outputs.get(name)) == reference
            for name, reference in expected_outputs.items()
        )
    except ValueError as exc:
        raise ClassificationBuildConflictError(
            "Published result manifest output references are invalid",
            code="RESULT_MANIFEST_OUTPUTS_INVALID",
        ) from exc
    if not output_matches:
        raise ClassificationBuildConflictError(
            "Published result manifest output references do not match artifacts",
            code="RESULT_MANIFEST_OUTPUT_MISMATCH",
        )


def _load_published_training_output(
    *,
    training_run_id: uuid.UUID,
    derivation_key: str,
    access: ClassificationArtifactAccess,
) -> tuple[dict[ArtifactType, ArtifactReference], dict[str, Any]]:
    references: dict[ArtifactType, ArtifactReference] = {}
    for artifact_type in (
        ArtifactType.CLASSIFICATION_MODEL_PACKAGE,
        ArtifactType.CLASSIFICATION_PREDICTIONS,
        ArtifactType.CLASSIFICATION_EVALUATION,
        ArtifactType.CLASSIFICATION_RESULT_MANIFEST,
    ):
        expected_derivation_key = classification_output_artifact_derivation_key(
            derivation_key,
            artifact_type.value,
        )
        reference = find_workflow_artifact_reference(
            artifact_type=artifact_type,
            derivation_key=expected_derivation_key,
            actor_user_id=access.actor_user_id,
            tenant_id=access.tenant_id,
            client_id=access.client_id,
        )
        if reference is None:
            raise ClassificationBuildConflictError(
                f"Published {artifact_type.value} was not found",
                code="PUBLISHED_OUTPUT_NOT_VISIBLE",
            )
        if (
            reference.tenant_id != access.tenant_id
            or reference.client_id != access.client_id
            or reference.core_scope_kind != "CLASSIFICATION_TRAINING_RUN"
            or reference.core_scope_id != training_run_id
            or reference.derivation_key != expected_derivation_key
            or reference.producer_run_id != training_run_id
        ):
            raise ClassificationBuildConflictError(
                f"Published {artifact_type.value} has invalid scope or producer identity",
                code="PUBLISHED_OUTPUT_SCOPE_MISMATCH",
            )
        references[artifact_type] = _workflow_reference(reference)
    destination = io.BytesIO()
    manifest_reference = references[ArtifactType.CLASSIFICATION_RESULT_MANIFEST]
    copy_workflow_artifact_content(
        artifact_id=manifest_reference.artifact_id,
        destination=destination,
        actor_user_id=access.actor_user_id,
        tenant_id=access.tenant_id,
        client_id=access.client_id,
    )
    manifest_bytes = destination.getvalue()
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_reference.content_hash:
        raise ClassificationBuildConflictError(
            "Published result manifest content hash is invalid",
            code="RESULT_MANIFEST_HASH_MISMATCH",
        )
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClassificationBuildConflictError(
            "Published result manifest is invalid",
            code="RESULT_MANIFEST_INVALID",
        ) from exc
    if not isinstance(manifest, dict):
        raise ClassificationBuildConflictError(
            "Published result manifest must be an object",
            code="RESULT_MANIFEST_INVALID",
        )
    _validate_published_manifest(
        training_run_id=training_run_id,
        derivation_key=derivation_key,
        manifest=manifest,
        references=references,
    )
    return references, manifest


def verify_published_training_result(
    result: ClassificationTrainingResult,
    access: ClassificationArtifactAccess,
) -> dict[str, Any]:
    references, manifest = _load_published_training_output(
        training_run_id=result.training_run_id,
        derivation_key=result.output_derivation_key,
        access=access,
    )
    if references != _result_artifact_references(result):
        raise ClassificationBuildConflictError(
            "Runtime response references do not match published artifacts",
            code="RUNTIME_OUTPUT_REFERENCE_MISMATCH",
        )
    return manifest


def _validate_training_result_manifest(
    run: ClassificationTrainingRun,
    manifest: dict[str, Any],
    references: dict[ArtifactType, ArtifactReference],
) -> None:
    _validate_published_manifest(
        training_run_id=run.id,
        derivation_key=run.derivation_key,
        manifest=manifest,
        references=references,
    )
    snapshot = run.dataset_snapshot
    configuration = run.configuration
    if (
        manifest.get("configuration_hash") != configuration.configuration_hash
        or manifest.get("feature_specification_hash") != snapshot.feature_specification_hash
        or manifest.get("runtime_image_digest") != run.runtime_image_digest
        or manifest.get("source_revision") != run.source_revision
        or manifest.get("input_artifact_hashes")
        != {
            "features": snapshot.feature_content_hash,
            "labels": snapshot.label_content_hash,
            "splits": snapshot.split_content_hash,
        }
    ):
        raise ClassificationBuildConflictError(
            "Published result manifest does not match frozen build inputs",
            code="RESULT_MANIFEST_INPUT_MISMATCH",
        )


def reconcile_training_run(
    db: Session,
    *,
    run: ClassificationTrainingRun,
    gateway: ClassificationGateway | None = None,
) -> uuid.UUID:
    if run.status == "CANCELED":
        raise ClassificationBuildConflictError("Canceled builds cannot be reconciled")
    if run.status == "COMPLETED":
        version = db.scalar(
            select(ClassificationModelVersion).where(ClassificationModelVersion.training_run_id == run.id)
        )
        if version is None:
            raise ClassificationBuildConflictError("Completed build is missing its model version")
        return version.id
    matter = db.get(Matter, run.matter_id)
    if matter is None:
        raise ClassificationBuildConflictError("Training-run matter not found")
    access = ClassificationArtifactAccess(
        tenant_id=run.tenant_id,
        client_id=matter.client_id,
        actor_user_id=run.execution_actor_user_id,
    )
    references, manifest = _load_published_training_output(
        training_run_id=run.id,
        derivation_key=run.derivation_key,
        access=access,
    )
    _validate_training_result_manifest(run, manifest, references)
    result = ClassificationTrainingResult(
        training_run_id=run.id,
        output_derivation_key=run.derivation_key,
        model_artifact=references[ArtifactType.CLASSIFICATION_MODEL_PACKAGE],
        prediction_artifact=references[ArtifactType.CLASSIFICATION_PREDICTIONS],
        evaluation_artifact=references[ArtifactType.CLASSIFICATION_EVALUATION],
        result_manifest_artifact=references[ArtifactType.CLASSIFICATION_RESULT_MANIFEST],
        model_content_hash=references[ArtifactType.CLASSIFICATION_MODEL_PACKAGE].content_hash,
        package_schema_version=run.package_schema_version,
        runtime_image_digest=manifest.get("runtime_image_digest", run.runtime_image_digest),
        metrics=manifest.get("metrics", {}),
        warnings=manifest.get("warnings", []),
    )
    package = (gateway or ClassificationGateway()).validate_package(result.model_artifact, access)
    run.status = "RECONCILING"
    run.current_stage = "FINALIZING_RECONCILED_OUTPUTS"
    db.commit()
    return finalize_training_run(
        run.id,
        {
            "completed": False,
            "result": result.model_dump(mode="json"),
            "result_manifest": manifest,
            "package": package.model_dump(mode="json"),
        },
        db=db,
    ) or uuid.uuid5(run.id, "classification-model-version-v1")


def execute_training_run(
    training_run_id: uuid.UUID,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    gateway: ClassificationGateway | None = None,
    published_result_verifier: Callable[
        [ClassificationTrainingResult, ClassificationArtifactAccess],
        dict[str, Any],
    ]
    | None = None,
) -> uuid.UUID | None:
    try:
        invocation = prepare_runtime_invocation(training_run_id, session_factory=session_factory)
        if invocation is None or invocation.get("completed"):
            return None
        output = invoke_runtime(
            invocation,
            gateway=gateway,
            published_result_verifier=published_result_verifier,
        )
        return finalize_training_run(training_run_id, output, session_factory=session_factory)
    except Exception as exc:
        code, retriable = training_failure_details(exc)
        fail_training_run(
            training_run_id,
            str(exc),
            code=code,
            retriable=retriable,
            session_factory=session_factory,
        )
        raise
