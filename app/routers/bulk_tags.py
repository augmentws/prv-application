import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.bulk_tags import preview_ranked_bulk_tag_scope
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.document_metadata import value_columns
from app.models import Matter, MatterBulkTagJob, MetadataDefinition, SearchIndexGeneration
from app.schemas import (
    MatterBulkTagCreate,
    MatterBulkTagJobRead,
    MatterBulkTagPreviewRequest,
    MatterBulkTagPreviewResponse,
)
from app.workflows.dispatcher import enqueue_bulk_tag

router = APIRouter(prefix="/v1/matters/{matter_id}/bulk-tag-jobs", tags=["matter bulk tags"])


def _matter(db: Session, matter_id: uuid.UUID, principal: Principal) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    if matter.status != "ACTIVE" or matter.client.status != "ACTIVE" or matter.client.tenant.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter, client, or tenant is not active")
    return matter


@router.post("/preview", response_model=MatterBulkTagPreviewResponse)
def preview_bulk_tag_job(
    matter_id: uuid.UUID,
    payload: MatterBulkTagPreviewRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterBulkTagPreviewResponse:
    matter = _matter(db, matter_id, principal)
    if payload.search.search_mode == "KEYWORD":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Preview is for ranked searches")
    generation = db.scalar(select(SearchIndexGeneration).where(
        SearchIndexGeneration.matter_id == matter.id, SearchIndexGeneration.status == "ACTIVE"
    ))
    if generation is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter search index is not ready")
    definitions = list(db.scalars(select(MetadataDefinition).where(MetadataDefinition.matter_id == matter.id)))
    candidate_count, matched_count = preview_ranked_bulk_tag_scope(
        request=payload.search, candidate_limit=payload.candidate_limit, definitions=definitions,
        tenant_id=str(matter.client.tenant_id), matter_id=str(matter.id), index_name=generation.index_name,
    )
    return MatterBulkTagPreviewResponse(candidate_count=candidate_count, matched_count=matched_count)


@router.post("", response_model=MatterBulkTagJobRead, status_code=status.HTTP_202_ACCEPTED)
def create_bulk_tag_job(
    matter_id: uuid.UUID,
    payload: MatterBulkTagCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterBulkTagJob:
    matter = _matter(db, matter_id, principal)
    requested_assignments = payload.normalized_assignments()
    definition_ids = [assignment.metadata_definition_id for assignment in requested_assignments]
    if len(set(definition_ids)) != len(definition_ids):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Each field may be assigned once")
    definitions = list(
        db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.id.in_(definition_ids),
                MetadataDefinition.matter_id == matter.id,
            )
        )
    )
    definition_by_id = {definition.id: definition for definition in definitions}
    stored_assignments: list[dict[str, object]] = []
    for assignment in requested_assignments:
        definition = definition_by_id.get(assignment.metadata_definition_id)
        if definition is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metadata definition not found")
        if definition.status != "ACTIVE" or definition.value_source != "ASSERTED" or not definition.reviewable:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Bulk tagging requires active, reviewable asserted fields",
            )
        if definition.cardinality == "SINGLE" and isinstance(assignment.value, list):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Single-value fields accept exactly one bulk tag value",
            )
        values = assignment.value if definition.cardinality == "MULTIPLE" and isinstance(assignment.value, list) else [assignment.value]
        if not values:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Select at least one bulk tag value",
            )
        try:
            for value in values:
                value_columns(value, definition)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        stored_assignments.append(
            {
                "metadata_definition_id": str(definition.id),
                "value": values if definition.cardinality == "MULTIPLE" else values[0],
            }
        )

    first_assignment = stored_assignments[0]

    generation = db.scalar(
        select(SearchIndexGeneration).where(
            SearchIndexGeneration.matter_id == matter.id,
            SearchIndexGeneration.status == "ACTIVE",
        )
    )
    if generation is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter search index is not ready")

    job_id = uuid.uuid4()
    frozen_search = payload.search.model_copy(update={"offset": 0, "size": 500, "facets": []})
    job = MatterBulkTagJob(
        id=job_id,
        matter_id=matter.id,
        metadata_definition_id=uuid.UUID(str(first_assignment["metadata_definition_id"])),
        search_index_generation_id=generation.id,
        search_index_snapshot={
            "generation": generation.generation,
            "index_name": generation.index_name,
            "schema_hash": generation.schema_hash,
            "activated_at": generation.activated_at.isoformat() if generation.activated_at else None,
            "candidate_limit": payload.candidate_limit,
        },
        search_definition=frozen_search.model_dump(mode="json", by_alias=True),
        value=first_assignment["value"],
        assignments=stored_assignments,
        status="QUEUED",
        workflow_id=f"matter-bulk-tag:{job_id}",
        created_by_user_id=principal.user.id,
    )
    db.add(job)
    db.flush()
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter.bulk_tag_job.created",
        target_type="matter_bulk_tag_job",
        target_id=job.id,
        details={
            "matter_id": str(matter.id),
            "assignments": stored_assignments,
            "search_index_generation_id": str(generation.id),
        },
    )
    enqueue_bulk_tag(db, job.workflow_id, str(job.id))
    db.commit()
    db.refresh(job)
    return job


@router.get("", response_model=list[MatterBulkTagJobRead])
def list_bulk_tag_jobs(
    matter_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterBulkTagJob]:
    _matter(db, matter_id, principal)
    return list(
        db.scalars(
            select(MatterBulkTagJob)
            .where(MatterBulkTagJob.matter_id == matter_id)
            .order_by(MatterBulkTagJob.created_at.desc())
            .limit(limit)
        )
    )


@router.get("/{job_id}", response_model=MatterBulkTagJobRead)
def get_bulk_tag_job(
    matter_id: uuid.UUID,
    job_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterBulkTagJob:
    _matter(db, matter_id, principal)
    job = db.scalar(
        select(MatterBulkTagJob).where(
            MatterBulkTagJob.id == job_id,
            MatterBulkTagJob.matter_id == matter_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bulk tag job not found")
    return job
