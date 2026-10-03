import hashlib
import io
import json
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifact_gateway import copy_workflow_artifact_content, find_workflow_artifact_reference
from app.audit import record_audit
from app.classification_gateway import ClassificationArtifactAccess, ClassificationGateway, ClassificationGatewayError
from app.classification_lifecycle import ClassificationLifecycleError, authorization_context
from app.classification_models import active_value_keys
from app.database import SessionLocal
from app.models import (
    ClassificationDatasetSnapshot,
    ClassificationModel,
    ClassificationModelConfiguration,
    ClassificationModelVersion,
    ClassificationScoringRun,
    Matter,
)
from app.schemas import ClassificationDatasetIssue, ClassificationScoringPreview
from classification_service.hashing import canonical_value_hash, classification_output_artifact_derivation_key
from classification_service.schemas import (
    ArtifactReference,
    ArtifactType,
    ClassificationScoringRequest,
    ClassificationScoringResult,
    ClassificationStrategy,
    PackageValidationResult,
    PredictionResolutionPolicy,
    TargetFieldSnapshot,
)


class ClassificationScoringError(ValueError):
    def __init__(self, message: str, *, code: str = "CLASSIFICATION_SCORING_ERROR") -> None:
        super().__init__(message)
        self.code = code


class ClassificationScoringNotFoundError(ClassificationScoringError):
    pass


class ClassificationScoringConflictError(ClassificationScoringError):
    pass


@dataclass(frozen=True)
class PreparedScoring:
    model: ClassificationModel
    version: ClassificationModelVersion | None
    configuration: ClassificationModelConfiguration | None
    snapshot: ClassificationDatasetSnapshot
    scoring_policy: PredictionResolutionPolicy | None
    scoring_policy_hash: str | None
    derivation_key: str | None
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
        raise ClassificationScoringConflictError(
            f"Frozen scoring input is missing {artifact_type.value}",
            code="SCORING_INPUT_ARTIFACT_MISSING",
        )
    return ArtifactReference(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        schema_version=1,
        content_hash=content_hash,
        media_type=media_type,
    )


def _compatibility_issues(
    model: ClassificationModel,
    version: ClassificationModelVersion,
    configuration: ClassificationModelConfiguration,
    snapshot: ClassificationDatasetSnapshot,
) -> list[ClassificationDatasetIssue]:
    issues: list[ClassificationDatasetIssue] = []
    definition = model.target_metadata_definition
    target = version.target_field_snapshot
    if model.status != "ACTIVE":
        issues.append(ClassificationDatasetIssue(code="MODEL_NOT_ACTIVE", message="Classification model is not active"))
    if version.status != "APPROVED":
        issues.append(
            ClassificationDatasetIssue(
                code="MODEL_VERSION_NOT_APPROVED",
                message="Scoring requires an approved model version",
            )
        )
    if definition.status != "ACTIVE" or not definition.reviewable:
        issues.append(
            ClassificationDatasetIssue(
                code="TARGET_FIELD_NOT_ELIGIBLE",
                message="Target field must be active and reviewable",
            )
        )
    if not (
        target.get("metadata_definition_id") == str(definition.id)
        and target.get("stable_key") == definition.key
        and target.get("metadata_type") == definition.type
        and target.get("cardinality") == definition.cardinality
    ):
        issues.append(
            ClassificationDatasetIssue(
                code="TARGET_FIELD_IDENTITY_CHANGED",
                message="Model-version target field identity is no longer compatible",
            )
        )
    available = set(active_value_keys(definition)) if definition.type == "ENUM" else {"false", "true"}
    if configuration.classification_strategy == ClassificationStrategy.BINARY.value:
        mapping = configuration.binary_mapping or {}
        selected = set(mapping.get("negative_value_keys", [])) | set(mapping.get("positive_value_keys", []))
    else:
        selected = set(configuration.ordered_class_keys)
    if not selected.issubset(available):
        issues.append(
            ClassificationDatasetIssue(
                code="TARGET_CLASSES_UNAVAILABLE",
                message="One or more model-version target classes are inactive or missing",
            )
        )
    if snapshot.status != "READY_FOR_TRAINING" or not snapshot.feature_artifact_id or not snapshot.feature_content_hash:
        issues.append(
            ClassificationDatasetIssue(
                code="SCORING_DATASET_NOT_READY",
                message="Dataset snapshot must contain a complete immutable feature artifact",
            )
        )
    if not snapshot.feature_specification_hash or snapshot.feature_specification_hash != version.feature_specification_hash:
        issues.append(
            ClassificationDatasetIssue(
                code="FEATURE_CONTRACT_MISMATCH",
                message="Dataset features are not compatible with the pinned model version",
            )
        )
    return issues


