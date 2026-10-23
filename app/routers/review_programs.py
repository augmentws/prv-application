import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.assessment_provider_batches import cancel_provider_batches
from app.authorization import ResourceRef, require
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.models import Matter, ReviewProgram, ReviewProgramRun
from app.review_programs import (
    ReviewProgramError,
    cancel_review_program_discovery_children,
    cancel_review_program_run,
    create_review_program,
    current_review_program_version,
    get_review_program,
    get_review_program_run,
    reconcile_review_program_run,
    resolve_review_program_action,
    retry_review_program_run,
    review_program_action_decision_context,
    review_program_assessment_cancellation_targets,
    review_program_run_details,
    setup_review_program_classifier_improvement,
    setup_review_program_definition,
    setup_review_program_discovery,
    setup_review_program_qa_task,
    start_review_program_run,
)
from app.review_workflow_templates import (
    REVIEW_WORKFLOW_TEMPLATES,
    ReviewWorkflowTemplateError,
    ReviewWorkflowTemplateSpec,
)
from app.schemas import (
    ReviewProgramActionRead,
    ReviewProgramActionResolve,
    ReviewProgramClassifierImprovementSetup,
    ReviewProgramCreate,
    ReviewProgramDefinitionSetup,
    ReviewProgramDiscoverySetup,
    ReviewProgramEventRead,
    ReviewProgramQASetup,
    ReviewProgramRead,
    ReviewProgramRunCommand,
    ReviewProgramRunRead,
    ReviewProgramRunStart,
    ReviewProgramStageAttemptRead,
    ReviewProgramVersionRead,
    ReviewWorkflowTemplateRead,
)
from app.workflows.dispatcher import (
    cancel_analysis_task_batch,
    cancel_analysis_task_batch_set_run_workflow,
    cancel_analysis_task_batch_set_workflow,
    cancel_classification_training_job,
    cancel_definition_assessment,
    cancel_review_batch,
)

template_router = APIRouter(prefix="/v1/review-workflow-templates", tags=["review programs"])
router = APIRouter(prefix="/v1/matters/{matter_id}/review-programs", tags=["review programs"])


def _template_read(template: ReviewWorkflowTemplateSpec) -> ReviewWorkflowTemplateRead:
    return ReviewWorkflowTemplateRead.model_validate(
        {
            **template.contract(),
            "content_hash": template.content_hash,
        }
    )


def _program_read(db: Session, program: ReviewProgram) -> ReviewProgramRead:
    version = current_review_program_version(db, program)
    return ReviewProgramRead.model_validate(program).model_copy(
        update={"current_version": ReviewProgramVersionRead.model_validate(version)}
    )


def _run_read(db: Session, run: ReviewProgramRun) -> ReviewProgramRunRead:
    attempts, actions, events = review_program_run_details(db, run)
    action_reads = [
        ReviewProgramActionRead.model_validate(item).model_copy(
            update={
                "decision_context": review_program_action_decision_context(
                    db,
                    run=run,
                    action=item,
                )
            }
        )
        for item in actions
    ]
    return ReviewProgramRunRead.model_validate(run).model_copy(
        update={
            "stage_attempts": [ReviewProgramStageAttemptRead.model_validate(item) for item in attempts],
            "actions": action_reads,
            "events": [ReviewProgramEventRead.model_validate(item) for item in events],
        }
    )


def _matter_with_permission(
    db: Session,
    principal: Principal,
    matter_id: uuid.UUID,
    permission_key: str,
) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    require(db, principal, permission_key, ResourceRef.matter(matter_id))
    return matter


def _http_error(exc: ReviewProgramError | ReviewWorkflowTemplateError) -> HTTPException:
    if "not found" in str(exc).lower():
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@template_router.get("", response_model=list[ReviewWorkflowTemplateRead])
def list_review_workflow_templates(
    _: Principal = Depends(get_principal),
) -> list[ReviewWorkflowTemplateRead]:
    return [_template_read(template) for template in REVIEW_WORKFLOW_TEMPLATES.values()]


@router.get("", response_model=list[ReviewProgramRead])
def list_review_programs(
    matter_id: uuid.UUID,
    include_archived: bool = False,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ReviewProgramRead]:
    _matter_with_permission(db, principal, matter_id, "review_program.view")
    statement = select(ReviewProgram).where(ReviewProgram.matter_id == matter_id)
    if not include_archived:
        statement = statement.where(ReviewProgram.status != "ARCHIVED")
    programs = list(db.scalars(statement.order_by(ReviewProgram.created_at, ReviewProgram.id)))
    return [_program_read(db, program) for program in programs]


