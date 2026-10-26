import logging
import uuid
from typing import Never

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.analysis_task_batch import queue_analysis_task_matter
from app.analysis_task_batch_set_runs import (
    AnalysisTaskBatchSetRunError,
    cancel_batch_set_run,
    create_batch_set_run,
    retry_batch_set_run,
)
from app.analysis_task_batch_sets import (
    AnalysisTaskBatchSetError,
    batch_set_child_ids,
    cancel_batch_set,
    complete_batch_set_materialization,
    create_batch_set,
    materialize_batch_set,
    preview_batch_set,
    retry_batch_set,
)
from app.analysis_task_compilation import queue_analysis_task_compilation
from app.analysis_task_execution_profiles import (
    AnalysisTaskExecutionProfileError,
    create_execution_profile,
    execution_profiles_for_version,
)
from app.analysis_task_playground import queue_analysis_task_playground
from app.analysis_tasks import (
    MatterAnalysisTaskError,
    MatterAnalysisTaskNotFound,
    append_analysis_task_version,
    create_analysis_task,
    publish_analysis_task_version,
    set_analysis_task_specification,
    set_fact_development_specification,
)
from app.audit import record_audit
from app.authorization import ResourceRef, can
from app.classification_analysis_tasks import (
    ClassificationAnalysisTaskError,
    cancel_classification_analysis_run,
    create_classification_analysis_run,
)
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.fact_development import (
    FactDevelopmentRunError,
    cancel_fact_development_run,
    create_fact_development_run,
    estimate_fact_development_run,
    finalize_canceled_fact_development_run,
)
from app.fact_issue_membership import FactIssueMembershipError, evaluate_issue_membership, issue_document_counts
from app.fact_specification_compilation import queue_fact_specification_compilation
from app.fact_specifications import FactSpecificationError, parse_fact_development_specification
from app.job_archives import job_not_archived
from app.job_executions import (
    ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
    CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
    FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
    JobExecutionConflict,
)
from app.models import (
    AnalysisInputManifest,
    AnalysisTaskBatchSet,
    AnalysisTaskBatchSetRun,
    AnalysisTaskExecutionProfile,
    ClassificationModelVersion,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskRun,
    MatterAnalysisTaskVersion,
    MatterDefinition,
    MatterDefinitionRevision,
    MatterSavedSearch,
    ReviewBatch,
    ReviewBatchDocument,
    ReviewBatchReviewerCoding,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewDecisionResult,
    SkillDefinitionVersion,
    SkillRun,
    WorkflowRun,
)
from app.routers.saved_searches import _saved_search
from app.schemas import (
    AnalysisTaskBatchSetChildRead,
    AnalysisTaskBatchSetCreate,
    AnalysisTaskBatchSetPreviewCreate,
    AnalysisTaskBatchSetPreviewRead,
    AnalysisTaskBatchSetRead,
    AnalysisTaskBatchSetRunCreate,
    AnalysisTaskBatchSetRunRead,
    AnalysisTaskExecutionProfileCreate,
    AnalysisTaskExecutionProfileRead,
    FactDevelopmentRunCreate,
    FactDevelopmentRunEstimateCreate,
    FactDevelopmentRunEstimateRead,
    FactDevelopmentRunRead,
    FactDevelopmentSpecificationUpdate,
    FactIssueMembershipPreviewCreate,
    FactIssueMembershipPreviewRead,
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
    MatterSearchRequest,
    QuestionAnsweringWholeMatterRunCreate,
    ReviewBatchRunRead,
)
from app.search.service import sync_review_batch_search
from app.workflow_specs import MATTER_ANALYSIS_TASK_PLAYGROUND_SPEC
from app.workflows.dispatcher import (
    cancel_analysis_task_batch_set_run_workflow,
    cancel_analysis_task_batch_set_workflow,
    cancel_classification_analysis_task,
    cancel_classification_model_scoring,
    cancel_fact_development_run_workflow,
    enqueue_analysis_task_batch_set,
    enqueue_analysis_task_batch_set_run,
    enqueue_classification_analysis_task,
    enqueue_fact_development_run,
)

router = APIRouter(prefix="/v1/matters/{matter_id}/analysis-tasks", tags=["matter analysis tasks"])
logger = logging.getLogger(__name__)


def _require_task_permission(
    db: Session,
    principal: Principal,
    matter_id: uuid.UUID,
    permission_key: str,
) -> Matter:
    matter = db.scalar(select(Matter).where(Matter.id == matter_id))
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can(db, principal, permission_key, ResourceRef.matter(matter.id)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission required: {permission_key}",
        )
    return matter


def _require_task_viewer(db: Session, principal: Principal, matter_id: uuid.UUID) -> Matter:
    return _require_task_permission(db, principal, matter_id, "analysis_task.view")


def _require_task_manager(db: Session, principal: Principal, matter_id: uuid.UUID) -> Matter:
    return _require_task_permission(db, principal, matter_id, "analysis_task.manage")