def prepare_scoring(
    db: Session,
    *,
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    dataset_snapshot_id: uuid.UUID,
    model_version_id: uuid.UUID | None,
) -> PreparedScoring:
    model = db.scalar(
        select(ClassificationModel).where(
            ClassificationModel.id == classification_model_id,
            ClassificationModel.matter_id == matter_id,
        )
    )
    if model is None:
        raise ClassificationScoringNotFoundError("Classification model not found")
    snapshot = db.scalar(
        select(ClassificationDatasetSnapshot).where(
            ClassificationDatasetSnapshot.id == dataset_snapshot_id,
            ClassificationDatasetSnapshot.classification_model_id == model.id,
            ClassificationDatasetSnapshot.matter_id == matter_id,
        )
    )
    if snapshot is None:
        raise ClassificationScoringNotFoundError("Classification dataset snapshot not found")
    pinned_version_id = model_version_id or model.active_model_version_id
    errors: list[ClassificationDatasetIssue] = []
    version: ClassificationModelVersion | None = None
    configuration: ClassificationModelConfiguration | None = None
    policy: PredictionResolutionPolicy | None = None
    policy_hash: str | None = None
    derivation_key: str | None = None
    existing_run_id: uuid.UUID | None = None
    if pinned_version_id is None:
        errors.append(
            ClassificationDatasetIssue(
                code="NO_MODEL_VERSION_SELECTED",
                message="Select an approved model version or activate one before scoring",
            )
        )
    else:
        version = db.scalar(
            select(ClassificationModelVersion).where(
                ClassificationModelVersion.id == pinned_version_id,
                ClassificationModelVersion.classification_model_id == model.id,
                ClassificationModelVersion.matter_id == matter_id,
            )
        )
        if version is None:
            raise ClassificationScoringNotFoundError("Classification model version not found")
        configuration = db.scalar(
            select(ClassificationModelConfiguration).where(
                ClassificationModelConfiguration.id == version.configuration_id,
                ClassificationModelConfiguration.classification_model_id == model.id,
            )
        )
        if configuration is None:
            errors.append(
                ClassificationDatasetIssue(
                    code="MODEL_VERSION_CONFIGURATION_MISSING",
                    message="Pinned model-version configuration is unavailable",
                )
            )
        else:
            errors.extend(_compatibility_issues(model, version, configuration, snapshot))
            policy = PredictionResolutionPolicy.model_validate(configuration.prediction_resolution_policy)
            policy_hash = canonical_value_hash(policy.model_dump(mode="json"))
            if snapshot.feature_content_hash and snapshot.feature_specification_hash:
                derivation_key = canonical_value_hash(
                    {
                        "contract_version": 1,
                        "classification_model_id": str(model.id),
                        "model_version_id": str(version.id),
                        "configuration_hash": configuration.configuration_hash,
                        "model_content_hash": version.model_content_hash,
                        "dataset_snapshot_id": str(snapshot.id),
                        "feature_content_hash": snapshot.feature_content_hash,
                        "feature_specification_hash": snapshot.feature_specification_hash,
                        "scoring_policy_hash": policy_hash,
                    }
                )
                existing_run_id = db.scalar(
                    select(ClassificationScoringRun.id).where(
                        ClassificationScoringRun.classification_model_id == model.id,
                        ClassificationScoringRun.derivation_key == derivation_key,
                    )
                )
    return PreparedScoring(
        model=model,
        version=version,
        configuration=configuration,
        snapshot=snapshot,
        scoring_policy=policy,
        scoring_policy_hash=policy_hash,
        derivation_key=derivation_key,
        blocking_errors=errors,
        warnings=[],
        existing_run_id=existing_run_id,
    )


