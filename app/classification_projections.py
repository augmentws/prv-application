from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any

import pyarrow.parquet as pq
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.artifact_gateway import copy_workflow_artifact_content
from app.audit import record_audit
from app.authorization import ResourceRef, user_can
from app.classification_scoring import scoring_request
from app.document_metadata import value_columns
from app.job_executions import (
    CLASSIFICATION_PROJECTION_JOB_TYPE,
    JobScope,
    create_job_execution,
    current_job_execution,
    transition_job_execution,
)
from app.models import (
    ClassificationScoringRun,
    Matter,
    MetadataDefinition,
    ReviewBatch,
    ReviewBatchCodingField,
    ReviewBatchCodingGroup,
    ReviewBatchDocument,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewBatchRunValue,
    ReviewBatchSearchCodingProjection,
    ReviewBatchSearchCodingRun,
    User,
)
from app.schemas import ReviewBatchClassifierRunCreate
from app.search.operations import create_search_operation
from classification_service.hashing import canonical_value_hash

PROJECTION_CONTRACT_VERSION = 2
REQUIRED_PREDICTION_COLUMNS = {
    "matter_document_id",
    "result_status",
    "failure_reason",
    "probabilities",
    "predicted_class_keys",
    "abstained",
}


class ClassificationProjectionError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _target_definition(db: Session, scoring_run: ClassificationScoringRun) -> MetadataDefinition:
    definition = scoring_run.classification_model.target_metadata_definition
    if definition.matter_id != scoring_run.matter_id or definition.status != "ACTIVE":
        raise ClassificationProjectionError("Classification target field is no longer active")
    return definition


def _batch_has_target(db: Session, batch_id: uuid.UUID, definition_id: uuid.UUID) -> bool:
    return (
        db.scalar(
            select(ReviewBatchCodingField.id)
            .join(
                ReviewBatchCodingGroup,
                ReviewBatchCodingGroup.id == ReviewBatchCodingField.review_batch_coding_group_id,
            )
            .where(
                ReviewBatchCodingGroup.review_batch_id == batch_id,
                ReviewBatchCodingField.metadata_definition_id == definition_id,
            )
            .limit(1)
        )
        is not None
    )


def _projection_derivation(
    *,
    batch_id: uuid.UUID,
    scoring_run: ClassificationScoringRun,
    target_definition_id: uuid.UUID,
    ordered_class_keys: list[str],
    payload: ReviewBatchClassifierRunCreate,
) -> str:
    return canonical_value_hash(
        {
            "contract_version": PROJECTION_CONTRACT_VERSION,
            "review_batch_id": str(batch_id),
            "classification_scoring_run_id": str(scoring_run.id),
            "prediction_content_hash": scoring_run.prediction_content_hash,
            "target_metadata_definition_id": str(target_definition_id),
            "ordered_class_keys": ordered_class_keys,
            "selected_class_keys": payload.selected_class_keys,
            "minimum_confidence": payload.minimum_confidence,
            "maximum_confidence": payload.maximum_confidence,
            "confidence_interval": "LOWER_INCLUSIVE_UPPER_EXCLUSIVE",
        }
    )


def _class_value_map(
    scoring_run: ClassificationScoringRun,
    ordered_class_keys: list[str],
) -> dict[str, str]:
    configuration = scoring_run.model_version.training_run.configuration
    if configuration.classification_strategy != "BINARY":
        return {class_key: class_key for class_key in ordered_class_keys}
    mapping = configuration.binary_mapping or {}
    class_value_map: dict[str, str] = {}
    for side in ("negative", "positive"):
        class_key = mapping.get(f"{side}_class_key")
        value_keys = mapping.get(f"{side}_value_keys")
        if not isinstance(class_key, str) or not isinstance(value_keys, list):
            raise ClassificationProjectionError("Binary classifier value mapping is invalid")
        if len(value_keys) != 1 or not isinstance(value_keys[0], str):
            raise ClassificationProjectionError(
                "Binary classifier projection requires exactly one destination value for each class"
            )
        class_value_map[class_key] = value_keys[0]
    if set(class_value_map) != set(ordered_class_keys):
        raise ClassificationProjectionError("Binary classifier class mapping does not match the model")
    return class_value_map


