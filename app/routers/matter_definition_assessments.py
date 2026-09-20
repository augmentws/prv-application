import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.matter_definition_assessments import AssessmentError, start_assessment, utcnow
from app.models import (
    Matter,
    MatterDefinitionAssessmentQuery,
    MatterDefinitionAssessmentQuestion,
    MatterDefinitionAssessmentRun,
    WorkflowRun,
)
from app.schemas import (
    MatterDefinitionAssessmentCreate,
    MatterDefinitionAssessmentQueryRead,
    MatterDefinitionAssessmentQuestionRead,
    MatterDefinitionAssessmentQuestionUpdate,
    MatterDefinitionAssessmentRead,
)
from app.workflows.dispatcher import cancel_definition_assessment

router = APIRouter(prefix="/v1/matters/{matter_id}/definition-assessments", tags=["matter definition assessments"])

TERMINAL_STATUSES = {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}


def _matter(db: Session, matter_id: uuid.UUID, principal: Principal) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=404, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=403, detail="Matter ADMIN required")
    return matter


def _assessment(db: Session, matter_id: uuid.UUID, assessment_id: uuid.UUID) -> MatterDefinitionAssessmentRun:
    assessment = db.scalar(
        select(MatterDefinitionAssessmentRun).where(
            MatterDefinitionAssessmentRun.id == assessment_id,
            MatterDefinitionAssessmentRun.matter_id == matter_id,
        )
    )
    if assessment is None:
        raise HTTPException(status_code=404, detail="Matter Definition assessment not found")
    return assessment


@router.post("", response_model=MatterDefinitionAssessmentRead, status_code=status.HTTP_202_ACCEPTED)
def create_assessment(
    matter_id: uuid.UUID,
    payload: MatterDefinitionAssessmentCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterDefinitionAssessmentRun:
    matter = _matter(db, matter_id, principal)
    try:
        assessment = start_assessment(
            db,
            matter=matter,
            initiated_by_user_id=principal.user.id,
            settings=settings,
            revision_number=payload.revision,
            maximum_document_count=payload.maximum_document_count,
            control_sample_size=payload.control_sample_size,
            acknowledge_large_run_warning=payload.acknowledge_large_run_warning,
        )
        db.commit()
    except AssessmentError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="The assessment could not be queued") from exc
    db.refresh(assessment)
    return assessment


@router.get("", response_model=list[MatterDefinitionAssessmentRead])
def list_assessments(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterDefinitionAssessmentRun]:
    _matter(db, matter_id, principal)
    return list(
        db.scalars(
            select(MatterDefinitionAssessmentRun)
            .where(MatterDefinitionAssessmentRun.matter_id == matter_id)
            .order_by(MatterDefinitionAssessmentRun.created_at.desc())
        )
    )


@router.get("/{assessment_id}", response_model=MatterDefinitionAssessmentRead)
def get_assessment(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentRun:
    _matter(db, matter_id, principal)
    return _assessment(db, matter_id, assessment_id)


@router.post("/{assessment_id}/cancel", response_model=MatterDefinitionAssessmentRead)
def cancel_assessment(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentRun:
    matter = _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    if assessment.status in TERMINAL_STATUSES:
        return assessment
    now = utcnow()
    assessment.status = "CANCELED"
    assessment.canceled_at = now
    assessment.completed_at = now
    workflow = db.get(WorkflowRun, assessment.workflow_run_id)
    if workflow is not None:
        workflow.status = "CANCELED"
        workflow.completed_at = now
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_definition.assessment.canceled",
        target_type="matter_definition_assessment_run",
        target_id=assessment.id,
        details={"matter_id": str(matter.id)},
    )
    db.commit()
    if workflow is not None:
        cancel_definition_assessment(workflow.dbos_workflow_id)
    db.refresh(assessment)
    return assessment


@router.get("/{assessment_id}/queries", response_model=list[MatterDefinitionAssessmentQueryRead])
def list_assessment_queries(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterDefinitionAssessmentQuery]:
    _matter(db, matter_id, principal)
    _assessment(db, matter_id, assessment_id)
    return list(
        db.scalars(
            select(MatterDefinitionAssessmentQuery)
            .where(MatterDefinitionAssessmentQuery.assessment_run_id == assessment_id)
            .order_by(MatterDefinitionAssessmentQuery.ordinal)
        )
    )


@router.get("/{assessment_id}/questions", response_model=list[MatterDefinitionAssessmentQuestionRead])
def list_assessment_questions(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterDefinitionAssessmentQuestion]:
    _matter(db, matter_id, principal)
    _assessment(db, matter_id, assessment_id)
    return list(
        db.scalars(
            select(MatterDefinitionAssessmentQuestion)
            .where(MatterDefinitionAssessmentQuestion.assessment_run_id == assessment_id)
            .order_by(MatterDefinitionAssessmentQuestion.created_at, MatterDefinitionAssessmentQuestion.id)
        )
    )


@router.put(
    "/{assessment_id}/questions/{question_id}", response_model=MatterDefinitionAssessmentQuestionRead
)
def update_assessment_question(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    question_id: uuid.UUID,
    payload: MatterDefinitionAssessmentQuestionUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentQuestion:
    matter = _matter(db, matter_id, principal)
    _assessment(db, matter_id, assessment_id)
    question = db.scalar(
        select(MatterDefinitionAssessmentQuestion).where(
            MatterDefinitionAssessmentQuestion.id == question_id,
            MatterDefinitionAssessmentQuestion.assessment_run_id == assessment_id,
        )
    )
    if question is None:
        raise HTTPException(status_code=404, detail="Assessment question not found")
    question.status = payload.status
    question.answer = payload.answer.strip() if payload.answer else None
    question.answered_by_user_id = principal.user.id
    question.answered_at = utcnow()
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_definition.assessment_question.updated",
        target_type="matter_definition_assessment_question",
        target_id=question.id,
        details={"assessment_id": str(assessment_id), "status": question.status},
    )
    db.commit()
    db.refresh(question)
    return question
