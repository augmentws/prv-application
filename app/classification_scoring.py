import hashlib
import io
import json
import tempfile
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifact_gateway import copy_workflow_artifact_content, find_workflow_artifact_reference
from app.audit import record_audit
from app.classification_gateway import ClassificationArtifactAccess, ClassificationGateway, ClassificationGatewayError
from app.classification_lifecycle import ClassificationLifecycleError, authorization_context
from app.classification_models import (
    CLASSIFICATION_TARGET_TYPES,
    effective_prediction_policy,
    trained_class_keys,
)
from app.database import SessionLocal
from app.job_executions import (
    CLASSIFICATION_SCORING_RUN_JOB_TYPE,
    JobScope,
    create_job_execution,
    current_job_execution,
    transition_job_execution,
)
from app.models import (
    ClassificationDatasetSnapshot,
    ClassificationModel,
    ClassificationModelConfiguration,
    ClassificationModelVersion,
    ClassificationScoringRun,
    Matter,
)
from app.schemas import (
    ClassificationDatasetIssue,
    ClassificationPredictionClassCount,
    ClassificationPredictionPreviewRead,
    ClassificationPredictionRowRead,
    ClassificationPredictionSummaryRead,
    ClassificationScoringPreview,
)
from classification_service.backend import ClassificationRuntimeError
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
    if (
        snapshot.status not in {"READY_FOR_TRAINING", "READY_FOR_SCORING"}
        or not snapshot.feature_artifact_id
        or not snapshot.feature_content_hash
    ):
        issues.append(
            ClassificationDatasetIssue(
                code="SCORING_DATASET_NOT_READY",
                message="Dataset snapshot must contain a complete immutable feature artifact",
            )
        )
    if (
        not snapshot.feature_specification_hash
        or snapshot.feature_specification_hash != version.feature_specification_hash
    ):
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
            policy = effective_prediction_policy(version.dataset_snapshot, configuration)
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
    notification_parent_type: str | None = None,
    notification_parent_id: uuid.UUID | None = None,
) -> tuple[ClassificationScoringRun, bool]:
    if (notification_parent_type is None) != (notification_parent_id is None):
        raise ClassificationScoringConflictError(
            "Scoring-run notification parent type and ID must be provided together",
            code="CLASSIFICATION_SCORING_NOTIFICATION_PARENT_INVALID",
        )
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
        (
            prepared.version,
            prepared.configuration,
            prepared.scoring_policy,
            prepared.scoring_policy_hash,
            prepared.derivation_key,
        )
    ):
        raise ClassificationScoringConflictError(
            "Classification scoring preview contains blocking errors", code="SCORING_BLOCKED"
        )
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
    matter = db.get(Matter, run.matter_id)
    if matter is None:
        raise ClassificationScoringConflictError("Scoring-run matter not found")
    create_job_execution(
        db,
        job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
        job_id=run.id,
        scope=JobScope(run.tenant_id, matter.client_id, run.matter_id),
        workflow_key="classification_model_scoring_v1",
        code_version="1",
        dbos_workflow_id=run.workflow_id,
        initiated_by_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        progress_total=run.progress_total,
        progress_unit="stages",
        input_snapshot={
            "classification_model_id": str(run.classification_model_id),
            "model_version_id": str(run.model_version_id),
            "dataset_snapshot_id": str(run.dataset_snapshot_id),
            "derivation_key": run.derivation_key,
        },
        configuration_snapshot={"scoring_policy": run.scoring_policy},
        notification_policy=(
            "PARENT_MANAGED"
            if notification_parent_type is not None and notification_parent_id is not None
            else "DEFAULT"
        ),
        notification_parent_type=notification_parent_type,
        notification_parent_id=notification_parent_id,
    )
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