def create_classifier_projection_run(
    db: Session,
    *,
    matter: Matter,
    batch: ReviewBatch,
    payload: ReviewBatchClassifierRunCreate,
    actor_user_id: uuid.UUID,
) -> tuple[ReviewBatchRun, bool]:
    scoring_run = db.get(ClassificationScoringRun, payload.classification_scoring_run_id)
    if scoring_run is None or scoring_run.matter_id != matter.id:
        raise ClassificationProjectionError("Classification scoring run was not found")
    if scoring_run.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise ClassificationProjectionError("Classification scoring must complete before projection")
    if scoring_run.prediction_artifact_id is None or scoring_run.prediction_content_hash is None:
        raise ClassificationProjectionError("Classification scoring output is unavailable")
    if batch.status != "READY" or batch.search_status != "READY":
        raise ClassificationProjectionError("Review batch and its search projection must be ready")
    if batch.matter_id != matter.id:
        raise ClassificationProjectionError("Review batch is outside the scoring matter")

    request = scoring_request(scoring_run)
    ordered_class_keys = list(request.ordered_class_keys)
    unknown = set(payload.selected_class_keys) - set(ordered_class_keys)
    if unknown:
        raise ClassificationProjectionError("Unknown selected classifier classes: " + ", ".join(sorted(unknown)))
    definition = _target_definition(db, scoring_run)
    class_value_map = _class_value_map(scoring_run, ordered_class_keys)
    if not _batch_has_target(db, batch.id, definition.id):
        raise ClassificationProjectionError("The classifier target field is not in this batch's coding configuration")

    derivation_key = _projection_derivation(
        batch_id=batch.id,
        scoring_run=scoring_run,
        target_definition_id=definition.id,
        ordered_class_keys=ordered_class_keys,
        payload=payload,
    )
    existing = db.scalar(
        select(ReviewBatchRun).where(
            ReviewBatchRun.review_batch_id == batch.id,
            ReviewBatchRun.run_type == "CLASSIFIER",
            ReviewBatchRun.idempotency_key == payload.idempotency_key,
        )
    )
    if existing is not None:
        if existing.derivation_key != derivation_key:
            raise ClassificationProjectionError(
                "Idempotency key was already used for different classifier projection inputs"
            )
        return existing, False
    existing = db.scalar(
        select(ReviewBatchRun).where(
            ReviewBatchRun.review_batch_id == batch.id,
            ReviewBatchRun.run_type == "CLASSIFIER",
            ReviewBatchRun.classification_scoring_run_id == scoring_run.id,
            ReviewBatchRun.derivation_key == derivation_key,
        )
    )
    if existing is not None:
        return existing, False

    run_id = uuid.uuid5(scoring_run.id, derivation_key)
    run = ReviewBatchRun(
        id=run_id,
        review_batch_id=batch.id,
        run_type="CLASSIFIER",
        purpose="CANDIDATE",
        status="QUEUED",
        result_policy="ISOLATED",
        classification_scoring_run_id=scoring_run.id,
        derivation_key=derivation_key,
        idempotency_key=payload.idempotency_key,
        configuration_snapshot={
            "contract_version": PROJECTION_CONTRACT_VERSION,
            "classification_model_id": str(scoring_run.classification_model_id),
            "model_version_id": str(scoring_run.model_version_id),
            "classification_scoring_run_id": str(scoring_run.id),
            "dataset_snapshot_id": str(scoring_run.dataset_snapshot_id),
            "prediction_artifact_id": str(scoring_run.prediction_artifact_id),
            "prediction_content_hash": scoring_run.prediction_content_hash,
            "target_metadata_definition_id": str(definition.id),
            "ordered_class_keys": ordered_class_keys,
            "class_value_map": class_value_map,
            "selected_class_keys": list(payload.selected_class_keys),
            "minimum_confidence": payload.minimum_confidence,
            "maximum_confidence": payload.maximum_confidence,
            "confidence_interval": "LOWER_INCLUSIVE_UPPER_EXCLUSIVE",
            "prediction_resolution_policy_hash": scoring_run.scoring_policy_hash,
        },
        initiated_by_user_id=actor_user_id,
        dbos_workflow_id=f"classification-prediction-project:{run_id}",
        started_at=None,
    )
    db.add(run)
    db.flush()
    create_job_execution(
        db,
        job_type=CLASSIFICATION_PROJECTION_JOB_TYPE,
        job_id=run.id,
        scope=JobScope(scoring_run.tenant_id, matter.client_id, matter.id),
        workflow_key="classification_prediction_projection_v1",
        code_version="1",
        dbos_workflow_id=run.dbos_workflow_id,
        initiated_by_user_id=actor_user_id,
        idempotency_key=payload.idempotency_key,
        progress_total=batch.document_count,
        progress_unit="documents",
        input_snapshot={
            "review_batch_id": str(batch.id),
            "classification_scoring_run_id": str(scoring_run.id),
            "prediction_content_hash": scoring_run.prediction_content_hash,
            "target_metadata_definition_id": str(definition.id),
        },
        configuration_snapshot=run.configuration_snapshot,
    )
    record_audit(
        db,
        tenant_id=scoring_run.tenant_id,
        actor_user_id=actor_user_id,
        action="classification_projection.requested",
        target_type="review_batch_run",
        target_id=run.id,
        details={
            "matter_id": str(matter.id),
            "review_batch_id": str(batch.id),
            "classification_scoring_run_id": str(scoring_run.id),
            "model_version_id": str(scoring_run.model_version_id),
            "minimum_confidence": payload.minimum_confidence,
            "maximum_confidence": payload.maximum_confidence,
            "selected_class_keys": list(payload.selected_class_keys),
            "derivation_key": derivation_key,
        },
    )
    from app.workflows.dispatcher import enqueue_classification_projection

    enqueue_classification_projection(db, run.dbos_workflow_id, str(run.id))
    return run, True


