import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.assessment_guidance_refinement import queue_guidance_refinement
from app.assessment_provider_batches import cancel_provider_batches
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
    MatterDefinition,
    MatterDefinitionAssessmentQuery,
    MatterDefinitionAssessmentQuestion,
    MatterDefinitionAssessmentRun,
    MatterDefinitionRevision,
    ReviewBatch,
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
    MatterDefinitionAssessmentUpdate,
    SkillRunExecutionRead,
    WorkflowExecutionRead,
    WorkflowStepExecutionRead,
)
from app.workflows.dispatcher import cancel_definition_assessment

router = APIRouter(prefix="/v1/matters/{matter_id}/definition-assessments", tags=["matter definition assessments"])
guidance_router = APIRouter(
    prefix="/v1/matters/{matter_id}/guidance/{guidance_id}/assessments",
    tags=["review guidance assessments"],
)

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


def _guidance_audit_details(
    db: Session,
    assessment: MatterDefinitionAssessmentRun,
) -> dict[str, str | int]:
    revision = db.get(MatterDefinitionRevision, assessment.matter_definition_revision_id)
    definition = db.get(MatterDefinition, revision.matter_definition_id) if revision else None
    if revision is None or definition is None:
        return {"matter_definition_revision_id": str(assessment.matter_definition_revision_id)}
    return {
        "guidance_id": str(definition.id),
        "guidance_key": definition.key,
        "revision": revision.revision,
        "matter_definition_revision_id": str(revision.id),
    }


def _assessment_reads(
    db: Session,
    assessments: list[MatterDefinitionAssessmentRun],
) -> list[MatterDefinitionAssessmentRead]:
    revision_ids = (
        {assessment.matter_definition_revision_id for assessment in assessments}
        if hasattr(db, "scalars")
        else set()
    )
    revisions = {
        revision.id: revision
        for revision in db.scalars(
            select(MatterDefinitionRevision).where(MatterDefinitionRevision.id.in_(revision_ids))
        )
    } if revision_ids else {}
    guidance_ids = {revision.matter_definition_id for revision in revisions.values()}
    guidance = {
        definition.id: definition
        for definition in db.scalars(
            select(MatterDefinition).where(MatterDefinition.id.in_(guidance_ids))
        )
    } if guidance_ids else {}
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
        revision = revisions.get(assessment.matter_definition_revision_id)
        definition = guidance.get(revision.matter_definition_id) if revision else None
        if definition is not None:
            result = result.model_copy(
                update={
                    "guidance_id": definition.id,
                    "guidance_key": definition.key,
                    "guidance_name": definition.name,
                }
            )
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


