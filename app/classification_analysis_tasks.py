from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.classification_datasets import (
    begin_dataset_snapshot,
    fail_dataset_snapshot,
    materialize_dataset_snapshot,
    prepare_dataset_snapshot,
)
from app.classification_features import (
    ClassificationFeatureSplitError,
    embedding_job_output_is_usable,
    feature_specification_hash_for_job,
    materialize_scoring_features,
    prepare_scoring_features,
)
from app.classification_lifecycle import ClassificationLifecycleError, authorization_context
from app.classification_scoring import (
    cancel_scoring_run,
    create_scoring_run,
    prepare_scoring,
    retry_scoring_run,
)
from app.database import SessionLocal
from app.job_executions import (
    CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
    JobScope,
    cancel_job_execution,
    create_job_execution,
    current_job_execution,
    execution_cancel_requested,
    transition_job_execution,
)
from app.models import (
    ClassificationDatasetSnapshot,
    ClassificationModel,
    ClassificationModelConfiguration,
    ClassificationModelVersion,
    ClassificationScoringRun,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskRun,
    MatterAnalysisTaskVersion,
    MatterEmbeddingJob,
    ReviewBatch,
    WorkflowRun,
)
from app.schemas import ClassificationDatasetSnapshotRequest, MatterAnalysisTaskRunCreate


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ClassificationAnalysisTaskError(RuntimeError):
    def __init__(self, message: str, *, code: str = "CLASSIFICATION_ANALYSIS_TASK_INVALID") -> None:
        super().__init__(message)
        self.code = code


class ClassificationAnalysisTaskCanceled(ClassificationAnalysisTaskError):
    def __init__(self) -> None:
        super().__init__(
            "Classification Analysis Task run was canceled",
            code="CLASSIFICATION_ANALYSIS_TASK_CANCELED",
        )


def cancel_classification_analysis_run(
    db: Session,
    run: MatterAnalysisTaskRun,
    *,
    actor_user_id: uuid.UUID,
) -> bool:
    if run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}:
        return False
    if run.classification_scoring_run is not None and run.classification_scoring_run.status not in {
        "COMPLETED",
        "COMPLETED_WITH_ERRORS",
        "FAILED",
        "CANCELED",
    }:
        cancel_scoring_run(run.classification_scoring_run)
    if run.current_workflow_run_id is not None:
        cancel_job_execution(
            db,
            job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
            job_id=run.id,
            actor_user_id=actor_user_id,
        )
    else:
        canceled_at = utcnow()
        run.status = "CANCELED"
        run.current_stage = "CANCELED"
        run.failure_retriable = False
        run.canceled_at = canceled_at
        run.completed_at = canceled_at
    return True


def retry_classification_analysis_run(
    db: Session,
    run: MatterAnalysisTaskRun,
    *,
    actor_user_id: uuid.UUID,
) -> WorkflowRun:
    current = current_job_execution(
        db,
        job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
        job_id=run.id,
        for_update=True,
    )
    if current is None:
        raise ClassificationAnalysisTaskError("Classification Analysis Task execution was not found")
    if run.status in {"FAILED", "CANCELED"} and current.status not in {"FAILED", "CANCELED"}:
        current = transition_job_execution(
            db,
            job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
            job_id=run.id,
            status=run.status,
            current_stage=run.current_stage,
            progress_completed=run.progress_completed,
            failure_code=run.failure_code,
            failure_summary=run.failure_summary,
            failure_retriable=run.failure_retriable,
        )
    if current.status not in {"FAILED", "CANCELED"}:
        raise ClassificationAnalysisTaskError("Only failed or canceled Analysis Task runs can be retried")
    if current.status == "FAILED" and not current.failure_retriable:
        raise ClassificationAnalysisTaskError("This Analysis Task failure is not retryable")

    attempt_number = current.attempt_number + 1
    workflow_id = f"classification-analysis-task:{run.id}:attempt:{attempt_number}"
    execution = create_job_execution(
        db,
        job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
        job_id=run.id,
        scope=JobScope(
            tenant_id=run.tenant_id,
            client_id=current.client_id,
            matter_id=run.matter_id,
        ),
        workflow_key="classification_analysis_task_v1",
        code_version="1",
        dbos_workflow_id=workflow_id,
        initiated_by_user_id=actor_user_id,
        idempotency_key=run.idempotency_key,
        current_stage="QUEUED",
        progress_total=run.progress_total,
        progress_unit="stages",
        input_snapshot=current.input_snapshot,
        binding_snapshot=current.binding_snapshot,
        configuration_snapshot=current.configuration_snapshot,
        retry_of=current,
    )
    record_audit(
        db,
        tenant_id=run.tenant_id,
        actor_user_id=actor_user_id,
        action="matter_analysis_task.classification_run.retried",
        target_type="matter_analysis_task_run",
        target_id=run.id,
        details={
            "previous_execution_id": str(current.id),
            "execution_id": str(execution.id),
            "attempt_number": execution.attempt_number,
            "matter_id": str(run.matter_id),
        },
    )
    return execution