def scoring_preview(prepared: PreparedScoring) -> ClassificationScoringPreview:
    return ClassificationScoringPreview(
        classification_model_id=prepared.model.id,
        model_version_id=prepared.version.id if prepared.version else None,
        dataset_snapshot_id=prepared.snapshot.id,
        configuration_hash=prepared.configuration.configuration_hash if prepared.configuration else None,
        model_content_hash=prepared.version.model_content_hash if prepared.version else None,
        feature_content_hash=prepared.snapshot.feature_content_hash,
        feature_specification_hash=prepared.snapshot.feature_specification_hash,
        scoring_policy=prepared.scoring_policy,
        scoring_policy_hash=prepared.scoring_policy_hash,
        derivation_key=prepared.derivation_key,
        population_document_count=prepared.snapshot.population_count,
        can_launch=not prepared.blocking_errors,
        blocking_errors=prepared.blocking_errors,
        warnings=prepared.warnings,
        existing_scoring_run_id=prepared.existing_run_id,
    )


def create_scoring_run(
    db: Session,
    *,
    prepared: PreparedScoring,
    idempotency_key: str,
    actor_user_id: uuid.UUID,
) -> tuple[ClassificationScoringRun, bool]:
    existing = db.scalar(
        select(ClassificationScoringRun).where(
            ClassificationScoringRun.classification_model_id == prepared.model.id,
            ClassificationScoringRun.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.derivation_key != prepared.derivation_key:
            raise ClassificationScoringConflictError(
                "Idempotency key was already used for different scoring inputs",
                code="IDEMPOTENCY_KEY_REUSED",
            )
        return existing, False
    if prepared.existing_run_id is not None:
        existing = db.get(ClassificationScoringRun, prepared.existing_run_id)
        if existing is not None:
            return existing, False
    if prepared.blocking_errors or not all(
        (prepared.version, prepared.configuration, prepared.scoring_policy, prepared.scoring_policy_hash, prepared.derivation_key)
    ):
        raise ClassificationScoringConflictError("Classification scoring preview contains blocking errors", code="SCORING_BLOCKED")
    assert prepared.version is not None
    assert prepared.configuration is not None
    assert prepared.scoring_policy is not None
    assert prepared.scoring_policy_hash is not None
    assert prepared.derivation_key is not None
    assert prepared.snapshot.feature_content_hash is not None
    assert prepared.snapshot.feature_specification_hash is not None
    run_id = uuid.uuid5(prepared.model.id, prepared.derivation_key)
    run = ClassificationScoringRun(
        id=run_id,
        tenant_id=prepared.model.tenant_id,
        matter_id=prepared.model.matter_id,
        classification_model_id=prepared.model.id,
        model_version_id=prepared.version.id,
        dataset_snapshot_id=prepared.snapshot.id,
        idempotency_key=idempotency_key,
        workflow_id=f"classification-model-score:{run_id}",
        derivation_key=prepared.derivation_key,
        configuration_hash=prepared.configuration.configuration_hash,
        model_content_hash=prepared.version.model_content_hash,
        feature_content_hash=prepared.snapshot.feature_content_hash,
        feature_specification_hash=prepared.snapshot.feature_specification_hash,
        scoring_policy=prepared.scoring_policy.model_dump(mode="json"),
        scoring_policy_hash=prepared.scoring_policy_hash,
        population_document_count=prepared.snapshot.population_count,
        created_by_user_id=actor_user_id,
        execution_actor_user_id=actor_user_id,
    )
    db.add(run)
    db.flush()
    record_audit(
        db,
        tenant_id=run.tenant_id,
        actor_user_id=actor_user_id,
        action="classification_scoring_run.created",
        target_type="classification_scoring_run",
        target_id=run.id,
        details={
            "classification_model_id": str(run.classification_model_id),
            "model_version_id": str(run.model_version_id),
            "dataset_snapshot_id": str(run.dataset_snapshot_id),
            "derivation_key": run.derivation_key,
        },
    )
    return run, True


def resolve_existing_scoring_run(
    db: Session,
    *,
    classification_model_id: uuid.UUID,
    idempotency_key: str,
    derivation_key: str,
) -> ClassificationScoringRun:
    run = db.scalar(
        select(ClassificationScoringRun).where(
            ClassificationScoringRun.classification_model_id == classification_model_id,
            ClassificationScoringRun.idempotency_key == idempotency_key,
        )
    )
    if run is not None:
        if run.derivation_key != derivation_key:
            raise ClassificationScoringConflictError("Idempotency key was reused", code="IDEMPOTENCY_KEY_REUSED")
        return run
    run = db.scalar(
        select(ClassificationScoringRun).where(
            ClassificationScoringRun.classification_model_id == classification_model_id,
            ClassificationScoringRun.derivation_key == derivation_key,
        )
    )
    if run is None:
        raise ClassificationScoringConflictError(
            "Concurrent scoring request could not be resolved",
            code="IDEMPOTENCY_RACE_UNRESOLVED",
        )
    return run


def get_scoring_run(
    db: Session,
    *,
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    scoring_run_id: uuid.UUID,
    for_update: bool = False,
) -> ClassificationScoringRun:
    statement = select(ClassificationScoringRun).where(
        ClassificationScoringRun.id == scoring_run_id,
        ClassificationScoringRun.matter_id == matter_id,
        ClassificationScoringRun.classification_model_id == classification_model_id,
    )
    if for_update:
        statement = statement.with_for_update()
    run = db.scalar(statement)
    if run is None:
        raise ClassificationScoringNotFoundError("Classification scoring run not found")
    return run


def scoring_request(run: ClassificationScoringRun) -> ClassificationScoringRequest:
    configuration = run.model_version.training_run.configuration
    snapshot = run.dataset_snapshot
    return ClassificationScoringRequest(
        scoring_run_id=run.id,
        tenant_id=run.tenant_id,
        matter_id=run.matter_id,
        classification_model_id=run.classification_model_id,
        model_version_id=run.model_version_id,
        configuration_hash=run.configuration_hash,
        target_field_snapshot=TargetFieldSnapshot.model_validate(run.model_version.target_field_snapshot),
        classification_strategy=ClassificationStrategy(configuration.classification_strategy),
        ordered_class_keys=configuration.ordered_class_keys,
        prediction_resolution_policy=PredictionResolutionPolicy.model_validate(run.scoring_policy),
        model_artifact=_artifact_reference(
            run.model_version.model_artifact_id,
            ArtifactType.CLASSIFICATION_MODEL_PACKAGE,
            run.model_content_hash,
            "application/zip",
        ),
        feature_artifact=_artifact_reference(
            snapshot.feature_artifact_id,
            ArtifactType.CLASSIFICATION_FEATURES,
            run.feature_content_hash,
            "application/vnd.apache.parquet",
        ),
        output_derivation_key=run.derivation_key,
    )


def prepare_runtime_invocation(
    scoring_run_id: uuid.UUID,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> dict[str, Any] | None:
    with session_factory() as db:
        run = db.scalar(
            select(ClassificationScoringRun).where(ClassificationScoringRun.id == scoring_run_id).with_for_update()
        )
        if run is None:
            raise ClassificationScoringNotFoundError("Classification scoring run not found")
        if run.status == "CANCELED":
            return None
        if run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
            return {"completed": True}
        matter = db.get(Matter, run.matter_id)
        if matter is None:
            raise ClassificationScoringConflictError("Scoring-run matter not found")
        try:
            authorization_context(db, matter=matter, actor_user_id=run.execution_actor_user_id)
        except ClassificationLifecycleError as exc:
            raise ClassificationScoringConflictError(
                "Scoring creator is no longer authorized for the matter",
                code="SCORING_AUTHORIZATION_REVOKED",
            ) from exc
        prepared = prepare_scoring(
            db,
            matter_id=run.matter_id,
            classification_model_id=run.classification_model_id,
            dataset_snapshot_id=run.dataset_snapshot_id,
            model_version_id=run.model_version_id,
        )
        if prepared.blocking_errors or prepared.derivation_key != run.derivation_key:
            raise ClassificationScoringConflictError(
                "Frozen scoring inputs are no longer valid",
                code="FROZEN_SCORING_INPUT_INVALID",
            )
        run.status = "WAITING_FOR_RUNTIME"
        run.current_stage = "VALIDATING_PACKAGE"
        run.progress_completed = 1
        run.started_at = run.started_at or utcnow()
        request = scoring_request(run)
        db.commit()
        return {
            "completed": False,
            "request": request.model_dump(mode="json"),
            "access": {
                "tenant_id": str(run.tenant_id),
                "client_id": str(matter.client_id),
                "actor_user_id": str(run.execution_actor_user_id),
            },
        }


def _validate_package(
    request: ClassificationScoringRequest,
    package: PackageValidationResult,
) -> None:
    if not package.valid or package.package_content_hash != request.model_artifact.content_hash:
        raise ClassificationScoringConflictError("Pinned model package validation failed", code="MODEL_PACKAGE_INVALID")
    manifest = package.manifest
    signature = package.signature
    if (
        manifest.tenant_id != request.tenant_id
        or manifest.matter_id != request.matter_id
        or manifest.classification_model_id != request.classification_model_id
        or manifest.configuration_hash != request.configuration_hash
        or manifest.target_field_snapshot != request.target_field_snapshot
        or manifest.classification_strategy != request.classification_strategy
        or manifest.ordered_class_keys != request.ordered_class_keys
        or manifest.prediction_resolution_policy != request.prediction_resolution_policy
        or signature.feature_specification_hash != package.manifest.feature_specification_hash
        or signature.classification_strategy != request.classification_strategy
        or signature.ordered_class_keys != request.ordered_class_keys
    ):
        raise ClassificationScoringConflictError(
            "Pinned model package identity or signature is incompatible",
            code="MODEL_PACKAGE_IDENTITY_MISMATCH",
        )


def _workflow_reference(reference: Any) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=reference.artifact_id,
        artifact_type=reference.artifact_type,
        schema_version=reference.schema_version,
        content_hash=reference.content_hash,
        media_type=reference.media_type,
    )


def _load_published_scoring_output(
    *,
    scoring_run_id: uuid.UUID,
    derivation_key: str,
    access: ClassificationArtifactAccess,
) -> tuple[dict[ArtifactType, ArtifactReference], dict[str, Any]]:
    references: dict[ArtifactType, ArtifactReference] = {}
    for artifact_type in (ArtifactType.CLASSIFICATION_PREDICTIONS, ArtifactType.CLASSIFICATION_RESULT_MANIFEST):
        artifact_derivation = classification_output_artifact_derivation_key(derivation_key, artifact_type.value)
        reference = find_workflow_artifact_reference(
            artifact_type=artifact_type,
            derivation_key=artifact_derivation,
            actor_user_id=access.actor_user_id,
            tenant_id=access.tenant_id,
            client_id=access.client_id,
        )
        if reference is None:
            raise ClassificationScoringConflictError(
                f"Published {artifact_type.value} was not found",
                code="PUBLISHED_OUTPUT_NOT_VISIBLE",
            )
        if (
            reference.tenant_id != access.tenant_id
            or reference.client_id != access.client_id
            or reference.core_scope_kind != "CLASSIFICATION_SCORING_RUN"
            or reference.core_scope_id != scoring_run_id
            or reference.derivation_key != artifact_derivation
            or reference.producer_run_id != scoring_run_id
        ):
            raise ClassificationScoringConflictError(
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
    content = destination.getvalue()
    if hashlib.sha256(content).hexdigest() != manifest_reference.content_hash:
        raise ClassificationScoringConflictError("Scoring result manifest hash mismatch", code="RESULT_MANIFEST_HASH_MISMATCH")
    try:
        manifest = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClassificationScoringConflictError("Scoring result manifest is invalid", code="RESULT_MANIFEST_INVALID") from exc
    prediction_reference = references[ArtifactType.CLASSIFICATION_PREDICTIONS]
    try:
        manifest_prediction = ArtifactReference.model_validate(manifest.get("prediction_artifact"))
    except ValueError as exc:
        raise ClassificationScoringConflictError("Scoring manifest prediction reference is invalid") from exc
    if (
        manifest.get("status") not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
        or manifest.get("scoring_run_id") != str(scoring_run_id)
        or manifest.get("output_derivation_key") != derivation_key
        or manifest_prediction != prediction_reference
    ):
        raise ClassificationScoringConflictError("Published scoring manifest identity does not match the run")
    return references, manifest


def verify_published_scoring_result(
    result: ClassificationScoringResult,
    access: ClassificationArtifactAccess,
) -> dict[str, Any]:
    references, manifest = _load_published_scoring_output(
        scoring_run_id=result.scoring_run_id,
        derivation_key=result.output_derivation_key,
        access=access,
    )
    if (
        references[ArtifactType.CLASSIFICATION_PREDICTIONS] != result.prediction_artifact
        or references[ArtifactType.CLASSIFICATION_RESULT_MANIFEST] != result.result_manifest_artifact
    ):
        raise ClassificationScoringConflictError(
            "Runtime scoring references do not match published artifacts",
            code="RUNTIME_OUTPUT_REFERENCE_MISMATCH",
        )
    return manifest


def invoke_runtime(
    invocation: dict[str, Any],
    *,
    gateway: ClassificationGateway | None = None,
    published_result_verifier: Callable[[ClassificationScoringResult, ClassificationArtifactAccess], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if invocation.get("completed"):
        return invocation
    request = ClassificationScoringRequest.model_validate(invocation["request"])
    access = ClassificationArtifactAccess(
        tenant_id=uuid.UUID(invocation["access"]["tenant_id"]),
        client_id=uuid.UUID(invocation["access"]["client_id"]),
        actor_user_id=uuid.UUID(invocation["access"]["actor_user_id"]),
    )
    resolved_gateway = gateway or ClassificationGateway()
    package = resolved_gateway.validate_package(request.model_artifact, access)
    _validate_package(request, package)
    result = resolved_gateway.score(request, access)
    manifest = (published_result_verifier or verify_published_scoring_result)(result, access)
    return {"completed": False, "result": result.model_dump(mode="json"), "result_manifest": manifest}


def _validate_runtime_output(
    run: ClassificationScoringRun,
    result: ClassificationScoringResult,
    manifest: dict[str, Any],
) -> None:
    expected_counts = {
        "population_document_count": result.population_document_count,
        "scored_document_count": result.scored_document_count,
        "abstained_document_count": result.abstained_document_count,
        "excluded_document_count": result.excluded_document_count,
        "failed_document_count": result.failed_document_count,
    }
    if result.scoring_run_id != run.id or result.output_derivation_key != run.derivation_key:
        raise ClassificationScoringConflictError("Runtime result does not match the scoring run")
    if result.population_document_count != run.population_document_count:
        raise ClassificationScoringConflictError("Runtime scoring population does not match the frozen snapshot")
    if (
        manifest.get("status")
        != ("COMPLETED_WITH_ERRORS" if result.failed_document_count else "COMPLETED")
        or
        manifest.get("model_version_id") != str(run.model_version_id)
        or manifest.get("configuration_hash") != run.configuration_hash
        or manifest.get("feature_specification_hash") != run.feature_specification_hash
        or manifest.get("input_artifact_hashes")
        != {"model": run.model_content_hash, "features": run.feature_content_hash}
        or any(manifest.get(key) != value for key, value in expected_counts.items())
    ):
        raise ClassificationScoringConflictError("Published scoring manifest does not match frozen inputs")


def finalize_scoring_run(
    scoring_run_id: uuid.UUID,
    runtime_output: dict[str, Any],
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    db: Session | None = None,
) -> None:
    if runtime_output.get("completed"):
        return
    result = ClassificationScoringResult.model_validate(runtime_output["result"])
    manifest = runtime_output.get("result_manifest")
    if not isinstance(manifest, dict):
        raise ClassificationScoringConflictError("Verified scoring result manifest is missing")
    with (nullcontext(db) if db is not None else session_factory()) as active_db:
        run = active_db.scalar(
            select(ClassificationScoringRun).where(ClassificationScoringRun.id == scoring_run_id).with_for_update()
        )
        if run is None:
            raise ClassificationScoringNotFoundError("Classification scoring run not found")
        if run.status == "CANCELED":
            return
        if run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
            return
        _validate_runtime_output(run, result, manifest)
        run.status = "COMPLETED_WITH_ERRORS" if result.failed_document_count else "COMPLETED"
        run.current_stage = run.status
        run.progress_completed = run.progress_total
        run.scored_document_count = result.scored_document_count
        run.abstained_document_count = result.abstained_document_count
        run.excluded_document_count = result.excluded_document_count
        run.failed_document_count = result.failed_document_count
        run.prediction_artifact_id = result.prediction_artifact.artifact_id
        run.prediction_content_hash = result.prediction_artifact.content_hash
        run.result_manifest_artifact_id = result.result_manifest_artifact.artifact_id
        run.result_manifest_content_hash = result.result_manifest_artifact.content_hash
        run.failure_code = None
        run.failure_summary = None
        run.failure_retriable = False
        run.completed_at = utcnow()
        record_audit(
            active_db,
            tenant_id=run.tenant_id,
            actor_user_id=run.execution_actor_user_id,
            action="classification_scoring_run.completed",
            target_type="classification_scoring_run",
            target_id=run.id,
            details={
                "classification_model_id": str(run.classification_model_id),
                "model_version_id": str(run.model_version_id),
                "status": run.status,
                "population_document_count": run.population_document_count,
                "scored_document_count": run.scored_document_count,
                "excluded_document_count": run.excluded_document_count,
                "failed_document_count": run.failed_document_count,
            },
        )
        active_db.commit()


def fail_scoring_run(
    scoring_run_id: uuid.UUID,
    message: str,
    *,
    code: str = "CLASSIFICATION_SCORING_FAILED",
    retriable: bool = True,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    with session_factory() as db:
        run = db.get(ClassificationScoringRun, scoring_run_id)
        if run is None or run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELED"}:
            return
        run.status = "FAILED"
        run.current_stage = "FAILED"
        run.failure_code = code[:100]
        run.failure_summary = message[:4000]
        run.failure_retriable = retriable
        run.completed_at = utcnow()
        db.commit()


def scoring_failure_details(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, ClassificationScoringError):
        return exc.code, exc.code in {"PUBLISHED_OUTPUT_NOT_VISIBLE"}
    if isinstance(exc, ClassificationGatewayError):
        return "CLASSIFICATION_RUNTIME_UNAVAILABLE", True
    return "CLASSIFICATION_SCORING_FAILED", True


def cancel_scoring_run(run: ClassificationScoringRun) -> bool:
    if run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELED"}:
        return False
    if run.status == "FAILED":
        raise ClassificationScoringConflictError("Failed scoring runs cannot be canceled")
    run.status = "CANCELED"
    run.current_stage = "CANCELED"
    run.canceled_at = utcnow()
    run.completed_at = run.canceled_at
    run.failure_retriable = False
    return True


def reconcile_scoring_run(db: Session, *, run: ClassificationScoringRun) -> None:
    if run.status == "CANCELED":
        raise ClassificationScoringConflictError("Canceled scoring runs cannot be reconciled")
    if run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        return
    matter = db.get(Matter, run.matter_id)
    if matter is None:
        raise ClassificationScoringConflictError("Scoring-run matter not found")
    access = ClassificationArtifactAccess(
        tenant_id=run.tenant_id,
        client_id=matter.client_id,
        actor_user_id=run.execution_actor_user_id,
    )
    references, manifest = _load_published_scoring_output(
        scoring_run_id=run.id,
        derivation_key=run.derivation_key,
        access=access,
    )
    result = ClassificationScoringResult(
        scoring_run_id=run.id,
        output_derivation_key=run.derivation_key,
        prediction_artifact=references[ArtifactType.CLASSIFICATION_PREDICTIONS],
        result_manifest_artifact=references[ArtifactType.CLASSIFICATION_RESULT_MANIFEST],
        population_document_count=manifest["population_document_count"],
        scored_document_count=manifest["scored_document_count"],
        abstained_document_count=manifest["abstained_document_count"],
        excluded_document_count=manifest["excluded_document_count"],
        failed_document_count=manifest["failed_document_count"],
    )
    run.status = "RECONCILING"
    run.current_stage = "FINALIZING_RECONCILED_OUTPUTS"
    db.commit()
    finalize_scoring_run(
        run.id,
        {"completed": False, "result": result.model_dump(mode="json"), "result_manifest": manifest},
        db=db,
    )


def execute_scoring_run(
    scoring_run_id: uuid.UUID,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    gateway: ClassificationGateway | None = None,
    published_result_verifier: Callable[[ClassificationScoringResult, ClassificationArtifactAccess], dict[str, Any]] | None = None,
) -> None:
    try:
        invocation = prepare_runtime_invocation(scoring_run_id, session_factory=session_factory)
        if invocation is None or invocation.get("completed"):
            return
        output = invoke_runtime(
            invocation,
            gateway=gateway,
            published_result_verifier=published_result_verifier,
        )
        finalize_scoring_run(scoring_run_id, output, session_factory=session_factory)
    except Exception as exc:
        code, retriable = scoring_failure_details(exc)
        fail_scoring_run(scoring_run_id, str(exc), code=code, retriable=retriable, session_factory=session_factory)
        raise
