import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.artifact_gateway import read_artifact_bytes
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.document_metadata import event_value, value_columns
from app.models import (
    AgentDefinition,
    AgentDefinitionVersion,
    BatchTopic,
    BatchTopicAssignment,
    BatchTopicTaxonomy,
    Matter,
    MatterDefinitionAssessmentRun,
    MatterDocument,
    MetadataDefinition,
    MetadataGroup,
    MetadataGroupField,
    ReviewBatch,
    ReviewBatchCodingField,
    ReviewBatchCodingGroup,
    ReviewBatchDocument,
    ReviewBatchNote,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewBatchRunValue,
    SkillRun,
    User,
)
from app.review_batches import materialize_review_batch, refresh_run_document_count
from app.routers.search import execute_matter_facet_values, execute_matter_search
from app.routers.search import execute_matter_batch_topic_facets
from app.schemas import (
    BatchTopicRead,
    BatchTopicTaxonomyRead,
    MatterFacetValuesRequest,
    MatterFacetValuesResponse,
    MatterSavedSearchUserRead,
    MatterSearchRequest,
    MatterSearchResponse,
    ReviewBatchAssignmentUpdate,
    ReviewBatchCodingFieldRead,
    ReviewBatchCodingGroupRead,
    ReviewBatchComparisonFieldRead,
    ReviewBatchComparisonRead,
    ReviewBatchCreate,
    ReviewBatchDocumentCodingRead,
    ReviewBatchDocumentRead,
    ReviewBatchDocumentAnalysisRead,
    ReviewBatchNoteCreate,
    ReviewBatchNoteRead,
    ReviewBatchRead,
    ReviewBatchReviewerValueRead,
    ReviewBatchRunCreate,
    ReviewBatchRunDocumentValues,
    ReviewBatchRunProgressRead,
    ReviewBatchRunRead,
    ReviewBatchRunValueRead,
)
from app.search.query import batch_topic_filter
from app.search.service import sync_review_batch_search
from app.workflows.dispatcher import enqueue_review_batch

router = APIRouter(prefix="/v1/matters/{matter_id}/review-batches", tags=["review batches"])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _matter(db: Session, matter_id: uuid.UUID, principal: Principal) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=404, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=403, detail="Matter access required")
    return matter


def _batch(db: Session, matter_id: uuid.UUID, batch_id: uuid.UUID) -> ReviewBatch:
    batch = db.scalar(select(ReviewBatch).where(ReviewBatch.id == batch_id, ReviewBatch.matter_id == matter_id))
    if batch is None:
        raise HTTPException(status_code=404, detail="Review batch not found")
    return batch


def _coding_groups(db: Session, batch_id: uuid.UUID) -> list[ReviewBatchCodingGroupRead]:
    groups = list(
        db.scalars(
            select(ReviewBatchCodingGroup)
            .where(ReviewBatchCodingGroup.review_batch_id == batch_id)
            .order_by(ReviewBatchCodingGroup.sort_order)
        )
    )
    fields = (
        list(
            db.scalars(
                select(ReviewBatchCodingField)
                .where(ReviewBatchCodingField.review_batch_coding_group_id.in_([group.id for group in groups]))
                .order_by(ReviewBatchCodingField.sort_order)
            )
        )
        if groups
        else []
    )
    by_group: dict[uuid.UUID, list[ReviewBatchCodingFieldRead]] = defaultdict(list)
    for field in fields:
        by_group[field.review_batch_coding_group_id].append(
            ReviewBatchCodingFieldRead(
                id=field.id,
                metadata_definition_id=field.metadata_definition_id,
                sort_order=field.sort_order,
                definition_snapshot=field.definition_snapshot,
            )
        )
    return [
        ReviewBatchCodingGroupRead(
            id=group.id,
            source_metadata_group_id=group.source_metadata_group_id,
            display_name=group.display_name,
            description=group.description,
            sort_order=group.sort_order,
            fields=by_group[group.id],
        )
        for group in groups
    ]


def _read_batch(db: Session, batch: ReviewBatch) -> ReviewBatchRead:
    assigned = db.get(User, batch.assigned_user_id) if batch.assigned_user_id else None
    return ReviewBatchRead(
        id=batch.id,
        matter_id=batch.matter_id,
        name=batch.name,
        description=batch.description,
        selection_type=batch.selection_type,
        selection_definition=batch.selection_definition,
        source_batch_id=batch.source_batch_id,
        search_index_generation_id=batch.search_index_generation_id,
        sample_size=batch.sample_size,
        random_seed=batch.random_seed,
        assigned_user_id=batch.assigned_user_id,
        assigned_user=MatterSavedSearchUserRead(
            id=assigned.id, display_name=assigned.display_name, email=assigned.email
        )
        if assigned
        else None,
        reviewer_value_visibility=batch.reviewer_value_visibility,
        status=batch.status,
        search_status=batch.search_status,
        search_error_message=batch.search_error_message,
        workflow_id=batch.workflow_id,
        document_count=batch.document_count,
        error_message=batch.error_message,
        created_by_user_id=batch.created_by_user_id,
        coding_groups=_coding_groups(db, batch.id),
        created_at=batch.created_at,
        updated_at=batch.updated_at,
        completed_at=batch.completed_at,
    )