@router.post("", response_model=ReviewProgramRead, status_code=status.HTTP_201_CREATED)
def create_program(
    matter_id: uuid.UUID,
    payload: ReviewProgramCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRead:
    matter = _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program, _ = create_review_program(
            db,
            matter=matter,
            actor=principal.user,
            template_key=payload.template_key,
            key=payload.key,
            name=payload.name,
            description=payload.description,
            owner_user_id=payload.owner_user_id,
            matter_definition_id=payload.matter_definition_id,
        )
        db.commit()
    except (ReviewProgramError, ReviewWorkflowTemplateError) as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The Review Program conflicts with an existing record",
        ) from exc
    db.refresh(program)
    return _program_read(db, program)


@router.get("/{program_id}", response_model=ReviewProgramRead)
def read_review_program(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRead:
    _matter_with_permission(db, principal, matter_id, "review_program.view")
    try:
        return _program_read(db, get_review_program(db, matter_id=matter_id, program_id=program_id))
    except ReviewProgramError as exc:
        raise _http_error(exc) from exc


@router.get("/{program_id}/runs", response_model=list[ReviewProgramRunRead])
def list_review_program_runs(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ReviewProgramRunRead]:
    _matter_with_permission(db, principal, matter_id, "review_program.view")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id)
    except ReviewProgramError as exc:
        raise _http_error(exc) from exc
    runs = list(
        db.scalars(
            select(ReviewProgramRun)
            .where(ReviewProgramRun.program_id == program.id)
            .order_by(ReviewProgramRun.created_at.desc(), ReviewProgramRun.id.desc())
        )
    )
    return [_run_read(db, run) for run in runs]