def create_classification_analysis_run(
    db: Session,
    *,
    matter: Matter,
    task: MatterAnalysisTask,
    payload: MatterAnalysisTaskRunCreate,
    actor_user_id: uuid.UUID,
) -> tuple[MatterAnalysisTaskRun, bool]:
    if task.task_type != "CLASSIFICATION" or task.status != "ACTIVE":
        raise ClassificationAnalysisTaskError("An active classification Analysis Task is required")
    if task.published_version is None:
        raise ClassificationAnalysisTaskError("Publish the classification task before running it")
    task_version = db.scalar(
        select(MatterAnalysisTaskVersion).where(
            MatterAnalysisTaskVersion.matter_analysis_task_id == task.id,
            MatterAnalysisTaskVersion.version == task.published_version,
        )
    )
    if task_version is None or task_version.status != "PUBLISHED":
        raise ClassificationAnalysisTaskError("Published classification task version is unavailable")
    if task_version.classification_model_id is None or task_version.classification_model_version_id is None:
        raise ClassificationAnalysisTaskError("Classification task version has no pinned model version")
    model = db.scalar(
        select(ClassificationModel).where(
            ClassificationModel.id == task_version.classification_model_id,
            ClassificationModel.matter_id == matter.id,
            ClassificationModel.status == "ACTIVE",
        )
    )
    version = db.scalar(
        select(ClassificationModelVersion).where(
            ClassificationModelVersion.id == task_version.classification_model_version_id,
            ClassificationModelVersion.classification_model_id == task_version.classification_model_id,
            ClassificationModelVersion.matter_id == matter.id,
            ClassificationModelVersion.status == "APPROVED",
        )
    )
    if model is None or version is None:
        raise ClassificationAnalysisTaskError("Pinned classification model version is no longer runnable")
    embedding_job = db.get(MatterEmbeddingJob, payload.embedding_job_id)
    if embedding_job is None or embedding_job.matter_id != matter.id:
        raise ClassificationAnalysisTaskError("Embedding job was not found in this matter")
    if not embedding_job_output_is_usable(embedding_job):
        raise ClassificationAnalysisTaskError("Embedding job does not have complete reusable output")
    configuration = db.get(ClassificationModelConfiguration, version.configuration_id)
    if configuration is None or configuration.classification_model_id != model.id:
        raise ClassificationAnalysisTaskError("Pinned model configuration is unavailable")
    try:
        feature_hash = feature_specification_hash_for_job(configuration, embedding_job)
    except ClassificationFeatureSplitError as exc:
        raise ClassificationAnalysisTaskError(str(exc), code=exc.code) from exc
    if feature_hash != version.feature_specification_hash:
        raise ClassificationAnalysisTaskError(
            "Selected embeddings are incompatible with the pinned model version",
            code="FEATURE_CONTRACT_MISMATCH",
        )
    if payload.scope_type == "MATTER":
        if payload.scope_id != matter.id:
            raise ClassificationAnalysisTaskError("Matter scope must identify this matter")
    else:
        batch = db.get(ReviewBatch, payload.scope_id)
        if batch is None or batch.matter_id != matter.id or batch.status != "READY":
            raise ClassificationAnalysisTaskError("Review-batch scope must identify a ready batch")
    existing = db.scalar(
        select(MatterAnalysisTaskRun).where(
            MatterAnalysisTaskRun.matter_analysis_task_id == task.id,
            MatterAnalysisTaskRun.idempotency_key == payload.idempotency_key,
        )
    )
    if existing is not None:
        return existing, False
    run_id = uuid.uuid4()
    run = MatterAnalysisTaskRun(
        id=run_id,
        tenant_id=matter.client.tenant_id,
        matter_id=matter.id,
        matter_analysis_task_id=task.id,
        matter_analysis_task_version_id=task_version.id,
        scope_type=payload.scope_type,
        scope_id=payload.scope_id,
        classification_model_id=model.id,
        classification_model_version_id=version.id,
        embedding_job_id=embedding_job.id,
        idempotency_key=payload.idempotency_key,
        workflow_id=f"classification-analysis-task:{run_id}",
        status="QUEUED",
        current_stage="QUEUED",
        progress_completed=0,
        progress_total=4,
        created_by_user_id=actor_user_id,
    )
    db.add(run)
    db.flush()
    create_job_execution(
        db,
        job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
        job_id=run.id,
        scope=JobScope(
            tenant_id=run.tenant_id,
            client_id=matter.client_id,
            matter_id=matter.id,
        ),
        workflow_key="classification_analysis_task_v1",
        code_version="1",
        dbos_workflow_id=run.workflow_id,
        initiated_by_user_id=actor_user_id,
        idempotency_key=payload.idempotency_key,
        current_stage="QUEUED",
        progress_total=run.progress_total,
        progress_unit="stages",
        input_snapshot={
            "matter_analysis_task_id": str(task.id),
            "matter_analysis_task_version_id": str(task_version.id),
            "scope_type": run.scope_type,
            "scope_id": str(run.scope_id),
            "embedding_job_id": str(run.embedding_job_id),
            "classification_model_id": str(run.classification_model_id),
            "classification_model_version_id": str(run.classification_model_version_id),
        },
    )
    record_audit(
        db,
        tenant_id=run.tenant_id,
        actor_user_id=actor_user_id,
        action="matter_analysis_task.classification_run.created",
        target_type="matter_analysis_task_run",
        target_id=run.id,
        details={
            "matter_id": str(matter.id),
            "task_id": str(task.id),
            "task_version_id": str(task_version.id),
            "classification_model_id": str(model.id),
            "classification_model_version_id": str(version.id),
            "scope_type": run.scope_type,
            "scope_id": str(run.scope_id),
        },
    )
    return run, True


