import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.artifact_gateway import (
    ensure_collection_scope,
    get_collection_item_read,
    read_artifact_bytes,
    remove_selection,
)
from app.audit import record_audit
from app.authorization import ResourceRef, can
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.job_archives import job_not_archived
from app.job_executions import (
    MATTER_DOCUMENT_IMPORT_JOB_TYPE,
    JobExecutionConflict,
    JobExecutionNotFound,
    JobScope,
    cancel_job_execution,
    create_job_execution,
    current_job_execution,
    transition_job_execution,
)
from app.models import (
    Matter,
    MatterDocument,
    MatterDocumentCustodian,
    MatterDocumentImportBatch,
    MatterDocumentImportJob,
)
from app.schemas import (
    MatterDocumentImportCreate,
    MatterDocumentImportRead,
    MatterDocumentRead,
    MatterOverviewCounts,
)
from app.workflows.dispatcher import cancel_matter_import, enqueue_matter_import, get_dbos_client
from artifact_service.auth import get_embedded_artifact_principal
from artifact_service.database import get_artifact_db
from artifact_service.schemas import CollectionItemRead

router = APIRouter(prefix="/v1", tags=["matter document imports"])
logger = logging.getLogger(__name__)
ACTIVE_IMPORT_STATUSES = {"QUEUED", "SNAPSHOTTING", "RUNNING"}
_DBOS_TERMINAL_STATUSES = {"SUCCESS", "ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED", "CANCELLED"}