def _typed_class_value(class_key: str, definition: MetadataDefinition) -> Any:
    if definition.type == "BOOLEAN":
        if class_key == "true":
            return True
        if class_key == "false":
            return False
        raise ClassificationProjectionError(f"Invalid Boolean class key: {class_key}")
    return class_key


def _load_parquet(
    *,
    run: ReviewBatchRun,
    scoring_run: ClassificationScoringRun,
    matter: Matter,
) -> tuple[tempfile.SpooledTemporaryFile[bytes], pq.ParquetFile, list[str]]:
    # The caller owns and closes the spool after all Parquet row groups are consumed.
    artifact_file = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)  # noqa: SIM115
    assert scoring_run.prediction_artifact_id is not None
    assert scoring_run.prediction_content_hash is not None
    copy_workflow_artifact_content(
        artifact_id=scoring_run.prediction_artifact_id,
        destination=artifact_file,
        actor_user_id=run.initiated_by_user_id,
        tenant_id=scoring_run.tenant_id,
        client_id=matter.client_id,
    )
    artifact_file.seek(0)
    digest = hashlib.sha256()
    while chunk := artifact_file.read(1024 * 1024):
        digest.update(chunk)
    if digest.hexdigest() != scoring_run.prediction_content_hash:
        artifact_file.close()
        raise ClassificationProjectionError("Prediction artifact content hash mismatch")
    artifact_file.seek(0)
    try:
        parquet = pq.ParquetFile(artifact_file)
    except Exception as exc:
        artifact_file.close()
        raise ClassificationProjectionError("Prediction artifact is not readable Parquet") from exc
    metadata = parquet.schema_arrow.metadata or {}
    if (
        metadata.get(b"pvr.schema") != b"classification-scoring-predictions"
        or metadata.get(b"pvr.scoring_run_id") != str(scoring_run.id).encode()
        or metadata.get(b"pvr.model_version_id") != str(scoring_run.model_version_id).encode()
    ):
        artifact_file.close()
        raise ClassificationProjectionError("Prediction artifact identity does not match scoring run")
    try:
        ordered_class_keys = json.loads(metadata[b"pvr.ordered_class_keys"])
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        artifact_file.close()
        raise ClassificationProjectionError("Prediction artifact class metadata is invalid") from exc
    if (
        not isinstance(ordered_class_keys, list)
        or not all(isinstance(value, str) for value in ordered_class_keys)
        or ordered_class_keys != run.configuration_snapshot.get("ordered_class_keys")
    ):
        artifact_file.close()
        raise ClassificationProjectionError("Prediction artifact class order changed")
    if not REQUIRED_PREDICTION_COLUMNS.issubset(parquet.schema_arrow.names):
        artifact_file.close()
        raise ClassificationProjectionError("Prediction artifact is missing required columns")
    return artifact_file, parquet, ordered_class_keys


