from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.decision_specifications import DecisionSpecification, validate_specification_for_task_version
from app.models import Matter, MatterAnalysisTask, MatterAnalysisTaskVersion, utcnow

TASK_WORKFLOW_KEYS: dict[str, str] = {
    "QUESTION_ANSWERING": "question_answering_v1",
}


class MatterAnalysisTaskError(ValueError):
    pass


class MatterAnalysisTaskConflict(MatterAnalysisTaskError):
    pass


class MatterAnalysisTaskNotFound(MatterAnalysisTaskError):
    pass


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def json_content_hash(value: Any) -> str:
    return content_hash(canonical_json(value))


def can_attach_analysis_task_specification(
    _task: MatterAnalysisTask,
    task_version: MatterAnalysisTaskVersion,
) -> bool:
    return task_version.status == "DRAFT"


def create_analysis_task(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    key: str,
    name: str,
    description: str | None,
    task_type: str,
    definition_markdown: str,
) -> tuple[MatterAnalysisTask, MatterAnalysisTaskVersion]:
    workflow_key = TASK_WORKFLOW_KEYS.get(task_type)
    if workflow_key is None:
        raise MatterAnalysisTaskError(f"Unsupported analysis task type: {task_type}")
    existing = db.scalar(
        select(MatterAnalysisTask).where(
            MatterAnalysisTask.matter_id == matter.id,
            MatterAnalysisTask.key == key,
        )
    )
    if existing is not None:
        raise MatterAnalysisTaskConflict(f"Analysis task key already exists for this matter: {key}")

    task = MatterAnalysisTask(
        matter_id=matter.id,
        key=key,
        name=name,
        description=description,
        task_type=task_type,
        workflow_key=workflow_key,
        current_version=1,
        status="ACTIVE",
        created_by_user_id=actor_user_id,
    )
    db.add(task)
    db.flush()
    task_version = MatterAnalysisTaskVersion(
        matter_analysis_task_id=task.id,
        version=1,
        status="DRAFT",
        compilation_status="NOT_GENERATED",
        definition_markdown=definition_markdown,
        definition_content_hash=content_hash(definition_markdown),
        created_by_user_id=actor_user_id,
        source_provenance={"origin": "USER_CREATED"},
    )
    db.add(task_version)
    db.flush()
    return task, task_version


def append_analysis_task_version(
    db: Session,
    *,
    task_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    definition_markdown: str,
    based_on_version: int,
) -> tuple[MatterAnalysisTask, MatterAnalysisTaskVersion]:
    task = db.scalar(select(MatterAnalysisTask).where(MatterAnalysisTask.id == task_id).with_for_update())
    if task is None:
        raise MatterAnalysisTaskNotFound("Matter Analysis Task not found")
    if task.status != "ACTIVE":
        raise MatterAnalysisTaskConflict("Only active analysis tasks can be edited")
    if based_on_version != task.current_version:
        raise MatterAnalysisTaskConflict(f"Analysis task changed; current version is {task.current_version}")

    prior_version = db.scalar(
        select(MatterAnalysisTaskVersion).where(
            MatterAnalysisTaskVersion.matter_analysis_task_id == task.id,
            MatterAnalysisTaskVersion.version == task.current_version,
        )
    )
    if prior_version is None:
        raise MatterAnalysisTaskConflict("Analysis task has no current version")
    if prior_version.status == "DRAFT":
        prior_version.status = "RETIRED"
        if prior_version.compilation_status == "GENERATING":
            prior_version.compilation_status = "STALE"

    next_version = task.current_version + 1
    task.current_version = next_version
    task_version = MatterAnalysisTaskVersion(
        matter_analysis_task_id=task.id,
        version=next_version,
        status="DRAFT",
        compilation_status="NOT_GENERATED",
        definition_markdown=definition_markdown,
        definition_content_hash=content_hash(definition_markdown),
        created_by_user_id=actor_user_id,
        source_provenance={
            "origin": "USER_EDIT",
            "based_on_task_version_id": str(prior_version.id),
            "based_on_version": prior_version.version,
        },
    )
    db.add(task_version)
    db.flush()
    return task, task_version