def _matter(
    db: Session,
    matter_id: uuid.UUID,
    principal: Principal,
    permission_key: str,
) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can(db, principal, permission_key, ResourceRef.matter(matter.id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission required: {permission_key}")
    return matter


def _matter_document(
    db: Session,
    matter_id: uuid.UUID,
    document_id: uuid.UUID,
) -> MatterDocument:
    document = db.scalar(
        select(MatterDocument).where(
            MatterDocument.id == document_id,
            MatterDocument.matter_id == matter_id,
        )
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter document not found")
    return document


def _refresh_import_totals(
    db: Session,
    jobs: list[MatterDocumentImportJob],
) -> bool:
    if not jobs:
        return False
    rows = db.execute(
        select(
            MatterDocumentImportBatch.job_id,
            func.coalesce(func.sum(MatterDocumentImportBatch.item_count), 0),
            func.coalesce(func.sum(MatterDocumentImportBatch.added_count), 0),
            func.coalesce(func.sum(MatterDocumentImportBatch.duplicate_count), 0),
            func.coalesce(func.sum(MatterDocumentImportBatch.failed_count), 0),
            func.coalesce(func.sum(MatterDocumentImportBatch.imported_label_document_count), 0),
            func.coalesce(func.sum(MatterDocumentImportBatch.imported_label_event_count), 0),
            func.coalesce(func.sum(MatterDocumentImportBatch.imported_label_warning_count), 0),
        )
        .where(
            MatterDocumentImportBatch.job_id.in_([job.id for job in jobs]),
            MatterDocumentImportBatch.status == "COMPLETED",
        )
        .group_by(MatterDocumentImportBatch.job_id)
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
            job.processed_count,
            job.added_count,
            job.duplicate_count,
            job.failed_count,
            job.imported_label_document_count,
            job.imported_label_event_count,
            job.imported_label_warning_count,
        )
        if current == totals:
            continue
        (
            job.processed_count,
            job.added_count,
            job.duplicate_count,
            job.failed_count,
            job.imported_label_document_count,
            job.imported_label_event_count,
            job.imported_label_warning_count,
        ) = totals
        changed = True
    return changed


def _durable_terminal_imports(
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


def _synchronize_document_imports(
    db: Session,
    jobs: list[MatterDocumentImportJob],
    settings: Settings,
) -> None:
    changed = _refresh_import_totals(db, jobs)
    active_jobs = [job for job in jobs if job.status in ACTIVE_IMPORT_STATUSES]
    for job in active_jobs:
        execution = current_job_execution(
            db,
            job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
            job_id=job.id,
        )
        if execution is None or execution.status in {
            "COMPLETED",
            "COMPLETED_WITH_ERRORS",
            "FAILED",
            "CANCELED",
        }:
            continue
        try:
            transition_job_execution(
                db,
                job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
                job_id=job.id,
                status="QUEUED" if job.status == "QUEUED" else "RUNNING",
                current_stage={
                    "QUEUED": "QUEUED",
                    "SNAPSHOTTING": "FREEZING_SELECTION",
                    "RUNNING": "PROCESSING_BATCHES",
                }[job.status],
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
                heartbeat=False,
            )
            changed = True
        except JobExecutionConflict:
            logger.warning("Could not synchronize document import progress for %s", job.id, exc_info=True)
    if changed:
        db.commit()
    if not active_jobs:
        return
    try:
        terminal = _durable_terminal_imports(settings, [job.workflow_id for job in active_jobs])
    except Exception:
        logger.warning("Could not reconcile durable document import workflow statuses", exc_info=True)
        return
    changed = False
    for job in active_jobs:
        durable = terminal.get(job.workflow_id)
        if durable is None:
            continue
        durable_status, durable_error, durable_completed_at = durable
        try:
            if durable_status == "SUCCESS":
                execution = current_job_execution(
                    db,
                    job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
                    job_id=job.id,
                )
                if execution is not None and execution.status == "QUEUED":
                    transition_job_execution(
                        db,
                        job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
                        job_id=job.id,
                        status="RUNNING",
                        current_stage="RECONCILING",
                        progress_completed=job.processed_count,
                        progress_total=job.matched_count or None,
                    )
                execution = transition_job_execution(
                    db,
                    job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
                    job_id=job.id,
                    status="COMPLETED_WITH_ERRORS" if job.failed_count else "COMPLETED",
                    current_stage="COMPLETED_WITH_ERRORS" if job.failed_count else "COMPLETED",
                    progress_completed=job.matched_count,
                    progress_total=job.matched_count,
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
            elif durable_status == "CANCELLED":
                execution, _ = cancel_job_execution(
                    db,
                    job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
                    job_id=job.id,
                    actor_user_id=job.created_by_user_id,
                )
            else:
                execution = transition_job_execution(
                    db,
                    job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
                    job_id=job.id,
                    status="FAILED",
                    current_stage="FAILED",
                    progress_completed=job.processed_count,
                    progress_total=job.matched_count or None,
                    failure_code="MATTER_DOCUMENT_IMPORT_DURABLE_WORKFLOW_FAILED",
                    failure_summary=(
                        durable_error
                        or f"Durable document import workflow ended with status {durable_status} before finalization"
                    ),
                    failure_retriable=False,
                )
            if durable_completed_at is not None:
                execution.completed_at = durable_completed_at
                job.completed_at = durable_completed_at
                if execution.status == "CANCELED":
                    job.canceled_at = durable_completed_at
        except (JobExecutionConflict, JobExecutionNotFound):
            logger.warning("Could not reconcile common execution for document import %s", job.id, exc_info=True)
            continue
        changed = True
    if changed:
        db.commit()


@router.post(
    "/matters/{matter_id}/document-imports",
    response_model=MatterDocumentImportRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_document_import(
    matter_id: uuid.UUID,
    payload: MatterDocumentImportCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    artifact_db: Session = Depends(get_artifact_db),
) -> MatterDocumentImportJob:
    matter = _matter(db, matter_id, principal, "document.import")
    if matter.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter is not active")
    artifact_principal = get_embedded_artifact_principal(principal, db)
    if not artifact_principal.can_access_client(matter.client.tenant_id, matter.client_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client artifact access is required to add collection documents",
        )
    try:
        ensure_collection_scope(
            payload.source_collection_id,
            matter.client.tenant_id,
            matter.client_id,
            principal.user.id,
            artifact_db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    job_id = uuid.uuid4()
    job = MatterDocumentImportJob(
        id=job_id,
        matter_id=matter.id,
        source_collection_id=payload.source_collection_id,
        selection_type=payload.selection.mode,
        selection=payload.selection.model_dump(mode="json"),
        selection_summary=payload.selection_summary,
        status="QUEUED",
        workflow_id=f"matter-import:{job_id}",
        created_by_user_id=principal.user.id,
    )
    db.add(job)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter.document_import.created",
        target_type="matter_document_import_job",
        target_id=job.id,
        details={
            "matter_id": str(matter.id),
            "source_collection_id": str(job.source_collection_id),
            "selection_type": job.selection_type,
        },
    )
    try:
        db.flush()
        create_job_execution(
            db,
            job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
            job_id=job.id,
            scope=JobScope(
                tenant_id=matter.client.tenant_id,
                client_id=matter.client_id,
                matter_id=matter.id,
            ),
            workflow_key="matter_document_import",
            code_version="1",
            dbos_workflow_id=job.workflow_id,
            initiated_by_user_id=principal.user.id,
            idempotency_key=f"matter-document-import:{job.id}:attempt:1",
            progress_unit="documents",
            input_snapshot={
                "matter_id": str(matter.id),
                "source_collection_id": str(job.source_collection_id),
                "selection_type": job.selection_type,
                "selection": job.selection,
                "selection_summary": job.selection_summary,
            },
        )
        enqueue_matter_import(db, job.workflow_id, str(job.id))
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The document import workflow could not be queued",
        ) from exc
    db.refresh(job)
    return job


@router.get("/matters/{matter_id}/document-imports", response_model=list[MatterDocumentImportRead])
def list_document_imports(
    matter_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[MatterDocumentImportJob]:
    _matter(db, matter_id, principal, "job.view")
    jobs = list(
        db.scalars(
            select(MatterDocumentImportJob)
            .where(
                MatterDocumentImportJob.matter_id == matter_id,
                job_not_archived(MATTER_DOCUMENT_IMPORT_JOB_TYPE, MatterDocumentImportJob.id),
            )
            .order_by(MatterDocumentImportJob.created_at.desc())
            .limit(limit)
        )
    )
    _synchronize_document_imports(db, jobs, settings)
    return jobs


@router.get("/matters/{matter_id}/document-imports/{job_id}", response_model=MatterDocumentImportRead)
def get_document_import(
    matter_id: uuid.UUID,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterDocumentImportJob:
    _matter(db, matter_id, principal, "job.view")
    job = db.scalar(
        select(MatterDocumentImportJob).where(
            MatterDocumentImportJob.id == job_id,
            MatterDocumentImportJob.matter_id == matter_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document import job not found")
    _synchronize_document_imports(db, [job], settings)
    return job


@router.post(
    "/matters/{matter_id}/document-imports/{job_id}/cancel",
    response_model=MatterDocumentImportRead,
)
def cancel_document_import(
    matter_id: uuid.UUID,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDocumentImportJob:
    matter = _matter(db, matter_id, principal, "job.cancel")
    job = db.scalar(
        select(MatterDocumentImportJob).where(
            MatterDocumentImportJob.id == job_id,
            MatterDocumentImportJob.matter_id == matter_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document import job not found")
    if job.status in {"COMPLETED", "FAILED", "CANCELED"}:
        return job
    execution = current_job_execution(
        db,
        job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
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
            job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
            job_id=job.id,
            status="QUEUED" if job.status == "QUEUED" else "RUNNING",
            current_stage=(
                "FREEZING_SELECTION" if job.status == "SNAPSHOTTING" else "PROCESSING_BATCHES"
            ),
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
    cancel_job_execution(
        db,
        job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
        job_id=job.id,
        actor_user_id=principal.user.id,
    )
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter.document_import.canceled",
        target_type="matter_document_import_job",
        target_id=job.id,
    )
    db.commit()
    if job.artifact_selection_id is not None:
        try:
            remove_selection(
                selection_id=job.artifact_selection_id,
                actor_user_id=job.created_by_user_id,
                tenant_id=matter.client.tenant_id,
                client_id=matter.client_id,
            )
            job.artifact_selection_id = None
            db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Artifact selection cleanup failed for %s: %s", job.workflow_id, exc)
    try:
        cancel_matter_import(job.workflow_id)
    except Exception as exc:  # noqa: BLE001
        # The Core record is authoritative for cancellation; a worker checks it
        # before writing any new matter-document links.
        logger.warning("DBOS cancellation request failed for %s: %s", job.workflow_id, exc)
    db.refresh(job)
    return job


@router.get("/matters/{matter_id}/documents", response_model=list[MatterDocumentRead])
def list_matter_documents(
    matter_id: uuid.UUID,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterDocument]:
    _matter(db, matter_id, principal, "document.view")
    return list(
        db.scalars(
            select(MatterDocument)
            .where(MatterDocument.matter_id == matter_id)
            .order_by(MatterDocument.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    )


@router.get("/matters/{matter_id}/documents/count")
def count_matter_documents(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    _matter(db, matter_id, principal, "document.view")
    count = db.scalar(select(func.count()).select_from(MatterDocument).where(MatterDocument.matter_id == matter_id))
    return {"count": count or 0}


@router.get(
    "/matters/{matter_id}/documents/{document_id}/collection-item",
    response_model=CollectionItemRead,
)
def get_matter_document_collection_item(
    matter_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> CollectionItemRead:
    matter = _matter(db, matter_id, principal, "document.view")
    document = _matter_document(db, matter.id, document_id)
    try:
        return get_collection_item_read(
            collection_item_id=document.collection_item_id,
            actor_user_id=principal.user.id,
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


@router.get("/matters/{matter_id}/documents/{document_id}/content")
def get_matter_document_content(
    matter_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Response:
    matter = _matter(db, matter_id, principal, "document.view")
    document = _matter_document(db, matter.id, document_id)
    try:
        item = get_collection_item_read(
            collection_item_id=document.collection_item_id,
            actor_user_id=principal.user.id,
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
        )
        content = read_artifact_bytes(
            artifact_id=item.native_artifact.id,
            actor_user_id=principal.user.id,
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return Response(content=content, media_type=item.native_artifact.media_type)


@router.get("/matters/{matter_id}/overview-counts", response_model=MatterOverviewCounts)
def matter_overview_counts(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterOverviewCounts:
    _matter(db, matter_id, principal, "document.view")
    document_count = db.scalar(
        select(func.count()).select_from(MatterDocument).where(MatterDocument.matter_id == matter_id)
    )
    custodian_count = db.scalar(
        select(func.count(func.distinct(MatterDocumentCustodian.custodian_id)))
        .select_from(MatterDocumentCustodian)
        .join(MatterDocument, MatterDocument.id == MatterDocumentCustodian.matter_document_id)
        .where(MatterDocument.matter_id == matter_id)
    )
    return MatterOverviewCounts(
        document_count=document_count or 0,
        custodian_count=custodian_count or 0,
    )
