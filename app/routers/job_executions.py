import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analysis_task_batch import (
    cancel_analysis_task_batch_run,
    retry_analysis_task_batch_documents,
)
from app.artifact_gateway import remove_selection
from app.audit import record_audit
from app.authorization import ResourceRef, can
from app.classification_analysis_tasks import (
    ClassificationAnalysisTaskError,
    cancel_classification_analysis_run,
    retry_classification_analysis_run,
)
from app.classification_builds import cancel_training_run
from app.classification_projections import (
    ClassificationProjectionError,
    retry_classifier_projection,
)
from app.classification_scoring import (
    ClassificationScoringError,
    cancel_scoring_run,
    retry_scoring_run,
)
from app.classification_training_jobs import ClassificationTrainingJobError, retry_training_job
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.fact_development import (
    cancel_fact_development_run,
    finalize_canceled_fact_development_run,
    retry_fact_development_run,
)
from app.fact_exports import retry_fact_export_job
from app.job_archives import MATTER_BULK_TAG_JOB_TYPE, MATTER_TOPIC_JOB_TYPE, is_job_archived
from app.job_executions import (
    ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
    CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
    CLASSIFICATION_PROJECTION_JOB_TYPE,
    CLASSIFICATION_SCORING_RUN_JOB_TYPE,
    CLASSIFICATION_TRAINING_JOB_TYPE,
    FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
    MATTER_DOCUMENT_IMPORT_JOB_TYPE,
    MATTER_EMBEDDING_JOB_TYPE,
    MATTER_FACT_EXPORT_JOB_TYPE,
    MATTER_SEARCH_EXPORT_JOB_TYPE,
    REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
    JobExecutionConflict,
    JobExecutionNotFound,
    cancel_job_execution,
    current_job_execution,
    job_execution_history,
    load_job_and_scope,
    transition_job_execution,
)
from app.matter_embeddings import cancel_voyage_batches
from app.models import (
    ClassificationScoringRun,
    ClassificationTrainingJob,
    JobArchive,
    Matter,
    MatterAnalysisTaskRun,
    MatterBulkTagJob,
    MatterDocument,
    MatterDocumentImportJob,
    MatterEmbeddingBatch,
    MatterEmbeddingJob,
    MatterFactExportJob,
    MatterSearchExportJob,
    MatterTopicJob,
    ReviewBatch,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    WorkflowRun,
)
from app.review_batches import retry_review_batch_execution
from app.schemas import (
    JobExecutionHistoryRead,
    JobExecutionRead,
    JobFailedDocumentPage,
    JobFailedDocumentRead,
    JobFailedDocumentRetryRequest,
)
from app.search_exports import retry_search_export_job
from app.workflows.dispatcher import (
    cancel_analysis_task_batch,
    cancel_classification_analysis_task,
    cancel_classification_model_build,
    cancel_classification_model_scoring,
    cancel_classification_training_job,
    cancel_fact_development_run_workflow,
    cancel_fact_export,
    cancel_matter_embedding,
    cancel_matter_import,
    cancel_review_batch,
    cancel_search_export,
    enqueue_analysis_task_batch,
    enqueue_classification_analysis_task,
    enqueue_classification_model_scoring,
    enqueue_classification_projection,
    enqueue_classification_training_job,
    enqueue_fact_development_run,
    enqueue_fact_export,
    enqueue_review_batch,
    enqueue_search_export,
)

router = APIRouter(prefix="/v1/jobs", tags=["job executions"])
logger = logging.getLogger(__name__)

_TERMINAL_JOB_STATUSES = {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}