@router.post("/{program_id}/runs", response_model=ReviewProgramRunRead, status_code=status.HTTP_201_CREATED)
def start_program_run(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    payload: ReviewProgramRunStart,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = start_review_program_run(
            db,
            program=program,
            actor=principal.user,
            idempotency_key=payload.idempotency_key,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The Review Program run conflicts with an existing run",
        ) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.get("/{program_id}/runs/{run_id}", response_model=ReviewProgramRunRead)
def read_program_run(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.view")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id)
        return _run_read(db, get_review_program_run(db, program=program, run_id=run_id))
    except ReviewProgramError as exc:
        raise _http_error(exc) from exc


@router.post("/{program_id}/runs/{run_id}/actions/{action_id}/resolve", response_model=ReviewProgramRunRead)
def resolve_program_action(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: ReviewProgramActionResolve,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    assessment_cancellations: list[tuple[uuid.UUID, str | None]] = []
    discovery_batch_set_workflows: list[str] = []
    discovery_batch_run_workflows: list[str] = []
    discovery_child_workflows: list[str] = []
    difference_batch_workflows: list[str] = []
    classification_training_workflows: list[str] = []
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        if payload.choice_key == "STOP":
            assessment_cancellations = [
                (assessment.id, workflow.dbos_workflow_id if workflow is not None else None)
                for assessment, workflow in review_program_assessment_cancellation_targets(db, run)
            ]
            (
                discovery_batch_set_workflows,
                discovery_batch_run_workflows,
                discovery_child_workflows,
                difference_batch_workflows,
                classification_training_workflows,
            ) = cancel_review_program_discovery_children(
                db,
                run=run,
                actor_user_id=principal.user.id,
            )
        resolve_review_program_action(
            db,
            program=program,
            run=run,
            action_id=action_id,
            actor=principal.user,
            preview_hash=payload.preview_hash,
            choice_key=payload.choice_key,
            reason=payload.reason,
            input_payload=payload.input,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    for assessment_id, dbos_workflow_id in assessment_cancellations:
        cancel_provider_batches(db, assessment_id)
        if dbos_workflow_id:
            cancel_definition_assessment(dbos_workflow_id)
    for workflow_id in discovery_batch_set_workflows:
        cancel_analysis_task_batch_set_workflow(workflow_id)
    for workflow_id in discovery_batch_run_workflows:
        cancel_analysis_task_batch_set_run_workflow(workflow_id)
    for workflow_id in discovery_child_workflows:
        cancel_analysis_task_batch(workflow_id)
    for workflow_id in difference_batch_workflows:
        cancel_review_batch(workflow_id)
    for workflow_id in classification_training_workflows:
        cancel_classification_training_job(workflow_id)
    db.refresh(run)
    return _run_read(db, run)


@router.post(
    "/{program_id}/runs/{run_id}/actions/{action_id}/definition-setup",
    response_model=ReviewProgramRunRead,
)
def setup_program_definition(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: ReviewProgramDefinitionSetup,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        setup_review_program_definition(
            db,
            program=program,
            run=run,
            action_id=action_id,
            actor=principal.user,
            preview_hash=payload.preview_hash,
            mode=payload.mode,
            existing_definition_id=payload.existing_definition_id,
            source_revision=payload.source_revision,
            definition_key=payload.definition_key,
            name=payload.name,
            description=payload.description,
            content_markdown=payload.content_markdown,
            assessment_target_document_count=payload.assessment_target_document_count,
            assessment_control_sample_size=payload.assessment_control_sample_size,
            assessment_use_batching=payload.assessment_use_batching,
            acknowledge_large_run_warning=payload.acknowledge_large_run_warning,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Matter Definition setup conflicts with an existing record",
        ) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.post(
    "/{program_id}/runs/{run_id}/actions/{action_id}/qa-setup",
    response_model=ReviewProgramRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def setup_program_qa_task(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: ReviewProgramQASetup,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        setup_review_program_qa_task(
            db,
            program=program,
            run=run,
            action_id=action_id,
            actor=principal.user,
            preview_hash=payload.preview_hash,
            task_key=payload.task_key,
            name=payload.name,
            description=payload.description,
            destination_metadata_definition_ids=payload.destination_metadata_definition_ids,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Q&A task setup conflicts with an existing record",
        ) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.post(
    "/{program_id}/runs/{run_id}/actions/{action_id}/discovery-setup",
    response_model=ReviewProgramRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def setup_program_discovery(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: ReviewProgramDiscoverySetup,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        setup_review_program_discovery(
            db,
            program=program,
            run=run,
            action_id=action_id,
            actor=principal.user,
            preview_hash=payload.preview_hash,
            target_document_count=payload.target_document_count,
            control_sample_size=payload.control_sample_size,
            target_batch_size=payload.target_batch_size,
            keep_families_together=payload.keep_families_together,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Discovery Q&A setup conflicts with an existing record",
        ) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.post(
    "/{program_id}/runs/{run_id}/actions/{action_id}/classifier-improvement-setup",
    response_model=ReviewProgramRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def setup_program_classifier_improvement(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: ReviewProgramClassifierImprovementSetup,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        setup_review_program_classifier_improvement(
            db,
            program=program,
            run=run,
            action_id=action_id,
            actor=principal.user,
            preview_hash=payload.preview_hash,
            queries=[query.model_dump(mode="json") for query in payload.queries],
            control_sample_size=payload.control_sample_size,
            target_batch_size=payload.target_batch_size,
            keep_families_together=payload.keep_families_together,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Classifier improvement setup conflicts with an existing record",
        ) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.post("/{program_id}/runs/{run_id}/cancel", response_model=ReviewProgramRunRead)
def cancel_program_run(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    payload: ReviewProgramRunCommand,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        assessment_cancellations = [
            (assessment.id, workflow.dbos_workflow_id if workflow is not None else None)
            for assessment, workflow in review_program_assessment_cancellation_targets(db, run)
        ]
        (
            discovery_batch_set_workflows,
            discovery_batch_run_workflows,
            discovery_child_workflows,
            difference_batch_workflows,
            classification_training_workflows,
        ) = cancel_review_program_discovery_children(
            db,
            run=run,
            actor_user_id=principal.user.id,
        )
        cancel_review_program_run(
            db,
            program=program,
            run=run,
            actor=principal.user,
            idempotency_key=payload.idempotency_key,
            reason=payload.reason,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    for assessment_id, dbos_workflow_id in assessment_cancellations:
        cancel_provider_batches(db, assessment_id)
        if dbos_workflow_id:
            cancel_definition_assessment(dbos_workflow_id)
    for workflow_id in discovery_batch_set_workflows:
        cancel_analysis_task_batch_set_workflow(workflow_id)
    for workflow_id in discovery_batch_run_workflows:
        cancel_analysis_task_batch_set_run_workflow(workflow_id)
    for workflow_id in discovery_child_workflows:
        cancel_analysis_task_batch(workflow_id)
    for workflow_id in difference_batch_workflows:
        cancel_review_batch(workflow_id)
    for workflow_id in classification_training_workflows:
        cancel_classification_training_job(workflow_id)
    db.refresh(run)
    return _run_read(db, run)


@router.post("/{program_id}/runs/{run_id}/retry", response_model=ReviewProgramRunRead)
def retry_program_run(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    payload: ReviewProgramRunCommand,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        retry_review_program_run(
            db,
            program=program,
            run=run,
            actor=principal.user,
            idempotency_key=payload.idempotency_key,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.post("/{program_id}/runs/{run_id}/reconcile", response_model=ReviewProgramRunRead)
def reconcile_program_run(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        reconcile_review_program_run(db, program=program, run=run, actor=principal.user)
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    db.refresh(run)
    return _run_read(db, run)
