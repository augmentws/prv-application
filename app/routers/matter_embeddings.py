import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.embeddings.configuration import (
    canonical_hash,
    embedding_execution_configuration,
    processing_configuration,
)
from app.job_archives import job_not_archived
from app.job_executions import (
    MATTER_EMBEDDING_JOB_TYPE,
    JobExecutionConflict,
    JobExecutionNotFound,
    JobScope,
    cancel_job_execution,
    create_job_execution,
    current_job_execution,
    transition_job_execution,
)
from app.job_outcomes import finalize_canceled_embedding_outcome_manifest
from app.matter_embeddings import cancel_voyage_batches
from app.models import (
    ExternalProviderUsage,
    Matter,
    MatterEmbeddingBatch,
    MatterEmbeddingJob,
    SearchProjectionOperation,
)
from app.schemas import MatterEmbeddingBatchRead, MatterEmbeddingJobRead
from app.search.client import OpenSearchError, is_retryable_opensearch_error
from app.workflows.dispatcher import cancel_matter_embedding, enqueue_matter_embedding, get_dbos_client
from embedding_service.config import EmbeddingSettings, get_embedding_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/matters/{matter_id}/embedding-jobs", tags=["matter jobs"])
ACTIVE_STATUSES = {"QUEUED", "PLANNING", "RUNNING"}
_DBOS_TERMINAL_STATUSES = {"SUCCESS", "ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED", "CANCELLED"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _matter(db: Session, matter_id: uuid.UUID, principal: Principal) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    return matter


def _jobs_with_provider_usage(
    db: Session,
    jobs: list[MatterEmbeddingJob],
) -> list[MatterEmbeddingJobRead]:
    if not jobs:
        return []
    rows = db.execute(
        select(
            ExternalProviderUsage.job_id,
            func.coalesce(func.sum(ExternalProviderUsage.request_count), 0),
            func.coalesce(func.sum(ExternalProviderUsage.input_tokens), 0),
            func.coalesce(func.sum(ExternalProviderUsage.output_tokens), 0),
        )
        .where(
            ExternalProviderUsage.job_type == "MATTER_EMBEDDING",
            ExternalProviderUsage.job_id.in_([job.id for job in jobs]),
        )
        .group_by(ExternalProviderUsage.job_id)
    )
    usage_by_job = {
        job_id: (int(request_count), int(input_tokens), int(output_tokens))
        for job_id, request_count, input_tokens, output_tokens in rows
    }
    reports: list[MatterEmbeddingJobRead] = []
    for job in jobs:
        request_count, input_tokens, output_tokens = usage_by_job.get(job.id, (0, 0, 0))
        reports.append(
            MatterEmbeddingJobRead.model_validate(job).model_copy(
                update={
                    "provider_request_count": request_count,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                }
            )
        )
    return reports


def _refresh_job_totals_from_batches(
    db: Session,
    jobs: list[MatterEmbeddingJob],
) -> bool:
    if not jobs:
        return False
    rows = db.execute(
        select(
            MatterEmbeddingBatch.job_id,
            func.count(MatterEmbeddingBatch.id),
            func.coalesce(func.sum(MatterEmbeddingBatch.item_count), 0),
            func.coalesce(func.sum(MatterEmbeddingBatch.processed_count), 0),
            func.coalesce(func.sum(MatterEmbeddingBatch.embedded_count), 0),
            func.coalesce(func.sum(MatterEmbeddingBatch.reused_count), 0),
            func.coalesce(func.sum(MatterEmbeddingBatch.skipped_count), 0),
            func.coalesce(func.sum(MatterEmbeddingBatch.failed_count), 0),
            func.coalesce(func.sum(MatterEmbeddingBatch.chunk_count), 0),
        )
        .where(MatterEmbeddingBatch.job_id.in_([job.id for job in jobs]))
        .group_by(MatterEmbeddingBatch.job_id)
    )
    totals_by_job = {
        job_id: tuple(int(value) for value in values)
        for job_id, *values in rows
    }
    changed = False
    for job in jobs:
        totals = totals_by_job.get(job.id)
        if totals is None:
            continue
        current = (
            job.batch_count,
            job.total_count,
            job.processed_count,
            job.embedded_count,
            job.reused_count,
            job.skipped_count,
            job.failed_count,
            job.chunk_count,
        )
        if current == totals:
            continue
        (
            job.batch_count,
            job.total_count,
            job.processed_count,
            job.embedded_count,
            job.reused_count,
            job.skipped_count,
            job.failed_count,
            job.chunk_count,
        ) = totals
        changed = True
    return changed


def _durable_terminal_workflows(
    settings: Settings,
    workflow_ids: list[str],
) -> dict[str, tuple[str, str | None, datetime | None]]:
    if not settings.dbos_enabled or not workflow_ids:
        return {}
    workflows = get_dbos_client().list_workflows(
        workflow_ids=workflow_ids,
        status=list(_DBOS_TERMINAL_STATUSES),
        load_input=False,
        load_output=True,
    )
    return {
        workflow.workflow_id: (
            workflow.status,
            str(workflow.error) if workflow.error is not None else None,
            (
                datetime.fromtimestamp(workflow.completed_at / 1000, tz=timezone.utc)
                if workflow.completed_at is not None
                else None
            ),
        )
        for workflow in workflows
    }


def _synchronize_embedding_jobs(
    db: Session,
    jobs: list[MatterEmbeddingJob],
    settings: Settings,
) -> None:
    totals_changed = _refresh_job_totals_from_batches(db, jobs)
    execution_changed = False
    for job in jobs:
        if job.status not in ACTIVE_STATUSES:
            continue
        execution = current_job_execution(
            db,
            job_type=MATTER_EMBEDDING_JOB_TYPE,
            job_id=job.id,
        )
        if execution is None or execution.status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}:
            continue
        try:
            transition_job_execution(
                db,
                job_type=MATTER_EMBEDDING_JOB_TYPE,
                job_id=job.id,
                status="QUEUED" if job.status == "QUEUED" else "RUNNING",
                current_stage={
                    "QUEUED": "QUEUED",
                    "PLANNING": "PLANNING",
                    "RUNNING": "PROCESSING_BATCHES",
                }[job.status],
                progress_completed=job.processed_count,
                progress_total=job.total_count or None,
                progress={
                    "batch_count": job.batch_count,
                    "embedded_count": job.embedded_count,
                    "reused_count": job.reused_count,
                    "skipped_count": job.skipped_count,
                    "failed_count": job.failed_count,
                    "chunk_count": job.chunk_count,
                },
                heartbeat=False,
            )
            execution_changed = True
        except JobExecutionConflict:
            logger.warning("Could not synchronize common embedding progress for %s", job.id, exc_info=True)
    if totals_changed or execution_changed:
        db.commit()

    active_jobs = [job for job in jobs if job.status in ACTIVE_STATUSES]
    if not active_jobs:
        return
    try:
        terminal = _durable_terminal_workflows(
            settings,
            [job.workflow_id for job in active_jobs],
        )
    except Exception:
        logger.warning(
            "Could not reconcile durable embedding workflow statuses",
            exc_info=True,
        )
        return

    status_changed = False
    for job in active_jobs:
        durable = terminal.get(job.workflow_id)
        if durable is None:
            continue
        durable_status, durable_error, durable_completed_at = durable
        try:
            if durable_status == "SUCCESS":
                current = current_job_execution(
                    db,
                    job_type=MATTER_EMBEDDING_JOB_TYPE,
                    job_id=job.id,
                )
                if current is not None and current.status == "QUEUED":
                    transition_job_execution(
                        db,
                        job_type=MATTER_EMBEDDING_JOB_TYPE,
                        job_id=job.id,
                        status="RUNNING",
                        current_stage="RECONCILING",
                        progress_completed=job.processed_count,
                        progress_total=job.total_count or None,
                    )
                execution = transition_job_execution(
                    db,
                    job_type=MATTER_EMBEDDING_JOB_TYPE,
                    job_id=job.id,
                    status="COMPLETED_WITH_ERRORS" if job.failed_count else "COMPLETED",
                    current_stage="COMPLETED_WITH_ERRORS" if job.failed_count else "COMPLETED",
                    progress_completed=job.processed_count,
                    progress_total=job.total_count or None,
                    progress={
                        "embedded_count": job.embedded_count,
                        "reused_count": job.reused_count,
                        "skipped_count": job.skipped_count,
                        "failed_count": job.failed_count,
                        "chunk_count": job.chunk_count,
                    },
                )
            elif durable_status == "CANCELLED":
                execution, _ = cancel_job_execution(
                    db,
                    job_type=MATTER_EMBEDDING_JOB_TYPE,
                    job_id=job.id,
                    actor_user_id=job.created_by_user_id,
                )
            else:
                execution = transition_job_execution(
                    db,
                    job_type=MATTER_EMBEDDING_JOB_TYPE,
                    job_id=job.id,
                    status="FAILED",
                    current_stage="FAILED",
                    progress_completed=job.processed_count,
                    progress_total=job.total_count or None,
                    failure_code="MATTER_EMBEDDING_DURABLE_WORKFLOW_FAILED",
                    failure_summary=(
                        durable_error
                        or f"Durable embedding workflow ended with status {durable_status} before finalization"
                    ),
                    failure_retriable=False,
                )
            if durable_completed_at is not None:
                execution.completed_at = durable_completed_at
                job.completed_at = durable_completed_at
                if execution.status == "CANCELED":
                    job.canceled_at = durable_completed_at
        except (JobExecutionConflict, JobExecutionNotFound):
            logger.warning("Could not reconcile common execution for embedding job %s", job.id, exc_info=True)
            continue
        status_changed = True
    if status_changed:
        db.commit()