def _require_task_runner(db: Session, principal: Principal, matter_id: uuid.UUID) -> Matter:
    return _require_task_permission(db, principal, matter_id, "analysis_task.run")


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


def _batch_set_read(db: Session, batch_set: AnalysisTaskBatchSet) -> AnalysisTaskBatchSetRead:
    children = list(
        db.scalars(
            select(ReviewBatch)
            .where(ReviewBatch.analysis_task_batch_set_id == batch_set.id)
            .order_by(ReviewBatch.batch_set_ordinal)
        )
    )
    return AnalysisTaskBatchSetRead.model_validate(batch_set).model_copy(
        update={
            "children": [
                AnalysisTaskBatchSetChildRead(
                    id=child.id,
                    ordinal=child.batch_set_ordinal or 0,
                    name=child.name,
                    status=child.status,
                    search_status=child.search_status,
                    search_error_message=child.search_error_message,
                    document_count=child.document_count,
                )
                for child in children
            ]
        }
    )


def _require_batch_set(
    db: Session,
    *,
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    batch_set_id: uuid.UUID,
) -> AnalysisTaskBatchSet:
    batch_set = db.scalar(
        select(AnalysisTaskBatchSet).where(
            AnalysisTaskBatchSet.id == batch_set_id,
            AnalysisTaskBatchSet.matter_id == matter_id,
            AnalysisTaskBatchSet.matter_analysis_task_id == task_id,
        )
    )
    if batch_set is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis Task Batch Set not found")
    return batch_set


def _require_batch_set_run(
    db: Session,
    *,
    batch_set: AnalysisTaskBatchSet,
    run_id: uuid.UUID,
    for_update: bool = False,
) -> AnalysisTaskBatchSetRun:
    statement = (
        select(AnalysisTaskBatchSetRun)
        .where(
            AnalysisTaskBatchSetRun.id == run_id,
            AnalysisTaskBatchSetRun.analysis_task_batch_set_id == batch_set.id,
        )
        .options(
            selectinload(AnalysisTaskBatchSetRun.children),
            selectinload(AnalysisTaskBatchSetRun.current_workflow_run),
        )
    )
    if for_update:
        statement = statement.with_for_update()
    run = db.scalar(statement)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch Set run not found")
    return run


def _batch_set_run_configuration(
    task: MatterAnalysisTask,
    payload: AnalysisTaskBatchSetRunCreate,
) -> dict:
    configuration: dict = {"max_concurrency": payload.max_concurrency}
    if task.task_type == "CLASSIFICATION":
        if payload.embedding_job_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Classification run-all requires embedding_job_id",
            )
        configuration["embedding_job_id"] = str(payload.embedding_job_id)
    elif payload.embedding_job_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="embedding_job_id applies only to classification tasks",
        )
    if task.task_type == "FACT_DEVELOPMENT":
        configuration.update(coding_source=payload.coding_source, use_batch_api=payload.use_batch_api)
    elif payload.use_batch_api:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="use_batch_api applies only to fact-development tasks",
        )
    return configuration


def _batch_set_search_source(
    db: Session,
    *,
    matter: Matter,
    payload: AnalysisTaskBatchSetCreate | AnalysisTaskBatchSetPreviewCreate,
    principal: Principal,
) -> tuple[MatterSearchRequest | None, MatterSavedSearch | None]:
    if payload.population_source_type == "SEARCH_QUERY":
        return payload.search, None
    if payload.population_source_type != "SAVED_SEARCH" or payload.saved_search_id is None:
        return None, None
    saved = _saved_search(db, matter.id, payload.saved_search_id, principal)
    search = MatterSearchRequest.model_validate(saved.search_definition)
    if search.search_mode != "KEYWORD":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Batch Set search populations currently require a Keyword saved search",
        )
    return search, saved


