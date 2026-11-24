import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.authorization import ResourceRef, can
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.identity_analysis import create_identity_analysis
from app.identity_domain_recommendations import (
    IdentityDomainRecommendationError,
    create_identity_domain_recommendation_run,
    decide_domain_recommendation,
    recommendation_result,
)
from app.job_executions import JobExecutionConflict
from app.matter_entities import MatterEntityError
from app.models import (
    Matter,
    MatterEntity,
    MatterIdentityAnalysis,
    MatterIdentityDomainDecision,
    MatterIdentityRecommendationRun,
)
from app.schemas import (
    MatterIdentityAnalysisRead,
    MatterIdentityDomainDecisionCreate,
    MatterIdentityDomainDecisionRead,
    MatterIdentityDomainRecommendationPage,
    MatterIdentityRecommendationRunRead,
)
from app.workflows.dispatcher import enqueue_identity_analysis, enqueue_identity_domain_recommendations

router = APIRouter(prefix="/v1/matters/{matter_id}/identity-analysis-jobs", tags=["matter identity analysis"])


def _matter(db: Session, matter_id: uuid.UUID, principal: Principal, permission_key: str) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can(db, principal, permission_key, ResourceRef.matter(matter.id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission required: {permission_key}")
    return matter


def _analysis(db: Session, matter_id: uuid.UUID, analysis_id: uuid.UUID) -> MatterIdentityAnalysis:
    analysis = db.scalar(
        select(MatterIdentityAnalysis).where(
            MatterIdentityAnalysis.id == analysis_id,
            MatterIdentityAnalysis.matter_id == matter_id,
        )
    )
    if analysis is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Identity Analysis not found")
    return analysis


def _recommendation_run(
    db: Session,
    matter_id: uuid.UUID,
    analysis_id: uuid.UUID,
    run_id: uuid.UUID,
) -> MatterIdentityRecommendationRun:
    run = db.scalar(
        select(MatterIdentityRecommendationRun).where(
            MatterIdentityRecommendationRun.id == run_id,
            MatterIdentityRecommendationRun.matter_id == matter_id,
            MatterIdentityRecommendationRun.identity_analysis_id == analysis_id,
        )
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Identity domain review not found")
    return run


@router.post("", response_model=MatterIdentityAnalysisRead, status_code=status.HTTP_202_ACCEPTED)
def start_identity_analysis(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterIdentityAnalysis:
    matter = _matter(db, matter_id, principal, "entity.manage")
    try:
        analysis = create_identity_analysis(db, matter, actor_user_id=principal.user.id)
        enqueue_identity_analysis(db, analysis.workflow_id, str(analysis.id))
        db.commit()
    except (JobExecutionConflict, IntegrityError) as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An Identity Analysis is already active for this matter",
        ) from exc
    db.refresh(analysis)
    return analysis


@router.get("", response_model=list[MatterIdentityAnalysisRead])
def list_identity_analyses(
    matter_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterIdentityAnalysis]:
    _matter(db, matter_id, principal, "entity.view")
    return list(
        db.scalars(
            select(MatterIdentityAnalysis)
            .where(MatterIdentityAnalysis.matter_id == matter_id)
            .order_by(MatterIdentityAnalysis.created_at.desc())
            .limit(limit)
        )
    )


@router.get("/{analysis_id}", response_model=MatterIdentityAnalysisRead)
def get_identity_analysis(
    matter_id: uuid.UUID,
    analysis_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterIdentityAnalysis:
    _matter(db, matter_id, principal, "entity.view")
    return _analysis(db, matter_id, analysis_id)


@router.post(
    "/{analysis_id}/domain-recommendation-runs",
    response_model=MatterIdentityRecommendationRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_identity_domain_recommendations(
    matter_id: uuid.UUID,
    analysis_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterIdentityRecommendationRun:
    matter = _matter(db, matter_id, principal, "entity.manage")
    analysis = _analysis(db, matter_id, analysis_id)
    try:
        run = create_identity_domain_recommendation_run(
            db,
            matter=matter,
            analysis=analysis,
            actor_user_id=principal.user.id,
        )
        enqueue_identity_domain_recommendations(db, run.workflow_id, str(run.id))
        db.commit()
    except (IdentityDomainRecommendationError, JobExecutionConflict, IntegrityError) as exc:
        db.rollback()
        code = status.HTTP_409_CONFLICT if isinstance(exc, (JobExecutionConflict, IntegrityError)) else 422
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    db.refresh(run)
    return run


@router.get(
    "/{analysis_id}/domain-recommendation-runs",
    response_model=list[MatterIdentityRecommendationRunRead],
)
def list_identity_domain_recommendations(
    matter_id: uuid.UUID,
    analysis_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterIdentityRecommendationRun]:
    _matter(db, matter_id, principal, "entity.view")
    _analysis(db, matter_id, analysis_id)
    return list(
        db.scalars(
            select(MatterIdentityRecommendationRun)
            .where(MatterIdentityRecommendationRun.identity_analysis_id == analysis_id)
            .order_by(MatterIdentityRecommendationRun.created_at.desc())
            .limit(limit)
        )
    )


@router.get(
    "/{analysis_id}/domain-recommendation-runs/{run_id}/recommendations",
    response_model=MatterIdentityDomainRecommendationPage,
)
def get_identity_domain_recommendations(
    matter_id: uuid.UUID,
    analysis_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterIdentityDomainRecommendationPage:
    matter = _matter(db, matter_id, principal, "entity.view")
    run = _recommendation_run(db, matter_id, analysis_id, run_id)
    try:
        result = recommendation_result(run, matter)
    except IdentityDomainRecommendationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    decisions = {
        row.domain: row
        for row in db.scalars(
            select(MatterIdentityDomainDecision).where(MatterIdentityDomainDecision.recommendation_run_id == run.id)
        )
    }
    proposed_entity_ids = {
        uuid.UUID(str(row["proposed_entity_id"]))
        for row in result.get("recommendations", [])
        if row.get("proposed_entity_id")
    }
    proposed_entities = {
        entity.id: entity
        for entity in db.scalars(select(MatterEntity).where(MatterEntity.id.in_(proposed_entity_ids)))
    } if proposed_entity_ids else {}
    recommendations = []
    for raw in result.get("recommendations", []):
        row = dict(raw)
        decision = decisions.get(row["domain"])
        row["decision"] = decision.decision if decision else None
        row["entity_id"] = decision.entity_id if decision else None
        proposed_entity_id = row.get("proposed_entity_id")
        proposal = proposed_entities.get(uuid.UUID(str(proposed_entity_id))) if proposed_entity_id else None
        if proposal is not None:
            if proposal.status == "CONFIRMED":
                row["publication_status"] = "ALREADY_CONFIRMED"
            elif proposal.status == "PROPOSED":
                row["publication_status"] = "PROPOSED"
            else:
                row["publication_status"] = "SKIPPED_REJECTED_MAPPING"
        recommendations.append(row)
    return MatterIdentityDomainRecommendationPage(
        run_id=run.id,
        reviewed_domain_count=int(result.get("reviewed_domain_count", 0)),
        recommendations=recommendations,
    )


@router.post(
    "/{analysis_id}/domain-recommendation-runs/{run_id}/decisions",
    response_model=MatterIdentityDomainDecisionRead,
)
def decide_identity_domain_recommendation(
    matter_id: uuid.UUID,
    analysis_id: uuid.UUID,
    run_id: uuid.UUID,
    payload: MatterIdentityDomainDecisionCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterIdentityDomainDecision:
    matter = _matter(db, matter_id, principal, "entity.manage")
    run = _recommendation_run(db, matter_id, analysis_id, run_id)
    try:
        decision = decide_domain_recommendation(
            db,
            matter=matter,
            run=run,
            domain=payload.domain,
            decision=payload.decision,
            organization_name=payload.organization_name,
            entity_id=payload.entity_id,
            actor_user_id=principal.user.id,
        )
        db.commit()
    except (IdentityDomainRecommendationError, MatterEntityError, IntegrityError) as exc:
        db.rollback()
        code = status.HTTP_409_CONFLICT if isinstance(exc, (MatterEntityError, IntegrityError)) else 422
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    db.refresh(decision)
    return decision
