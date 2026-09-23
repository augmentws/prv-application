import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.matter_definition_assessments import (
    AssessmentError,
    regenerate_assessment_document_analyses,
    regenerate_assessment_synthesis,
    retry_assessment,
    start_assessment,
    utcnow,
)
from app.models import (
    Matter,
    MatterDefinitionAssessmentQuery,
    MatterDefinitionAssessmentQuestion,
    MatterDefinitionAssessmentRun,
    ReviewBatchRunDocument,
    SkillRun,
    WorkflowRun,
    WorkflowStepRun,
)
from app.schemas import (
    MatterDefinitionAssessmentCreate,
    MatterDefinitionAssessmentQueryRead,
    MatterDefinitionAssessmentQuestionRead,
    MatterDefinitionAssessmentQuestionUpdate,
    MatterDefinitionAssessmentRead,
    SkillRunExecutionRead,
    WorkflowExecutionRead,
    WorkflowStepExecutionRead,
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


def _assessment_reads(
    db: Session,
    assessments: list[MatterDefinitionAssessmentRun],
) -> list[MatterDefinitionAssessmentRead]:
    run_ids = [assessment.review_batch_run_id for assessment in assessments if assessment.review_batch_run_id]
    live_counts: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
    if run_ids:
        rows = db.execute(
            select(
                ReviewBatchRunDocument.review_batch_run_id,
                ReviewBatchRunDocument.status,
                func.count(),
            )
            .where(ReviewBatchRunDocument.review_batch_run_id.in_(run_ids))
            .group_by(ReviewBatchRunDocument.review_batch_run_id, ReviewBatchRunDocument.status)
        )
        for run_id, document_status, count in rows:
            live_counts[run_id][document_status] = int(count)

    results: list[MatterDefinitionAssessmentRead] = []
    for assessment in assessments:
        result = MatterDefinitionAssessmentRead.model_validate(assessment)
        if assessment.review_batch_run_id in live_counts:
            counts = live_counts[assessment.review_batch_run_id]
            result = result.model_copy(
                update={
                    "summarized_count": counts.get("COMPLETED", 0),
                    "skipped_count": counts.get("SKIPPED", 0),
                    "failed_count": counts.get("FAILED", 0),
                }
            )
        results.append(result)
    return results


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
            name=payload.name,
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


@router.post("/{assessment_id}/retry", response_model=MatterDefinitionAssessmentRead, status_code=status.HTTP_202_ACCEPTED)
def retry_failed_assessment(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentRun:
    matter = _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    try:
        retry_assessment(db, assessment, initiated_by_user_id=principal.user.id)
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=principal.user.id,
            action="matter_definition.assessment.retried",
            target_type="matter_definition_assessment_run",
            target_id=assessment.id,
            details={"matter_id": str(matter.id)},
        )
        db.commit()
    except AssessmentError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(assessment)
    return assessment


@router.post(
    "/{assessment_id}/regenerate-synthesis",
    response_model=MatterDefinitionAssessmentRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def regenerate_synthesis(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentRun:
    matter = _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    try:
        regenerate_assessment_synthesis(db, assessment, initiated_by_user_id=principal.user.id)
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=principal.user.id,
            action="matter_definition.assessment.synthesis_regenerated",
            target_type="matter_definition_assessment_run",
            target_id=assessment.id,
            details={"matter_id": str(matter.id)},
        )
        db.commit()
    except AssessmentError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(assessment)
    return assessment


@router.post(
    "/{assessment_id}/regenerate-document-analyses",
    response_model=MatterDefinitionAssessmentRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def regenerate_document_analyses(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentRun:
    matter = _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    try:
        regenerate_assessment_document_analyses(db, assessment, initiated_by_user_id=principal.user.id)
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=principal.user.id,
            action="matter_definition.assessment.document_analyses_regenerated",
            target_type="matter_definition_assessment_run",
            target_id=assessment.id,
            details={
                "matter_id": str(matter.id),
                "review_batch_id": str(assessment.review_batch_id),
                "review_batch_run_id": str(assessment.review_batch_run_id),
            },
        )
        db.commit()
    except AssessmentError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(assessment)
    return assessment


@router.get("", response_model=list[MatterDefinitionAssessmentRead])
def list_assessments(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterDefinitionAssessmentRead]:
    _matter(db, matter_id, principal)
    assessments = list(
        db.scalars(
            select(MatterDefinitionAssessmentRun)
            .where(MatterDefinitionAssessmentRun.matter_id == matter_id)
            .order_by(MatterDefinitionAssessmentRun.created_at.desc())
        )
    )
    return _assessment_reads(db, assessments)


@router.get("/{assessment_id}", response_model=MatterDefinitionAssessmentRead)
def get_assessment(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentRead:
    _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    return _assessment_reads(db, [assessment])[0]


@router.get("/{assessment_id}/execution", response_model=WorkflowExecutionRead)
def get_assessment_execution(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    include_skill_runs: bool = True,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> WorkflowExecutionRead:
    _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    workflow = db.get(WorkflowRun, assessment.workflow_run_id)
    if workflow is None:
        raise HTTPException(status_code=409, detail="Assessment workflow record is unavailable")
    steps = list(
        db.scalars(
            select(WorkflowStepRun)
            .where(WorkflowStepRun.workflow_run_id == workflow.id)
            .order_by(WorkflowStepRun.ordinal)
        )
    )
    skill_runs = (
        list(
            db.scalars(
                select(SkillRun)
                .where(SkillRun.workflow_run_id == workflow.id)
                .order_by(SkillRun.created_at, SkillRun.id)
            )
        )
        if include_skill_runs
        else []
    )
    return WorkflowExecutionRead(
        id=workflow.id,
        workflow_key=workflow.workflow_key,
        code_version=workflow.code_version,
        status=workflow.status,
        request_count=workflow.request_count,
        input_tokens=workflow.input_tokens,
        cached_input_tokens=workflow.cached_input_tokens,
        cache_write_tokens=workflow.cache_write_tokens,
        output_tokens=workflow.output_tokens,
        error_message=workflow.error_message,
        started_at=workflow.started_at,
        completed_at=workflow.completed_at,
        steps=[WorkflowStepExecutionRead.model_validate(step) for step in steps],
        skill_runs=[SkillRunExecutionRead.model_validate(run) for run in skill_runs],
    )


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
