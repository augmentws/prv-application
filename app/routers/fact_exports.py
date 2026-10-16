import uuid
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifact_gateway import get_workflow_artifact_reference, iter_workflow_artifact_content
from app.audit import record_audit
from app.authorization import ResourceRef, can
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.fact_exports import as_utc, create_fact_export_job
from app.fact_queries import FactFilterError
from app.job_archives import job_not_archived
from app.job_executions import MATTER_FACT_EXPORT_JOB_TYPE, JobExecutionConflict
from app.models import Matter, MatterFactExportJob
from app.schemas import MatterFactExportCreate, MatterFactExportRead
from app.workflows.dispatcher import enqueue_fact_export
from classification_service.schemas import ArtifactType

router = APIRouter(prefix="/v1/matters/{matter_id}/fact-exports", tags=["matter fact exports"])
EXPORT_PERMISSIONS = ("fact.export", "fact.view", "document.view")


def _export_matter(db: Session, matter_id: uuid.UUID, principal: Principal) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    for permission_key in EXPORT_PERMISSIONS:
        if not can(db, principal, permission_key, ResourceRef.matter(matter.id)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission required: {permission_key}",
            )
    return matter


def _export_or_404(db: Session, matter_id: uuid.UUID, export_id: uuid.UUID) -> MatterFactExportJob:
    job = db.scalar(
        select(MatterFactExportJob).where(
            MatterFactExportJob.id == export_id,
            MatterFactExportJob.matter_id == matter_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fact export not found")
    return job


@router.post("", response_model=MatterFactExportRead, status_code=status.HTTP_202_ACCEPTED)
def create_fact_export(
    matter_id: uuid.UUID,
    payload: MatterFactExportCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactExportJob:
    matter = _export_matter(db, matter_id, principal)
    try:
        job, created = create_fact_export_job(
            db,
            matter,
            filters=payload.filters,
            requested_filename=payload.requested_filename,
            idempotency_key=payload.idempotency_key,
            spreadsheet_safe=payload.spreadsheet_safe,
            actor_user_id=principal.user.id,
        )
    except FactFilterError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except JobExecutionConflict as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if created:
        enqueue_fact_export(db, job.workflow_id, str(job.id))
    db.commit()
    db.refresh(job)
    return job


@router.get("", response_model=list[MatterFactExportRead])
def list_fact_exports(
    matter_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterFactExportJob]:
    matter = _export_matter(db, matter_id, principal)
    return list(
        db.scalars(
            select(MatterFactExportJob)
            .where(
                MatterFactExportJob.matter_id == matter.id,
                job_not_archived(MATTER_FACT_EXPORT_JOB_TYPE, MatterFactExportJob.id),
            )
            .order_by(MatterFactExportJob.created_at.desc(), MatterFactExportJob.id.desc())
            .limit(limit)
        )
    )


@router.get("/{export_id}", response_model=MatterFactExportRead)
def get_fact_export(
    matter_id: uuid.UUID,
    export_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterFactExportJob:
    _export_matter(db, matter_id, principal)
    return _export_or_404(db, matter_id, export_id)


@router.get("/{export_id}/download")
def download_fact_export(
    matter_id: uuid.UUID,
    export_id: uuid.UUID,
    file: Literal["facts", "evidence"] = Query(default="facts"),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Stream one of the export's two CSV files through Core after fresh authorization."""

    matter = _export_matter(db, matter_id, principal)
    job = _export_or_404(db, matter_id, export_id)
    artifact_id = job.facts_artifact_id if file == "facts" else job.evidence_artifact_id
    content_hash = job.facts_content_hash if file == "facts" else job.evidence_content_hash
    byte_length = job.facts_byte_length if file == "facts" else job.evidence_byte_length
    filename = job.facts_filename if file == "facts" else job.evidence_filename
    if job.status != "COMPLETED" or artifact_id is None or filename is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Fact export is not ready")
    if job.expires_at is not None and as_utc(job.expires_at) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Fact export has expired")
    try:
        artifact = get_workflow_artifact_reference(
            artifact_id=artifact_id,
            actor_user_id=principal.user.id,
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
        )
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Fact export file is unavailable") from exc
    expected_kind = "FACTS" if file == "facts" else "EVIDENCE"
    if not (
        artifact.artifact_type == ArtifactType.FACT_EXPORT
        and artifact.core_scope_kind == "MATTER"
        and artifact.core_scope_id == matter.id
        and artifact.producer_run_id == job.id
        and artifact.content_hash == content_hash
        and artifact.byte_length == byte_length
        and str(artifact.metadata.get("export_job_id")) == str(job.id)
        and artifact.metadata.get("file_kind") == expected_kind
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Fact export file provenance is invalid")
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="fact_export.downloaded",
        target_type="matter_fact_export_job",
        target_id=job.id,
        details={"matter_id": str(matter.id), "file": file, "artifact_id": str(artifact_id), "content_hash": content_hash},
    )
    db.commit()
    return StreamingResponse(
        iter_workflow_artifact_content(
            artifact_id=artifact_id,
            actor_user_id=principal.user.id,
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
        ),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Content-Length": str(byte_length or 0),
            "ETag": content_hash or "",
        },
    )
