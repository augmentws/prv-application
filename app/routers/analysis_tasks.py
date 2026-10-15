import logging
import uuid
from typing import Never

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.analysis_task_compilation import queue_analysis_task_compilation
from app.analysis_task_playground import queue_analysis_task_playground
from app.analysis_tasks import (
    MatterAnalysisTaskError,
    MatterAnalysisTaskNotFound,
    append_analysis_task_version,
    create_analysis_task,
    publish_analysis_task_version,
    set_analysis_task_specification,
)
from app.audit import record_audit
from app.classification_analysis_tasks import (
    ClassificationAnalysisTaskError,
    cancel_classification_analysis_run,
    create_classification_analysis_run,
)
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.job_archives import job_not_archived
from app.job_executions import ANALYSIS_TASK_BATCH_RUN_JOB_TYPE, CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE
from app.models import (
    ClassificationModelVersion,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskRun,
    MatterAnalysisTaskVersion,
    ReviewBatch,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewDecisionResult,
    SkillDefinitionVersion,
    SkillRun,
    WorkflowRun,
)
from app.schemas import (
    JobExecutionRead,
    MatterAnalysisTaskCreate,
    MatterAnalysisTaskPlaygroundCreate,
    MatterAnalysisTaskPlaygroundRunRead,
    MatterAnalysisTaskRead,
    MatterAnalysisTaskRunCreate,
    MatterAnalysisTaskRunRead,
    MatterAnalysisTaskSpecificationUpdate,
    MatterAnalysisTaskVersionCreate,
    MatterAnalysisTaskVersionRead,
    MatterQuestionAnsweringJobRead,
)
from app.workflow_specs import MATTER_ANALYSIS_TASK_PLAYGROUND_SPEC
from app.workflows.dispatcher import (
    cancel_classification_analysis_task,
    cancel_classification_model_scoring,
    enqueue_classification_analysis_task,
)

router = APIRouter(prefix="/v1/matters/{matter_id}/analysis-tasks", tags=["matter analysis tasks"])
logger = logging.getLogger(__name__)


def _require_matter_admin(db: Session, principal: Principal, matter_id: uuid.UUID) -> Matter:
    matter = db.scalar(select(Matter).where(Matter.id == matter_id))
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    return matter