def prediction_preview(
    run: ClassificationScoringRun,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    offset: int,
    limit: int,
) -> ClassificationPredictionPreviewRead:
    if run.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise ClassificationScoringConflictError(
            "Predictions are available only after scoring completes",
            code="SCORING_OUTPUT_NOT_READY",
        )
    if run.prediction_artifact_id is None or run.prediction_content_hash is None:
        raise ClassificationScoringConflictError(
            "Completed scoring run is missing its prediction artifact",
            code="SCORING_OUTPUT_MISSING",
        )
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as artifact_file:
        copy_workflow_artifact_content(
            artifact_id=run.prediction_artifact_id,
            destination=artifact_file,
            actor_user_id=actor_user_id,
            tenant_id=run.tenant_id,
            client_id=matter.client_id,
        )
        artifact_file.seek(0)
        digest = hashlib.sha256()
        while chunk := artifact_file.read(1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != run.prediction_content_hash:
            raise ClassificationScoringConflictError(
                "Prediction artifact content hash does not match the scoring run",
                code="PREDICTION_ARTIFACT_HASH_MISMATCH",
            )
        artifact_file.seek(0)
        try:
            parquet = pq.ParquetFile(artifact_file)
        except Exception as exc:
            raise ClassificationScoringConflictError(
                "Prediction artifact is not readable Parquet",
                code="PREDICTION_ARTIFACT_INVALID",
            ) from exc
        metadata = parquet.schema_arrow.metadata or {}
        if (
            metadata.get(b"pvr.schema") != b"classification-scoring-predictions"
            or metadata.get(b"pvr.scoring_run_id") != str(run.id).encode()
            or metadata.get(b"pvr.model_version_id") != str(run.model_version_id).encode()
        ):
            raise ClassificationScoringConflictError(
                "Prediction artifact identity does not match the scoring run",
                code="PREDICTION_ARTIFACT_IDENTITY_MISMATCH",
            )
        try:
            ordered_class_keys = json.loads(metadata[b"pvr.ordered_class_keys"])
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClassificationScoringConflictError(
                "Prediction artifact class metadata is invalid",
                code="PREDICTION_ARTIFACT_INVALID",
            ) from exc
        if not isinstance(ordered_class_keys, list) or not all(isinstance(value, str) for value in ordered_class_keys):
            raise ClassificationScoringConflictError(
                "Prediction artifact class metadata is invalid",
                code="PREDICTION_ARTIFACT_INVALID",
            )
        required_columns = {
            "matter_document_id",
            "collection_item_id",
            "scope_ordinal",
            "source_vector_content_hash",
            "result_status",
            "failure_reason",
            "probabilities",
            "predicted_class_keys",
            "abstained",
            "probability_margin",
            "uncertainty",
        }
        if not required_columns.issubset(parquet.schema_arrow.names):
            raise ClassificationScoringConflictError(
                "Prediction artifact is missing required columns",
                code="PREDICTION_ARTIFACT_INVALID",
            )
        total = parquet.metadata.num_rows
        remaining_offset = offset
        remaining_limit = limit
        rows: list[dict[str, Any]] = []
        for row_group_index in range(parquet.num_row_groups):
            row_group_count = parquet.metadata.row_group(row_group_index).num_rows
            if remaining_offset >= row_group_count:
                remaining_offset -= row_group_count
                continue
            table = parquet.read_row_group(row_group_index, columns=sorted(required_columns))
            selected = table.slice(remaining_offset, remaining_limit)
            rows.extend(selected.to_pylist())
            remaining_limit -= selected.num_rows
            remaining_offset = 0
            if remaining_limit == 0:
                break
    return ClassificationPredictionPreviewRead(
        scoring_run_id=run.id,
        model_version_id=run.model_version_id,
        ordered_class_keys=ordered_class_keys,
        total=total,
        offset=offset,
        limit=limit,
        items=[ClassificationPredictionRowRead.model_validate(row) for row in rows],
    )


def _prediction_class_labels(run: ClassificationScoringRun) -> dict[str, str]:
    definition = run.classification_model.target_metadata_definition
    labels: dict[str, str] = {}
    for value in definition.allowed_values or []:
        if not isinstance(value, dict):
            continue
        key = value.get("key")
        label = value.get("label")
        if isinstance(key, str) and isinstance(label, str) and label.strip():
            labels[key] = label
    if definition.type == "BOOLEAN":
        labels.setdefault("false", "False")
        labels.setdefault("true", "True")
    return labels


def _prediction_summary_read(
    run: ClassificationScoringRun,
    summary: dict[str, Any],
    *,
    minimum_probability: float | None = None,
) -> ClassificationPredictionSummaryRead:
    ordered_class_keys = summary.get("ordered_class_keys")
    class_counts = summary.get("class_counts")
    if not isinstance(ordered_class_keys, list) or not all(isinstance(value, str) for value in ordered_class_keys):
        raise ValueError("Prediction summary class keys are invalid")
    if not isinstance(class_counts, list):
        raise TypeError("Prediction summary class counts are invalid")
    count_by_key: dict[str, int] = {}
    for item in class_counts:
        if not isinstance(item, dict) or not isinstance(item.get("class_key"), str):
            raise TypeError("Prediction summary class count is invalid")
        document_count = item.get("document_count")
        if not isinstance(document_count, int) or isinstance(document_count, bool) or document_count < 0:
            raise ValueError("Prediction summary document count is invalid")
        count_by_key[item["class_key"]] = document_count
    all_class_keys = [*ordered_class_keys, *sorted(set(count_by_key) - set(ordered_class_keys))]
    labels = _prediction_class_labels(run)
    return ClassificationPredictionSummaryRead(
        scoring_run_id=run.id,
        model_version_id=run.model_version_id,
        ordered_class_keys=ordered_class_keys,
        minimum_probability=minimum_probability,
        total_document_count=summary["total_document_count"],
        evaluated_document_count=summary["evaluated_document_count"],
        predicted_document_count=summary["predicted_document_count"],
        no_prediction_document_count=summary["no_prediction_document_count"],
        predicted_value_count=summary["predicted_value_count"],
        abstained_document_count=summary["abstained_document_count"],
        class_counts=[
            ClassificationPredictionClassCount(
                class_key=class_key,
                label=labels.get(class_key, class_key),
                document_count=count_by_key.get(class_key, 0),
            )
            for class_key in all_class_keys
        ],
    )


def prediction_summary(
    run: ClassificationScoringRun,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    minimum_probability: float | None = None,
) -> ClassificationPredictionSummaryRead:
    if run.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise ClassificationScoringConflictError(
            "Prediction summary is available only after scoring completes",
            code="SCORING_OUTPUT_NOT_READY",
        )
    if run.prediction_artifact_id is None or run.prediction_content_hash is None:
        raise ClassificationScoringConflictError(
            "Completed scoring run is missing its prediction artifact",
            code="SCORING_OUTPUT_MISSING",
        )
    artifact_derivation = classification_output_artifact_derivation_key(
        run.derivation_key,
        ArtifactType.CLASSIFICATION_PREDICTIONS.value,
    )
    reference = find_workflow_artifact_reference(
        artifact_type=ArtifactType.CLASSIFICATION_PREDICTIONS,
        derivation_key=artifact_derivation,
        actor_user_id=actor_user_id,
        tenant_id=run.tenant_id,
        client_id=matter.client_id,
    )
    cached_summary = reference.metadata.get("prediction_summary") if reference is not None else None
    if (
        minimum_probability is None
        and reference is not None
        and reference.artifact_id == run.prediction_artifact_id
        and reference.content_hash == run.prediction_content_hash
        and isinstance(cached_summary, dict)
    ):
        try:
            return _prediction_summary_read(run, cached_summary)
        except (KeyError, TypeError, ValueError):
            pass

    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as artifact_file:
        copy_workflow_artifact_content(
            artifact_id=run.prediction_artifact_id,
            destination=artifact_file,
            actor_user_id=actor_user_id,
            tenant_id=run.tenant_id,
            client_id=matter.client_id,
        )
        artifact_file.seek(0)
        digest = hashlib.sha256()
        while chunk := artifact_file.read(1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != run.prediction_content_hash:
            raise ClassificationScoringConflictError(
                "Prediction artifact content hash does not match the scoring run",
                code="PREDICTION_ARTIFACT_HASH_MISMATCH",
            )
        artifact_file.seek(0)
        try:
            parquet = pq.ParquetFile(artifact_file)
        except Exception as exc:
            raise ClassificationScoringConflictError(
                "Prediction artifact is not readable Parquet",
                code="PREDICTION_ARTIFACT_INVALID",
            ) from exc
        metadata = parquet.schema_arrow.metadata or {}
        if (
            metadata.get(b"pvr.schema") != b"classification-scoring-predictions"
            or metadata.get(b"pvr.scoring_run_id") != str(run.id).encode()
            or metadata.get(b"pvr.model_version_id") != str(run.model_version_id).encode()
        ):
            raise ClassificationScoringConflictError(
                "Prediction artifact identity does not match the scoring run",
                code="PREDICTION_ARTIFACT_IDENTITY_MISMATCH",
            )
        try:
            ordered_class_keys = json.loads(metadata[b"pvr.ordered_class_keys"])
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClassificationScoringConflictError(
                "Prediction artifact class metadata is invalid",
                code="PREDICTION_ARTIFACT_INVALID",
            ) from exc
        if not isinstance(ordered_class_keys, list) or not all(isinstance(value, str) for value in ordered_class_keys):
            raise ClassificationScoringConflictError(
                "Prediction artifact class metadata is invalid",
                code="PREDICTION_ARTIFACT_INVALID",
            )
        required_columns = {"result_status", "predicted_class_keys", "abstained"}
        if minimum_probability is not None:
            required_columns.add("probabilities")
        if not required_columns.issubset(parquet.schema_arrow.names):
            raise ClassificationScoringConflictError(
                "Prediction artifact is missing summary columns",
                code="PREDICTION_ARTIFACT_INVALID",
            )
        class_counts: dict[str, int] = {}
        evaluated_document_count = predicted_document_count = no_prediction_document_count = 0
        predicted_value_count = abstained_document_count = 0
        strategy = run.model_version.training_run.configuration.classification_strategy
        for batch in parquet.iter_batches(columns=sorted(required_columns)):
            for row in batch.to_pylist():
                if row["result_status"] != "SUCCESS":
                    continue
                evaluated_document_count += 1
                abstained_document_count += int(bool(row["abstained"]))
                if minimum_probability is None:
                    predicted_keys = list(dict.fromkeys(row["predicted_class_keys"] or []))
                else:
                    probabilities = row["probabilities"] or []
                    if len(probabilities) != len(ordered_class_keys):
                        raise ClassificationScoringConflictError(
                            "Prediction artifact probability vectors do not match its class metadata",
                            code="PREDICTION_ARTIFACT_INVALID",
                        )
                    if strategy == ClassificationStrategy.ONE_VS_REST.value:
                        predicted_keys = [
                            class_key
                            for class_key, probability in zip(ordered_class_keys, probabilities, strict=True)
                            if probability >= minimum_probability
                        ]
                    else:
                        selected = max(range(len(probabilities)), key=probabilities.__getitem__)
                        predicted_keys = (
                            [ordered_class_keys[selected]]
                            if probabilities[selected] >= minimum_probability
                            else []
                        )
                if predicted_keys:
                    predicted_document_count += 1
                    predicted_value_count += len(predicted_keys)
                    for class_key in predicted_keys:
                        class_counts[class_key] = class_counts.get(class_key, 0) + 1
                else:
                    no_prediction_document_count += 1
        return _prediction_summary_read(
            run,
            {
                "ordered_class_keys": ordered_class_keys,
                "total_document_count": parquet.metadata.num_rows,
                "evaluated_document_count": evaluated_document_count,
                "predicted_document_count": predicted_document_count,
                "no_prediction_document_count": no_prediction_document_count,
                "predicted_value_count": predicted_value_count,
                "abstained_document_count": abstained_document_count,
                "class_counts": [
                    {"class_key": class_key, "document_count": count}
                    for class_key, count in class_counts.items()
                ],
            },
            minimum_probability=minimum_probability,
        )


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
        ordered_class_keys=(
            run.model_version.trained_class_keys
            or trained_class_keys(run.model_version.dataset_snapshot, configuration)
        ),
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
        transition_job_execution(
            db,
            job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
            job_id=run.id,
            status="RUNNING",
            current_stage="VALIDATING_PACKAGE",
            progress_completed=1,
            progress_total=run.progress_total,
            progress={"population_document_count": run.population_document_count},
        )
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


def mark_scoring_runtime_started(
    scoring_run_id: uuid.UUID,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    with session_factory() as db:
        run = db.get(ClassificationScoringRun, scoring_run_id)
        if run is None:
            raise ClassificationScoringNotFoundError("Classification scoring run not found")
        if run.status in {"CANCELED", "COMPLETED", "COMPLETED_WITH_ERRORS"}:
            return
        transition_job_execution(
            db,
            job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
            job_id=run.id,
            status="RUNNING",
            current_stage="SCORING_DOCUMENTS",
            progress_completed=max(2, run.progress_completed),
            progress_total=run.progress_total,
            progress={"population_document_count": run.population_document_count},
        )
        db.commit()


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
        raise ClassificationScoringConflictError(
            "Scoring result manifest hash mismatch", code="RESULT_MANIFEST_HASH_MISMATCH"
        )
    try:
        manifest = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClassificationScoringConflictError(
            "Scoring result manifest is invalid", code="RESULT_MANIFEST_INVALID"
        ) from exc
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
    published_result_verifier: Callable[[ClassificationScoringResult, ClassificationArtifactAccess], dict[str, Any]]
    | None = None,
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
        manifest.get("status") != ("COMPLETED_WITH_ERRORS" if result.failed_document_count else "COMPLETED")
        or manifest.get("model_version_id") != str(run.model_version_id)
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
    with nullcontext(db) if db is not None else session_factory() as active_db:
        run = active_db.scalar(
            select(ClassificationScoringRun).where(ClassificationScoringRun.id == scoring_run_id).with_for_update()
        )
        if run is None:
            raise ClassificationScoringNotFoundError("Classification scoring run not found")
        if run.status == "CANCELED":
            return
        if run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
            return
        transition_job_execution(
            active_db,
            job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
            job_id=run.id,
            status="RUNNING",
            current_stage="FINALIZING",
            progress_completed=max(3, run.progress_completed),
            progress_total=run.progress_total,
        )
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
        workflow = transition_job_execution(
            active_db,
            job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
            job_id=run.id,
            status=run.status,
            current_stage=run.status,
            progress_completed=run.progress_total,
            progress={
                "population_document_count": run.population_document_count,
                "scored_document_count": run.scored_document_count,
                "abstained_document_count": run.abstained_document_count,
                "excluded_document_count": run.excluded_document_count,
                "failed_document_count": run.failed_document_count,
            },
        )
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
        from app.review_programs import project_review_program_classifier_scoring_terminal

        project_review_program_classifier_scoring_terminal(
            active_db,
            scoring_run=run,
            workflow=workflow,
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
        workflow = transition_job_execution(
            db,
            job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
            job_id=run.id,
            status="FAILED",
            current_stage="FAILED",
            progress_completed=run.progress_completed,
            failure_code=run.failure_code,
            failure_summary=run.failure_summary,
            failure_retriable=retriable,
        )
        from app.review_programs import project_review_program_classifier_scoring_terminal

        project_review_program_classifier_scoring_terminal(
            db,
            scoring_run=run,
            workflow=workflow,
        )
        db.commit()


def scoring_failure_cause(exc: Exception) -> Exception:
    errors = getattr(exc, "errors", None)
    if isinstance(errors, list) and errors and isinstance(errors[-1], Exception):
        return scoring_failure_cause(errors[-1])
    return exc


def scoring_failure_details(exc: Exception) -> tuple[str, bool]:
    cause = scoring_failure_cause(exc)
    if isinstance(cause, ClassificationScoringError):
        return cause.code, cause.code in {"PUBLISHED_OUTPUT_NOT_VISIBLE"}
    if isinstance(cause, ClassificationRuntimeError):
        return cause.code, False
    if isinstance(cause, ClassificationGatewayError):
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


def retry_scoring_run(
    db: Session,
    *,
    run: ClassificationScoringRun,
    actor_user_id: uuid.UUID,
):
    previous = current_job_execution(
        db,
        job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
        job_id=run.id,
        for_update=True,
    )
    matter = db.get(Matter, run.matter_id)
    if matter is None:
        raise ClassificationScoringConflictError("Scoring-run matter not found")
    if previous is None and run.status in {"FAILED", "CANCELED"}:
        terminal_status = run.status
        terminal_stage = run.current_stage
        terminal_progress = run.progress_completed
        terminal_failure_code = run.failure_code
        terminal_failure_summary = run.failure_summary
        terminal_failure_retriable = run.failure_retriable
        previous = create_job_execution(
            db,
            job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
            job_id=run.id,
            scope=JobScope(run.tenant_id, matter.client_id, matter.id),
            workflow_key="classification_model_scoring_v1",
            code_version="1",
            dbos_workflow_id=run.workflow_id,
            initiated_by_user_id=run.execution_actor_user_id,
            idempotency_key=run.idempotency_key,
            progress_total=run.progress_total,
            progress_unit="stages",
            input_snapshot={
                "classification_model_id": str(run.classification_model_id),
                "model_version_id": str(run.model_version_id),
                "dataset_snapshot_id": str(run.dataset_snapshot_id),
                "derivation_key": run.derivation_key,
            },
            configuration_snapshot={"scoring_policy": run.scoring_policy},
        )
        previous = transition_job_execution(
            db,
            job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
            job_id=run.id,
            status=terminal_status,
            current_stage=terminal_stage,
            progress_completed=terminal_progress,
            failure_code=terminal_failure_code,
            failure_summary=terminal_failure_summary,
            failure_retriable=terminal_failure_retriable,
        )
    elif run.status in {"FAILED", "CANCELED"} and previous.status not in {"FAILED", "CANCELED"}:
        previous = transition_job_execution(
            db,
            job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
            job_id=run.id,
            status=run.status,
            current_stage=run.current_stage,
            progress_completed=max(run.progress_completed, previous.progress_completed),
            failure_code=run.failure_code,
            failure_summary=run.failure_summary,
            failure_retriable=run.failure_retriable,
        )
    if previous is None or previous.status not in {"FAILED", "CANCELED"}:
        raise ClassificationScoringConflictError("Only a failed or canceled scoring run can be retried")
    if previous.status == "FAILED" and not previous.failure_retriable:
        raise ClassificationScoringConflictError("This scoring failure is not marked as retryable")
    attempt = previous.attempt_number + 1
    run.workflow_id = f"classification-model-score:{run.id}:attempt:{attempt}"
    run.status = "QUEUED"
    run.current_stage = "QUEUED"
    run.progress_completed = 0
    run.scored_document_count = 0
    run.abstained_document_count = 0
    run.excluded_document_count = 0
    run.failed_document_count = 0
    run.prediction_artifact_id = None
    run.prediction_content_hash = None
    run.result_manifest_artifact_id = None
    run.result_manifest_content_hash = None
    run.failure_code = None
    run.failure_summary = None
    run.failure_retriable = False
    run.execution_actor_user_id = actor_user_id
    run.started_at = None
    run.completed_at = None
    run.canceled_at = None
    return create_job_execution(
        db,
        job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
        job_id=run.id,
        scope=JobScope(run.tenant_id, matter.client_id, matter.id),
        workflow_key="classification_model_scoring_v1",
        code_version="1",
        dbos_workflow_id=run.workflow_id,
        initiated_by_user_id=actor_user_id,
        idempotency_key=f"{run.idempotency_key}:attempt:{attempt}",
        progress_total=run.progress_total,
        progress_unit="stages",
        input_snapshot={
            "classification_model_id": str(run.classification_model_id),
            "model_version_id": str(run.model_version_id),
            "dataset_snapshot_id": str(run.dataset_snapshot_id),
            "derivation_key": run.derivation_key,
        },
        configuration_snapshot={"scoring_policy": run.scoring_policy},
        retry_of=previous,
    )


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
    transition_job_execution(
        db,
        job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
        job_id=run.id,
        status="RUNNING",
        current_stage="FINALIZING_RECONCILED_OUTPUTS",
        progress_completed=max(1, run.progress_completed),
    )
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
    published_result_verifier: Callable[[ClassificationScoringResult, ClassificationArtifactAccess], dict[str, Any]]
    | None = None,
) -> None:
    try:
        invocation = prepare_runtime_invocation(scoring_run_id, session_factory=session_factory)
        if invocation is None or invocation.get("completed"):
            return
        mark_scoring_runtime_started(scoring_run_id, session_factory=session_factory)
        output = invoke_runtime(
            invocation,
            gateway=gateway,
            published_result_verifier=published_result_verifier,
        )
        finalize_scoring_run(scoring_run_id, output, session_factory=session_factory)
    except Exception as exc:
        code, retriable = scoring_failure_details(exc)
        fail_scoring_run(
            scoring_run_id,
            str(scoring_failure_cause(exc)),
            code=code,
            retriable=retriable,
            session_factory=session_factory,
        )
        raise