def set_analysis_task_specification(
    db: Session,
    *,
    task_id: uuid.UUID,
    version_number: int,
    specification: DecisionSpecification,
    input_contract: dict[str, Any],
    output_contract: dict[str, Any],
    evidence_policy: dict[str, Any],
    routing_policy: dict[str, Any],
    compiler_skill_definition_version_id: uuid.UUID | None,
    compiler_skill_run_id: uuid.UUID | None,
    compiler_model_configuration: dict[str, Any],
    validation_report: dict[str, Any],
    source_provenance: dict[str, Any],
) -> MatterAnalysisTaskVersion:
    task = db.scalar(select(MatterAnalysisTask).where(MatterAnalysisTask.id == task_id).with_for_update())
    if task is None:
        raise MatterAnalysisTaskNotFound("Matter Analysis Task not found")
    if version_number != task.current_version:
        raise MatterAnalysisTaskConflict("A specification may only be attached to the current task version")
    task_version = db.scalar(
        select(MatterAnalysisTaskVersion)
        .where(
            MatterAnalysisTaskVersion.matter_analysis_task_id == task.id,
            MatterAnalysisTaskVersion.version == version_number,
        )
        .with_for_update()
    )
    if task_version is None:
        raise MatterAnalysisTaskNotFound("Matter Analysis Task version not found")
    if not (
        task_version.status == "DRAFT"
        or (
            can_attach_analysis_task_specification(task, task_version)
            and task_version.compilation_status == "GENERATING"
        )
    ):
        raise MatterAnalysisTaskConflict("Published or retired task versions are immutable")

    try:
        validate_specification_for_task_version(specification, task_version.id)
    except ValueError as exc:
        raise MatterAnalysisTaskError(str(exc)) from exc

    specification_json = specification.model_dump(mode="json")
    task_version.decision_specification = specification_json
    task_version.specification_content_hash = json_content_hash(specification_json)
    task_version.input_contract = input_contract
    task_version.output_contract = output_contract
    task_version.evidence_policy = evidence_policy
    task_version.routing_policy = routing_policy
    task_version.compiler_skill_definition_version_id = compiler_skill_definition_version_id
    task_version.compiler_skill_run_id = compiler_skill_run_id
    task_version.compiler_model_configuration = compiler_model_configuration
    task_version.validation_report = validation_report
    task_version.source_provenance = {
        **task_version.source_provenance,
        **source_provenance,
    }
    task_version.compilation_status = "READY"
    db.flush()
    return task_version


def publish_analysis_task_version(
    db: Session,
    *,
    task_id: uuid.UUID,
    version_number: int,
    actor_user_id: uuid.UUID,
) -> tuple[MatterAnalysisTask, MatterAnalysisTaskVersion]:
    task = db.scalar(select(MatterAnalysisTask).where(MatterAnalysisTask.id == task_id).with_for_update())
    if task is None:
        raise MatterAnalysisTaskNotFound("Matter Analysis Task not found")
    if task.status != "ACTIVE":
        raise MatterAnalysisTaskConflict("Only active analysis tasks can be published")
    if version_number != task.current_version:
        raise MatterAnalysisTaskConflict("Only the current task version can be published")
    task_version = db.scalar(
        select(MatterAnalysisTaskVersion)
        .where(
            MatterAnalysisTaskVersion.matter_analysis_task_id == task.id,
            MatterAnalysisTaskVersion.version == version_number,
        )
        .with_for_update()
    )
    if task_version is None:
        raise MatterAnalysisTaskNotFound("Matter Analysis Task version not found")
    if task_version.status != "DRAFT":
        raise MatterAnalysisTaskConflict("Only a draft task version can be published")
    if task_version.compilation_status != "READY" or task_version.decision_specification is None:
        raise MatterAnalysisTaskConflict("The Decision Specification must be valid and READY before publication")

    if task.published_version is not None:
        prior_published = db.scalar(
            select(MatterAnalysisTaskVersion).where(
                MatterAnalysisTaskVersion.matter_analysis_task_id == task.id,
                MatterAnalysisTaskVersion.version == task.published_version,
            )
        )
        if prior_published is not None:
            prior_published.status = "RETIRED"

    now = utcnow()
    task_version.status = "PUBLISHED"
    task_version.published_by_user_id = actor_user_id
    task_version.published_at = now
    task.published_version = task_version.version
    db.flush()
    return task, task_version
