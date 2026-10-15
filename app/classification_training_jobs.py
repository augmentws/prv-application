import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.classification_builds import create_training_run, prepare_build, retry_training_run
from app.classification_datasets import (
    begin_dataset_snapshot,
    fail_dataset_snapshot,
    materialize_dataset_snapshot,
    prepare_dataset_snapshot,
)
from app.classification_features import (
    ClassificationFeatureSplitError,
    begin_feature_split,
    embedding_job_output_is_usable,
    materialize_feature_split,
    prepare_feature_split,
    request_matches_materialized_snapshot,
)
from app.classification_lifecycle import ClassificationLifecycleError, authorization_context
from app.database import SessionLocal
from app.job_executions import (
    CLASSIFICATION_TRAINING_JOB_TYPE,
    JobScope,
    create_job_execution,
    current_job_execution,
    transition_job_execution,
)
from app.models import (
    ClassificationDatasetSnapshot,
    ClassificationModel,
    ClassificationModelConfiguration,
    ClassificationTrainingJob,
    ClassificationTrainingRun,
    Matter,
    MatterEmbeddingJob,
    ReviewBatch,
    WorkflowRun,
)
from app.schemas import (
    ClassificationDatasetSnapshotRequest,
    ClassificationFeatureSplitRequest,
    ClassificationTrainingJobCreate,
)