def _validate_user(db: Session, matter: Matter, user_id: uuid.UUID | None) -> User | None:
    if user_id is None:
        return None
    user = db.scalar(
        select(User).where(
            User.id == user_id,
            User.tenant_id == matter.client.tenant_id,
            User.status == "ACTIVE",
        )
    )
    if user is None:
        raise HTTPException(status_code=422, detail="Assignee must be an active user in the matter tenant")
    return user


def _snapshot_groups(db: Session, batch: ReviewBatch, group_ids: list[uuid.UUID]) -> None:
    if not group_ids:
        return
    groups = list(
        db.scalars(
            select(MetadataGroup).where(
                MetadataGroup.id.in_(set(group_ids)),
                MetadataGroup.matter_id == batch.matter_id,
                MetadataGroup.status == "ACTIVE",
                MetadataGroup.scope.in_(["SYSTEM", "MATTER"]),
            )
        )
    )
    if len(groups) != len(set(group_ids)):
        raise HTTPException(status_code=422, detail="Coding groups must be active shared groups in this matter")
    by_id = {group.id: group for group in groups}
    for group_order, group_id in enumerate(group_ids):
        group = by_id[group_id]
        snapshot = ReviewBatchCodingGroup(
            review_batch_id=batch.id,
            source_metadata_group_id=group.id,
            display_name=group.display_name,
            description=group.description,
            sort_order=group_order,
        )
        db.add(snapshot)
        db.flush()
        rows = db.execute(
            select(MetadataGroupField, MetadataDefinition)
            .join(MetadataDefinition, MetadataDefinition.id == MetadataGroupField.metadata_definition_id)
            .where(
                MetadataGroupField.metadata_group_id == group.id,
                MetadataDefinition.status == "ACTIVE",
                MetadataDefinition.reviewable.is_(True),
                MetadataDefinition.value_source == "ASSERTED",
            )
            .order_by(MetadataGroupField.sort_order)
        ).all()
        for field_order, (_, definition) in enumerate(rows):
            db.add(
                ReviewBatchCodingField(
                    review_batch_coding_group_id=snapshot.id,
                    metadata_definition_id=definition.id,
                    sort_order=field_order,
                    definition_snapshot={
                        "key": definition.key,
                        "display_name": definition.display_name,
                        "description": definition.description,
                        "type": definition.type,
                        "cardinality": definition.cardinality,
                        "allowed_values": definition.allowed_values,
                    },
                )
            )


@router.post("", response_model=ReviewBatchRead, status_code=status.HTTP_202_ACCEPTED)
def create_review_batch(
    matter_id: uuid.UUID,
    payload: ReviewBatchCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ReviewBatchRead:
    matter = _matter(db, matter_id, principal)
    _validate_user(db, matter, payload.assigned_user_id)
    if payload.source_batch_id:
        source = _batch(db, matter_id, payload.source_batch_id)
        if source.status != "READY":
            raise HTTPException(status_code=409, detail="Source batch is not ready")
    batch_id = uuid.uuid4()
    seed = payload.random_seed or (str(batch_id) if payload.selection_type.startswith("RANDOM") else None)
    selection = {"type": payload.selection_type}
    if payload.search is not None:
        selection["search"] = payload.search.model_copy(update={"offset": 0}).model_dump(mode="json", by_alias=True)
    batch = ReviewBatch(
        id=batch_id,
        matter_id=matter.id,
        name=" ".join(payload.name.split()),
        description=payload.description.strip() if payload.description else None,
        selection_type=payload.selection_type,
        selection_definition=selection,
        source_batch_id=payload.source_batch_id,
        sample_size=payload.sample_size,
        random_seed=seed,
        assigned_user_id=payload.assigned_user_id,
        assigned_by_user_id=principal.user.id if payload.assigned_user_id else None,
        assigned_at=utcnow() if payload.assigned_user_id else None,
        reviewer_value_visibility=payload.reviewer_value_visibility,
        status="QUEUED",
        workflow_id=f"review-batch:{batch_id}",
        created_by_user_id=principal.user.id,
    )
    db.add(batch)
    db.flush()
    _snapshot_groups(db, batch, payload.coding_group_ids)
    if payload.note:
        db.add(ReviewBatchNote(review_batch_id=batch.id, body=payload.note.strip(), author_user_id=principal.user.id))
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="review_batch.created",
        target_type="review_batch",
        target_id=batch.id,
        details={"matter_id": str(matter.id), "selection_type": batch.selection_type},
    )
    enqueue_review_batch(db, batch.workflow_id, str(batch.id))
    db.commit()
    if not settings.dbos_enabled:
        materialize_review_batch(db, batch.id, settings)
        if settings.search_enabled and db.get_bind().dialect.name == "postgresql":
            sync_review_batch_search(batch.id, settings)
        else:
            batch = db.get(ReviewBatch, batch.id) or batch
            batch.search_status = "NOT_CONFIGURED"
            batch.search_error_message = None
            db.commit()
        batch = db.get(ReviewBatch, batch.id) or batch
    return _read_batch(db, batch)


