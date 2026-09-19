import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.audit import record_audit
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.embeddings.configuration import canonical_hash
from app.models import Matter, MatterEmbeddingJob, MatterTopicJob
from app.schemas import MatterTopicApplyRequest, MatterTopicJobCreate, MatterTopicJobRead
from app.topic_clustering import prepare_application
from app.workflows.dispatcher import (
    cancel_matter_topic_application,
    cancel_matter_topics,
    enqueue_matter_topic_application,
    enqueue_matter_topics,
)

router = APIRouter(prefix="/v1/matters/{matter_id}/topic-jobs", tags=["matter jobs"])
ACTIVE_STATUSES = {"QUEUED", "SAMPLING", "CLUSTERING", "AWAITING_REVIEW", "PUBLISHING"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _matter(db: Session, matter_id: uuid.UUID, principal: Principal) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    return matter


def _job_query(matter_id: uuid.UUID):
    return (
        select(MatterTopicJob)
        .options(selectinload(MatterTopicJob.clusters))
        .where(MatterTopicJob.matter_id == matter_id)
    )


@router.post("", response_model=MatterTopicJobRead, status_code=status.HTTP_202_ACCEPTED)
def create_topic_job(
    matter_id: uuid.UUID,
    payload: MatterTopicJobCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterTopicJob:
    matter = _matter(db, matter_id, principal)
    if matter.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter is not active")
    if db.scalar(
        select(MatterTopicJob).where(
            MatterTopicJob.matter_id == matter.id,
            MatterTopicJob.status.in_(ACTIVE_STATUSES),
        )
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A topic job is already active for this matter")
    embedding_job = db.scalar(
        select(MatterEmbeddingJob)
        .where(
            MatterEmbeddingJob.matter_id == matter.id,
            MatterEmbeddingJob.status.in_(("COMPLETED", "COMPLETED_WITH_ERRORS")),
            MatterEmbeddingJob.chunk_count > 0,
        )
        .order_by(MatterEmbeddingJob.completed_at.desc(), MatterEmbeddingJob.created_at.desc())
    )
    if embedding_job is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Generate document embeddings before starting topic clustering",
        )

    configuration = {
        "schema_version": 3,
        "clustering_version": 3,
        "operating_mode": payload.operating_mode,
        "sample_size": payload.sample_size,
        "requested_topic_count": payload.requested_topic_count,
        "assignment_mode": payload.assignment_mode,
        "max_topics": 50,
        "max_topics_per_document": 3,
        "minimum_assignment_confidence": 0.35,
        "random_seed": 42,
        "dimensionality_reduction": {
            "pca_dimensions": 50,
        },
        "automatic_topic_candidates": [10, 20, 30, 40, 50],
        "automatic_evaluation_sample_size": 2000,
        "embedding_configuration_hash": embedding_job.configuration_hash,
        "embedding_dimensions": embedding_job.embedding_dimensions,
    }
    job_id = uuid.uuid4()
    job = MatterTopicJob(
        id=job_id,
        matter_id=matter.id,
        embedding_job_id=embedding_job.id,
        status="QUEUED",
        workflow_id=f"matter-topics:{job_id}",
        operating_mode=payload.operating_mode,
        sample_size=payload.sample_size,
        requested_topic_count=payload.requested_topic_count,
        assignment_mode=payload.assignment_mode,
        configuration_hash=canonical_hash(configuration),
        configuration=configuration,
        created_by_user_id=principal.user.id,
    )
    db.add(job)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter.topic_job.created",
        target_type="matter_topic_job",
        target_id=job.id,
        details={
            "matter_id": str(matter.id),
            "operating_mode": job.operating_mode,
            "sample_size": job.sample_size,
            "requested_topic_count": job.requested_topic_count,
            "assignment_mode": job.assignment_mode,
            "embedding_job_id": str(job.embedding_job_id),
        },
    )
    try:
        db.flush()
        enqueue_matter_topics(db, job.workflow_id, str(job.id))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A topic job is already active for this matter") from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The topic job could not be queued",
        ) from exc
    return db.scalar(_job_query(matter.id).where(MatterTopicJob.id == job.id))


@router.get("", response_model=list[MatterTopicJobRead])
def list_topic_jobs(
    matter_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterTopicJob]:
    _matter(db, matter_id, principal)
    return list(db.scalars(_job_query(matter_id).order_by(MatterTopicJob.created_at.desc()).limit(limit)))


@router.get("/{job_id}", response_model=MatterTopicJobRead)
def get_topic_job(
    matter_id: uuid.UUID,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterTopicJob:
    _matter(db, matter_id, principal)
    job = db.scalar(_job_query(matter_id).where(MatterTopicJob.id == job_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topic job not found")
    return job


@router.post("/{job_id}/apply", response_model=MatterTopicJobRead, status_code=status.HTTP_202_ACCEPTED)
def apply_topic_job(
    matter_id: uuid.UUID,
    job_id: uuid.UUID,
    payload: MatterTopicApplyRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterTopicJob:
    matter = _matter(db, matter_id, principal)
    job = db.scalar(
        _job_query(matter_id).where(MatterTopicJob.id == job_id).with_for_update()
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topic job not found")
    if job.status != "AWAITING_REVIEW":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Topic proposals are not awaiting review")

    proposals_by_id = {proposal.id: proposal for proposal in payload.topics}
    cluster_ids = {cluster.id for cluster in job.clusters}
    if len(proposals_by_id) != len(payload.topics) or set(proposals_by_id) != cluster_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Submit each current topic proposal exactly once",
        )
    if not any(proposal.included for proposal in payload.topics):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Include at least one topic before applying",
        )

    for cluster in job.clusters:
        proposal = proposals_by_id[cluster.id]
        cluster.name = proposal.name.strip()
        cluster.description = proposal.description.strip() if proposal.description else None
        cluster.included = proposal.included
    prepare_application(db, job, reviewer_user_id=principal.user.id)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter.topic_job.approved",
        target_type="matter_topic_job",
        target_id=job.id,
        details={
            "matter_id": str(matter.id),
            "included_topic_count": sum(proposal.included for proposal in payload.topics),
            "excluded_topic_count": sum(not proposal.included for proposal in payload.topics),
        },
    )
    try:
        db.flush()
        enqueue_matter_topic_application(db, str(job.id))
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The approved topics could not be queued for application",
        ) from exc
    return db.scalar(_job_query(matter.id).where(MatterTopicJob.id == job.id))


@router.post("/{job_id}/cancel", response_model=MatterTopicJobRead)
def cancel_topic_job(
    matter_id: uuid.UUID,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterTopicJob:
    matter = _matter(db, matter_id, principal)
    job = db.scalar(_job_query(matter_id).where(MatterTopicJob.id == job_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Topic job not found")
    if job.status not in ACTIVE_STATUSES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Topic job is not active")
    previous_status = job.status
    job.status = "CANCELED"
    job.canceled_at = utcnow()
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter.topic_job.canceled",
        target_type="matter_topic_job",
        target_id=job.id,
        details={"matter_id": str(matter.id)},
    )
    db.commit()
    if previous_status in {"QUEUED", "SAMPLING", "CLUSTERING"}:
        cancel_matter_topics(job.workflow_id)
    elif previous_status == "PUBLISHING":
        cancel_matter_topic_application(str(job.id))
    return job