def _validate_population(
    db: Session,
    *,
    batch_id: uuid.UUID,
    parquet: pq.ParquetFile,
) -> set[uuid.UUID]:
    batch_ids = set(
        db.scalars(
            select(ReviewBatchDocument.matter_document_id).where(ReviewBatchDocument.review_batch_id == batch_id)
        )
    )
    artifact_ids: set[uuid.UUID] = set()
    for index in range(parquet.num_row_groups):
        rows = parquet.read_row_group(index, columns=["matter_document_id"]).to_pylist()
        for row in rows:
            document_id = uuid.UUID(str(row["matter_document_id"]))
            if document_id in artifact_ids:
                raise ClassificationProjectionError("Prediction artifact contains duplicate documents")
            artifact_ids.add(document_id)
    if artifact_ids != batch_ids:
        raise ClassificationProjectionError(
            "Classification scoring population does not exactly match review batch membership"
        )
    return batch_ids


def execute_classifier_projection(db: Session, run_id: uuid.UUID) -> None:
    run = db.scalar(select(ReviewBatchRun).where(ReviewBatchRun.id == run_id).with_for_update())
    if run is None or run.run_type != "CLASSIFIER":
        raise ClassificationProjectionError("Classifier projection run was not found")
    if run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        return
    if run.status == "CANCELED":
        return
    scoring_run = db.get(ClassificationScoringRun, run.classification_scoring_run_id)
    batch = db.get(ReviewBatch, run.review_batch_id)
    if scoring_run is None or batch is None:
        raise ClassificationProjectionError("Classifier projection source is unavailable")
    matter = db.get(Matter, scoring_run.matter_id)
    actor = db.get(User, run.initiated_by_user_id)
    if matter is None or actor is None:
        raise ClassificationProjectionError("Classifier projection authorization context is unavailable")
    resource = ResourceRef.matter(matter.id)
    if not user_can(db, actor, "classification.score", resource) or not user_can(
        db, actor, "batch.configure", resource
    ):
        raise ClassificationProjectionError("Classifier projection creator is no longer authorized")
    if scoring_run.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise ClassificationProjectionError("Classification scoring run is no longer complete")
    if batch.status != "READY" or batch.matter_id != matter.id:
        raise ClassificationProjectionError("Classifier projection batch is unavailable")
    definition = _target_definition(db, scoring_run)
    if str(definition.id) != run.configuration_snapshot.get("target_metadata_definition_id"):
        raise ClassificationProjectionError("Classifier projection target field identity changed")

    run.status = "RUNNING"
    run.started_at = run.started_at or utcnow()
    run.error_message = None
    transition_job_execution(
        db,
        job_type=CLASSIFICATION_PROJECTION_JOB_TYPE,
        job_id=run.id,
        status="RUNNING",
        current_stage="VALIDATING_PREDICTIONS",
        progress_completed=run.processed_document_count,
        progress_total=batch.document_count,
    )
    db.commit()

    artifact_file, parquet, ordered_class_keys = _load_parquet(
        run=run,
        scoring_run=scoring_run,
        matter=matter,
    )
    try:
        _validate_population(db, batch_id=batch.id, parquet=parquet)
        db.execute(delete(ReviewBatchRunValue).where(ReviewBatchRunValue.review_batch_run_id == run.id))
        db.execute(delete(ReviewBatchRunDocument).where(ReviewBatchRunDocument.review_batch_run_id == run.id))
        db.commit()

        minimum = float(run.configuration_snapshot["minimum_confidence"])
        maximum_raw = run.configuration_snapshot.get("maximum_confidence")
        maximum = float(maximum_raw) if maximum_raw is not None else None
        selected_classes = set(run.configuration_snapshot.get("selected_class_keys") or ordered_class_keys)
        class_value_map = run.configuration_snapshot.get("class_value_map")
        if not isinstance(class_value_map, dict) or set(class_value_map) != set(ordered_class_keys):
            raise ClassificationProjectionError("Classifier destination value mapping is invalid")
        class_ordinals = {key: index for index, key in enumerate(ordered_class_keys)}
        summary = {
            "population_document_count": 0,
            "successful_document_count": 0,
            "projected_document_count": 0,
            "projected_value_count": 0,
            "no_value_document_count": 0,
            "below_window_count": 0,
            "above_window_count": 0,
            "class_filtered_count": 0,
            "abstained_document_count": 0,
            "feature_excluded_document_count": 0,
            "failed_document_count": 0,
            "missing_confidence_count": 0,
        }
        columns = sorted(REQUIRED_PREDICTION_COLUMNS)
        for row_group_index in range(parquet.num_row_groups):
            rows = parquet.read_row_group(row_group_index, columns=columns).to_pylist()
            document_rows: list[ReviewBatchRunDocument] = []
            value_rows: list[ReviewBatchRunValue] = []
            now = utcnow()
            for row in rows:
                document_id = uuid.UUID(str(row["matter_document_id"]))
                summary["population_document_count"] += 1
                result_status = row.get("result_status")
                projected = 0
                document_status = "SKIPPED"
                if result_status == "FAILED":
                    summary["failed_document_count"] += 1
                    document_status = "FAILED"
                elif result_status == "EXCLUDED":
                    summary["feature_excluded_document_count"] += 1
                elif row.get("abstained") is True:
                    summary["abstained_document_count"] += 1
                    summary["no_value_document_count"] += 1
                    document_status = "COMPLETED"
                elif result_status == "SUCCESS":
                    summary["successful_document_count"] += 1
                    document_status = "COMPLETED"
                    probabilities = row.get("probabilities") or []
                    if len(probabilities) != len(ordered_class_keys):
                        raise ClassificationProjectionError(
                            f"Prediction probability shape is invalid for document {document_id}"
                        )
                    for class_key in row.get("predicted_class_keys") or []:
                        if class_key not in class_ordinals:
                            raise ClassificationProjectionError(f"Prediction contains unknown class key: {class_key}")
                        confidence_raw = probabilities[class_ordinals[class_key]]
                        if confidence_raw is None:
                            summary["missing_confidence_count"] += 1
                            continue
                        confidence = float(confidence_raw)
                        if class_key not in selected_classes:
                            summary["class_filtered_count"] += 1
                            continue
                        if confidence < minimum:
                            summary["below_window_count"] += 1
                            continue
                        if maximum is not None and confidence >= maximum:
                            summary["above_window_count"] += 1
                            continue
                        destination_value = class_value_map.get(class_key)
                        if not isinstance(destination_value, str):
                            raise ClassificationProjectionError(
                                f"Classifier destination mapping is missing for class: {class_key}"
                            )
                        typed_value = _typed_class_value(destination_value, definition)
                        value_rows.append(
                            ReviewBatchRunValue(
                                review_batch_run_id=run.id,
                                matter_document_id=document_id,
                                metadata_definition_id=definition.id,
                                value_ordinal=class_ordinals[class_key],
                                confidence=confidence,
                                confidence_kind="DERIVED_PROBABILITY",
                                review_decision_result_id=None,
                                question_key=None,
                                **value_columns(typed_value, definition),
                            )
                        )
                        projected += 1
                    if projected:
                        summary["projected_document_count"] += 1
                        summary["projected_value_count"] += projected
                    else:
                        summary["no_value_document_count"] += 1
                else:
                    raise ClassificationProjectionError(
                        f"Prediction contains unsupported result status: {result_status}"
                    )
                document_rows.append(
                    ReviewBatchRunDocument(
                        review_batch_run_id=run.id,
                        matter_document_id=document_id,
                        status=document_status,
                        started_at=now,
                        completed_at=now,
                    )
                )
            db.add_all(document_rows)
            db.add_all(value_rows)
            transition_job_execution(
                db,
                job_type=CLASSIFICATION_PROJECTION_JOB_TYPE,
                job_id=run.id,
                status="RUNNING",
                current_stage="PROJECTING_PREDICTIONS",
                progress_completed=summary["population_document_count"],
                progress_total=batch.document_count,
                progress=summary.copy(),
            )
            db.commit()

        run = db.scalar(select(ReviewBatchRun).where(ReviewBatchRun.id == run.id).with_for_update())
        assert run is not None
        run.processed_document_count = summary["population_document_count"]
        run.result_summary = summary
        run.status = "COMPLETED_WITH_ERRORS" if summary["failed_document_count"] else "COMPLETED"
        run.completed_at = utcnow()
        transition_job_execution(
            db,
            job_type=CLASSIFICATION_PROJECTION_JOB_TYPE,
            job_id=run.id,
            status=run.status,
            current_stage=run.status,
            progress_completed=summary["population_document_count"],
            progress_total=batch.document_count,
            progress=summary.copy(),
        )

        selection = db.scalar(
            select(ReviewBatchSearchCodingRun)
            .where(ReviewBatchSearchCodingRun.review_batch_id == batch.id)
            .with_for_update()
        )
        selection_error: str | None = None
        if selection is None:
            selection = ReviewBatchSearchCodingRun(
                review_batch_id=batch.id,
                review_batch_run_id=run.id,
                status="QUEUED",
                selected_by_user_id=run.initiated_by_user_id,
                selected_at=utcnow(),
            )
            db.add(selection)
        else:
            selection.review_batch_run_id = run.id
            selection.status = "QUEUED"
            selection.selected_by_user_id = run.initiated_by_user_id
            selection.selected_at = utcnow()
            selection.projected_at = None
            selection.error_message = None
        projection = db.get(ReviewBatchSearchCodingProjection, (batch.id, run.id))
        if projection is None:
            projection = ReviewBatchSearchCodingProjection(
                review_batch_id=batch.id,
                review_batch_run_id=run.id,
                status="QUEUED",
                requested_by_user_id=run.initiated_by_user_id,
            )
            db.add(projection)
        else:
            projection.status = "QUEUED"
            projection.requested_by_user_id = run.initiated_by_user_id
            projection.search_index_generation_id = None
            projection.projected_at = None
            projection.error_message = None
        operation = create_search_operation(
            db,
            matter_id=matter.id,
            kind="BATCH_CODING_SYNC",
            payload={"batch_id": str(batch.id), "run_id": str(run.id)},
            created_by_user_id=run.initiated_by_user_id,
        )
        record_audit(
            db,
            tenant_id=scoring_run.tenant_id,
            actor_user_id=run.initiated_by_user_id,
            action="classification_projection.completed",
            target_type="review_batch_run",
            target_id=run.id,
            details={
                "review_batch_id": str(batch.id),
                "classification_scoring_run_id": str(scoring_run.id),
                "search_projection_operation_id": str(operation.id) if operation else None,
                "search_selection_error": selection_error,
                "result_summary": summary,
            },
        )
        db.commit()
    finally:
        artifact_file.close()