def _require_task(db: Session, *, matter_id: uuid.UUID, task_id: uuid.UUID) -> MatterAnalysisTask:
    task = db.scalar(
        select(MatterAnalysisTask).where(
            MatterAnalysisTask.id == task_id,
            MatterAnalysisTask.matter_id == matter_id,
        )
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter Analysis Task not found")
    return task


def _task_version(
    db: Session,
    *,
    task_id: uuid.UUID,
    version_number: int,
) -> MatterAnalysisTaskVersion:
    task_version = db.scalar(
        select(MatterAnalysisTaskVersion).where(
            MatterAnalysisTaskVersion.matter_analysis_task_id == task_id,
            MatterAnalysisTaskVersion.version == version_number,
        )
    )
    if task_version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter Analysis Task version not found")
    return task_version


def _task_read(
    db: Session,
    task: MatterAnalysisTask,
    version: MatterAnalysisTaskVersion | None = None,
) -> MatterAnalysisTaskRead:
    current = version or _task_version(db, task_id=task.id, version_number=task.current_version)
    published = (
        None
        if task.published_version is None
        else current
        if task.published_version == current.version
        else _task_version(db, task_id=task.id, version_number=task.published_version)
    )
    return MatterAnalysisTaskRead(
        id=task.id,
        matter_id=task.matter_id,
        key=task.key,
        name=task.name,
        description=task.description,
        task_type=task.task_type,
        workflow_key=task.workflow_key,
        current_version=task.current_version,
        published_version=task.published_version,
        status=task.status,
        created_by_user_id=task.created_by_user_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
        version=_task_version_read(db, current),
        published_version_details=_task_version_read(db, published) if published is not None else None,
    )


def _task_version_read(
    db: Session,
    version: MatterAnalysisTaskVersion,
) -> MatterAnalysisTaskVersionRead:
    progress: dict = {}
    if version.compiler_workflow_run_id is not None:
        workflow = db.get(WorkflowRun, version.compiler_workflow_run_id)
        if workflow is not None:
            progress = workflow.progress or {}
    embedding_job_id: uuid.UUID | None = None
    if version.classification_model_version_id is not None:
        model_version = db.get(ClassificationModelVersion, version.classification_model_version_id)
        raw_embedding_job_id = (
            (model_version.dataset_snapshot.feature_specification or {}).get("embedding_job_id")
            if model_version is not None and model_version.dataset_snapshot is not None
            else None
        )
        if raw_embedding_job_id:
            embedding_job_id = uuid.UUID(str(raw_embedding_job_id))
    return MatterAnalysisTaskVersionRead.model_validate(version).model_copy(
        update={
            "compilation_progress": progress,
            "classification_embedding_job_id": embedding_job_id,
        }
    )


def _raise_task_error(exc: MatterAnalysisTaskError) -> Never:
    if isinstance(exc, MatterAnalysisTaskNotFound):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _playground_run_read(
    workflow: WorkflowRun,
    review_run: ReviewBatchRun,
    result: ReviewDecisionResult | None = None,
) -> MatterAnalysisTaskPlaygroundRunRead:
    snapshot = workflow.input_snapshot
    return MatterAnalysisTaskPlaygroundRunRead(
        workflow_run_id=workflow.id,
        review_batch_id=review_run.review_batch_id,
        review_batch_run_id=review_run.id,
        matter_document_id=uuid.UUID(str(snapshot["matter_document_id"])),
        task_version_id=uuid.UUID(str(snapshot["task_version_id"])),
        status=workflow.status,
        result_id=result.id if result is not None else None,
        error_message=workflow.error_message,
    )


@router.get("", response_model=list[MatterAnalysisTaskRead])
def list_analysis_tasks(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterAnalysisTaskRead]:
    _require_matter_admin(db, principal, matter_id)
    tasks = db.scalars(
        select(MatterAnalysisTask)
        .where(MatterAnalysisTask.matter_id == matter_id)
        .order_by(MatterAnalysisTask.created_at, MatterAnalysisTask.id)
    ).all()
    return [_task_read(db, task) for task in tasks]


@router.get("/question-answering-jobs", response_model=list[MatterQuestionAnsweringJobRead])
def list_question_answering_jobs(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterQuestionAnsweringJobRead]:
    _require_matter_admin(db, principal, matter_id)
    rows = db.execute(
        select(ReviewBatchRun, ReviewBatch)
        .join(ReviewBatch, ReviewBatch.id == ReviewBatchRun.review_batch_id)
        .where(
            ReviewBatch.matter_id == matter_id,
            ReviewBatchRun.run_type == "WORKFLOW",
            job_not_archived(ANALYSIS_TASK_BATCH_RUN_JOB_TYPE, ReviewBatchRun.id),
        )
        .order_by(ReviewBatchRun.created_at.desc(), ReviewBatchRun.id.desc())
    ).all()
    if not rows:
        return []

    run_ids = [run.id for run, _ in rows]
    executions_by_run_id: dict[uuid.UUID, WorkflowRun] = {}
    for execution in db.scalars(
        select(WorkflowRun)
        .where(
            WorkflowRun.job_type == ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
            WorkflowRun.job_id.in_(run_ids),
        )
        .order_by(WorkflowRun.attempt_number.desc(), WorkflowRun.created_at.desc())
    ):
        if execution.job_id is not None:
            executions_by_run_id.setdefault(execution.job_id, execution)
    outcome_counts = {
        run_id: (
            int(successful_count or 0),
            int(failed_count or 0),
            int(retryable_count or 0),
        )
        for run_id, successful_count, failed_count, retryable_count in db.execute(
            select(
                ReviewBatchRunDocument.review_batch_run_id,
                func.count().filter(ReviewBatchRunDocument.status == "COMPLETED"),
                func.count().filter(ReviewBatchRunDocument.status == "FAILED"),
                func.count().filter(
                    ReviewBatchRunDocument.status == "FAILED",
                    ReviewBatchRunDocument.failure_retriable.is_(True),
                ),
            )
            .where(
                ReviewBatchRunDocument.review_batch_run_id.in_(run_ids),
            )
            .group_by(ReviewBatchRunDocument.review_batch_run_id)
        )
    }

    jobs: list[MatterQuestionAnsweringJobRead] = []
    for run, batch in rows:
        execution = executions_by_run_id.get(run.id)
        snapshot = run.configuration_snapshot or {}
        workflow_input = execution.input_snapshot if execution is not None else {}
        raw_task_id = snapshot.get("task_id") or workflow_input.get("task_id")
        try:
            task_id = uuid.UUID(str(raw_task_id)) if raw_task_id is not None else None
        except ValueError:
            logger.warning("Question-answering run %s has an invalid task id", run.id)
            task_id = None
        raw_task_version = snapshot.get("task_version") or workflow_input.get("task_version")
        try:
            task_version = int(raw_task_version) if raw_task_version is not None else None
        except (TypeError, ValueError):
            logger.warning("Question-answering run %s has an invalid task version", run.id)
            task_version = None
        task_name = str(
            snapshot.get("task_name")
            or workflow_input.get("task_name")
            or "Question answering run"
        )
        provider = str(snapshot.get("decision_provider") or "JEV").upper()
        if provider not in {"JEV", "OPENAI_DECISIONS"}:
            provider = "JEV"
        jobs.append(
            MatterQuestionAnsweringJobRead(
                id=run.id,
                review_batch_id=batch.id,
                review_batch_name=batch.name,
                matter_analysis_task_id=task_id,
                task_name=task_name,
                task_version=task_version,
                decision_provider=provider,
                model_key=str(snapshot.get("model_key") or "unknown"),
                status=run.status,
                processed_document_count=run.processed_document_count,
                successful_document_count=outcome_counts.get(run.id, (0, 0, 0))[0],
                document_count=batch.document_count,
                failed_document_count=outcome_counts.get(run.id, (0, 0, 0))[1],
                retryable_failed_document_count=outcome_counts.get(run.id, (0, 0, 0))[2],
                error_message=run.error_message,
                started_at=run.started_at,
                completed_at=run.completed_at,
                created_at=run.created_at,
                updated_at=run.updated_at,
                execution=JobExecutionRead.model_validate(execution) if execution is not None else None,
            )
        )
    return jobs


@router.post("", response_model=MatterAnalysisTaskRead, status_code=status.HTTP_201_CREATED)
def create_matter_analysis_task(
    matter_id: uuid.UUID,
    payload: MatterAnalysisTaskCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterAnalysisTaskRead:
    matter = _require_matter_admin(db, principal, matter_id)
    try:
        task, task_version = create_analysis_task(
            db,
            matter=matter,
            actor_user_id=principal.user.id,
            key=payload.key,
            name=payload.name,
            description=payload.description,
            task_type=payload.task_type,
            decision_provider=payload.decision_provider,
            definition_markdown=payload.definition_markdown,
            destination_metadata_definition_ids=payload.destination_metadata_definition_ids,
            classification_model_version_id=payload.classification_model_version_id,
        )
    except MatterAnalysisTaskError as exc:
        _raise_task_error(exc)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Analysis task key already exists") from exc
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_analysis_task.created",
        target_type="matter_analysis_task",
        target_id=task.id,
        details={
            "matter_id": str(matter.id),
            "task_type": task.task_type,
            "version": 1,
            "decision_provider": task_version.decision_provider,
            "decision_model": task_version.decision_model,
        },
    )
    db.commit()
    db.refresh(task)
    db.refresh(task_version)
    return _task_read(db, task, task_version)


@router.get("/{task_id}", response_model=MatterAnalysisTaskRead)
def get_analysis_task(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterAnalysisTaskRead:
    _require_matter_admin(db, principal, matter_id)
    return _task_read(db, _require_task(db, matter_id=matter_id, task_id=task_id))


@router.get("/{task_id}/versions", response_model=list[MatterAnalysisTaskVersionRead])
def list_analysis_task_versions(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterAnalysisTaskVersionRead]:
    _require_matter_admin(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    versions = list(
        db.scalars(
            select(MatterAnalysisTaskVersion)
            .where(MatterAnalysisTaskVersion.matter_analysis_task_id == task.id)
            .order_by(MatterAnalysisTaskVersion.version.desc())
        )
    )
    return [_task_version_read(db, version) for version in versions]


@router.get("/{task_id}/runs", response_model=list[MatterAnalysisTaskRunRead])
def list_analysis_task_runs(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterAnalysisTaskRun]:
    _require_matter_admin(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    return list(
        db.scalars(
            select(MatterAnalysisTaskRun)
            .where(
                MatterAnalysisTaskRun.matter_analysis_task_id == task.id,
                job_not_archived(CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE, MatterAnalysisTaskRun.id),
            )
            .options(
                selectinload(MatterAnalysisTaskRun.dataset_snapshot),
                selectinload(MatterAnalysisTaskRun.current_workflow_run),
            )
            .order_by(MatterAnalysisTaskRun.created_at.desc(), MatterAnalysisTaskRun.id.desc())
        )
    )


@router.post("/{task_id}/runs", response_model=MatterAnalysisTaskRunRead, status_code=status.HTTP_202_ACCEPTED)
def start_analysis_task_run(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: MatterAnalysisTaskRunCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterAnalysisTaskRun:
    matter = _require_matter_admin(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    try:
        run, created = create_classification_analysis_run(
            db,
            matter=matter,
            task=task,
            payload=payload,
            actor_user_id=principal.user.id,
        )
        if created:
            enqueue_classification_analysis_task(db, run.workflow_id, str(run.id))
        db.commit()
        db.refresh(run)
        return run
    except ClassificationAnalysisTaskError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        existing = db.scalar(
            select(MatterAnalysisTaskRun).where(
                MatterAnalysisTaskRun.matter_analysis_task_id == task.id,
                MatterAnalysisTaskRun.idempotency_key == payload.idempotency_key,
            )
        )
        if existing is not None:
            return existing
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Analysis Task run conflicted") from exc


@router.post("/{task_id}/runs/{run_id}/cancel", response_model=MatterAnalysisTaskRunRead)
def cancel_analysis_task_run(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterAnalysisTaskRun:
    _require_matter_admin(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    run = db.scalar(
        select(MatterAnalysisTaskRun)
        .where(
            MatterAnalysisTaskRun.id == run_id,
            MatterAnalysisTaskRun.matter_id == matter_id,
            MatterAnalysisTaskRun.matter_analysis_task_id == task.id,
        )
        .with_for_update()
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis Task run not found")
    canceled = cancel_classification_analysis_run(
        db,
        run,
        actor_user_id=principal.user.id,
    )
    scoring_workflow_id = (
        run.classification_scoring_run.workflow_id
        if canceled and run.classification_scoring_run is not None
        else None
    )
    if canceled:
        record_audit(
            db,
            tenant_id=run.tenant_id,
            actor_user_id=principal.user.id,
            action="matter_analysis_task.classification_run.canceled",
            target_type="matter_analysis_task_run",
            target_id=run.id,
            details={
                "matter_id": str(run.matter_id),
                "task_id": str(task.id),
                "classification_scoring_run_id": (
                    str(run.classification_scoring_run_id)
                    if run.classification_scoring_run_id is not None
                    else None
                ),
            },
        )
    db.commit()
    if canceled:
        try:
            cancel_classification_analysis_task(run.workflow_id)
            if scoring_workflow_id is not None:
                cancel_classification_model_scoring(scoring_workflow_id)
        except Exception as exc:  # noqa: BLE001 - the Core cancellation remains authoritative
            logger.warning("DBOS cancellation request failed for %s: %s", run.workflow_id, exc)
    db.refresh(run)
    return run


@router.post("/{task_id}/versions", response_model=MatterAnalysisTaskRead, status_code=status.HTTP_201_CREATED)
def create_analysis_task_version(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: MatterAnalysisTaskVersionCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterAnalysisTaskRead:
    matter = _require_matter_admin(db, principal, matter_id)
    _require_task(db, matter_id=matter_id, task_id=task_id)
    try:
        task, task_version = append_analysis_task_version(
            db,
            task_id=task_id,
            actor_user_id=principal.user.id,
            definition_markdown=payload.definition_markdown,
            based_on_version=payload.based_on_version,
            decision_provider=payload.decision_provider,
            destination_metadata_definition_ids=payload.destination_metadata_definition_ids,
            classification_model_version_id=payload.classification_model_version_id,
        )
    except MatterAnalysisTaskError as exc:
        _raise_task_error(exc)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_analysis_task.version.created",
        target_type="matter_analysis_task",
        target_id=task.id,
        details={
            "matter_id": str(matter.id),
            "version": task_version.version,
            "decision_provider": task_version.decision_provider,
            "decision_model": task_version.decision_model,
        },
    )
    db.commit()
    db.refresh(task)
    db.refresh(task_version)
    return _task_read(db, task, task_version)


@router.put("/{task_id}/versions/{version_number}/specification", response_model=MatterAnalysisTaskRead)
def update_analysis_task_specification(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    version_number: int,
    payload: MatterAnalysisTaskSpecificationUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterAnalysisTaskRead:
    matter = _require_matter_admin(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    if task.task_type != "QUESTION_ANSWERING":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only question-answering tasks compile a Decision Specification",
        )
    if (
        payload.compiler_skill_definition_version_id is not None
        and db.get(SkillDefinitionVersion, payload.compiler_skill_definition_version_id) is None
    ):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Compiler skill version not found")
    if payload.compiler_skill_run_id is not None and db.get(SkillRun, payload.compiler_skill_run_id) is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Compiler skill run not found")
    try:
        task_version = set_analysis_task_specification(
            db,
            task_id=task_id,
            version_number=version_number,
            specification=payload.decision_specification,
            input_contract=payload.input_contract,
            output_contract=payload.output_contract,
            evidence_policy=payload.evidence_policy,
            routing_policy=payload.routing_policy,
            compiler_skill_definition_version_id=payload.compiler_skill_definition_version_id,
            compiler_skill_run_id=payload.compiler_skill_run_id,
            compiler_model_configuration=payload.compiler_model_configuration,
            validation_report=payload.validation_report,
            source_provenance=payload.source_provenance,
        )
    except MatterAnalysisTaskError as exc:
        _raise_task_error(exc)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_analysis_task.specification.updated",
        target_type="matter_analysis_task",
        target_id=task.id,
        details={
            "matter_id": str(matter.id),
            "version": task_version.version,
            "specification_content_hash": task_version.specification_content_hash,
        },
    )
    db.commit()
    db.refresh(task)
    db.refresh(task_version)
    return _task_read(db, task, task_version)


@router.post(
    "/{task_id}/versions/{version_number}/compile",
    response_model=MatterAnalysisTaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def compile_analysis_task_specification(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    version_number: int,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterAnalysisTaskRead:
    matter = _require_matter_admin(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    if task.task_type != "QUESTION_ANSWERING":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only question-answering tasks compile a Decision Specification",
        )
    try:
        task_version, workflow = queue_analysis_task_compilation(
            db,
            matter=matter,
            task=task,
            version_number=version_number,
            initiated_by_user_id=principal.user.id,
        )
    except MatterAnalysisTaskError as exc:
        _raise_task_error(exc)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_analysis_task.compilation.started",
        target_type="matter_analysis_task",
        target_id=task.id,
        details={
            "matter_id": str(matter.id),
            "version": task_version.version,
            "workflow_run_id": str(workflow.id),
        },
    )
    db.commit()
    db.refresh(task)
    db.refresh(task_version)
    return _task_read(db, task, task_version)


@router.post(
    "/{task_id}/versions/{version_number}/playground-runs",
    response_model=MatterAnalysisTaskPlaygroundRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_analysis_task_playground_run(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    version_number: int,
    payload: MatterAnalysisTaskPlaygroundCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterAnalysisTaskPlaygroundRunRead:
    matter = _require_matter_admin(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    if task.task_type != "QUESTION_ANSWERING":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The single-document Jev playground is only available for question-answering tasks",
        )
    task_version = _task_version(db, task_id=task.id, version_number=version_number)
    try:
        workflow, review_run = queue_analysis_task_playground(
            db,
            matter=matter,
            task=task,
            task_version=task_version,
            review_batch_id=payload.review_batch_id,
            matter_document_id=payload.matter_document_id,
            initiated_by_user_id=principal.user.id,
        )
    except MatterAnalysisTaskError as exc:
        _raise_task_error(exc)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_analysis_task.playground.started",
        target_type="workflow_run",
        target_id=workflow.id,
        details={
            "matter_id": str(matter.id),
            "task_id": str(task.id),
            "task_version_id": str(task_version.id),
            "review_batch_id": str(payload.review_batch_id),
            "review_batch_run_id": str(review_run.id),
            "matter_document_id": str(payload.matter_document_id),
        },
    )
    db.commit()
    return _playground_run_read(workflow, review_run)


@router.get(
    "/{task_id}/versions/{version_number}/playground-runs/{workflow_run_id}",
    response_model=MatterAnalysisTaskPlaygroundRunRead,
)
def get_analysis_task_playground_run(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    version_number: int,
    workflow_run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterAnalysisTaskPlaygroundRunRead:
    _require_matter_admin(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    task_version = _task_version(db, task_id=task.id, version_number=version_number)
    workflow = db.get(WorkflowRun, workflow_run_id)
    if (
        workflow is None
        or workflow.matter_id != matter_id
        or workflow.workflow_key != MATTER_ANALYSIS_TASK_PLAYGROUND_SPEC.key
        or workflow.input_snapshot.get("task_id") != str(task.id)
        or workflow.input_snapshot.get("task_version_id") != str(task_version.id)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playground run not found")
    review_run = db.scalar(
        select(ReviewBatchRun).where(ReviewBatchRun.workflow_run_record_id == workflow.id)
    )
    if review_run is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Playground run provenance is incomplete")
    result = db.scalar(
        select(ReviewDecisionResult).where(ReviewDecisionResult.workflow_run_id == workflow.id)
    )
    return _playground_run_read(workflow, review_run, result)


@router.post("/{task_id}/versions/{version_number}/publish", response_model=MatterAnalysisTaskRead)
def publish_analysis_task(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    version_number: int,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterAnalysisTaskRead:
    matter = _require_matter_admin(db, principal, matter_id)
    _require_task(db, matter_id=matter_id, task_id=task_id)
    try:
        task, task_version = publish_analysis_task_version(
            db,
            task_id=task_id,
            version_number=version_number,
            actor_user_id=principal.user.id,
        )
    except MatterAnalysisTaskError as exc:
        _raise_task_error(exc)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_analysis_task.version.published",
        target_type="matter_analysis_task",
        target_id=task.id,
        details={"matter_id": str(matter.id), "version": task_version.version},
    )
    db.commit()
    db.refresh(task)
    db.refresh(task_version)
    return _task_read(db, task, task_version)
