import uuid
from typing import Never

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analysis_task_compilation import queue_analysis_task_compilation
from app.analysis_tasks import (
    MatterAnalysisTaskError,
    MatterAnalysisTaskNotFound,
    append_analysis_task_version,
    create_analysis_task,
    publish_analysis_task_version,
    set_analysis_task_specification,
)
from app.audit import record_audit
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.models import (
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    SkillDefinitionVersion,
    SkillRun,
)
from app.schemas import (
    MatterAnalysisTaskCreate,
    MatterAnalysisTaskRead,
    MatterAnalysisTaskSpecificationUpdate,
    MatterAnalysisTaskVersionCreate,
    MatterAnalysisTaskVersionRead,
)

router = APIRouter(prefix="/v1/matters/{matter_id}/analysis-tasks", tags=["matter analysis tasks"])


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
        version=MatterAnalysisTaskVersionRead.model_validate(current),
    )


def _raise_task_error(exc: MatterAnalysisTaskError) -> Never:
    if isinstance(exc, MatterAnalysisTaskNotFound):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


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
            definition_markdown=payload.definition_markdown,
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
        details={"matter_id": str(matter.id), "task_type": task.task_type, "version": 1},
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
) -> list[MatterAnalysisTaskVersion]:
    _require_matter_admin(db, principal, matter_id)
    task = _require_task(db, matter_id=matter_id, task_id=task_id)
    return list(
        db.scalars(
            select(MatterAnalysisTaskVersion)
            .where(MatterAnalysisTaskVersion.matter_analysis_task_id == task.id)
            .order_by(MatterAnalysisTaskVersion.version.desc())
        )
    )


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
        details={"matter_id": str(matter.id), "version": task_version.version},
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