@router.post("", response_model=MatterEmbeddingJobRead, status_code=status.HTTP_202_ACCEPTED)
def create_embedding_job(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    embedding_settings: EmbeddingSettings = Depends(get_embedding_settings),
) -> MatterEmbeddingJobRead:
    matter = _matter(db, matter_id, principal)
    if matter.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter is not active")
    active = db.scalar(
        select(MatterEmbeddingJob).where(
            MatterEmbeddingJob.matter_id == matter.id,
            MatterEmbeddingJob.status.in_(ACTIVE_STATUSES),
        )
    )
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An embedding job is already active for this matter",
        )
    processing = processing_configuration(settings, embedding_settings)
    configuration = {
        **processing,
        "execution": embedding_execution_configuration(embedding_settings),
    }
    job_id = uuid.uuid4()
    job = MatterEmbeddingJob(
        id=job_id,
        matter_id=matter.id,
        status="QUEUED",
        workflow_id=f"matter-embedding:{job_id}",
        configuration_hash=canonical_hash(processing),
        configuration=configuration,
        embedding_model=embedding_settings.model,
        embedding_model_revision=embedding_settings.model_revision,
        embedding_dimensions=embedding_settings.dimensions,
        embedding_normalized=embedding_settings.normalize,
        created_by_user_id=principal.user.id,
    )
    db.add(job)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter.embedding_job.created",
        target_type="matter_embedding_job",
        target_id=job.id,
        details={
            "matter_id": str(matter.id),
            "model": job.embedding_model,
            "dimensions": job.embedding_dimensions,
            "configuration_hash": job.configuration_hash,
        },
    )
    try:
        db.flush()
        create_job_execution(
            db,
            job_type=MATTER_EMBEDDING_JOB_TYPE,
            job_id=job.id,
            scope=JobScope(
                tenant_id=matter.client.tenant_id,
                client_id=matter.client_id,
                matter_id=matter.id,
            ),
            workflow_key="matter_embedding_job_v2",
            code_version="2",
            dbos_workflow_id=job.workflow_id,
            initiated_by_user_id=principal.user.id,
            idempotency_key=f"matter-embedding:{job.id}:attempt:1",
            progress_unit="documents",
            input_snapshot={
                "matter_id": str(matter.id),
                "embedding_model": job.embedding_model,
                "embedding_model_revision": job.embedding_model_revision,
                "embedding_dimensions": job.embedding_dimensions,
            },
            configuration_snapshot=job.configuration,
        )
        enqueue_matter_embedding(db, job.workflow_id, str(job.id))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An embedding job is already active for this matter",
        ) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The embedding job could not be queued",
        ) from exc
    db.refresh(job)
    return _jobs_with_provider_usage(db, [job])[0]


