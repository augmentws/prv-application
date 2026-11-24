import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization import ResourceRef, can
from app.coding_comparisons import create_comparison, result_page
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.job_executions import JobExecutionConflict
from app.models import (
    ClassificationScoringRun,
    Matter,
    MatterCodingComparison,
    MetadataDefinition,
    ReviewBatch,
    ReviewBatchReviewerCoding,
    ReviewBatchReviewerValue,
    ReviewBatchRun,
    ReviewBatchRunValue,
    User,
)
from app.schemas import (
    MatterCodingComparisonBatchOptionRead,
    MatterCodingComparisonCreate,
    MatterCodingComparisonDocumentPage,
    MatterCodingComparisonFieldOptionRead,
    MatterCodingComparisonOptionsRead,
    MatterCodingComparisonRead,
    MatterCodingComparisonReviewerCodingOptionRead,
    MatterCodingComparisonRunOptionRead,
    MatterCodingComparisonScoringRunOptionRead,
)
from app.workflows.dispatcher import enqueue_coding_comparison

router = APIRouter(prefix="/v1/matters/{matter_id}/coding-comparisons", tags=["coding comparisons"])


def _matter(db: Session, matter_id: uuid.UUID, principal: Principal) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=404, detail="Matter not found")
    if not can(db, principal, "batch.view", ResourceRef.matter(matter.id)):
        raise HTTPException(status_code=403, detail="Permission required: batch.view")
    return matter


def _comparison(db: Session, matter_id: uuid.UUID, comparison_id: uuid.UUID) -> MatterCodingComparison:
    comparison = db.scalar(
        select(MatterCodingComparison).where(
            MatterCodingComparison.id == comparison_id,
            MatterCodingComparison.matter_id == matter_id,
        )
    )
    if comparison is None:
        raise HTTPException(status_code=404, detail="Coding comparison not found")
    return comparison


