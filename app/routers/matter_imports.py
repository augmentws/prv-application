import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.artifact_gateway import ensure_collection_scope, remove_selection
from app.audit import record_audit
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.models import Matter, MatterDocument, MatterDocumentCustodian, MatterDocumentImportJob
from app.schemas import (
    MatterDocumentImportCreate,
    MatterDocumentImportRead,
    MatterDocumentRead,
    MatterOverviewCounts,
)
from app.workflows.dispatcher import cancel_matter_import, enqueue_matter_import
from artifact_service.auth import get_embedded_artifact_principal
from artifact_service.database import get_artifact_db

router = APIRouter(prefix="/v1", tags=["matter document imports"])
logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _matter(db: Session, matter_id: uuid.UUID, principal: Principal) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    return matter


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
    matter = _matter(db, matter_id, principal)
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
) -> list[MatterDocumentImportJob]:
    _matter(db, matter_id, principal)
    return list(
        db.scalars(
            select(MatterDocumentImportJob)
            .where(MatterDocumentImportJob.matter_id == matter_id)
            .order_by(MatterDocumentImportJob.created_at.desc())
            .limit(limit)
        )
    )


@router.get("/matters/{matter_id}/document-imports/{job_id}", response_model=MatterDocumentImportRead)
def get_document_import(
    matter_id: uuid.UUID,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDocumentImportJob:
    _matter(db, matter_id, principal)
    job = db.scalar(
        select(MatterDocumentImportJob).where(
            MatterDocumentImportJob.id == job_id,
            MatterDocumentImportJob.matter_id == matter_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document import job not found")
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
    matter = _matter(db, matter_id, principal)
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
    job.status = "CANCELED"
    job.canceled_at = utcnow()
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
    _matter(db, matter_id, principal)
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
    _matter(db, matter_id, principal)
    count = db.scalar(select(func.count()).select_from(MatterDocument).where(MatterDocument.matter_id == matter_id))
    return {"count": count or 0}


@router.get("/matters/{matter_id}/overview-counts", response_model=MatterOverviewCounts)
def matter_overview_counts(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterOverviewCounts:
    _matter(db, matter_id, principal)
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
