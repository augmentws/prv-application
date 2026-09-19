import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.artifact_gateway import (
    create_collection_deletion,
    fail_collection_deletion_job,
    get_collection_snapshot,
    get_latest_collection_deletion,
)
from app.artifact_gateway import (
    get_collection_deletion_job as load_collection_deletion_job,
)
from app.artifact_gateway import (
    retry_collection_deletion as retry_artifact_collection_deletion,
)
from app.audit import record_audit
from app.collection_deletions import collection_deletion_blockers
from app.database import get_db
from app.dependencies import Principal, can_admin_client, get_principal
from app.models import Client
from app.workflows.dispatcher import enqueue_collection_deletion
from artifact_service.auth import ArtifactPrincipal, get_embedded_artifact_principal
from artifact_service.database import get_artifact_db
from artifact_service.deletion import ACTIVE_DELETION_STATUSES
from artifact_service.schemas import CollectionDeletionJobRead

router = APIRouter(prefix="/v1", tags=["collection deletions"])


def _authorized_job(
    artifact_db: Session,
    db: Session,
    principal: Principal,
    artifact_principal: ArtifactPrincipal,
    job_id: uuid.UUID,
) -> CollectionDeletionJobRead:
    try:
        job = load_collection_deletion_job(job_id, artifact_principal, artifact_db)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection deletion job not found")
    client = db.get(Client, job.client_id)
    if client is None or client.tenant_id != job.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    if not can_admin_client(db, principal, client):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client ADMIN required")
    return job


@router.delete(
    "/collections/{collection_id}",
    response_model=CollectionDeletionJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def delete_collection(
    collection_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    artifact_principal: ArtifactPrincipal = Depends(get_embedded_artifact_principal),
    db: Session = Depends(get_db),
    artifact_db: Session = Depends(get_artifact_db),
) -> CollectionDeletionJobRead:
    try:
        collection = get_collection_snapshot(collection_id, artifact_principal, artifact_db)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    client = db.get(Client, collection.client_id)
    if client is None or client.tenant_id != collection.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    if not can_admin_client(db, principal, client):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client ADMIN required")

    latest_job = get_latest_collection_deletion(collection.id, artifact_principal, artifact_db)
    if latest_job is not None and latest_job.status in ACTIVE_DELETION_STATUSES:
        return latest_job

    blockers = collection_deletion_blockers(db, collection.id)
    if blockers.blocked:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=blockers.message())
    try:
        job = create_collection_deletion(collection.id, artifact_principal, artifact_db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if job.status not in ACTIVE_DELETION_STATUSES:
        return job
    record_audit(
        db,
        tenant_id=job.tenant_id,
        actor_user_id=principal.user.id,
        action="collection.deletion_requested",
        target_type="collection_deletion_job",
        target_id=job.id,
        details={
            "collection_id": str(job.collection_id),
            "collection_name": job.collection_name,
            "item_count": job.item_count,
            "artifact_count": job.artifact_count,
        },
    )
    db.commit()
    try:
        enqueue_collection_deletion(job.workflow_id, str(job.id))
    except Exception as exc:
        fail_collection_deletion_job(
            job.id,
            f"Unable to enqueue deletion: {exc}",
            artifact_principal,
            artifact_db,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The collection deletion workflow could not be queued",
        ) from exc
    return job


@router.get(
    "/collections/{collection_id}/deletion",
    response_model=CollectionDeletionJobRead,
)
def get_collection_deletion(
    collection_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    artifact_principal: ArtifactPrincipal = Depends(get_embedded_artifact_principal),
    db: Session = Depends(get_db),
    artifact_db: Session = Depends(get_artifact_db),
) -> CollectionDeletionJobRead:
    job = get_latest_collection_deletion(collection_id, artifact_principal, artifact_db)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection deletion job not found")
    return _authorized_job(artifact_db, db, principal, artifact_principal, job.id)


@router.get(
    "/collection-deletions/{job_id}",
    response_model=CollectionDeletionJobRead,
)
def get_collection_deletion_job(
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    artifact_principal: ArtifactPrincipal = Depends(get_embedded_artifact_principal),
    db: Session = Depends(get_db),
    artifact_db: Session = Depends(get_artifact_db),
) -> CollectionDeletionJobRead:
    return _authorized_job(artifact_db, db, principal, artifact_principal, job_id)


@router.post(
    "/collection-deletions/{job_id}/retry",
    response_model=CollectionDeletionJobRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_collection_deletion(
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    artifact_principal: ArtifactPrincipal = Depends(get_embedded_artifact_principal),
    db: Session = Depends(get_db),
    artifact_db: Session = Depends(get_artifact_db),
) -> CollectionDeletionJobRead:
    job = _authorized_job(artifact_db, db, principal, artifact_principal, job_id)
    if job.status != "FAILED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only failed deletions can be retried")
    blockers = collection_deletion_blockers(db, job.collection_id)
    if blockers.blocked:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=blockers.message())

    try:
        job = retry_artifact_collection_deletion(job.id, artifact_principal, artifact_db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=job.tenant_id,
        actor_user_id=principal.user.id,
        action="collection.deletion_retried",
        target_type="collection_deletion_job",
        target_id=job.id,
        details={"collection_id": str(job.collection_id), "attempt_count": job.attempt_count},
    )
    db.commit()
    try:
        enqueue_collection_deletion(job.workflow_id, str(job.id))
    except Exception as exc:
        fail_collection_deletion_job(
            job.id,
            f"Unable to enqueue deletion retry: {exc}",
            artifact_principal,
            artifact_db,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The collection deletion workflow could not be queued",
        ) from exc
    return job