def _authorized_run(db: Session, run_id: uuid.UUID) -> tuple[MatterAnalysisTaskRun, Matter]:
    run = db.scalar(select(MatterAnalysisTaskRun).where(MatterAnalysisTaskRun.id == run_id).with_for_update())
    if run is None:
        raise ClassificationAnalysisTaskError("Classification Analysis Task run was not found")
    matter = db.get(Matter, run.matter_id)
    if matter is None:
        raise ClassificationAnalysisTaskError("Analysis Task matter was not found")
    try:
        authorization_context(db, matter=matter, actor_user_id=run.created_by_user_id)
    except ClassificationLifecycleError as exc:
        raise ClassificationAnalysisTaskError(
            "Analysis Task creator is no longer authorized",
            code="ANALYSIS_TASK_AUTHORIZATION_REVOKED",
        ) from exc
    return run, matter


def materialize_classification_analysis_snapshot(
    run_id: uuid.UUID,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> uuid.UUID | None:
    with session_factory() as db:
        run, matter = _authorized_run(db, run_id)
        if run.status == "CANCELED" or execution_cancel_requested(
            db,
            job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
            job_id=run.id,
        ):
            return None
        version = db.get(ClassificationModelVersion, run.classification_model_version_id)
        if version is None:
            raise ClassificationAnalysisTaskError("Pinned model version is unavailable")
        transition_job_execution(
            db,
            job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
            job_id=run.id,
            status="RUNNING",
            current_stage="FREEZING_POPULATION",
            progress_completed=0,
        )
        db.commit()
        request = ClassificationDatasetSnapshotRequest.model_validate(
            {
                "configuration_id": version.configuration_id,
                "purpose": "SCORING",
                "training_scope": {"scope_type": run.scope_type, "scope_id": run.scope_id},
                "embedding_job_id": run.embedding_job_id,
            }
        )
        prepared = prepare_dataset_snapshot(
            db,
            matter=matter,
            classification_model_id=run.classification_model_id,
            actor_user_id=run.created_by_user_id,
            request=request,
        )
        snapshot, should_materialize, _ = begin_dataset_snapshot(
            db,
            prepared=prepared,
            actor_user_id=run.created_by_user_id,
        )
        run.dataset_snapshot_id = snapshot.id
        run.population_document_count = snapshot.population_count
        db.commit()
        if should_materialize:
            materialize_dataset_snapshot(
                db,
                snapshot=snapshot,
                prepared=prepared,
                actor_user_id=run.created_by_user_id,
            )
        transition_job_execution(
            db,
            job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
            job_id=run.id,
            current_stage="MATERIALIZING_FEATURES",
            progress_completed=1,
        )
        db.commit()
        return snapshot.id


def materialize_classification_analysis_features(
    run_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    def record_progress(processed: int, ready: int, missing: int, failed: int) -> None:
        del processed  # The persisted category counts add up to the processed total.
        with session_factory() as progress_db:
            progress_run = progress_db.scalar(
                select(MatterAnalysisTaskRun).where(MatterAnalysisTaskRun.id == run_id).with_for_update()
            )
            if (
                progress_run is None
                or progress_run.status == "CANCELED"
                or execution_cancel_requested(
                    progress_db,
                    job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
                    job_id=run_id,
                )
            ):
                raise ClassificationAnalysisTaskCanceled()
            progress_snapshot = progress_db.get(ClassificationDatasetSnapshot, snapshot_id)
            if progress_snapshot is None:
                raise ClassificationAnalysisTaskError("Scoring snapshot is unavailable")
            progress_snapshot.feature_ready_count = ready
            progress_snapshot.feature_missing_count = missing
            progress_snapshot.feature_failed_count = failed
            transition_job_execution(
                progress_db,
                job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
                job_id=run_id,
                current_stage="MATERIALIZING_FEATURES",
                progress={
                    "feature_ready_count": ready,
                    "feature_missing_count": missing,
                    "feature_failed_count": failed,
                },
            )
            progress_db.commit()

    with session_factory() as db:
        run, _ = _authorized_run(db, run_id)
        if run.status == "CANCELED" or execution_cancel_requested(
            db,
            job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
            job_id=run.id,
        ):
            return
        snapshot = db.get(ClassificationDatasetSnapshot, snapshot_id)
        if snapshot is None or snapshot.id != run.dataset_snapshot_id or snapshot.purpose != "SCORING":
            raise ClassificationAnalysisTaskError("Scoring snapshot is unavailable")
        if not (
            snapshot.status == "READY_FOR_SCORING"
            and (snapshot.feature_specification or {}).get("embedding_job_id") == str(run.embedding_job_id)
        ):
            snapshot.status = "BUILDING"
            db.commit()
            try:
                prepared = prepare_scoring_features(
                    db,
                    snapshot=snapshot,
                    embedding_job_id=run.embedding_job_id,
                    actor_user_id=run.created_by_user_id,
                    progress_callback=record_progress,
                )
                materialize_scoring_features(
                    db,
                    snapshot=snapshot,
                    prepared=prepared,
                    actor_user_id=run.created_by_user_id,
                )
            except ClassificationFeatureSplitError as exc:
                fail_dataset_snapshot(snapshot, failure_code=exc.code, failure_summary=str(exc))
                db.commit()
                raise
        transition_job_execution(
            db,
            job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
            job_id=run.id,
            current_stage="LAUNCHING_SCORING",
            progress_completed=2,
        )
        db.commit()


def launch_classification_analysis_scoring(
    run_id: uuid.UUID,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> dict[str, str] | None:
    with session_factory() as db:
        run, _ = _authorized_run(db, run_id)
        if run.status == "CANCELED" or execution_cancel_requested(
            db,
            job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
            job_id=run.id,
        ):
            return None
        if run.dataset_snapshot_id is None:
            raise ClassificationAnalysisTaskError("Scoring snapshot is missing")
        prepared = prepare_scoring(
            db,
            matter_id=run.matter_id,
            classification_model_id=run.classification_model_id,
            dataset_snapshot_id=run.dataset_snapshot_id,
            model_version_id=run.classification_model_version_id,
        )
        scoring_run, _ = create_scoring_run(
            db,
            prepared=prepared,
            idempotency_key=f"analysis-task-run:{run.id}",
            actor_user_id=run.created_by_user_id,
        )
        if scoring_run.status in {"FAILED", "CANCELED"}:
            analysis_execution = current_job_execution(
                db,
                job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
                job_id=run.id,
            )
            if analysis_execution is None or analysis_execution.attempt_number <= 1:
                raise ClassificationAnalysisTaskError("Terminal scoring run cannot be reused without a retry attempt")
            retry_scoring_run(
                db,
                run=scoring_run,
                actor_user_id=run.created_by_user_id,
            )
        run.classification_scoring_run_id = scoring_run.id
        transition_job_execution(
            db,
            job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
            job_id=run.id,
            current_stage="SCORING_DOCUMENTS",
            progress_completed=3,
        )
        db.commit()
        return {"scoring_run_id": str(scoring_run.id), "workflow_id": scoring_run.workflow_id}


def complete_classification_analysis_run(
    run_id: uuid.UUID,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    with session_factory() as db:
        run, _ = _authorized_run(db, run_id)
        if run.status == "CANCELED" or execution_cancel_requested(
            db,
            job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
            job_id=run.id,
        ):
            return
        scoring_run = db.get(ClassificationScoringRun, run.classification_scoring_run_id)
        if scoring_run is None or scoring_run.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
            raise ClassificationAnalysisTaskError("Classification scoring did not complete")
        transition_job_execution(
            db,
            job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
            job_id=run.id,
            status=scoring_run.status,
            current_stage=scoring_run.status,
            progress_completed=run.progress_total,
            failure_code=scoring_run.failure_code,
            failure_summary=scoring_run.failure_summary,
            failure_retriable=scoring_run.failure_retriable,
        )
        record_audit(
            db,
            tenant_id=run.tenant_id,
            actor_user_id=run.created_by_user_id,
            action="matter_analysis_task.classification_run.completed",
            target_type="matter_analysis_task_run",
            target_id=run.id,
            details={"classification_scoring_run_id": str(scoring_run.id), "status": run.status},
        )
        db.commit()


def fail_classification_analysis_run(
    run_id: uuid.UUID,
    message: str,
    *,
    code: str = "CLASSIFICATION_ANALYSIS_TASK_FAILED",
    retriable: bool = True,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    with session_factory() as db:
        run = db.get(MatterAnalysisTaskRun, run_id)
        if run is None or run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELED"}:
            return
        scoring_run = db.get(ClassificationScoringRun, run.classification_scoring_run_id)
        if scoring_run is not None and scoring_run.status == "FAILED":
            code = scoring_run.failure_code or code
            message = scoring_run.failure_summary or message
            retriable = scoring_run.failure_retriable
        if run.current_workflow_run_id is not None:
            transition_job_execution(
                db,
                job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
                job_id=run.id,
                status="FAILED",
                current_stage="FAILED",
                failure_code=code,
                failure_summary=message,
                failure_retriable=retriable,
            )
        else:
            run.status = "FAILED"
            run.current_stage = "FAILED"
            run.failure_code = code[:100]
            run.failure_summary = message[:4000]
            run.failure_retriable = retriable
            run.completed_at = utcnow()
        db.commit()


def classification_analysis_failure_details(exc: Exception) -> tuple[str, bool]:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code, False
    return "CLASSIFICATION_ANALYSIS_TASK_FAILED", True