def _materialize_batch_set_inline(
    db: Session,
    *,
    batch_set_id: uuid.UUID,
    settings: Settings,
) -> AnalysisTaskBatchSet:
    try:
        batch_set = materialize_batch_set(db, batch_set_id, settings)
    except Exception:
        failed = db.get(AnalysisTaskBatchSet, batch_set_id)
        if failed is None:
            raise
        return failed
    if settings.search_enabled and db.get_bind().dialect.name == "postgresql":
        for child_id in batch_set_child_ids(db, batch_set.id):
            try:
                sync_review_batch_search(child_id, settings)
            except Exception:
                logger.exception("Batch Set child search projection failed child_batch_id=%s", child_id)
        db.expire_all()
    batch_set = complete_batch_set_materialization(db, batch_set.id)
    db.commit()
    return batch_set


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
    guidance_revision_number: int | None = None
    guidance_pin_stale = False
    if version.review_guidance_revision_id is not None:
        guidance_revision = db.get(MatterDefinitionRevision, version.review_guidance_revision_id)
        guidance = db.get(MatterDefinition, version.review_guidance_id) if version.review_guidance_id else None
        if guidance_revision is not None:
            guidance_revision_number = guidance_revision.revision
            guidance_pin_stale = guidance is not None and guidance.published_revision != guidance_revision.revision
    return MatterAnalysisTaskVersionRead.model_validate(version).model_copy(
        update={
            "compilation_progress": progress,
            "classification_embedding_job_id": embedding_job_id,
            "review_guidance_revision": guidance_revision_number,
            "review_guidance_pin_stale": guidance_pin_stale,
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
    _require_task_viewer(db, principal, matter_id)
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
    _require_task_viewer(db, principal, matter_id)
    rows = db.execute(
        select(ReviewBatchRun, ReviewBatch)
        .outerjoin(ReviewBatch, ReviewBatch.id == ReviewBatchRun.review_batch_id)
        .outerjoin(
            AnalysisInputManifest,
            AnalysisInputManifest.id == ReviewBatchRun.analysis_input_manifest_id,
        )
        .where(
            or_(ReviewBatch.matter_id == matter_id, AnalysisInputManifest.matter_id == matter_id),
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
                scope_type=run.scope_type,
                scope_id=run.scope_id or matter_id,
                review_batch_id=batch.id if batch is not None else None,
                review_batch_name=batch.name if batch is not None else "Entire matter",
                matter_analysis_task_id=task_id,
                task_name=task_name,
                task_version=task_version,
                decision_provider=provider,
                model_key=str(snapshot.get("model_key") or "unknown"),
                status=run.status,
                processed_document_count=run.processed_document_count,
                successful_document_count=outcome_counts.get(run.id, (0, 0, 0))[0],
                document_count=(
                    batch.document_count
                    if batch is not None
                    else int(workflow_input.get("document_count") or 0)
                ),
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
    matter = _require_task_manager(db, principal, matter_id)
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
            review_guidance_id=payload.review_guidance_id,
            review_guidance_revision=payload.review_guidance_revision,
            scope_policy=payload.scope_policy.model_dump(mode="json") if payload.scope_policy else None,
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
    _require_task_viewer(db, principal, matter_id)
    return _task_read(db, _require_task(db, matter_id=matter_id, task_id=task_id))


@router.get("/{task_id}/batch-sets", response_model=list[AnalysisTaskBatchSetRead])
def list_analysis_task_batch_sets(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[AnalysisTaskBatchSetRead]:
    _require_task_viewer(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    rows = list(
        db.scalars(
            select(AnalysisTaskBatchSet)
            .where(AnalysisTaskBatchSet.matter_analysis_task_id == task.id)
            .order_by(AnalysisTaskBatchSet.created_at.desc(), AnalysisTaskBatchSet.id.desc())
        )
    )
    return [_batch_set_read(db, row) for row in rows]


@router.post("/{task_id}/batch-sets/preview", response_model=AnalysisTaskBatchSetPreviewRead)
def preview_analysis_task_batch_set(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: AnalysisTaskBatchSetPreviewCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AnalysisTaskBatchSetPreviewRead:
    matter = _require_task_manager(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    if task.published_version is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Analysis Task has no published version")
    task_version = _task_version(db, task_id=task.id, version_number=task.published_version)
    search, _ = _batch_set_search_source(db, matter=matter, payload=payload, principal=principal)
    try:
        plan = preview_batch_set(
            db,
            matter=matter,
            task=task,
            task_version=task_version,
            population_source_type=payload.population_source_type,
            source_review_batch_id=payload.source_review_batch_id,
            input_manifest_id=payload.input_manifest_id,
            search=search,
            target_batch_size=payload.target_batch_size,
            keep_together=list(payload.keep_together),
            actor_user_id=principal.user.id,
            settings=settings,
        )
    except AnalysisTaskBatchSetError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return AnalysisTaskBatchSetPreviewRead(
        population_count=len(plan.document_ids),
        estimated_batch_count=len(plan.partitions),
        estimated_batch_sizes=[len(partition) for partition in plan.partitions],
        population_hash=plan.population_hash,
        oversized_groups=plan.oversized_groups,
        warnings=plan.warnings,
    )


@router.get("/{task_id}/batch-sets/{batch_set_id}", response_model=AnalysisTaskBatchSetRead)
def get_analysis_task_batch_set(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    batch_set_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AnalysisTaskBatchSetRead:
    _require_task_viewer(db, principal, matter_id)
    _require_task(db, matter_id=matter_id, task_id=task_id)
    return _batch_set_read(
        db,
        _require_batch_set(
            db,
            matter_id=matter_id,
            task_id=task_id,
            batch_set_id=batch_set_id,
        ),
    )


@router.post(
    "/{task_id}/batch-sets",
    response_model=AnalysisTaskBatchSetRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_analysis_task_batch_set(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: AnalysisTaskBatchSetCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AnalysisTaskBatchSetRead:
    matter = _require_task_manager(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    if task.published_version is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Analysis Task has no published version")
    task_version = _task_version(db, task_id=task.id, version_number=task.published_version)
    search, saved_search = _batch_set_search_source(
        db,
        matter=matter,
        payload=payload,
        principal=principal,
    )
    try:
        batch_set = create_batch_set(
            db,
            matter=matter,
            task=task,
            task_version=task_version,
            name=payload.name,
            description=payload.description,
            population_source_type=payload.population_source_type,
            source_review_batch_id=payload.source_review_batch_id,
            input_manifest_id=payload.input_manifest_id,
            search=search,
            saved_search_id=saved_search.id if saved_search else None,
            saved_search_name=saved_search.name if saved_search else None,
            saved_search_updated_at=saved_search.updated_at if saved_search else None,
            target_batch_size=payload.target_batch_size,
            keep_together=list(payload.keep_together),
            random_seed=payload.random_seed,
            actor_user_id=principal.user.id,
        )
    except AnalysisTaskBatchSetError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="analysis_task_batch_set.created",
        target_type="analysis_task_batch_set",
        target_id=batch_set.id,
        details={
            "matter_id": str(matter.id),
            "task_id": str(task.id),
            "task_version_id": str(task_version.id),
            "population_source_type": batch_set.population_source_type,
            "source_review_batch_id": (
                str(batch_set.source_review_batch_id) if batch_set.source_review_batch_id else None
            ),
            "input_manifest_id": str(batch_set.input_manifest_id) if batch_set.input_manifest_id else None,
            "saved_search_id": batch_set.selection_definition.get("saved_search_id"),
            "search_index_generation_id": (
                str(batch_set.search_index_generation_id)
                if batch_set.search_index_generation_id
                else None
            ),
            "target_batch_size": batch_set.target_batch_size,
        },
    )
    enqueue_analysis_task_batch_set(db, batch_set.workflow_id, str(batch_set.id))
    db.commit()
    if not settings.dbos_enabled:
        batch_set = _materialize_batch_set_inline(db, batch_set_id=batch_set.id, settings=settings)
    return _batch_set_read(db, batch_set)


@router.post("/{task_id}/batch-sets/{batch_set_id}/cancel", response_model=AnalysisTaskBatchSetRead)
def cancel_analysis_task_batch_set(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    batch_set_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AnalysisTaskBatchSetRead:
    matter = _require_task_manager(db, principal, matter_id)
    _require_task(db, matter_id=matter_id, task_id=task_id)
    batch_set = _require_batch_set(
        db,
        matter_id=matter_id,
        task_id=task_id,
        batch_set_id=batch_set_id,
    )
    execution, canceled = cancel_batch_set(db, batch_set, actor_user_id=principal.user.id)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="analysis_task_batch_set.canceled",
        target_type="analysis_task_batch_set",
        target_id=batch_set.id,
        details={
            "matter_id": str(matter.id),
            "task_id": str(task_id),
            "execution_id": str(execution.id),
            "cancellation_applied": canceled,
        },
    )
    workflow_id = execution.dbos_workflow_id
    db.commit()
    if canceled:
        try:
            cancel_analysis_task_batch_set_workflow(workflow_id)
        except Exception as exc:  # noqa: BLE001 - durable cancellation is already committed
            logger.warning("DBOS cancellation request failed for %s: %s", workflow_id, exc)
    db.refresh(batch_set)
    return _batch_set_read(db, batch_set)


@router.post(
    "/{task_id}/batch-sets/{batch_set_id}/retry",
    response_model=AnalysisTaskBatchSetRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_analysis_task_batch_set(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    batch_set_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AnalysisTaskBatchSetRead:
    matter = _require_task_manager(db, principal, matter_id)
    _require_task(db, matter_id=matter_id, task_id=task_id)
    batch_set = _require_batch_set(
        db,
        matter_id=matter_id,
        task_id=task_id,
        batch_set_id=batch_set_id,
    )
    try:
        execution = retry_batch_set(db, batch_set, actor_user_id=principal.user.id)
    except JobExecutionConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="analysis_task_batch_set.retried",
        target_type="analysis_task_batch_set",
        target_id=batch_set.id,
        details={
            "matter_id": str(matter.id),
            "task_id": str(task_id),
            "execution_id": str(execution.id),
            "attempt_number": execution.attempt_number,
        },
    )
    enqueue_analysis_task_batch_set(db, execution.dbos_workflow_id, str(batch_set.id))
    db.commit()
    if not settings.dbos_enabled:
        batch_set = _materialize_batch_set_inline(db, batch_set_id=batch_set.id, settings=settings)
    return _batch_set_read(db, batch_set)


@router.get(
    "/{task_id}/batch-sets/{batch_set_id}/runs",
    response_model=list[AnalysisTaskBatchSetRunRead],
)
def list_analysis_task_batch_set_runs(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    batch_set_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[AnalysisTaskBatchSetRun]:
    _require_task_viewer(db, principal, matter_id)
    _require_task(db, matter_id=matter_id, task_id=task_id)
    batch_set = _require_batch_set(db, matter_id=matter_id, task_id=task_id, batch_set_id=batch_set_id)
    return list(
        db.scalars(
            select(AnalysisTaskBatchSetRun)
            .where(AnalysisTaskBatchSetRun.analysis_task_batch_set_id == batch_set.id)
            .options(
                selectinload(AnalysisTaskBatchSetRun.children),
                selectinload(AnalysisTaskBatchSetRun.current_workflow_run),
            )
            .order_by(AnalysisTaskBatchSetRun.created_at.desc(), AnalysisTaskBatchSetRun.id.desc())
        )
    )


@router.post(
    "/{task_id}/batch-sets/{batch_set_id}/runs",
    response_model=AnalysisTaskBatchSetRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_analysis_task_batch_set_run(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    batch_set_id: uuid.UUID,
    payload: AnalysisTaskBatchSetRunCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AnalysisTaskBatchSetRun:
    matter = _require_task_runner(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    batch_set = _require_batch_set(db, matter_id=matter_id, task_id=task_id, batch_set_id=batch_set_id)
    task_version = db.get(MatterAnalysisTaskVersion, batch_set.matter_analysis_task_version_id)
    if task_version is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Batch Set task version was not found")
    execution_profile = None
    if payload.execution_profile_id is not None:
        execution_profile = db.get(AnalysisTaskExecutionProfile, payload.execution_profile_id)
        if (
            execution_profile is None
            or execution_profile.matter_analysis_task_version_id != task_version.id
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Execution profile does not belong to the Batch Set task version",
            )
    try:
        run, created = create_batch_set_run(
            db,
            matter=matter,
            batch_set=batch_set,
            task=task,
            task_version=task_version,
            configuration=_batch_set_run_configuration(task, payload),
            idempotency_key=payload.idempotency_key,
            actor_user_id=principal.user.id,
            execution_profile=execution_profile,
        )
    except AnalysisTaskBatchSetRunError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if created:
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=principal.user.id,
            action="analysis_task_batch_set.run.started",
            target_type="analysis_task_batch_set_run",
            target_id=run.id,
            details={
                "matter_id": str(matter.id),
                "task_id": str(task.id),
                "batch_set_id": str(batch_set.id),
                "child_count": run.child_count,
                "max_concurrency": run.configuration_snapshot["max_concurrency"],
                "execution_profile_id": (
                    str(execution_profile.id) if execution_profile is not None else None
                ),
            },
        )
        enqueue_analysis_task_batch_set_run(db, run.workflow_id, str(run.id))
    db.commit()
    return _require_batch_set_run(db, batch_set=batch_set, run_id=run.id)


@router.post(
    "/{task_id}/batch-sets/{batch_set_id}/runs/{run_id}/cancel",
    response_model=AnalysisTaskBatchSetRunRead,
)
def cancel_analysis_task_batch_set_run(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    batch_set_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AnalysisTaskBatchSetRun:
    matter = _require_task_runner(db, principal, matter_id)
    _require_task(db, matter_id=matter_id, task_id=task_id)
    batch_set = _require_batch_set(db, matter_id=matter_id, task_id=task_id, batch_set_id=batch_set_id)
    run = _require_batch_set_run(db, batch_set=batch_set, run_id=run_id, for_update=True)
    execution, canceled, _ = cancel_batch_set_run(db, run, actor_user_id=principal.user.id)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="analysis_task_batch_set.run.canceled",
        target_type="analysis_task_batch_set_run",
        target_id=run.id,
        details={"matter_id": str(matter.id), "cancellation_applied": canceled},
    )
    workflow_id = execution.dbos_workflow_id
    db.commit()
    if canceled:
        try:
            cancel_analysis_task_batch_set_run_workflow(workflow_id)
        except Exception as exc:  # noqa: BLE001 - durable cancellation is already committed
            logger.warning("DBOS cancellation request failed for %s: %s", workflow_id, exc)
    return _require_batch_set_run(db, batch_set=batch_set, run_id=run.id)


@router.post(
    "/{task_id}/batch-sets/{batch_set_id}/runs/{run_id}/retry",
    response_model=AnalysisTaskBatchSetRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_analysis_task_batch_set_run_endpoint(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    batch_set_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AnalysisTaskBatchSetRun:
    matter = _require_task_runner(db, principal, matter_id)
    _require_task(db, matter_id=matter_id, task_id=task_id)
    batch_set = _require_batch_set(db, matter_id=matter_id, task_id=task_id, batch_set_id=batch_set_id)
    run = _require_batch_set_run(db, batch_set=batch_set, run_id=run_id, for_update=True)
    try:
        execution = retry_batch_set_run(db, run, actor_user_id=principal.user.id)
    except JobExecutionConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="analysis_task_batch_set.run.retried",
        target_type="analysis_task_batch_set_run",
        target_id=run.id,
        details={"matter_id": str(matter.id), "attempt_number": execution.attempt_number},
    )
    enqueue_analysis_task_batch_set_run(db, execution.dbos_workflow_id, str(run.id))
    db.commit()
    return _require_batch_set_run(db, batch_set=batch_set, run_id=run.id)


@router.get("/{task_id}/versions", response_model=list[MatterAnalysisTaskVersionRead])
def list_analysis_task_versions(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterAnalysisTaskVersionRead]:
    _require_task_viewer(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    versions = list(
        db.scalars(
            select(MatterAnalysisTaskVersion)
            .where(MatterAnalysisTaskVersion.matter_analysis_task_id == task.id)
            .order_by(MatterAnalysisTaskVersion.version.desc())
        )
    )
    return [_task_version_read(db, version) for version in versions]


@router.get(
    "/{task_id}/versions/{version_number}/execution-profiles",
    response_model=list[AnalysisTaskExecutionProfileRead],
)
def list_analysis_task_execution_profiles(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    version_number: int,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[AnalysisTaskExecutionProfile]:
    _require_task_viewer(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    task_version = _task_version(db, task_id=task.id, version_number=version_number)
    return execution_profiles_for_version(db, task_version.id)


@router.post(
    "/{task_id}/versions/{version_number}/execution-profiles",
    response_model=AnalysisTaskExecutionProfileRead,
    status_code=status.HTTP_201_CREATED,
)
def create_analysis_task_execution_profile(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    version_number: int,
    payload: AnalysisTaskExecutionProfileCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AnalysisTaskExecutionProfile:
    matter = _require_task_manager(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    task_version = _task_version(db, task_id=task.id, version_number=version_number)
    try:
        profile = create_execution_profile(
            db,
            matter=matter,
            task=task,
            task_version=task_version,
            key=payload.key,
            name=payload.name,
            decision_provider=payload.decision_provider,
            actor_user_id=principal.user.id,
        )
    except AnalysisTaskExecutionProfileError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_analysis_task.execution_profile.created",
        target_type="analysis_task_execution_profile",
        target_id=profile.id,
        details={
            "matter_id": str(matter.id),
            "task_id": str(task.id),
            "task_version_id": str(task_version.id),
            "decision_provider": profile.decision_provider,
            "content_hash": profile.content_hash,
        },
    )
    db.commit()
    db.refresh(profile)
    return profile


@router.post(
    "/{task_id}/question-answering-runs",
    response_model=ReviewBatchRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_whole_matter_question_answering_run(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: QuestionAnsweringWholeMatterRunCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRun:
    matter = _require_task_runner(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    if task.task_type != "QUESTION_ANSWERING":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A question-answering task is required")
    if payload.matter_analysis_task_version_id is not None:
        task_version = db.scalar(
            select(MatterAnalysisTaskVersion).where(
                MatterAnalysisTaskVersion.id == payload.matter_analysis_task_version_id,
                MatterAnalysisTaskVersion.matter_analysis_task_id == task.id,
            )
        )
    else:
        task_version = (
            _task_version(db, task_id=task.id, version_number=task.published_version)
            if task.published_version is not None
            else None
        )
    if task_version is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Published task version is unavailable")
    try:
        workflow, run, created = queue_analysis_task_matter(
            db,
            matter=matter,
            task=task,
            task_version=task_version,
            initiated_by_user_id=principal.user.id,
            idempotency_key=payload.idempotency_key,
        )
    except MatterAnalysisTaskError as exc:
        _raise_task_error(exc)
    if created:
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=principal.user.id,
            action="matter_analysis_task.question_answering_run.created",
            target_type="review_batch_run",
            target_id=run.id,
            details={
                "matter_id": str(matter.id),
                "task_id": str(task.id),
                "task_version_id": str(task_version.id),
                "scope_type": "MATTER",
                "analysis_input_manifest_id": str(run.analysis_input_manifest_id),
                "workflow_run_id": str(workflow.id),
            },
        )
    db.commit()
    db.refresh(run)
    return run


@router.get("/{task_id}/runs", response_model=list[MatterAnalysisTaskRunRead])
def list_analysis_task_runs(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterAnalysisTaskRun]:
    _require_task_viewer(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    return list(
        db.scalars(
            select(MatterAnalysisTaskRun)
            .where(
                MatterAnalysisTaskRun.matter_analysis_task_id == task.id,
                MatterAnalysisTaskRun.run_kind == "CLASSIFICATION",
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
    matter = _require_task_runner(db, principal, matter_id)
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
    _require_task_runner(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    run = db.scalar(
        select(MatterAnalysisTaskRun)
        .where(
            MatterAnalysisTaskRun.id == run_id,
            MatterAnalysisTaskRun.matter_id == matter_id,
            MatterAnalysisTaskRun.matter_analysis_task_id == task.id,
            MatterAnalysisTaskRun.run_kind == "CLASSIFICATION",
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
    matter = _require_task_manager(db, principal, matter_id)
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
            review_guidance_revision=payload.review_guidance_revision,
            scope_policy=payload.scope_policy.model_dump(mode="json") if payload.scope_policy else None,
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
    matter = _require_task_manager(db, principal, matter_id)
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


@router.put("/{task_id}/versions/{version_number}/fact-specification", response_model=MatterAnalysisTaskRead)
def update_fact_development_specification(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    version_number: int,
    payload: FactDevelopmentSpecificationUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterAnalysisTaskRead:
    """Save a reviewer-edited issue catalog; it is validated deterministically before it becomes READY."""

    matter = _require_task_manager(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    prior = _task_version(db, task_id=task.id, version_number=version_number)
    try:
        task_version = set_fact_development_specification(
            db,
            task_id=task_id,
            version_number=version_number,
            specification=payload.fact_development_specification,
            compiler_skill_definition_version_id=prior.compiler_skill_definition_version_id,
            compiler_skill_run_id=prior.compiler_skill_run_id,
            compiler_model_configuration=prior.compiler_model_configuration,
            validation_report={"warnings": []},
            source_provenance={"specification_origin": "USER_EDIT"},
        )
    except MatterAnalysisTaskError as exc:
        _raise_task_error(exc)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_analysis_task.fact_specification.updated",
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
    "/{task_id}/versions/{version_number}/issue-membership-preview",
    response_model=FactIssueMembershipPreviewRead,
)
def preview_fact_issue_membership(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    version_number: int,
    payload: FactIssueMembershipPreviewCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> FactIssueMembershipPreviewRead:
    """Count how many documents of a ready batch belong to each issue, without starting a run."""

    _require_task_manager(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    if task.task_type != "FACT_DEVELOPMENT":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only fact-development tasks have issues")
    task_version = _task_version(db, task_id=task.id, version_number=version_number)
    try:
        specification = parse_fact_development_specification(task_version.fact_development_specification)
    except FactSpecificationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    batch = db.scalar(
        select(ReviewBatch).where(ReviewBatch.id == payload.review_batch_id, ReviewBatch.matter_id == matter_id)
    )
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review batch not found")
    if batch.status != "READY":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The review batch is not ready")
    reviewer_coding_revision: int | None = None
    if payload.coding_source == "REVIEWER_CODING":
        coding = (
            db.get(ReviewBatchReviewerCoding, payload.reviewer_coding_id)
            if payload.reviewer_coding_id is not None
            else None
        )
        if coding is None or coding.review_batch_id != batch.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Reviewer coding was not found for this review batch",
            )
        reviewer_coding_revision = coding.revision
    elif payload.reviewer_coding_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="reviewer_coding_id applies only to the REVIEWER_CODING source",
        )
    document_ids = list(
        db.scalars(
            select(ReviewBatchDocument.matter_document_id)
            .where(ReviewBatchDocument.review_batch_id == batch.id)
            .order_by(ReviewBatchDocument.sequence_number)
        )
    )
    try:
        membership = evaluate_issue_membership(
            db,
            matter_id=matter_id,
            specification=specification,
            document_ids=document_ids,
            coding_source=payload.coding_source,
            reviewer_coding_id=payload.reviewer_coding_id,
            reviewer_coding_revision=reviewer_coding_revision,
        )
    except FactIssueMembershipError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return FactIssueMembershipPreviewRead(
        review_batch_id=batch.id,
        coding_source=payload.coding_source,
        reviewer_coding_id=payload.reviewer_coding_id,
        reviewer_coding_revision=reviewer_coding_revision,
        batch_document_count=len(document_ids),
        matched_document_count=len(membership),
        issues=issue_document_counts(specification, membership),
    )


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
    matter = _require_task_manager(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    if task.task_type not in {"QUESTION_ANSWERING", "FACT_DEVELOPMENT"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only question-answering and fact-development tasks compile a specification",
        )
    queue = (
        queue_fact_specification_compilation
        if task.task_type == "FACT_DEVELOPMENT"
        else queue_analysis_task_compilation
    )
    try:
        task_version, workflow = queue(
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
    matter = _require_task_runner(db, principal, matter_id)
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
    _require_task_viewer(db, principal, matter_id)
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
    matter = _require_task_manager(db, principal, matter_id)
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


def _require_fact_runner(db: Session, principal: Principal, matter_id: uuid.UUID) -> Matter:
    return _require_task_runner(db, principal, matter_id)


def _fact_run(
    db: Session,
    *,
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    for_update: bool = False,
) -> MatterAnalysisTaskRun:
    statement = select(MatterAnalysisTaskRun).where(
        MatterAnalysisTaskRun.id == run_id,
        MatterAnalysisTaskRun.matter_id == matter_id,
        MatterAnalysisTaskRun.matter_analysis_task_id == task_id,
        MatterAnalysisTaskRun.run_kind == "FACT_DEVELOPMENT",
    )
    if for_update:
        statement = statement.with_for_update()
    run = db.scalar(statement)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fact-development run not found")
    return run


@router.get("/{task_id}/fact-runs", response_model=list[FactDevelopmentRunRead])
def list_fact_development_runs(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterAnalysisTaskRun]:
    _require_fact_runner(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    return list(
        db.scalars(
            select(MatterAnalysisTaskRun)
            .where(
                MatterAnalysisTaskRun.matter_analysis_task_id == task.id,
                MatterAnalysisTaskRun.run_kind == "FACT_DEVELOPMENT",
                job_not_archived(FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE, MatterAnalysisTaskRun.id),
            )
            .options(selectinload(MatterAnalysisTaskRun.current_workflow_run))
            .order_by(MatterAnalysisTaskRun.created_at.desc(), MatterAnalysisTaskRun.id.desc())
        )
    )


@router.post(
    "/{task_id}/fact-runs",
    response_model=FactDevelopmentRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_fact_development_run(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: FactDevelopmentRunCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterAnalysisTaskRun:
    """Launch a fact-development run over one ready review batch, using the task's published issue catalog."""

    matter = _require_fact_runner(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    try:
        run, created = create_fact_development_run(
            db,
            matter=matter,
            task=task,
            review_batch_id=payload.review_batch_id,
            scope_type=payload.scope_type,
            coding_source=payload.coding_source,
            reviewer_coding_id=payload.reviewer_coding_id,
            idempotency_key=payload.idempotency_key,
            actor_user_id=principal.user.id,
            task_version_id=payload.matter_analysis_task_version_id,
            use_batch_api=payload.use_batch_api,
        )
        if created:
            enqueue_fact_development_run(
                db, run.workflow_id, str(run.id), batch_api=bool(run.configuration_snapshot.get("use_batch_api"))
            )
        db.commit()
        db.refresh(run)
        return run
    except FactDevelopmentRunError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        existing = db.scalar(
            select(MatterAnalysisTaskRun).where(
                MatterAnalysisTaskRun.matter_analysis_task_id == task.id,
                MatterAnalysisTaskRun.idempotency_key == payload.idempotency_key,
                MatterAnalysisTaskRun.run_kind == "FACT_DEVELOPMENT",
            )
        )
        if existing is not None:
            return existing
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Fact-development run conflicted") from exc


@router.post("/{task_id}/fact-runs/estimate", response_model=FactDevelopmentRunEstimateRead)
def estimate_fact_development_run_endpoint(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: FactDevelopmentRunEstimateCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> FactDevelopmentRunEstimateRead:
    """Show what a run would process before launching it: issue counts, reusable documents, and token volume."""

    matter = _require_fact_runner(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    try:
        estimate = estimate_fact_development_run(
            db,
            matter=matter,
            task=task,
            review_batch_id=payload.review_batch_id,
            scope_type=payload.scope_type,
            coding_source=payload.coding_source,
            reviewer_coding_id=payload.reviewer_coding_id,
            task_version_id=payload.matter_analysis_task_version_id,
            actor_user_id=principal.user.id,
        )
    except FactDevelopmentRunError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    db.rollback()  # The estimate only reads; end its read transaction.
    return FactDevelopmentRunEstimateRead.model_validate(estimate)


@router.get("/{task_id}/fact-runs/{run_id}", response_model=FactDevelopmentRunRead)
def get_fact_development_run(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterAnalysisTaskRun:
    _require_fact_runner(db, principal, matter_id)
    return _fact_run(db, matter_id=matter_id, task_id=task_id, run_id=run_id)


@router.post("/{task_id}/fact-runs/{run_id}/cancel", response_model=FactDevelopmentRunRead)
def cancel_fact_development_run_endpoint(
    matter_id: uuid.UUID,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterAnalysisTaskRun:
    _require_fact_runner(db, principal, matter_id)
    run = _fact_run(db, matter_id=matter_id, task_id=task_id, run_id=run_id, for_update=True)
    _, canceled = cancel_fact_development_run(db, run, actor_user_id=principal.user.id)
    workflow_id = run.workflow_id
    db.commit()
    if canceled:
        try:
            cancel_fact_development_run_workflow(workflow_id)
        except Exception as exc:  # noqa: BLE001 - cancellation is already durable
            logger.warning("DBOS cancellation request failed for %s: %s", workflow_id, exc)
        try:
            finalize_canceled_fact_development_run(db, run)
        except Exception:
            db.rollback()
            logger.exception("Canceled fact-development run outcome finalization failed run_id=%s", run.id)
    db.refresh(run)
    return run