@router.get("", response_model=list[MatterEmbeddingJobRead])
def list_embedding_jobs(
    matter_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[MatterEmbeddingJobRead]:
    _matter(db, matter_id, principal)
    jobs = list(
        db.scalars(
            select(MatterEmbeddingJob)
            .where(
                MatterEmbeddingJob.matter_id == matter_id,
                job_not_archived(MATTER_EMBEDDING_JOB_TYPE, MatterEmbeddingJob.id),
            )
            .order_by(MatterEmbeddingJob.created_at.desc())
            .limit(limit)
        )
    )
    _synchronize_embedding_jobs(db, jobs, settings)
    return _jobs_with_provider_usage(db, jobs)


@router.get("/{job_id}", response_model=MatterEmbeddingJobRead)
def get_embedding_job(
    matter_id: uuid.UUID,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterEmbeddingJobRead:
    _matter(db, matter_id, principal)
    job = db.scalar(
        select(MatterEmbeddingJob).where(
            MatterEmbeddingJob.id == job_id,
            MatterEmbeddingJob.matter_id == matter_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Embedding job not found")
    _synchronize_embedding_jobs(db, [job], settings)
    return _jobs_with_provider_usage(db, [job])[0]


@router.get("/{job_id}/batches", response_model=list[MatterEmbeddingBatchRead])
def list_embedding_batches(
    matter_id: uuid.UUID,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterEmbeddingBatch]:
    _matter(db, matter_id, principal)
    job = db.scalar(
        select(MatterEmbeddingJob).where(
            MatterEmbeddingJob.id == job_id,
            MatterEmbeddingJob.matter_id == matter_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Embedding job not found")
    return list(
        db.scalars(
            select(MatterEmbeddingBatch)
            .where(MatterEmbeddingBatch.job_id == job.id)
            .order_by(MatterEmbeddingBatch.batch_number)
        )
    )


@router.post("/{job_id}/retry-index", response_model=MatterEmbeddingJobRead, status_code=status.HTTP_202_ACCEPTED)
def retry_embedding_index(
    matter_id: uuid.UUID,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEmbeddingJobRead:
    matter = _matter(db, matter_id, principal)
    job = db.scalar(
        select(MatterEmbeddingJob)
        .where(MatterEmbeddingJob.id == job_id, MatterEmbeddingJob.matter_id == matter.id)
        .with_for_update()
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Embedding job not found")
    if job.status != "FAILED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Embedding job has not failed")
    active = db.scalar(
        select(MatterEmbeddingJob.id).where(
            MatterEmbeddingJob.matter_id == matter.id,
            MatterEmbeddingJob.id != job.id,
            MatterEmbeddingJob.status.in_(ACTIVE_STATUSES),
        )
    )
    if active is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Another embedding job is active")
    incomplete_batch_count = db.scalar(
        select(func.count())
        .select_from(MatterEmbeddingBatch)
        .where(MatterEmbeddingBatch.job_id == job.id, MatterEmbeddingBatch.status != "COMPLETED")
    )
    if not job.batch_count or incomplete_batch_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Embedding generation did not complete; start a new embedding job instead",
        )
    projection = db.scalar(
        select(SearchProjectionOperation)
        .where(SearchProjectionOperation.workflow_id == f"embedding-index-job:{job.id}")
        .with_for_update()
    )
    projection_error = projection.error_message if projection is not None else None
    if projection is None or not projection_error or not is_retryable_opensearch_error(OpenSearchError(projection_error)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Embedding job did not fail because of retryable search-index pressure",
        )

    retried_at = utcnow()
    processed_count = job.processed_count
    total_count = job.total_count
    prior_execution = current_job_execution(
        db,
        job_type=MATTER_EMBEDDING_JOB_TYPE,
        job_id=job.id,
        for_update=True,
    )
    if prior_execution is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Embedding job has no common execution")
    if prior_execution.status not in {"FAILED", "CANCELED"}:
        transition_job_execution(
            db,
            job_type=MATTER_EMBEDDING_JOB_TYPE,
            job_id=job.id,
            status="FAILED",
            current_stage="FAILED",
            progress_completed=processed_count,
            progress_total=total_count or None,
            failure_code="MATTER_EMBEDDING_INDEX_FAILED",
            failure_summary=projection_error,
            failure_retriable=True,
        )
    retry_history = job.configuration.get("index_retry_history")
    if not isinstance(retry_history, list):
        retry_history = []
    job.configuration = {
        **job.configuration,
        "index_retry_history": [
            *retry_history,
            {
                "workflow_id": job.workflow_id,
                "error_message": projection_error,
                "retried_at": retried_at.isoformat(),
                "retried_by_user_id": str(principal.user.id),
            },
        ],
    }
    retry_workflow_id = f"matter-embedding:{job.id}:index-retry:{uuid.uuid4()}"
    create_job_execution(
        db,
        job_type=MATTER_EMBEDDING_JOB_TYPE,
        job_id=job.id,
        scope=JobScope(
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
            matter_id=matter.id,
        ),
        workflow_key="matter_embedding_job_v2",
        code_version="2",
        dbos_workflow_id=retry_workflow_id,
        initiated_by_user_id=principal.user.id,
        idempotency_key=f"matter-embedding:{job.id}:index-retry:{retried_at.isoformat()}",
        current_stage="QUEUED",
        progress_total=total_count or None,
        progress_unit="documents",
        input_snapshot={
            "matter_id": str(matter.id),
            "embedding_model": job.embedding_model,
            "retry_phase": "INDEXING_SEARCH",
        },
        configuration_snapshot=job.configuration,
        retry_of=prior_execution,
    )
    transition_job_execution(
        db,
        job_type=MATTER_EMBEDDING_JOB_TYPE,
        job_id=job.id,
        progress_completed=processed_count,
        progress_total=total_count or None,
        progress={
            "batch_count": job.batch_count,
            "embedded_count": job.embedded_count,
            "reused_count": job.reused_count,
            "skipped_count": job.skipped_count,
            "failed_count": job.failed_count,
            "chunk_count": job.chunk_count,
            "retry_phase": "INDEXING_SEARCH",
        },
    )
    projection.status = "QUEUED"
    projection.error_message = None
    projection.started_at = None
    projection.completed_at = None
    enqueue_matter_embedding(db, retry_workflow_id, str(job.id))
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter.embedding_job.index_retried",
        target_type="matter_embedding_job",
        target_id=job.id,
        details={"prior_error": projection_error},
    )
    db.commit()
    db.refresh(job)
    return _jobs_with_provider_usage(db, [job])[0]


@router.post("/{job_id}/cancel", response_model=MatterEmbeddingJobRead)
def cancel_embedding_job(
    matter_id: uuid.UUID,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEmbeddingJobRead:
    matter = _matter(db, matter_id, principal)
    job = db.scalar(
        select(MatterEmbeddingJob).where(
            MatterEmbeddingJob.id == job_id,
            MatterEmbeddingJob.matter_id == matter_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Embedding job not found")
    totals_changed = _refresh_job_totals_from_batches(db, [job])
    if job.status not in ACTIVE_STATUSES:
        if job.status == "CANCELED":
            try:
                finalize_canceled_embedding_outcome_manifest(db, job)
                totals_changed = True
            except Exception:
                db.rollback()
                logger.exception(
                    "Canceled embedding job outcome reconciliation failed job_id=%s",
                    job.id,
                )
        if totals_changed:
            db.commit()
        return _jobs_with_provider_usage(db, [job])[0]
    execution = current_job_execution(
        db,
        job_type=MATTER_EMBEDDING_JOB_TYPE,
        job_id=job.id,
    )
    if execution is not None and execution.status not in {
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
                "reused_count": job.reused_count,
                "skipped_count": job.skipped_count,
                "failed_count": job.failed_count,
                "chunk_count": job.chunk_count,
            },
        )
    cancel_job_execution(
        db,
        job_type=MATTER_EMBEDDING_JOB_TYPE,
        job_id=job.id,
        actor_user_id=principal.user.id,
    )
    db.execute(
        MatterEmbeddingBatch.__table__.update()
        .where(MatterEmbeddingBatch.job_id == job.id, MatterEmbeddingBatch.status == "QUEUED")
        .values(status="CANCELED")
    )
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter.embedding_job.canceled",
        target_type="matter_embedding_job",
        target_id=job.id,
    )
    db.commit()
    try:
        cancel_matter_embedding(job.workflow_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("DBOS cancellation request failed for %s: %s", job.workflow_id, exc)
    cancel_voyage_batches(job.id, db=db)
    try:
        finalize_canceled_embedding_outcome_manifest(db, job)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "Canceled embedding job outcome finalization will require reconciliation job_id=%s",
            job.id,
        )
    db.refresh(job)
    return _jobs_with_provider_usage(db, [job])[0]
