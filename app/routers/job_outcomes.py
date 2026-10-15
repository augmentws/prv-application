"""Matter-authorized access to Artifact-backed per-item job outcomes."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.job_outcomes import (
    JobOutcomeError,
    get_job_outcome_adapter,
    iter_job_outcome_artifact,
    iter_job_outcomes_csv,
    page_job_outcomes,
)
from app.models import JobOutcomeManifest, MatterEmbeddingJob
from app.schemas import JobOutcomeItemPage, JobOutcomeManifestRead

router = APIRouter(prefix="/v1/matters/{matter_id}/job-outcomes", tags=["matter jobs"])


def _authorized(
    db: Session,
    principal: Principal,
    *,
    matter_id: uuid.UUID,
    job_type: str,
    job_id: uuid.UUID,
) -> tuple[MatterEmbeddingJob, JobOutcomeManifest | None]:
    try:
        adapter = get_job_outcome_adapter(job_type)
    except JobOutcomeError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    job = adapter.load(db, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    matter = adapter.matter(db, job)
    if matter is None or matter.id != matter_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    manifest = db.scalar(
        select(JobOutcomeManifest).where(
            JobOutcomeManifest.job_type == adapter.job_type,
            JobOutcomeManifest.job_id == job.id,
        )
    )
    if manifest is not None and (
        manifest.tenant_id != matter.client.tenant_id
        or manifest.client_id != matter.client_id
        or manifest.matter_id != matter.id
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job outcome scope does not match the job")
    return job, manifest


def _summary(job: MatterEmbeddingJob, manifest: JobOutcomeManifest | None) -> JobOutcomeManifestRead:
    if manifest is None:
        status_counts = {
            key: value
            for key, value in {
                "SUCCEEDED": job.embedded_count,
                "REUSED": job.reused_count,
                "SKIPPED": job.skipped_count,
                "FAILED": job.failed_count,
            }.items()
            if value
        }
        return JobOutcomeManifestRead(
            id=None,
            job_type="MATTER_EMBEDDING_JOB",
            job_id=job.id,
            manifest_status="UNAVAILABLE",
            outcome_schema="priv-view.job-item-outcomes",
            outcome_schema_version=1,
            item_count=job.total_count,
            status_counts=status_counts,
            reason_counts={},
            artifact_id=None,
            artifact_content_hash=None,
            artifact_byte_length=None,
            finalized_at=None,
            failure_summary="This historical job predates complete per-document outcome retention.",
            item_details_available=False,
            legacy=True,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )
    return JobOutcomeManifestRead(
        **{
            field: getattr(manifest, field)
            for field in (
                "id",
                "job_type",
                "job_id",
                "manifest_status",
                "outcome_schema",
                "outcome_schema_version",
                "item_count",
                "status_counts",
                "reason_counts",
                "artifact_id",
                "artifact_content_hash",
                "artifact_byte_length",
                "finalized_at",
                "failure_summary",
                "created_at",
                "updated_at",
            )
        },
        item_details_available=manifest.manifest_status == "FINALIZED" and manifest.artifact_id is not None,
        legacy=False,
    )


@router.get("/{job_type}/{job_id}", response_model=JobOutcomeManifestRead)
def get_job_outcome_summary(
    matter_id: uuid.UUID,
    job_type: str,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> JobOutcomeManifestRead:
    job, manifest = _authorized(
        db,
        principal,
        matter_id=matter_id,
        job_type=job_type,
        job_id=job_id,
    )
    return _summary(job, manifest)


@router.get("/{job_type}/{job_id}/items", response_model=JobOutcomeItemPage)
def list_job_outcome_items(
    matter_id: uuid.UUID,
    job_type: str,
    job_id: uuid.UUID,
    outcome_status: str | None = Query(default=None, alias="status"),
    reason_code: str | None = Query(default=None, min_length=1, max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> JobOutcomeItemPage:
    _, manifest = _authorized(
        db,
        principal,
        matter_id=matter_id,
        job_type=job_type,
        job_id=job_id,
    )
    if manifest is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Item details are unavailable for this job")
    try:
        total, rows = page_job_outcomes(
            manifest,
            actor_user_id=principal.user.id,
            status=outcome_status,
            reason_code=reason_code,
            offset=offset,
            limit=limit,
        )
    except JobOutcomeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return JobOutcomeItemPage(total=total, offset=offset, limit=limit, items=rows)


@router.get("/{job_type}/{job_id}/download")
def download_job_outcomes(
    matter_id: uuid.UUID,
    job_type: str,
    job_id: uuid.UUID,
    format: Literal["csv", "parquet"] = Query(default="csv"),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    _, manifest = _authorized(
        db,
        principal,
        matter_id=matter_id,
        job_type=job_type,
        job_id=job_id,
    )
    if manifest is None or manifest.manifest_status != "FINALIZED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Item details are unavailable for this job")
    try:
        iterator = (
            iter_job_outcomes_csv(manifest, actor_user_id=principal.user.id)
            if format == "csv"
            else iter_job_outcome_artifact(manifest, actor_user_id=principal.user.id)
        )
    except JobOutcomeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=manifest.tenant_id,
        actor_user_id=principal.user.id,
        action="job_outcome_manifest.downloaded",
        target_type="job_outcome_manifest",
        target_id=manifest.id,
        details={"job_type": job_type, "job_id": str(job_id), "format": format},
    )
    db.commit()
    media_type = "text/csv" if format == "csv" else "application/vnd.apache.parquet"
    return StreamingResponse(
        iterator,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="job-outcomes-{job_id}.{format}"'},
    )