@router.get("", response_model=list[ReviewBatchRead])
def list_review_batches(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ReviewBatchRead]:
    _matter(db, matter_id, principal)
    batches = list(
        db.scalars(
            select(ReviewBatch).where(ReviewBatch.matter_id == matter_id).order_by(ReviewBatch.created_at.desc())
        )
    )
    return [_read_batch(db, batch) for batch in batches]


@router.get("/{batch_id}", response_model=ReviewBatchRead)
def get_review_batch(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRead:
    _matter(db, matter_id, principal)
    return _read_batch(db, _batch(db, matter_id, batch_id))


@router.put("/{batch_id}/assignment", response_model=ReviewBatchRead)
def update_review_batch_assignment(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchAssignmentUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRead:
    matter = _matter(db, matter_id, principal)
    batch = _batch(db, matter_id, batch_id)
    _validate_user(db, matter, payload.assigned_user_id)
    batch.assigned_user_id = payload.assigned_user_id
    batch.assigned_by_user_id = principal.user.id if payload.assigned_user_id else None
    batch.assigned_at = utcnow() if payload.assigned_user_id else None
    db.commit()
    return _read_batch(db, batch)


def _require_batch_search(batch: ReviewBatch) -> None:
    if batch.search_status != "READY":
        detail = batch.search_error_message or f"Batch search is {batch.search_status.lower().replace('_', ' ')}"
        raise HTTPException(status_code=409, detail=detail)


def _active_taxonomy(db: Session, batch_id: uuid.UUID) -> BatchTopicTaxonomy | None:
    return db.scalar(
        select(BatchTopicTaxonomy).where(
            BatchTopicTaxonomy.review_batch_id == batch_id,
            BatchTopicTaxonomy.status == "ACTIVE",
        )
    )


def _taxonomy_read(db: Session, taxonomy: BatchTopicTaxonomy) -> BatchTopicTaxonomyRead:
    rows = db.execute(
        select(BatchTopic, func.count(BatchTopicAssignment.id))
        .outerjoin(BatchTopicAssignment, BatchTopicAssignment.topic_id == BatchTopic.id)
        .where(BatchTopic.taxonomy_id == taxonomy.id)
        .group_by(BatchTopic.id)
        .order_by(BatchTopic.ordinal)
    ).all()
    assessment = db.get(MatterDefinitionAssessmentRun, taxonomy.source_assessment_run_id)
    return BatchTopicTaxonomyRead(
        id=taxonomy.id,
        review_batch_id=taxonomy.review_batch_id,
        source_assessment_run_id=taxonomy.source_assessment_run_id,
        review_batch_run_id=assessment.review_batch_run_id if assessment else None,
        version=taxonomy.version,
        status=taxonomy.status,
        topics=[
            BatchTopicRead(
                id=topic.id,
                topic_key=topic.topic_key,
                label=topic.label,
                description=topic.description,
                ordinal=topic.ordinal,
                assignment_count=count,
            )
            for topic, count in rows
        ],
        created_at=taxonomy.created_at,
    )


@router.get("/{batch_id}/topic-taxonomy", response_model=BatchTopicTaxonomyRead | None)
def get_review_batch_topic_taxonomy(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> BatchTopicTaxonomyRead | None:
    _matter(db, matter_id, principal)
    batch = _batch(db, matter_id, batch_id)
    taxonomy = _active_taxonomy(db, batch.id)
    return _taxonomy_read(db, taxonomy) if taxonomy is not None else None


@router.post("/{batch_id}/search", response_model=MatterSearchResponse)
def search_review_batch(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: MatterSearchRequest,
    topic_key: list[str] = Query(default=[]),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterSearchResponse:
    matter = _matter(db, matter_id, principal)
    batch = _batch(db, matter_id, batch_id)
    _require_batch_search(batch)
    required_filters: list[dict[str, Any]] = [{"term": {"batch_ids": str(batch.id)}}]
    if topic_key:
        taxonomy = _active_taxonomy(db, batch.id)
        if taxonomy is None:
            raise HTTPException(status_code=422, detail="This batch has no active diagnostic topic taxonomy")
        allowed = set(
            db.scalars(select(BatchTopic.topic_key).where(BatchTopic.taxonomy_id == taxonomy.id))
        )
        unknown = set(topic_key) - allowed
        if unknown:
            raise HTTPException(status_code=422, detail="Unknown diagnostic batch topic")
        required_filters.append(
            batch_topic_filter(
                batch_id=str(batch.id),
                taxonomy_id=str(taxonomy.id),
                topic_keys=topic_key,
            )
        )
    return execute_matter_search(
        matter,
        payload,
        db=db,
        settings=settings,
        required_filters=required_filters,
    )


@router.post("/{batch_id}/topic-facets", response_model=MatterFacetValuesResponse)
def search_review_batch_topic_facets(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: MatterSearchRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterFacetValuesResponse:
    matter = _matter(db, matter_id, principal)
    batch = _batch(db, matter_id, batch_id)
    _require_batch_search(batch)
    taxonomy = _active_taxonomy(db, batch.id)
    if taxonomy is None:
        return MatterFacetValuesResponse(field="batch_topic", values=[])
    return execute_matter_batch_topic_facets(
        matter,
        payload,
        batch_id=batch.id,
        taxonomy_id=taxonomy.id,
        db=db,
        settings=settings,
        required_filters=[{"term": {"batch_ids": str(batch.id)}}],
    )


@router.post("/{batch_id}/facets/{field}/values", response_model=MatterFacetValuesResponse)
def search_review_batch_facet_values(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    field: str,
    payload: MatterFacetValuesRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterFacetValuesResponse:
    matter = _matter(db, matter_id, principal)
    batch = _batch(db, matter_id, batch_id)
    _require_batch_search(batch)
    return execute_matter_facet_values(
        matter,
        field,
        payload,
        db=db,
        settings=settings,
        required_filters=[{"term": {"batch_ids": str(batch.id)}}],
    )


@router.get("/{batch_id}/documents", response_model=list[ReviewBatchDocumentRead])
def list_review_batch_documents(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    run_id: uuid.UUID | None = None,
    document_id: list[uuid.UUID] = Query(default=[]),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ReviewBatchDocumentRead]:
    _matter(db, matter_id, principal)
    batch = _batch(db, matter_id, batch_id)
    if run_id is not None:
        run = _run(db, batch.id, run_id)
        _require_human_reviewer(run, principal)
    statement = (
        select(ReviewBatchDocument, MatterDocument, ReviewBatchRunDocument.status)
        .join(MatterDocument, MatterDocument.id == ReviewBatchDocument.matter_document_id)
        .outerjoin(
            ReviewBatchRunDocument,
            and_(
                ReviewBatchRunDocument.review_batch_run_id == run_id,
                ReviewBatchRunDocument.matter_document_id == ReviewBatchDocument.matter_document_id,
            ),
        )
        .where(ReviewBatchDocument.review_batch_id == batch_id)
        .order_by(ReviewBatchDocument.sequence_number)
        .offset(offset)
        .limit(limit)
    )
    if document_id:
        statement = statement.where(ReviewBatchDocument.matter_document_id.in_(set(document_id)))
    rows = db.execute(statement).all()
    return [
        ReviewBatchDocumentRead(
            matter_document_id=member.matter_document_id,
            source_collection_id=document.source_collection_id,
            collection_item_id=document.collection_item_id,
            sequence_number=member.sequence_number,
            review_status=run_status or "NOT_STARTED",
        )
        for member, document, run_status in rows
    ]


@router.get("/{batch_id}/notes", response_model=list[ReviewBatchNoteRead])
def list_review_batch_notes(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ReviewBatchNote]:
    _matter(db, matter_id, principal)
    _batch(db, matter_id, batch_id)
    return list(
        db.scalars(
            select(ReviewBatchNote)
            .where(ReviewBatchNote.review_batch_id == batch_id)
            .order_by(ReviewBatchNote.created_at, ReviewBatchNote.id)
        )
    )


@router.post("/{batch_id}/notes", response_model=ReviewBatchNoteRead, status_code=201)
def create_review_batch_note(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchNoteCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchNote:
    matter = _matter(db, matter_id, principal)
    batch = _batch(db, matter_id, batch_id)
    note = ReviewBatchNote(
        review_batch_id=batch.id,
        body=payload.body.strip(),
        author_type="USER",
        author_user_id=principal.user.id,
    )
    db.add(note)
    db.flush()
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="review_batch.note.created",
        target_type="review_batch_note",
        target_id=note.id,
        details={"batch_id": str(batch.id)},
    )
    db.commit()
    return note


def _run(db: Session, batch_id: uuid.UUID, run_id: uuid.UUID) -> ReviewBatchRun:
    run = db.scalar(
        select(ReviewBatchRun).where(
            ReviewBatchRun.id == run_id,
            ReviewBatchRun.review_batch_id == batch_id,
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Review batch run not found")
    return run


def _require_human_reviewer(run: ReviewBatchRun, principal: Principal) -> None:
    if run.run_type == "HUMAN" and run.actor_user_id != principal.user.id:
        raise HTTPException(status_code=403, detail="A human run can only be opened by its reviewer")


def _run_value_read(row: ReviewBatchRunValue) -> ReviewBatchRunValueRead:
    return ReviewBatchRunValueRead(
        review_batch_run_id=row.review_batch_run_id,
        matter_document_id=row.matter_document_id,
        metadata_definition_id=row.metadata_definition_id,
        value_ordinal=row.value_ordinal,
        value=event_value(row),
        confidence=row.confidence,
    )


@router.post("/{batch_id}/runs", response_model=ReviewBatchRunRead, status_code=201)
def create_review_batch_run(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchRunCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRun:
    matter = _matter(db, matter_id, principal)
    batch = _batch(db, matter_id, batch_id)
    if batch.status != "READY":
        raise HTTPException(status_code=409, detail="Review batch is not ready")
    if payload.run_type == "HUMAN" and batch.assigned_user_id not in {None, principal.user.id}:
        raise HTTPException(status_code=403, detail="This review batch is assigned to another user")
    if payload.actor_user_id not in {None, principal.user.id}:
        raise HTTPException(status_code=422, detail="A human run actor must be the current user")
    actor_user_id = principal.user.id if payload.run_type == "HUMAN" else None
    configuration: dict[str, Any] = {}
    if payload.run_type == "AGENT":
        version = db.scalar(
            select(AgentDefinitionVersion)
            .join(AgentDefinition, AgentDefinition.id == AgentDefinitionVersion.agent_definition_id)
            .where(
                AgentDefinitionVersion.id == payload.agent_definition_version_id,
                AgentDefinitionVersion.status == "PUBLISHED",
                AgentDefinition.status == "ACTIVE",
                or_(
                    AgentDefinition.owner_tenant_id == matter.client.tenant_id,
                    AgentDefinition.scope == "SYSTEM",
                ),
            )
        )
        if version is None:
            raise HTTPException(status_code=422, detail="Agent run requires an available published agent version")
        configuration = {
            "agent_definition_version_id": str(version.id),
            "version": version.version,
            "model_key": version.model_key,
            "system_prompt": version.system_prompt,
            "model_policy": version.model_policy,
            "output_schema": version.output_schema,
            "limits": version.limits,
        }
    if payload.parent_run_id and _run(db, batch.id, payload.parent_run_id).id == payload.parent_run_id:
        pass
    run = ReviewBatchRun(
        review_batch_id=batch.id,
        run_type=payload.run_type,
        purpose=payload.purpose,
        status="RUNNING",
        result_policy="ISOLATED",
        parent_run_id=payload.parent_run_id,
        actor_user_id=actor_user_id,
        agent_definition_version_id=payload.agent_definition_version_id,
        configuration_snapshot=configuration,
        initiated_by_user_id=principal.user.id,
    )
    db.add(run)
    db.flush()
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="review_batch.run.created",
        target_type="review_batch_run",
        target_id=run.id,
        details={"batch_id": str(batch.id), "run_type": run.run_type, "purpose": run.purpose},
    )
    db.commit()
    return run


@router.get("/{batch_id}/runs", response_model=list[ReviewBatchRunRead])
def list_review_batch_runs(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ReviewBatchRun]:
    _matter(db, matter_id, principal)
    batch = _batch(db, matter_id, batch_id)
    return list(
        db.scalars(
            select(ReviewBatchRun)
            .where(ReviewBatchRun.review_batch_id == batch.id)
            .order_by(ReviewBatchRun.created_at.desc())
        )
    )


@router.get(
    "/{batch_id}/runs/{run_id}/documents/{document_id}/analysis",
    response_model=ReviewBatchDocumentAnalysisRead,
)
def get_review_batch_document_analysis(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    run_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchDocumentAnalysisRead:
    matter = _matter(db, matter_id, principal)
    batch = _batch(db, matter_id, batch_id)
    run = _run(db, batch.id, run_id)
    if run.run_type != "WORKFLOW" or run.purpose != "ASSESSMENT" or run.workflow_run_record_id is None:
        raise HTTPException(status_code=409, detail="This run does not contain assessment analyses")
    run_document = db.get(ReviewBatchRunDocument, (run.id, document_id))
    if run_document is None:
        raise HTTPException(status_code=404, detail="Assessment document not found")
    skill_run = db.scalar(
        select(SkillRun)
        .where(
            SkillRun.workflow_run_id == run.workflow_run_record_id,
            SkillRun.scope_type == "MATTER_DOCUMENT",
            SkillRun.scope_id == document_id,
            SkillRun.status == "COMPLETED",
        )
        .order_by(SkillRun.created_at.desc())
        .limit(1)
    )
    artifact_id = skill_run.output_artifact_id if skill_run is not None else None
    analysis = None
    if artifact_id is not None:
        try:
            analysis = json.loads(
                read_artifact_bytes(
                    artifact_id=artifact_id,
                    actor_user_id=principal.user.id,
                    tenant_id=matter.client.tenant_id,
                    client_id=matter.client_id,
                )
            )
        except (ValueError, PermissionError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=409, detail="Assessment analysis artifact is unavailable") from exc
    return ReviewBatchDocumentAnalysisRead(
        review_batch_run_id=run.id,
        matter_document_id=document_id,
        status=run_document.status,
        output_artifact_id=artifact_id,
        analysis=analysis,
    )


@router.post("/{batch_id}/review-run", response_model=ReviewBatchRunRead)
def start_or_resume_review_batch_run(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRun:
    matter = _matter(db, matter_id, principal)
    batch = db.scalar(
        select(ReviewBatch)
        .where(ReviewBatch.id == batch_id, ReviewBatch.matter_id == matter_id)
        .with_for_update()
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Review batch not found")
    if batch.status != "READY":
        raise HTTPException(status_code=409, detail="Review batch is not ready")
    if batch.assigned_user_id not in {None, principal.user.id}:
        raise HTTPException(status_code=403, detail="This review batch is assigned to another user")
    if batch.assigned_user_id is None:
        batch.assigned_user_id = principal.user.id
        batch.assigned_by_user_id = principal.user.id
        batch.assigned_at = utcnow()

    run = db.scalar(
        select(ReviewBatchRun)
        .where(
            ReviewBatchRun.review_batch_id == batch.id,
            ReviewBatchRun.run_type == "HUMAN",
            ReviewBatchRun.purpose == "REVIEW",
            ReviewBatchRun.actor_user_id == principal.user.id,
            ReviewBatchRun.status == "RUNNING",
        )
        .order_by(ReviewBatchRun.created_at.desc())
        .limit(1)
    )
    if run is None:
        completed = db.scalar(
            select(ReviewBatchRun)
            .where(
                ReviewBatchRun.review_batch_id == batch.id,
                ReviewBatchRun.run_type == "HUMAN",
                ReviewBatchRun.purpose == "REVIEW",
                ReviewBatchRun.actor_user_id == principal.user.id,
                ReviewBatchRun.status == "COMPLETED",
            )
            .order_by(ReviewBatchRun.completed_at.desc())
            .limit(1)
        )
        if completed is not None:
            db.commit()
            return completed
        run = ReviewBatchRun(
            review_batch_id=batch.id,
            run_type="HUMAN",
            purpose="REVIEW",
            status="RUNNING",
            result_policy="ISOLATED",
            actor_user_id=principal.user.id,
            configuration_snapshot={"reviewer_value_visibility": batch.reviewer_value_visibility},
            initiated_by_user_id=principal.user.id,
        )
        db.add(run)
        db.flush()
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=principal.user.id,
            action="review_batch.review.started",
            target_type="review_batch_run",
            target_id=run.id,
            details={"batch_id": str(batch.id)},
        )
    db.commit()
    return run


@router.get("/{batch_id}/runs/{run_id}/progress", response_model=ReviewBatchRunProgressRead)
def get_review_batch_run_progress(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRunProgressRead:
    _matter(db, matter_id, principal)
    batch = _batch(db, matter_id, batch_id)
    run = _run(db, batch.id, run_id)
    _require_human_reviewer(run, principal)
    counts = dict(
        db.execute(
            select(ReviewBatchRunDocument.status, func.count())
            .where(ReviewBatchRunDocument.review_batch_run_id == run.id)
            .group_by(ReviewBatchRunDocument.status)
        ).all()
    )
    in_progress = int(counts.get("IN_PROGRESS", 0))
    completed = int(counts.get("COMPLETED", 0))
    skipped = int(counts.get("SKIPPED", 0))
    return ReviewBatchRunProgressRead(
        review_batch_run_id=run.id,
        document_count=batch.document_count,
        not_started_count=max(0, batch.document_count - in_progress - completed - skipped),
        in_progress_count=in_progress,
        completed_count=completed,
        skipped_count=skipped,
    )


@router.get(
    "/{batch_id}/runs/{run_id}/documents/{document_id}",
    response_model=ReviewBatchDocumentCodingRead,
)
def get_review_batch_document_coding(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    run_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchDocumentCodingRead:
    _matter(db, matter_id, principal)
    batch = _batch(db, matter_id, batch_id)
    run = _run(db, batch.id, run_id)
    _require_human_reviewer(run, principal)
    if db.get(ReviewBatchDocument, (batch.id, document_id)) is None:
        raise HTTPException(status_code=404, detail="Document is not in this review batch")
    state = db.get(ReviewBatchRunDocument, (run.id, document_id))
    values = list(
        db.scalars(
            select(ReviewBatchRunValue)
            .where(
                ReviewBatchRunValue.review_batch_run_id == run.id,
                ReviewBatchRunValue.matter_document_id == document_id,
            )
            .order_by(ReviewBatchRunValue.metadata_definition_id, ReviewBatchRunValue.value_ordinal)
        )
    )
    reviewer_values: list[ReviewBatchReviewerValueRead] = []
    if batch.reviewer_value_visibility == "ALL_REVIEWER_VALUES" and run.run_type == "HUMAN":
        other_rows = db.execute(
            select(ReviewBatchRunValue, ReviewBatchRun, User)
            .join(ReviewBatchRun, ReviewBatchRun.id == ReviewBatchRunValue.review_batch_run_id)
            .join(User, User.id == ReviewBatchRun.actor_user_id)
            .where(
                ReviewBatchRun.review_batch_id == batch.id,
                ReviewBatchRun.run_type == "HUMAN",
                ReviewBatchRun.id != run.id,
                ReviewBatchRunValue.matter_document_id == document_id,
            )
            .order_by(
                ReviewBatchRun.created_at.desc(),
                ReviewBatchRunValue.metadata_definition_id,
                ReviewBatchRunValue.value_ordinal,
            )
        ).all()
        grouped: dict[tuple[uuid.UUID, uuid.UUID], ReviewBatchReviewerValueRead] = {}
        for value, other_run, user in other_rows:
            key = (other_run.id, value.metadata_definition_id)
            visible = grouped.get(key)
            if visible is None:
                visible = ReviewBatchReviewerValueRead(
                    review_batch_run_id=other_run.id,
                    actor_user_id=user.id,
                    actor_user=MatterSavedSearchUserRead(
                        id=user.id,
                        display_name=user.display_name,
                        email=user.email,
                    ),
                    metadata_definition_id=value.metadata_definition_id,
                    values=[],
                )
                grouped[key] = visible
            visible.values.append(event_value(value))
        reviewer_values = list(grouped.values())
    return ReviewBatchDocumentCodingRead(
        matter_document_id=document_id,
        review_status=state.status if state else "NOT_STARTED",
        values=[_run_value_read(value) for value in values],
        reviewer_values=reviewer_values,
    )


@router.put("/{batch_id}/runs/{run_id}/documents/{document_id}/values", response_model=list[ReviewBatchRunValueRead])
def replace_review_batch_run_values(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    run_id: uuid.UUID,
    document_id: uuid.UUID,
    payload: ReviewBatchRunDocumentValues,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ReviewBatchRunValueRead]:
    _matter(db, matter_id, principal)
    batch = _batch(db, matter_id, batch_id)
    run = _run(db, batch.id, run_id)
    if run.status != "RUNNING":
        raise HTTPException(status_code=409, detail="Only running batch runs accept values")
    _require_human_reviewer(run, principal)
    if payload.matter_document_id != document_id:
        raise HTTPException(status_code=422, detail="Document id does not match the request path")
    member = db.get(ReviewBatchDocument, (batch.id, document_id))
    if member is None:
        raise HTTPException(status_code=422, detail="Document is not in this review batch")
    allowed = set(
        db.scalars(
            select(ReviewBatchCodingField.metadata_definition_id)
            .join(ReviewBatchCodingGroup)
            .where(ReviewBatchCodingGroup.review_batch_id == batch.id)
        )
    )
    definitions = {
        definition.id: definition
        for definition in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter_id,
                MetadataDefinition.id.in_([field.metadata_definition_id for field in payload.fields]),
            )
        )
    }
    for field in payload.fields:
        if field.metadata_definition_id not in allowed or field.metadata_definition_id not in definitions:
            raise HTTPException(status_code=422, detail="Field is not in the batch coding configuration")
        definition = definitions[field.metadata_definition_id]
        if definition.cardinality == "SINGLE" and len(field.values) > 1:
            raise HTTPException(status_code=422, detail=f"{definition.display_name} accepts one value")
        db.execute(
            delete(ReviewBatchRunValue).where(
                ReviewBatchRunValue.review_batch_run_id == run.id,
                ReviewBatchRunValue.matter_document_id == document_id,
                ReviewBatchRunValue.metadata_definition_id == definition.id,
            )
        )
        for ordinal, value in enumerate(field.values):
            try:
                columns = value_columns(value, definition)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            db.add(
                ReviewBatchRunValue(
                    review_batch_run_id=run.id,
                    matter_document_id=document_id,
                    metadata_definition_id=definition.id,
                    value_ordinal=ordinal,
                    confidence=field.confidence,
                    **columns,
                )
            )
    now = utcnow()
    run_document = db.get(ReviewBatchRunDocument, (run.id, document_id))
    if run_document is None:
        run_document = ReviewBatchRunDocument(
            review_batch_run_id=run.id,
            matter_document_id=document_id,
            status="COMPLETED",
            started_at=now,
            completed_at=now,
        )
        db.add(run_document)
    else:
        run_document.status = "COMPLETED"
        run_document.completed_at = now
    db.flush()
    refresh_run_document_count(db, run.id)
    if run.processed_document_count >= batch.document_count:
        run.status = "COMPLETED"
        run.completed_at = now
    db.commit()
    rows = list(
        db.scalars(
            select(ReviewBatchRunValue)
            .where(
                ReviewBatchRunValue.review_batch_run_id == run.id,
                ReviewBatchRunValue.matter_document_id == document_id,
            )
            .order_by(ReviewBatchRunValue.metadata_definition_id, ReviewBatchRunValue.value_ordinal)
        )
    )
    return [
        _run_value_read(row)
        for row in rows
    ]


@router.post(
    "/{batch_id}/runs/{run_id}/documents/{document_id}/skip",
    response_model=ReviewBatchDocumentCodingRead,
)
def skip_review_batch_document(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    run_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchDocumentCodingRead:
    _matter(db, matter_id, principal)
    batch = _batch(db, matter_id, batch_id)
    run = _run(db, batch.id, run_id)
    if run.status != "RUNNING":
        raise HTTPException(status_code=409, detail="Only running batch runs can skip documents")
    _require_human_reviewer(run, principal)
    if db.get(ReviewBatchDocument, (batch.id, document_id)) is None:
        raise HTTPException(status_code=404, detail="Document is not in this review batch")
    now = utcnow()
    run_document = db.get(ReviewBatchRunDocument, (run.id, document_id))
    if run_document is None:
        run_document = ReviewBatchRunDocument(
            review_batch_run_id=run.id,
            matter_document_id=document_id,
            status="SKIPPED",
            started_at=now,
            completed_at=now,
        )
        db.add(run_document)
    else:
        run_document.status = "SKIPPED"
        run_document.completed_at = now
    db.flush()
    refresh_run_document_count(db, run.id)
    if run.processed_document_count >= batch.document_count:
        run.status = "COMPLETED"
        run.completed_at = now
    db.commit()
    return get_review_batch_document_coding(
        matter_id=matter_id,
        batch_id=batch_id,
        run_id=run_id,
        document_id=document_id,
        principal=principal,
        db=db,
    )


@router.post("/{batch_id}/runs/{run_id}/complete", response_model=ReviewBatchRunRead)
def complete_review_batch_run(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRun:
    _matter(db, matter_id, principal)
    run = _run(db, _batch(db, matter_id, batch_id).id, run_id)
    if run.run_type == "HUMAN" and run.actor_user_id != principal.user.id:
        raise HTTPException(status_code=403, detail="A human run can only be completed by its reviewer")
    if run.status == "RUNNING":
        run.status = "COMPLETED"
        run.completed_at = utcnow()
        db.commit()
    return run


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


@router.get("/{batch_id}/comparisons", response_model=ReviewBatchComparisonRead)
def compare_review_batch_runs(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    left_run_id: uuid.UUID,
    right_run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchComparisonRead:
    _matter(db, matter_id, principal)
    batch = _batch(db, matter_id, batch_id)
    _run(db, batch.id, left_run_id)
    _run(db, batch.id, right_run_id)
    values: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], list[str]] = defaultdict(list)
    rows = list(
        db.scalars(
            select(ReviewBatchRunValue).where(ReviewBatchRunValue.review_batch_run_id.in_([left_run_id, right_run_id]))
        )
    )
    for row in rows:
        values[(row.review_batch_run_id, row.matter_document_id, row.metadata_definition_id)].append(
            _canonical(event_value(row))
        )
    definitions = {
        definition.id: definition
        for definition in db.scalars(select(MetadataDefinition).where(MetadataDefinition.matter_id == matter_id))
    }
    document_ids = list(
        db.scalars(
            select(ReviewBatchDocument.matter_document_id).where(ReviewBatchDocument.review_batch_id == batch.id)
        )
    )
    field_ids = sorted({key[2] for key in values}, key=str)
    metrics = []
    for field_id in field_ids:
        match = mismatch = missing_left = missing_right = 0
        for document_id in document_ids:
            left = sorted(values.get((left_run_id, document_id, field_id), []))
            right = sorted(values.get((right_run_id, document_id, field_id), []))
            if not left and not right:
                continue
            if not left:
                missing_left += 1
            elif not right:
                missing_right += 1
            elif left == right:
                match += 1
            else:
                mismatch += 1
        compared = match + mismatch
        definition = definitions.get(field_id)
        metrics.append(
            ReviewBatchComparisonFieldRead(
                metadata_definition_id=field_id,
                display_name=definition.display_name if definition else str(field_id),
                compared_count=compared,
                match_count=match,
                mismatch_count=mismatch,
                missing_left_count=missing_left,
                missing_right_count=missing_right,
                agreement=(match / compared) if compared else None,
            )
        )
    return ReviewBatchComparisonRead(
        left_run_id=left_run_id,
        right_run_id=right_run_id,
        document_count=len(document_ids),
        fields=metrics,
    )