def _create_assessment_for_guidance(
    *,
    db: Session,
    matter: Matter,
    guidance_id: uuid.UUID | None,
    payload: MatterDefinitionAssessmentCreate,
    principal: Principal,
    settings: Settings,
) -> MatterDefinitionAssessmentRun:
    try:
        assessment = start_assessment(
            db,
            matter=matter,
            matter_definition_id=guidance_id,
            initiated_by_user_id=principal.user.id,
            settings=settings,
            name=payload.name,
            revision_number=payload.revision,
            target_document_count=payload.target_document_count,
            control_sample_size=payload.control_sample_size,
            use_batching=payload.use_batching,
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


@guidance_router.post(
    "",
    response_model=MatterDefinitionAssessmentRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_guidance_assessment(
    matter_id: uuid.UUID,
    guidance_id: uuid.UUID,
    payload: MatterDefinitionAssessmentCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterDefinitionAssessmentRead:
    matter = _matter(db, matter_id, principal)
    assessment = _create_assessment_for_guidance(
        db=db,
        matter=matter,
        guidance_id=guidance_id,
        payload=payload,
        principal=principal,
        settings=settings,
    )
    return _assessment_reads(db, [assessment])[0]


@guidance_router.get("", response_model=list[MatterDefinitionAssessmentRead])
def list_guidance_assessments(
    matter_id: uuid.UUID,
    guidance_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterDefinitionAssessmentRead]:
    _matter(db, matter_id, principal)
    guidance_exists = db.scalar(
        select(MatterDefinition.id).where(
            MatterDefinition.id == guidance_id,
            MatterDefinition.matter_id == matter_id,
        )
    )
    if guidance_exists is None:
        raise HTTPException(status_code=404, detail="Review Guidance not found")
    assessments = list(
        db.scalars(
            select(MatterDefinitionAssessmentRun)
            .join(
                MatterDefinitionRevision,
                MatterDefinitionRevision.id
                == MatterDefinitionAssessmentRun.matter_definition_revision_id,
            )
            .where(
                MatterDefinitionAssessmentRun.matter_id == matter_id,
                MatterDefinitionRevision.matter_definition_id == guidance_id,
            )
            .order_by(MatterDefinitionAssessmentRun.created_at.desc())
        )
    )
    return _assessment_reads(db, assessments)


@router.post("", response_model=MatterDefinitionAssessmentRead, status_code=status.HTTP_202_ACCEPTED)
def create_assessment(
    matter_id: uuid.UUID,
    payload: MatterDefinitionAssessmentCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterDefinitionAssessmentRead:
    matter = _matter(db, matter_id, principal)
    assessment = _create_assessment_for_guidance(
        db=db,
        matter=matter,
        guidance_id=None,
        payload=payload,
        principal=principal,
        settings=settings,
    )
    return _assessment_reads(db, [assessment])[0]


@router.patch("/{assessment_id}", response_model=MatterDefinitionAssessmentRead)
def update_assessment(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    payload: MatterDefinitionAssessmentUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentRead:
    matter = _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    previous_name = assessment.name
    assessment.name = payload.name
    batch_renamed = False
    if assessment.review_batch_id is not None:
        batch = db.get(ReviewBatch, assessment.review_batch_id)
        if batch is not None and batch.name == previous_name:
            batch.name = payload.name
            batch_renamed = True
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_definition.assessment.renamed",
        target_type="matter_definition_assessment_run",
        target_id=assessment.id,
        details={
            "matter_id": str(matter.id),
            **_guidance_audit_details(db, assessment),
            "previous_name": previous_name,
            "name": assessment.name,
            "review_batch_renamed": batch_renamed,
        },
    )
    db.commit()
    db.refresh(assessment)
    return _assessment_reads(db, [assessment])[0]


@router.post("/{assessment_id}/retry", response_model=MatterDefinitionAssessmentRead, status_code=status.HTTP_202_ACCEPTED)
def retry_failed_assessment(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentRead:
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
            details={"matter_id": str(matter.id), **_guidance_audit_details(db, assessment)},
        )
        db.commit()
    except AssessmentError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(assessment)
    return _assessment_reads(db, [assessment])[0]


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
) -> MatterDefinitionAssessmentRead:
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
            details={"matter_id": str(matter.id), **_guidance_audit_details(db, assessment)},
        )
        db.commit()
    except AssessmentError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(assessment)
    return _assessment_reads(db, [assessment])[0]


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
) -> MatterDefinitionAssessmentRead:
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
                **_guidance_audit_details(db, assessment),
                "review_batch_id": str(assessment.review_batch_id),
                "review_batch_run_id": str(assessment.review_batch_run_id),
            },
        )
        db.commit()
    except AssessmentError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(assessment)
    return _assessment_reads(db, [assessment])[0]


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
) -> MatterDefinitionAssessmentRead:
    matter = _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    if assessment.status in TERMINAL_STATUSES:
        return _assessment_reads(db, [assessment])[0]
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
        details={"matter_id": str(matter.id), **_guidance_audit_details(db, assessment)},
    )
    db.commit()
    cancel_provider_batches(db, assessment.id)
    if workflow is not None:
        cancel_definition_assessment(workflow.dbos_workflow_id)
    db.refresh(assessment)
    return _assessment_reads(db, [assessment])[0]


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
    try:
        queue_guidance_refinement(
            db,
            assessment_id,
            initiated_by_user_id=principal.user.id,
        )
    except ValueError as exc:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        if assessment is not None:
            assessment.guidance_refinement_status = "FAILED"
            assessment.guidance_refinement_error_message = str(exc)[:4000]
    db.commit()
    db.refresh(question)
    return question
