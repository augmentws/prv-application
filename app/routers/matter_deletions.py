import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifact_gateway import count_workflow_artifacts_in_scopes
from app.audit import record_audit
from app.database import get_db
from app.dependencies import Principal, can_admin_client, get_principal
from app.matter_deletions import (
    ACTIVE_DELETION_STATUSES,
    MatterDeletionValidationUnavailable,
    create_matter_deletion_job,
    fail_matter_deletion,
    matter_artifact_scopes,
    matter_deletion_blockers,
    retry_matter_deletion_job,
)
from app.models import Client, Matter, MatterDeletionJob
from app.schemas import MatterDeletionJobRead
from app.workflows.dispatcher import enqueue_matter_deletion
from artifact_service.database import get_artifact_db

router = APIRouter(prefix="/v1", tags=["matter deletions"])


def _authorized_job(
    db: Session,
    principal: Principal,
    job_id: uuid.UUID,
) -> MatterDeletionJob:
    job = db.get(MatterDeletionJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter deletion job not found")
    client = db.get(Client, job.client_id)
    if client is None or client.tenant_id != job.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    if not can_admin_client(db, principal, client):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client ADMIN required")
    return job


@router.delete(
    "/matters/{matter_id}",
    response_model=MatterDeletionJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def delete_matter(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    artifact_db: Session = Depends(get_artifact_db),
) -> MatterDeletionJob:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_client(db, principal, matter.client):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client ADMIN required")
    existing = db.scalar(
        select(MatterDeletionJob)
        .where(
            MatterDeletionJob.matter_id == matter.id,
            MatterDeletionJob.status.in_(ACTIVE_DELETION_STATUSES),
        )
        .order_by(MatterDeletionJob.created_at.desc())
    )
    if existing is not None:
        return existing
    try:
        blockers = matter_deletion_blockers(db, matter.id)
    except MatterDeletionValidationUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    if blockers.blocked:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=blockers.message())
    scopes = matter_artifact_scopes(db, matter.id)
    artifact_count = count_workflow_artifacts_in_scopes(
        actor_user_id=principal.user.id,
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        scopes=scopes,
        artifact_db=artifact_db,
    )
    try:
        job = create_matter_deletion_job(
            db,
            matter,
            requested_by_user_id=principal.user.id,
            artifact_count=artifact_count,
            scopes=scopes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=job.tenant_id,
        actor_user_id=principal.user.id,
        action="matter.deletion_requested",
        target_type="matter_deletion_job",
        target_id=job.id,
        details={
            "matter_id": str(job.matter_id),
            "matter_name": job.matter_name,
            "document_count": job.document_count,
            "artifact_count": job.artifact_count,
        },
    )
    db.commit()
    try:
        enqueue_matter_deletion(job.workflow_id, str(job.id))
    except Exception as exc:
        fail_matter_deletion(db, job.id, f"Unable to enqueue deletion: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The matter deletion workflow could not be queued",
        ) from exc
    return job


@router.get("/matters/{matter_id}/deletion", response_model=MatterDeletionJobRead)
def get_matter_deletion(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDeletionJob:
    job = db.scalar(
        select(MatterDeletionJob)
        .where(MatterDeletionJob.matter_id == matter_id)
        .order_by(MatterDeletionJob.created_at.desc())
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter deletion job not found")
    return _authorized_job(db, principal, job.id)


@router.get("/matter-deletions/{job_id}", response_model=MatterDeletionJobRead)
def get_matter_deletion_job(
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDeletionJob:
    return _authorized_job(db, principal, job_id)


@router.post(
    "/matter-deletions/{job_id}/retry",
    response_model=MatterDeletionJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_matter_deletion(
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDeletionJob:
    job = _authorized_job(db, principal, job_id)
    if job.status != "FAILED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only failed deletions can be retried")
    try:
        blockers = matter_deletion_blockers(db, job.matter_id)
    except MatterDeletionValidationUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    if blockers.blocked:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=blockers.message())
    job = retry_matter_deletion_job(db, job.id)
    record_audit(
        db,
        tenant_id=job.tenant_id,
        actor_user_id=principal.user.id,
        action="matter.deletion_retried",
        target_type="matter_deletion_job",
        target_id=job.id,
        details={"matter_id": str(job.matter_id), "attempt_count": job.attempt_count},
    )
    db.commit()
    try:
        enqueue_matter_deletion(job.workflow_id, str(job.id))
    except Exception as exc:
        fail_matter_deletion(db, job.id, f"Unable to enqueue deletion retry: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The matter deletion workflow could not be queued",
        ) from exc
    return job