def _authorized_job(
    db: Session,
    principal: Principal,
    *,
    job_type: str,
    job_id: uuid.UUID,
    for_update: bool = False,
):
    try:
        adapter, job, scope, matter = load_job_and_scope(
            db,
            job_type=job_type,
            job_id=job_id,
            for_update=for_update,
        )
    except JobExecutionNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if matter is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter access required")
    if adapter.job_type == CLASSIFICATION_SCORING_RUN_JOB_TYPE:
        authorized = can(db, principal, "classification.score", ResourceRef.matter(matter.id))
    elif adapter.job_type == CLASSIFICATION_PROJECTION_JOB_TYPE:
        authorized = can(db, principal, "classification.score", ResourceRef.matter(matter.id)) and can(
            db, principal, "batch.configure", ResourceRef.matter(matter.id)
        )
    elif adapter.job_type == REVIEW_BATCH_MATERIALIZATION_JOB_TYPE:
        authorized = can(db, principal, "batch.configure", ResourceRef.matter(matter.id))
    elif adapter.job_type == MATTER_SEARCH_EXPORT_JOB_TYPE:
        authorized = can(db, principal, "document.export", ResourceRef.matter(matter.id))
    elif adapter.job_type == FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE:
        authorized = can(db, principal, "analysis_task.run", ResourceRef.matter(matter.id))
    elif adapter.job_type == MATTER_FACT_EXPORT_JOB_TYPE:
        authorized = all(
            can(db, principal, permission_key, ResourceRef.matter(matter.id))
            for permission_key in ("fact.export", "fact.view", "document.view")
        )
    else:
        authorized = can_admin_matter(db, principal, matter)
    if not authorized:
        detail = (
            "Permission required for this job type"
            if adapter.job_type
            in {
                REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
                CLASSIFICATION_SCORING_RUN_JOB_TYPE,
                CLASSIFICATION_PROJECTION_JOB_TYPE,
                MATTER_SEARCH_EXPORT_JOB_TYPE,
                MATTER_FACT_EXPORT_JOB_TYPE,
                FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
            }
            else "Matter ADMIN required"
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    return adapter, job, scope, matter


def _archivable_job(
    db: Session,
    principal: Principal,
    *,
    job_type: str,
    job_id: uuid.UUID,
):
    normalized_type = job_type.upper()
    if normalized_type == MATTER_BULK_TAG_JOB_TYPE:
        job = db.scalar(select(MatterBulkTagJob).where(MatterBulkTagJob.id == job_id).with_for_update())
        matter = db.get(Matter, job.matter_id) if job is not None else None
        current_status = job.status if job is not None else None
    elif normalized_type == MATTER_TOPIC_JOB_TYPE:
        job = db.scalar(select(MatterTopicJob).where(MatterTopicJob.id == job_id).with_for_update())
        matter = db.get(Matter, job.matter_id) if job is not None else None
        current_status = job.status if job is not None else None
    else:
        try:
            adapter, job, _, matter = load_job_and_scope(
                db,
                job_type=normalized_type,
                job_id=job_id,
                for_update=True,
            )
        except JobExecutionNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        normalized_type = adapter.job_type
        execution = current_job_execution(
            db,
            job_type=normalized_type,
            job_id=job_id,
            for_update=True,
        )
        current_status = execution.status if execution is not None else getattr(job, "status", None)
    if job is None or matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job was not found")
    if not can(db, principal, "job.delete", ResourceRef.matter(matter.id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission required: job.delete")
    return normalized_type, job, matter, current_status


@router.delete("/{job_type}/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_job(
    job_type: str,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Response:
    normalized_type, _, matter, current_status = _archivable_job(
        db,
        principal,
        job_type=job_type,
        job_id=job_id,
    )
    if is_job_archived(db, job_type=normalized_type, job_id=job_id):
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if current_status not in _TERMINAL_JOB_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cancel or complete the job before deleting it",
        )
    db.add(
        JobArchive(
            job_type=normalized_type,
            job_id=job_id,
            tenant_id=matter.client.tenant_id,
            matter_id=matter.id,
            archived_by_user_id=principal.user.id,
        )
    )
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="job.deleted",
        target_type=normalized_type.lower(),
        target_id=job_id,
        details={
            "matter_id": str(matter.id),
            "job_type": normalized_type,
            "previous_status": current_status,
            "soft_delete": True,
        },
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{job_type}/{job_id}/execution", response_model=JobExecutionHistoryRead)
def get_job_execution(
    job_type: str,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> JobExecutionHistoryRead:
    adapter, _, _, _ = _authorized_job(
        db,
        principal,
        job_type=job_type,
        job_id=job_id,
    )
    attempts = job_execution_history(db, job_type=adapter.job_type, job_id=job_id)
    return JobExecutionHistoryRead(
        job_type=adapter.job_type,
        job_id=job_id,
        current_execution=(JobExecutionRead.model_validate(attempts[0]) if attempts else None),
        attempts=[JobExecutionRead.model_validate(attempt) for attempt in attempts],
    )


@router.get(
    "/{job_type}/{job_id}/failed-documents",
    response_model=JobFailedDocumentPage,
)
def list_failed_job_documents(
    job_type: str,
    job_id: uuid.UUID,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> JobFailedDocumentPage:
    adapter, job, _, _ = _authorized_job(
        db,
        principal,
        job_type=job_type,
        job_id=job_id,
    )
    if adapter.job_type != ANALYSIS_TASK_BATCH_RUN_JOB_TYPE or not isinstance(job, ReviewBatchRun):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This job type does not expose document-level failures yet",
        )
    failure_filter = (
        ReviewBatchRunDocument.review_batch_run_id == job.id,
        ReviewBatchRunDocument.status == "FAILED",
    )
    total, retryable_count = db.execute(
        select(
            func.count(),
            func.count().filter(ReviewBatchRunDocument.failure_retriable.is_(True)),
        ).where(*failure_filter)
    ).one()
    rows = db.execute(
        select(ReviewBatchRunDocument, MatterDocument, WorkflowRun.attempt_number)
        .join(MatterDocument, MatterDocument.id == ReviewBatchRunDocument.matter_document_id)
        .outerjoin(WorkflowRun, WorkflowRun.id == ReviewBatchRunDocument.last_workflow_run_id)
        .where(*failure_filter)
        .order_by(
            ReviewBatchRunDocument.failure_retriable.desc(),
            ReviewBatchRunDocument.completed_at.desc(),
            ReviewBatchRunDocument.matter_document_id,
        )
        .offset(offset)
        .limit(limit)
    ).all()
    return JobFailedDocumentPage(
        job_type=adapter.job_type,
        job_id=job.id,
        total=int(total),
        retryable_count=int(retryable_count or 0),
        offset=offset,
        limit=limit,
        items=[
            JobFailedDocumentRead(
                matter_document_id=run_document.matter_document_id,
                source_collection_id=document.source_collection_id,
                collection_item_id=document.collection_item_id,
                failure_code=run_document.failure_code or "FAILURE_DETAILS_UNAVAILABLE",
                failure_message=(
                    run_document.failure_message
                    or "No per-document failure reason was retained for this historical run."
                ),
                failure_retriable=run_document.failure_retriable,
                attempt_number=attempt_number,
                failed_at=run_document.completed_at,
            )
            for run_document, document, attempt_number in rows
        ],
    )


@router.post(
    "/{job_type}/{job_id}/failed-documents/retry",
    response_model=JobExecutionRead,
)
def retry_failed_job_documents(
    job_type: str,
    job_id: uuid.UUID,
    payload: JobFailedDocumentRetryRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> JobExecutionRead:
    adapter, job, _, matter = _authorized_job(
        db,
        principal,
        job_type=job_type,
        job_id=job_id,
        for_update=True,
    )
    if adapter.job_type != ANALYSIS_TASK_BATCH_RUN_JOB_TYPE or not isinstance(job, ReviewBatchRun):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This job type does not support document-level retry yet",
        )
    try:
        execution = retry_analysis_task_batch_documents(
            db,
            job,
            initiated_by_user_id=principal.user.id,
            document_ids=payload.document_ids,
        )
    except JobExecutionConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=execution.tenant_id,
        actor_user_id=principal.user.id,
        action="analysis_task_batch.failed_documents_retried",
        target_type="review_batch_run",
        target_id=job.id,
        details={
            "matter_id": str(matter.id) if matter is not None else None,
            "document_count": execution.progress_total,
            "attempt_number": execution.attempt_number,
        },
    )
    enqueue_analysis_task_batch(db, execution.dbos_workflow_id, str(execution.id))
    db.commit()
    db.refresh(execution)
    return JobExecutionRead.model_validate(execution)


@router.post("/{job_type}/{job_id}/cancel", response_model=JobExecutionRead)
def cancel_job(
    job_type: str,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> JobExecutionRead:
    adapter, job, _, matter = _authorized_job(
        db,
        principal,
        job_type=job_type,
        job_id=job_id,
    )
    if adapter.job_type == MATTER_EMBEDDING_JOB_TYPE and isinstance(job, MatterEmbeddingJob):
        current = current_job_execution(
            db,
            job_type=MATTER_EMBEDDING_JOB_TYPE,
            job_id=job.id,
        )
        if current is not None and current.status not in {
            "COMPLETED",
            "COMPLETED_WITH_ERRORS",
            "FAILED",
            "CANCELED",
        }:
            transition_job_execution(
                db,
                job_type=MATTER_EMBEDDING_JOB_TYPE,
                job_id=job.id,
                status="QUEUED" if job.status == "QUEUED" else "RUNNING",
                current_stage="QUEUED" if job.status == "QUEUED" else "PROCESSING_BATCHES",
                progress_completed=job.processed_count,
                progress_total=job.total_count or None,
                progress={
                    "batch_count": job.batch_count,
                    "embedded_count": job.embedded_count,
                    "skipped_count": job.skipped_count,
                    "failed_count": job.failed_count,
                    "chunk_count": job.chunk_count,
                },
            )
        execution, canceled = cancel_job_execution(
            db,
            job_type=MATTER_EMBEDDING_JOB_TYPE,
            job_id=job.id,
            actor_user_id=principal.user.id,
        )
        db.query(MatterEmbeddingBatch).filter(
            MatterEmbeddingBatch.job_id == job.id,
            MatterEmbeddingBatch.status == "QUEUED",
        ).update({MatterEmbeddingBatch.status: "CANCELED"}, synchronize_session=False)
        db.commit()
        if canceled:
            try:
                cancel_matter_embedding(job.workflow_id)
            except Exception as exc:  # noqa: BLE001 - durable cancellation is already committed
                logger.warning("DBOS cancellation request failed for %s: %s", job.workflow_id, exc)
            cancel_voyage_batches(job.id, db=db)
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type == MATTER_DOCUMENT_IMPORT_JOB_TYPE and isinstance(job, MatterDocumentImportJob):
        current = current_job_execution(
            db,
            job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
            job_id=job.id,
        )
        if current is not None and current.status not in {
            "COMPLETED",
            "COMPLETED_WITH_ERRORS",
            "FAILED",
            "CANCELED",
        }:
            transition_job_execution(
                db,
                job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
                job_id=job.id,
                status="QUEUED" if job.status == "QUEUED" else "RUNNING",
                current_stage=("FREEZING_SELECTION" if job.status == "SNAPSHOTTING" else "PROCESSING_BATCHES"),
                progress_completed=job.processed_count,
                progress_total=job.matched_count or None,
                progress={
                    "batch_count": job.batch_count,
                    "added_count": job.added_count,
                    "duplicate_count": job.duplicate_count,
                    "failed_count": job.failed_count,
                    "imported_label_document_count": job.imported_label_document_count,
                    "imported_label_event_count": job.imported_label_event_count,
                    "imported_label_warning_count": job.imported_label_warning_count,
                },
            )
        execution, canceled = cancel_job_execution(
            db,
            job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
            job_id=job.id,
            actor_user_id=principal.user.id,
        )
        selection_id = job.artifact_selection_id
        db.commit()
        if canceled:
            if selection_id is not None:
                try:
                    remove_selection(
                        selection_id=selection_id,
                        actor_user_id=job.created_by_user_id,
                        tenant_id=matter.client.tenant_id,
                        client_id=matter.client_id,
                    )
                    job.artifact_selection_id = None
                    db.commit()
                except Exception as exc:  # noqa: BLE001 - cancellation is already durable
                    logger.warning("Artifact selection cleanup failed for %s: %s", job.workflow_id, exc)
            try:
                cancel_matter_import(job.workflow_id)
            except Exception as exc:  # noqa: BLE001 - durable cancellation is already committed
                logger.warning("DBOS cancellation request failed for %s: %s", job.workflow_id, exc)
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type == REVIEW_BATCH_MATERIALIZATION_JOB_TYPE and isinstance(job, ReviewBatch):
        execution, canceled = cancel_job_execution(
            db,
            job_type=REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
            job_id=job.id,
            actor_user_id=principal.user.id,
        )
        workflow_id = job.workflow_id
        db.commit()
        if canceled:
            try:
                cancel_review_batch(workflow_id)
            except Exception as exc:  # noqa: BLE001 - durable cancellation is already committed
                logger.warning("DBOS cancellation request failed for %s: %s", workflow_id, exc)
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type == MATTER_SEARCH_EXPORT_JOB_TYPE and isinstance(job, MatterSearchExportJob):
        if job.current_stage == "UPLOADING":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The search export is finalizing and can no longer be canceled",
            )
        execution, canceled = cancel_job_execution(
            db,
            job_type=MATTER_SEARCH_EXPORT_JOB_TYPE,
            job_id=job.id,
            actor_user_id=principal.user.id,
        )
        workflow_id = job.workflow_id
        db.commit()
        if canceled:
            try:
                cancel_search_export(workflow_id)
            except Exception as exc:  # noqa: BLE001 - cancellation is already durable
                logger.warning("DBOS cancellation request failed for %s: %s", workflow_id, exc)
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type == MATTER_FACT_EXPORT_JOB_TYPE and isinstance(job, MatterFactExportJob):
        if job.current_stage == "UPLOADING":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The fact export is finalizing and can no longer be canceled",
            )
        execution, canceled = cancel_job_execution(
            db,
            job_type=MATTER_FACT_EXPORT_JOB_TYPE,
            job_id=job.id,
            actor_user_id=principal.user.id,
        )
        workflow_id = job.workflow_id
        db.commit()
        if canceled:
            try:
                cancel_fact_export(workflow_id)
            except Exception as exc:  # noqa: BLE001 - cancellation is already durable
                logger.warning("DBOS cancellation request failed for %s: %s", workflow_id, exc)
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type == CLASSIFICATION_TRAINING_JOB_TYPE and isinstance(job, ClassificationTrainingJob):
        child_workflow_id = job.training_run.workflow_id if job.training_run is not None else None
        execution, canceled = cancel_job_execution(
            db,
            job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
            job_id=job.id,
            actor_user_id=principal.user.id,
        )
        if canceled and job.training_run is not None:
            cancel_training_run(job.training_run)
        workflow_id = job.workflow_id
        db.commit()
        if canceled:
            try:
                cancel_classification_training_job(workflow_id)
                if child_workflow_id is not None:
                    cancel_classification_model_build(child_workflow_id)
            except Exception as exc:  # noqa: BLE001 - durable cancellation is already committed
                logger.warning("DBOS cancellation request failed for %s: %s", workflow_id, exc)
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type == CLASSIFICATION_SCORING_RUN_JOB_TYPE and isinstance(job, ClassificationScoringRun):
        canceled = cancel_scoring_run(job)
        execution, _ = cancel_job_execution(
            db,
            job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
            job_id=job.id,
            actor_user_id=principal.user.id,
        )
        workflow_id = job.workflow_id
        db.commit()
        if canceled:
            try:
                cancel_classification_model_scoring(workflow_id)
            except Exception as exc:  # noqa: BLE001 - durable cancellation is already committed
                logger.warning("DBOS cancellation request failed for %s: %s", workflow_id, exc)
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type == ANALYSIS_TASK_BATCH_RUN_JOB_TYPE and isinstance(job, ReviewBatchRun):
        execution, canceled = cancel_analysis_task_batch_run(
            db,
            job.id,
            actor_user_id=principal.user.id,
        )
        workflow_id = job.dbos_workflow_id
        db.commit()
        if canceled and workflow_id is not None:
            try:
                cancel_analysis_task_batch(workflow_id)
            except Exception as exc:  # noqa: BLE001 - durable cancellation is already committed
                logger.warning("DBOS cancellation request failed for %s: %s", workflow_id, exc)
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type == FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE and isinstance(job, MatterAnalysisTaskRun):
        execution, canceled = cancel_fact_development_run(db, job, actor_user_id=principal.user.id)
        workflow_id = job.workflow_id
        db.commit()
        if canceled:
            try:
                cancel_fact_development_run_workflow(workflow_id)
            except Exception as exc:  # noqa: BLE001 - durable cancellation is already committed
                logger.warning("DBOS cancellation request failed for %s: %s", workflow_id, exc)
            try:
                finalize_canceled_fact_development_run(db, job)
            except Exception:
                db.rollback()
                logger.exception("Canceled fact-development run outcome finalization failed run_id=%s", job.id)
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type != CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE or not isinstance(job, MatterAnalysisTaskRun):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This job type does not support cancellation yet",
        )
    scoring_workflow_id = (
        job.classification_scoring_run.workflow_id if job.classification_scoring_run is not None else None
    )
    canceled = cancel_classification_analysis_run(
        db,
        job,
        actor_user_id=principal.user.id,
    )
    execution = job.current_workflow_run
    db.commit()
    if canceled:
        try:
            cancel_classification_analysis_task(job.workflow_id)
            if scoring_workflow_id is not None:
                cancel_classification_model_scoring(scoring_workflow_id)
        except Exception as exc:  # noqa: BLE001 - durable cancellation is already committed
            logger.warning("DBOS cancellation request failed for %s: %s", job.workflow_id, exc)
    if execution is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job has no common execution")
    db.refresh(execution)
    return JobExecutionRead.model_validate(execution)


@router.post("/{job_type}/{job_id}/retry", response_model=JobExecutionRead)
def retry_job(
    job_type: str,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> JobExecutionRead:
    adapter, job, _, _ = _authorized_job(
        db,
        principal,
        job_type=job_type,
        job_id=job_id,
        for_update=True,
    )
    if not adapter.supports_retry:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This job type does not support safe retry yet",
        )
    if adapter.job_type == REVIEW_BATCH_MATERIALIZATION_JOB_TYPE and isinstance(job, ReviewBatch):
        try:
            execution = retry_review_batch_execution(
                db,
                job,
                initiated_by_user_id=principal.user.id,
            )
        except JobExecutionConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        enqueue_review_batch(db, execution.dbos_workflow_id, str(job.id))
        db.commit()
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type == MATTER_SEARCH_EXPORT_JOB_TYPE and isinstance(job, MatterSearchExportJob):
        try:
            execution = retry_search_export_job(db, job, actor_user_id=principal.user.id)
        except JobExecutionConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        enqueue_search_export(db, execution.dbos_workflow_id, str(job.id))
        db.commit()
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type == FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE and isinstance(job, MatterAnalysisTaskRun):
        try:
            execution = retry_fact_development_run(db, job, actor_user_id=principal.user.id)
        except JobExecutionConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        enqueue_fact_development_run(db, execution.dbos_workflow_id, str(job.id))
        db.commit()
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type == MATTER_FACT_EXPORT_JOB_TYPE and isinstance(job, MatterFactExportJob):
        try:
            execution = retry_fact_export_job(db, job, actor_user_id=principal.user.id)
        except JobExecutionConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        enqueue_fact_export(db, execution.dbos_workflow_id, str(job.id))
        db.commit()
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type == CLASSIFICATION_TRAINING_JOB_TYPE and isinstance(job, ClassificationTrainingJob):
        try:
            execution = retry_training_job(
                db,
                job,
                actor_user_id=principal.user.id,
            )
        except (ClassificationTrainingJobError, JobExecutionConflict) as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        enqueue_classification_training_job(db, execution.dbos_workflow_id, str(job.id))
        db.commit()
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type == CLASSIFICATION_SCORING_RUN_JOB_TYPE and isinstance(job, ClassificationScoringRun):
        try:
            execution = retry_scoring_run(
                db,
                run=job,
                actor_user_id=principal.user.id,
            )
        except ClassificationScoringError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        enqueue_classification_model_scoring(db, execution.dbos_workflow_id, str(job.id))
        db.commit()
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type == CLASSIFICATION_PROJECTION_JOB_TYPE and isinstance(job, ReviewBatchRun):
        try:
            execution = retry_classifier_projection(
                db,
                run=job,
                actor_user_id=principal.user.id,
            )
        except ClassificationProjectionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        enqueue_classification_projection(db, execution.dbos_workflow_id, str(job.id))
        db.commit()
        db.refresh(execution)
        return JobExecutionRead.model_validate(execution)
    if adapter.job_type != CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE or not isinstance(
        job,
        MatterAnalysisTaskRun,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This job type does not support safe retry yet",
        )
    try:
        execution = retry_classification_analysis_run(
            db,
            job,
            actor_user_id=principal.user.id,
        )
    except (ClassificationAnalysisTaskError, JobExecutionConflict) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    enqueue_classification_analysis_task(db, execution.dbos_workflow_id, str(job.id))
    db.commit()
    db.refresh(execution)
    return JobExecutionRead.model_validate(execution)
