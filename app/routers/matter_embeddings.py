import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.embeddings.configuration import canonical_hash, processing_configuration
from app.models import Matter, MatterEmbeddingBatch, MatterEmbeddingJob
from app.schemas import MatterEmbeddingJobRead
from app.workflows.dispatcher import cancel_matter_embedding, enqueue_matter_embedding
from embedding_service.config import EmbeddingSettings, get_embedding_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/matters/{matter_id}/embedding-jobs", tags=["matter jobs"])
ACTIVE_STATUSES = {"QUEUED", "PLANNING", "RUNNING"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _matter(db: Session, matter_id: uuid.UUID, principal: Principal) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    return matter


@router.post("", response_model=MatterEmbeddingJobRead, status_code=status.HTTP_202_ACCEPTED)
def create_embedding_job(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    embedding_settings: EmbeddingSettings = Depends(get_embedding_settings),
) -> MatterEmbeddingJob:
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
    configuration = processing_configuration(settings, embedding_settings)
    job_id = uuid.uuid4()
    job = MatterEmbeddingJob(
        id=job_id,
        matter_id=matter.id,
        status="QUEUED",
        workflow_id=f"matter-embedding:{job_id}",
        configuration_hash=canonical_hash(configuration),
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
    return job


@router.get("", response_model=list[MatterEmbeddingJobRead])
def list_embedding_jobs(
    matter_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterEmbeddingJob]:
    _matter(db, matter_id, principal)
    return list(
        db.scalars(
            select(MatterEmbeddingJob)
            .where(MatterEmbeddingJob.matter_id == matter_id)
            .order_by(MatterEmbeddingJob.created_at.desc())
            .limit(limit)
        )
    )


@router.get("/{job_id}", response_model=MatterEmbeddingJobRead)
def get_embedding_job(
    matter_id: uuid.UUID,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEmbeddingJob:
    _matter(db, matter_id, principal)
    job = db.scalar(
        select(MatterEmbeddingJob).where(
            MatterEmbeddingJob.id == job_id,
            MatterEmbeddingJob.matter_id == matter_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Embedding job not found")
    return job


@router.post("/{job_id}/cancel", response_model=MatterEmbeddingJobRead)
def cancel_embedding_job(
    matter_id: uuid.UUID,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterEmbeddingJob:
    matter = _matter(db, matter_id, principal)
    job = db.scalar(
        select(MatterEmbeddingJob).where(
            MatterEmbeddingJob.id == job_id,
            MatterEmbeddingJob.matter_id == matter_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Embedding job not found")
    if job.status not in ACTIVE_STATUSES:
        return job
    job.status = "CANCELED"
    job.canceled_at = utcnow()
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
    db.refresh(job)
    return job