def fail_classifier_projection(db: Session, run_id: uuid.UUID, message: str) -> None:
    run = db.get(ReviewBatchRun, run_id)
    if (
        run is None
        or run.run_type != "CLASSIFIER"
        or run.status
        in {
            "COMPLETED",
            "COMPLETED_WITH_ERRORS",
            "CANCELED",
        }
    ):
        return
    run.status = "FAILED"
    run.error_message = message[:4000]
    run.completed_at = utcnow()
    transition_job_execution(
        db,
        job_type=CLASSIFICATION_PROJECTION_JOB_TYPE,
        job_id=run.id,
        status="FAILED",
        current_stage="FAILED",
        progress_completed=run.processed_document_count,
        failure_code="CLASSIFICATION_PROJECTION_FAILED",
        failure_summary=run.error_message,
        failure_retriable=True,
    )
    scoring_run = db.get(ClassificationScoringRun, run.classification_scoring_run_id)
    if scoring_run is not None:
        record_audit(
            db,
            tenant_id=scoring_run.tenant_id,
            actor_user_id=run.initiated_by_user_id,
            action="classification_projection.failed",
            target_type="review_batch_run",
            target_id=run.id,
            details={
                "review_batch_id": str(run.review_batch_id),
                "classification_scoring_run_id": str(scoring_run.id),
                "error": message[:1000],
            },
        )
    db.commit()