@router.get("/options", response_model=MatterCodingComparisonOptionsRead)
def comparison_options(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterCodingComparisonOptionsRead:
    matter = _matter(db, matter_id, principal)
    batches = list(
        db.scalars(
            select(ReviewBatch)
            .where(ReviewBatch.matter_id == matter.id, ReviewBatch.status == "READY")
            .order_by(ReviewBatch.created_at.desc())
        )
    )
    fields = list(
        db.scalars(
            select(MetadataDefinition)
            .where(MetadataDefinition.matter_id == matter.id, MetadataDefinition.status == "ACTIVE")
            .order_by(MetadataDefinition.display_name, MetadataDefinition.key)
        )
    )
    qa_runs = list(
        db.scalars(
            select(ReviewBatchRun)
            .join(ReviewBatch, ReviewBatch.id == ReviewBatchRun.review_batch_id)
            .where(
                ReviewBatch.matter_id == matter.id,
                ReviewBatchRun.run_type.in_(("AGENT", "WORKFLOW")),
                ReviewBatchRun.status.in_(("COMPLETED", "COMPLETED_WITH_ERRORS")),
            )
            .order_by(ReviewBatchRun.created_at.desc())
        )
    )
    definition_rows = db.execute(
        select(
            ReviewBatchRunValue.review_batch_run_id,
            ReviewBatchRunValue.metadata_definition_id,
        )
        .where(ReviewBatchRunValue.review_batch_run_id.in_([run.id for run in qa_runs] or [uuid.uuid4()]))
        .distinct()
    )
    definition_ids: dict[uuid.UUID, list[uuid.UUID]] = {}
    for run_id, definition_id in definition_rows:
        definition_ids.setdefault(run_id, []).append(definition_id)
    scoring_runs = list(
        db.scalars(
            select(ClassificationScoringRun)
            .where(
                ClassificationScoringRun.matter_id == matter.id,
                ClassificationScoringRun.status.in_(("COMPLETED", "COMPLETED_WITH_ERRORS")),
            )
            .order_by(ClassificationScoringRun.completed_at.desc())
        )
    )
    batch_names = {batch.id: batch.name for batch in batches}
    reviewer_rows = db.execute(
        select(ReviewBatchReviewerCoding, User)
        .join(User, User.id == ReviewBatchReviewerCoding.reviewer_user_id)
        .join(ReviewBatch, ReviewBatch.id == ReviewBatchReviewerCoding.review_batch_id)
        .where(ReviewBatch.matter_id == matter.id)
        .order_by(ReviewBatchReviewerCoding.created_at.desc())
    ).all()
    reviewer_definition_ids: dict[uuid.UUID, list[uuid.UUID]] = {}
    reviewer_coding_ids = [coding.id for coding, _user in reviewer_rows]
    for coding_id, definition_id in db.execute(
        select(
            ReviewBatchReviewerValue.reviewer_coding_id,
            ReviewBatchReviewerValue.metadata_definition_id,
        )
        .where(ReviewBatchReviewerValue.reviewer_coding_id.in_(reviewer_coding_ids or [uuid.uuid4()]))
        .distinct()
    ):
        reviewer_definition_ids.setdefault(coding_id, []).append(definition_id)
    return MatterCodingComparisonOptionsRead(
        batches=[
            MatterCodingComparisonBatchOptionRead(id=batch.id, name=batch.name, document_count=batch.document_count)
            for batch in batches
        ],
        fields=[
            MatterCodingComparisonFieldOptionRead(
                id=field.id,
                key=field.key,
                display_name=field.display_name,
                type=field.type,
                cardinality=field.cardinality,
                allowed_values=field.allowed_values or [],
            )
            for field in fields
        ],
        qa_runs=[
            MatterCodingComparisonRunOptionRead(
                id=run.id,
                review_batch_id=run.review_batch_id,
                label=f"{batch_names.get(run.review_batch_id, 'Batch')} · {run.run_type.title()} run",
                run_type=run.run_type,
                status=run.status,
                metadata_definition_ids=definition_ids.get(run.id, []),
            )
            for run in qa_runs
            if run.review_batch_id is not None
        ],
        reviewer_codings=[
            MatterCodingComparisonReviewerCodingOptionRead(
                id=coding.id,
                review_batch_id=coding.review_batch_id,
                reviewer_user_id=user.id,
                label=user.display_name or user.email,
                detail=f"{user.email} · {batch_names.get(coding.review_batch_id, 'Batch')}",
                revision=coding.revision,
                metadata_definition_ids=sorted(reviewer_definition_ids.get(coding.id, []), key=str),
            )
            for coding, user in reviewer_rows
        ],
        classifier_scoring_runs=[
            MatterCodingComparisonScoringRunOptionRead(
                id=run.id,
                label=f"{run.classification_model.name} · {run.completed_at:%Y-%m-%d %H:%M}"
                if run.completed_at
                else run.classification_model.name,
                metadata_definition_id=run.classification_model.target_metadata_definition_id,
                population_document_count=run.population_document_count,
                completed_at=run.completed_at,
            )
            for run in scoring_runs
        ],
    )


@router.post("", response_model=MatterCodingComparisonRead, status_code=status.HTTP_202_ACCEPTED)
def start_comparison(
    matter_id: uuid.UUID,
    payload: MatterCodingComparisonCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterCodingComparison:
    matter = _matter(db, matter_id, principal)
    try:
        comparison, created = create_comparison(db, matter, payload, actor_user_id=principal.user.id)
    except JobExecutionConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if created:
        enqueue_coding_comparison(db, comparison.workflow_id, str(comparison.id))
    db.commit()
    db.refresh(comparison)
    return comparison


@router.get("", response_model=list[MatterCodingComparisonRead])
def list_comparisons(
    matter_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterCodingComparison]:
    _matter(db, matter_id, principal)
    return list(
        db.scalars(
            select(MatterCodingComparison)
            .where(MatterCodingComparison.matter_id == matter_id)
            .order_by(MatterCodingComparison.created_at.desc())
            .limit(limit)
        )
    )


@router.get("/{comparison_id}", response_model=MatterCodingComparisonRead)
def get_comparison(
    matter_id: uuid.UUID,
    comparison_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterCodingComparison:
    _matter(db, matter_id, principal)
    return _comparison(db, matter_id, comparison_id)


@router.get("/{comparison_id}/documents", response_model=MatterCodingComparisonDocumentPage)
def comparison_documents(
    matter_id: uuid.UUID,
    comparison_id: uuid.UUID,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    agreement_status: str | None = Query(default=None, pattern="^(EXACT|PARTIAL|DISAGREEMENT|INCOMPLETE)$"),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterCodingComparisonDocumentPage:
    matter = _matter(db, matter_id, principal)
    comparison = _comparison(db, matter_id, comparison_id)
    # Artifact download and Parquet paging can be slower than the authorization lookup.
    # Detach the loaded scalars and release the Core connection before that I/O begins.
    _ = (
        matter.client_id,
        comparison.id,
        comparison.status,
        comparison.result_artifact_id,
        comparison.result_content_hash,
        comparison.tenant_id,
    )
    db.expunge(comparison)
    db.expunge(matter)
    db.rollback()
    try:
        total, items = result_page(
            comparison,
            matter=matter,
            actor_user_id=principal.user.id,
            offset=offset,
            limit=limit,
            agreement_status=agreement_status,
        )
    except JobExecutionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return MatterCodingComparisonDocumentPage(
        comparison_id=comparison.id,
        total=total,
        offset=offset,
        limit=limit,
        items=items,
    )