ACTIVE_STATUSES = {
    "QUEUED",
    "SNAPSHOTTING",
    "PREPARING_FEATURES",
    "LAUNCHING",
    "TRAINING",
    "FINALIZING",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ClassificationTrainingJobError(RuntimeError):
    def __init__(self, message: str, *, code: str = "CLASSIFICATION_TRAINING_JOB_INVALID") -> None:
        super().__init__(message)
        self.code = code


class ClassificationTrainingJobNotFoundError(ClassificationTrainingJobError):
    pass


class ClassificationTrainingJobConflictError(ClassificationTrainingJobError):
    pass


def get_training_job(
    db: Session,
    *,
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    training_job_id: uuid.UUID,
    for_update: bool = False,
) -> ClassificationTrainingJob:
    statement = select(ClassificationTrainingJob).where(
        ClassificationTrainingJob.id == training_job_id,
        ClassificationTrainingJob.matter_id == matter_id,
        ClassificationTrainingJob.classification_model_id == classification_model_id,
    )
    if for_update:
        statement = statement.with_for_update()
    job = db.scalar(statement)
    if job is None:
        raise ClassificationTrainingJobNotFoundError(
            "Classification training job not found",
            code="CLASSIFICATION_TRAINING_JOB_NOT_FOUND",
        )
    return job


def _validate_inputs(
    db: Session,
    *,
    matter: Matter,
    classification_model_id: uuid.UUID,
    payload: ClassificationTrainingJobCreate,
) -> tuple[ClassificationModel, ClassificationModelConfiguration, MatterEmbeddingJob]:
    model = db.scalar(
        select(ClassificationModel).where(
            ClassificationModel.id == classification_model_id,
            ClassificationModel.matter_id == matter.id,
        )
    )
    if model is None:
        raise ClassificationTrainingJobNotFoundError(
            "Classification model not found",
            code="CLASSIFICATION_MODEL_NOT_FOUND",
        )
    if model.status != "ACTIVE":
        raise ClassificationTrainingJobConflictError(
            "Classification model must be active",
            code="CLASSIFICATION_MODEL_NOT_ACTIVE",
        )
    configuration = db.scalar(
        select(ClassificationModelConfiguration).where(
            ClassificationModelConfiguration.id == payload.configuration_id,
            ClassificationModelConfiguration.classification_model_id == model.id,
        )
    )
    if configuration is None:
        raise ClassificationTrainingJobConflictError(
            "Classification configuration does not belong to this model",
            code="CLASSIFICATION_CONFIGURATION_MISMATCH",
        )
    embedding_job = db.get(MatterEmbeddingJob, payload.embedding_job_id)
    if embedding_job is None or embedding_job.matter_id != matter.id:
        raise ClassificationTrainingJobConflictError(
            "Embedding job was not found in this matter",
            code="EMBEDDING_JOB_NOT_FOUND",
        )
    if not embedding_job_output_is_usable(embedding_job):
        raise ClassificationTrainingJobConflictError(
            "Embedding job does not have complete reusable output",
            code="EMBEDDING_JOB_NOT_COMPLETE",
        )
    scope = payload.training_scope
    if scope.scope_type.value == "MATTER":
        if scope.scope_id != matter.id:
            raise ClassificationTrainingJobConflictError(
                "Matter training scope must identify the model matter",
                code="TRAINING_SCOPE_MISMATCH",
            )
    else:
        batch = db.get(ReviewBatch, scope.scope_id)
        if batch is None or batch.matter_id != matter.id or batch.status != "READY":
            raise ClassificationTrainingJobConflictError(
                "Review-batch training scope must identify a ready batch in this matter",
                code="TRAINING_SCOPE_NOT_READY",
            )
    return model, configuration, embedding_job


def create_training_job(
    db: Session,
    *,
    matter: Matter,
    classification_model_id: uuid.UUID,
    payload: ClassificationTrainingJobCreate,
    actor_user_id: uuid.UUID,
) -> tuple[ClassificationTrainingJob, bool]:
    model, configuration, _ = _validate_inputs(
        db,
        matter=matter,
        classification_model_id=classification_model_id,
        payload=payload,
    )
    existing = db.scalar(
        select(ClassificationTrainingJob).where(
            ClassificationTrainingJob.classification_model_id == model.id,
            ClassificationTrainingJob.idempotency_key == payload.idempotency_key,
        )
    )
    if existing is not None:
        return existing, False
    active = db.scalar(
        select(ClassificationTrainingJob).where(
            ClassificationTrainingJob.classification_model_id == model.id,
            ClassificationTrainingJob.status.in_(ACTIVE_STATUSES),
        )
    )
    if active is not None:
        raise ClassificationTrainingJobConflictError(
            "This model already has an active training job",
            code="CLASSIFICATION_TRAINING_JOB_ACTIVE",
        )
    job_id = uuid.uuid4()
    job = ClassificationTrainingJob(
        id=job_id,
        tenant_id=model.tenant_id,
        matter_id=matter.id,
        classification_model_id=model.id,
        configuration_id=configuration.id,
        embedding_job_id=payload.embedding_job_id,
        idempotency_key=payload.idempotency_key,
        workflow_id=f"classification-training-job:{job_id}",
        scope_type=payload.training_scope.scope_type.value,
        scope_id=payload.training_scope.scope_id,
        random_seed=payload.random_seed,
        validation_specification=payload.validation_policy.model_dump(mode="json"),
        status="QUEUED",
        current_stage="QUEUED",
        progress_completed=0,
        progress_total=4,
        created_by_user_id=actor_user_id,
    )
    db.add(job)
    db.flush()
    create_job_execution(
        db,
        job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
        job_id=job.id,
        scope=JobScope(
            tenant_id=job.tenant_id,
            client_id=matter.client_id,
            matter_id=matter.id,
        ),
        workflow_key="classification_training_job_v1",
        code_version="1",
        dbos_workflow_id=job.workflow_id,
        initiated_by_user_id=actor_user_id,
        idempotency_key=f"classification-training-job:{job.id}:attempt:1",
        current_stage="QUEUED",
        progress_total=job.progress_total,
        progress_unit="stages",
        input_snapshot={
            "classification_model_id": str(job.classification_model_id),
            "configuration_id": str(job.configuration_id),
            "embedding_job_id": str(job.embedding_job_id),
            "scope_type": job.scope_type,
            "scope_id": str(job.scope_id),
            "random_seed": job.random_seed,
        },
        configuration_snapshot={
            "validation_specification": job.validation_specification,
        },
    )
    record_audit(
        db,
        tenant_id=job.tenant_id,
        actor_user_id=actor_user_id,
        action="classification_training_job.created",
        target_type="classification_training_job",
        target_id=job.id,
        details={
            "matter_id": str(job.matter_id),
            "classification_model_id": str(job.classification_model_id),
            "configuration_id": str(job.configuration_id),
            "embedding_job_id": str(job.embedding_job_id),
            "scope_type": job.scope_type,
            "scope_id": str(job.scope_id),
        },
    )
    return job, True


def resolve_training_job(
    db: Session,
    *,
    classification_model_id: uuid.UUID,
    idempotency_key: str,
) -> ClassificationTrainingJob:
    job = db.scalar(
        select(ClassificationTrainingJob).where(
            ClassificationTrainingJob.classification_model_id == classification_model_id,
            ClassificationTrainingJob.idempotency_key == idempotency_key,
        )
    )
    if job is None:
        raise ClassificationTrainingJobConflictError(
            "Concurrent training job could not be resolved",
            code="CLASSIFICATION_TRAINING_JOB_CONFLICT",
        )
    return job


def _authorized_job(db: Session, job_id: uuid.UUID) -> tuple[ClassificationTrainingJob, Matter]:
    job = db.scalar(
        select(ClassificationTrainingJob)
        .where(ClassificationTrainingJob.id == job_id)
        .with_for_update()
    )
    if job is None:
        raise ClassificationTrainingJobNotFoundError("Classification training job not found")
    matter = db.get(Matter, job.matter_id)
    if matter is None:
        raise ClassificationTrainingJobConflictError("Training-job matter not found")
    try:
        authorization_context(db, matter=matter, actor_user_id=job.created_by_user_id)
    except ClassificationLifecycleError as exc:
        raise ClassificationTrainingJobConflictError(
            "Training-job creator is no longer authorized for the matter",
            code="TRAINING_JOB_AUTHORIZATION_REVOKED",
        ) from exc
    return job, matter


def materialize_training_job_snapshot(job_id: uuid.UUID) -> uuid.UUID | None:
    with SessionLocal() as db:
        job, matter = _authorized_job(db, job_id)
        if job.status == "CANCELED":
            return None
        transition_job_execution(
            db,
            job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
            job_id=job.id,
            status="RUNNING",
            current_stage="RESOLVING_LABELS_AND_SNAPSHOTTING",
            progress_completed=0,
            progress_total=job.progress_total,
            progress={"dataset_snapshot_id": None, "training_run_id": None},
        )
        db.commit()

        request = ClassificationDatasetSnapshotRequest.model_validate(
            {
                "configuration_id": job.configuration_id,
                "purpose": "TRAINING",
                "training_scope": {
                    "scope_type": job.scope_type,
                    "scope_id": job.scope_id,
                },
            }
        )
        prepared = prepare_dataset_snapshot(
            db,
            matter=matter,
            classification_model_id=job.classification_model_id,
            actor_user_id=job.created_by_user_id,
            request=request,
        )
        snapshot, should_materialize, _ = begin_dataset_snapshot(
            db,
            prepared=prepared,
            actor_user_id=job.created_by_user_id,
        )
        job.dataset_snapshot_id = snapshot.id
        db.commit()
        if should_materialize:
            materialize_dataset_snapshot(
                db,
                snapshot=snapshot,
                prepared=prepared,
                actor_user_id=job.created_by_user_id,
            )
        transition_job_execution(
            db,
            job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
            job_id=job.id,
            status="RUNNING",
            current_stage="PREPARING_FEATURES_AND_SPLITS",
            progress_completed=1,
            progress_total=job.progress_total,
            progress={"dataset_snapshot_id": str(snapshot.id), "training_run_id": None},
        )
        db.commit()
        return snapshot.id


def materialize_training_job_features(job_id: uuid.UUID, snapshot_id: uuid.UUID) -> None:
    def record_progress(processed: int, ready: int, missing: int, failed: int) -> None:
        del processed  # The persisted category counts add up to the processed total.
        with SessionLocal() as progress_db:
            progress_job = progress_db.get(ClassificationTrainingJob, job_id)
            progress_snapshot = progress_db.get(ClassificationDatasetSnapshot, snapshot_id)
            if progress_job is None or progress_snapshot is None:
                return
            if progress_job.status not in ACTIVE_STATUSES:
                return
            progress_snapshot.feature_ready_count = max(
                progress_snapshot.feature_ready_count,
                ready,
            )
            progress_snapshot.feature_missing_count = max(
                progress_snapshot.feature_missing_count,
                missing,
            )
            progress_snapshot.feature_failed_count = max(
                progress_snapshot.feature_failed_count,
                failed,
            )
            progress_job.updated_at = utcnow()
            execution = current_job_execution(
                progress_db,
                job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
                job_id=job_id,
            )
            if execution is not None and execution.status in {"RUNNING", "WAITING"}:
                transition_job_execution(
                    progress_db,
                    job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
                    job_id=job_id,
                    status="RUNNING",
                    current_stage="PREPARING_FEATURES_AND_SPLITS",
                    progress_completed=max(execution.progress_completed, 1),
                    progress_total=progress_job.progress_total,
                    progress={
                        "dataset_snapshot_id": str(snapshot_id),
                        "features_ready": ready,
                        "features_missing": missing,
                        "features_failed": failed,
                    },
                )
            progress_db.commit()

    with SessionLocal() as db:
        job, _ = _authorized_job(db, job_id)
        if job.status == "CANCELED":
            return
        snapshot = db.get(ClassificationDatasetSnapshot, snapshot_id)
        if (
            snapshot is None
            or snapshot.matter_id != job.matter_id
            or snapshot.classification_model_id != job.classification_model_id
        ):
            raise ClassificationTrainingJobConflictError("Training-job dataset snapshot was not found")
        if snapshot.status == "FAILED" and snapshot.failure_code:
            raise ClassificationFeatureSplitError(
                snapshot.failure_summary or "Feature and split materialization failed",
                code=snapshot.failure_code,
            )
        request = ClassificationFeatureSplitRequest.model_validate(
            {
                "embedding_job_id": job.embedding_job_id,
                "random_seed": job.random_seed,
                "validation_policy": job.validation_specification,
            }
        )
        transition_job_execution(
            db,
            job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
            job_id=job.id,
            status="RUNNING",
            current_stage="PREPARING_FEATURES_AND_SPLITS",
            progress_completed=max(job.progress_completed, 1),
            progress_total=job.progress_total,
        )
        db.commit()
        if not request_matches_materialized_snapshot(snapshot, request):
            if snapshot.status != "BUILDING":
                begin_feature_split(snapshot)
                db.commit()
            try:
                prepared = prepare_feature_split(
                    db,
                    snapshot=snapshot,
                    request=request,
                    actor_user_id=job.created_by_user_id,
                    progress_callback=record_progress,
                )
                materialize_feature_split(
                    db,
                    snapshot=snapshot,
                    prepared=prepared,
                    actor_user_id=job.created_by_user_id,
                )
            except ClassificationFeatureSplitError as exc:
                fail_dataset_snapshot(
                    snapshot,
                    failure_code=exc.code,
                    failure_summary=str(exc),
                )
                db.commit()
                raise
        transition_job_execution(
            db,
            job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
            job_id=job.id,
            status="RUNNING",
            current_stage="LAUNCHING_TRAINING",
            progress_completed=2,
            progress_total=job.progress_total,
        )
        db.commit()


def launch_training_job_build(job_id: uuid.UUID) -> dict[str, str] | None:
    with SessionLocal() as db:
        job, _ = _authorized_job(db, job_id)
        if job.status == "CANCELED":
            return None
        if job.dataset_snapshot_id is None:
            raise ClassificationTrainingJobConflictError("Training-job dataset snapshot is missing")
        transition_job_execution(
            db,
            job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
            job_id=job.id,
            status="RUNNING",
            current_stage="LAUNCHING_TRAINING",
            progress_completed=max(job.progress_completed, 2),
            progress_total=job.progress_total,
        )
        db.commit()
        prepared = prepare_build(
            db,
            matter_id=job.matter_id,
            classification_model_id=job.classification_model_id,
            dataset_snapshot_id=job.dataset_snapshot_id,
        )
        run, _ = create_training_run(
            db,
            prepared=prepared,
            idempotency_key=f"training-job:{job.id}",
            actor_user_id=job.created_by_user_id,
        )
        job.training_run_id = run.id
        transition_job_execution(
            db,
            job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
            job_id=job.id,
            status="WAITING",
            current_stage="TRAINING_MODEL",
            progress_completed=3,
            progress_total=job.progress_total,
            progress={
                "dataset_snapshot_id": str(job.dataset_snapshot_id),
                "training_run_id": str(run.id),
            },
        )
        db.commit()
        return {"training_run_id": str(run.id), "workflow_id": run.workflow_id}


def complete_training_job(job_id: uuid.UUID) -> None:
    with SessionLocal() as db:
        job = db.scalar(
            select(ClassificationTrainingJob)
            .where(ClassificationTrainingJob.id == job_id)
            .with_for_update()
        )
        if job is None or job.status == "CANCELED":
            return
        run = db.get(ClassificationTrainingRun, job.training_run_id) if job.training_run_id else None
        if run is None or run.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
            raise ClassificationTrainingJobConflictError("Training run did not complete successfully")
        transition_job_execution(
            db,
            job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
            job_id=job.id,
            status="COMPLETED",
            current_stage="COMPLETED",
            progress_completed=job.progress_total,
            progress_total=job.progress_total,
            progress={
                "dataset_snapshot_id": str(job.dataset_snapshot_id),
                "training_run_id": str(run.id),
            },
            failure_retriable=False,
        )
        record_audit(
            db,
            tenant_id=job.tenant_id,
            actor_user_id=job.created_by_user_id,
            action="classification_training_job.completed",
            target_type="classification_training_job",
            target_id=job.id,
            details={"training_run_id": str(run.id)},
        )
        db.commit()


def fail_training_job(
    job_id: uuid.UUID,
    message: str,
    *,
    code: str = "CLASSIFICATION_TRAINING_JOB_FAILED",
    retriable: bool = False,
) -> None:
    with SessionLocal() as db:
        job = db.scalar(
            select(ClassificationTrainingJob)
            .where(ClassificationTrainingJob.id == job_id)
            .with_for_update()
        )
        if job is None or job.status in {"COMPLETED", "CANCELED"}:
            return
        run = db.get(ClassificationTrainingRun, job.training_run_id) if job.training_run_id else None
        if run is not None and run.status == "FAILED":
            code = run.failure_code or code
            message = run.failure_summary or message
            retriable = run.failure_retriable
        snapshot = (
            db.get(ClassificationDatasetSnapshot, job.dataset_snapshot_id)
            if job.dataset_snapshot_id
            else None
        )
        if snapshot is not None and snapshot.status == "BUILDING":
            fail_dataset_snapshot(
                snapshot,
                failure_code=code,
                failure_summary=message,
            )
        execution = current_job_execution(
            db,
            job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
            job_id=job.id,
        )
        if execution is not None and execution.status not in {"COMPLETED", "FAILED", "CANCELED"}:
            transition_job_execution(
                db,
                job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
                job_id=job.id,
                status="FAILED",
                current_stage="FAILED",
                failure_code=code,
                failure_summary=message,
                failure_retriable=retriable,
            )
        else:
            job.status = "FAILED"
            job.current_stage = "FAILED"
            job.failure_code = code[:100]
            job.failure_summary = message[:4000]
            job.failure_retriable = retriable
            job.completed_at = utcnow()
        db.commit()


def retry_training_job(
    db: Session,
    job: ClassificationTrainingJob,
    *,
    actor_user_id: uuid.UUID,
) -> WorkflowRun:
    current = current_job_execution(
        db,
        job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
        job_id=job.id,
        for_update=True,
    )
    if current is None:
        raise ClassificationTrainingJobConflictError("Training job has no execution history")
    if current.status not in {"FAILED", "CANCELED"}:
        raise ClassificationTrainingJobConflictError("Only failed or canceled training jobs can be retried")
    if current.status == "FAILED" and not current.failure_retriable:
        raise ClassificationTrainingJobConflictError("This training failure is not marked as retryable")
    attempt_number = current.attempt_number + 1
    workflow_id = f"classification-training-job:{job.id}:attempt:{attempt_number}"
    job.status = "QUEUED"
    job.current_stage = "QUEUED"
    job.progress_completed = 0
    job.failure_code = None
    job.failure_summary = None
    job.failure_retriable = False
    job.started_at = None
    job.completed_at = None
    job.canceled_at = None
    if job.training_run_id is not None:
        run = db.get(ClassificationTrainingRun, job.training_run_id)
        if run is not None and run.status in {"FAILED", "CANCELED"}:
            retry_training_run(db, run=run, actor_user_id=actor_user_id)
    matter = db.get(Matter, job.matter_id)
    if matter is None:
        raise ClassificationTrainingJobConflictError("Training-job matter not found")
    return create_job_execution(
        db,
        job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
        job_id=job.id,
        scope=JobScope(
            tenant_id=job.tenant_id,
            client_id=matter.client_id,
            matter_id=job.matter_id,
        ),
        workflow_key="classification_training_job_v1",
        code_version="1",
        dbos_workflow_id=workflow_id,
        initiated_by_user_id=actor_user_id,
        idempotency_key=f"classification-training-job:{job.id}:attempt:{attempt_number}",
        current_stage="QUEUED",
        progress_total=job.progress_total,
        progress_unit="stages",
        input_snapshot=current.input_snapshot,
        binding_snapshot=current.binding_snapshot,
        configuration_snapshot=current.configuration_snapshot,
        retry_of=current,
    )


def training_job_failure_details(exc: Exception) -> tuple[str, bool, str]:
    nested_errors = getattr(exc, "errors", None)
    if isinstance(nested_errors, list):
        for nested in reversed(nested_errors):
            if isinstance(nested, Exception):
                return training_job_failure_details(nested)
    if isinstance(exc, ClassificationTrainingJobError):
        return exc.code, False, str(exc)
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code, False, str(exc)
    return "CLASSIFICATION_TRAINING_JOB_FAILED", True, str(exc)