def retry_classifier_projection(
    db: Session,
    *,
    run: ReviewBatchRun,
    actor_user_id: uuid.UUID,
):
    if run.run_type != "CLASSIFIER":
        raise ClassificationProjectionError("Classifier projection run was not found")
    previous = current_job_execution(
        db,
        job_type=CLASSIFICATION_PROJECTION_JOB_TYPE,
        job_id=run.id,
        for_update=True,
    )
    if previous is None or previous.status != "FAILED" or not previous.failure_retriable:
        raise ClassificationProjectionError("Only a retryable failed classifier projection can be retried")
    scoring_run = db.get(ClassificationScoringRun, run.classification_scoring_run_id)
    batch = db.get(ReviewBatch, run.review_batch_id)
    matter = db.get(Matter, scoring_run.matter_id) if scoring_run is not None else None
    if scoring_run is None or batch is None or matter is None:
        raise ClassificationProjectionError("Classifier projection source is unavailable")
    attempt = previous.attempt_number + 1
    run.dbos_workflow_id = f"classification-prediction-project:{run.id}:attempt:{attempt}"
    run.status = "QUEUED"
    run.processed_document_count = 0
    run.result_summary = None
    run.error_message = None
    run.started_at = None
    run.completed_at = None
    run.initiated_by_user_id = actor_user_id
    return create_job_execution(
        db,
        job_type=CLASSIFICATION_PROJECTION_JOB_TYPE,
        job_id=run.id,
        scope=JobScope(scoring_run.tenant_id, matter.client_id, matter.id),
        workflow_key="classification_prediction_projection_v1",
        code_version="1",
        dbos_workflow_id=run.dbos_workflow_id,
        initiated_by_user_id=actor_user_id,
        idempotency_key=f"{run.idempotency_key}:attempt:{attempt}",
        progress_total=batch.document_count,
        progress_unit="documents",
        input_snapshot={
            "review_batch_id": str(batch.id),
            "classification_scoring_run_id": str(scoring_run.id),
            "prediction_content_hash": scoring_run.prediction_content_hash,
            "target_metadata_definition_id": run.configuration_snapshot.get("target_metadata_definition_id"),
        },
        configuration_snapshot=run.configuration_snapshot,
        retry_of=previous,
    )
