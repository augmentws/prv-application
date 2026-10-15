from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.decision_specifications import DecisionSpecification, validate_specification_for_task_version
from app.models import (
    ClassificationModel,
    ClassificationModelVersion,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    MetadataDefinition,
    utcnow,
)

TASK_WORKFLOW_KEYS: dict[str, str] = {
    "QUESTION_ANSWERING": "question_answering_v1",
    "CLASSIFICATION": "classification_scoring_v1",
}

DECISION_PROVIDER_MODELS: dict[str, str | None] = {
    "JEV": None,
    "OPENAI_DECISIONS": "gpt-6-luna",
}


class MatterAnalysisTaskError(ValueError):
    pass


class MatterAnalysisTaskConflict(MatterAnalysisTaskError):
    pass


class MatterAnalysisTaskInputTooLarge(MatterAnalysisTaskConflict):
    code = "ANALYSIS_DOCUMENT_INPUT_TOO_LARGE"

    def __init__(self, *, actual_characters: int, maximum_characters: int) -> None:
        self.actual_characters = actual_characters
        self.maximum_characters = maximum_characters
        super().__init__(
            f"Document decision input is {actual_characters:,} characters, exceeding the configured "
            f"single-request limit of {maximum_characters:,}; increase "
            "ANALYSIS_TASK_DECISION_MAX_CHARACTERS and retry, or enable windowed evaluation"
        )


class MatterAnalysisTaskNotFound(MatterAnalysisTaskError):
    pass


def decision_provider_model(provider: str) -> str | None:
    try:
        return DECISION_PROVIDER_MODELS[provider]
    except KeyError as exc:
        raise MatterAnalysisTaskError(f"Unsupported decision provider: {provider}") from exc


def decision_provider_engine_key(provider: str) -> str:
    decision_provider_model(provider)
    return "openai_decisions" if provider == "OPENAI_DECISIONS" else "jev"


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


def _validated_destination_ids(
    db: Session,
    *,
    matter_id: uuid.UUID,
    destination_metadata_definition_ids: list[uuid.UUID],
) -> list[str]:
    if not destination_metadata_definition_ids:
        return []
    definitions = {
        definition.id: definition
        for definition in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter_id,
                MetadataDefinition.id.in_(destination_metadata_definition_ids),
            )
        )
    }
    missing = [field_id for field_id in destination_metadata_definition_ids if field_id not in definitions]
    if missing:
        raise MatterAnalysisTaskError("Every destination field must belong to the analysis task's matter")
    unavailable = [
        definitions[field_id].display_name
        for field_id in destination_metadata_definition_ids
        if definitions[field_id].status != "ACTIVE"
        or definitions[field_id].value_source != "ASSERTED"
        or not definitions[field_id].ai_assignable
    ]
    if unavailable:
        raise MatterAnalysisTaskError(
            "Destination fields must be active, matter-owned, and AI-assignable: " + ", ".join(unavailable)
        )
    return [str(field_id) for field_id in destination_metadata_definition_ids]


def _classification_binding(
    db: Session,
    *,
    matter_id: uuid.UUID,
    classification_model_version_id: uuid.UUID | None,
    destination_ids: list[str],
) -> tuple[uuid.UUID, uuid.UUID]:
    if classification_model_version_id is None:
        raise MatterAnalysisTaskError("Classification tasks require an approved model version")
    version = db.scalar(
        select(ClassificationModelVersion).where(
            ClassificationModelVersion.id == classification_model_version_id,
            ClassificationModelVersion.matter_id == matter_id,
        )
    )
    if version is None:
        raise MatterAnalysisTaskError("Classification model version was not found in this matter")
    if version.status != "APPROVED":
        raise MatterAnalysisTaskError("Classification tasks must pin an approved model version")
    model = db.scalar(
        select(ClassificationModel).where(
            ClassificationModel.id == version.classification_model_id,
            ClassificationModel.matter_id == matter_id,
            ClassificationModel.status == "ACTIVE",
        )
    )
    if model is None:
        raise MatterAnalysisTaskError("Classification model must remain active")
    expected_destinations = [str(model.target_metadata_definition_id)]
    if destination_ids != expected_destinations:
        raise MatterAnalysisTaskError(
            "A classification task destination must be the model's target coding field"
        )
    return model.id, version.id


def _validate_specification_destinations(
    db: Session,
    *,
    task: MatterAnalysisTask,
    task_version: MatterAnalysisTaskVersion,
    specification: DecisionSpecification,
) -> None:
    selected_ids = task_version.destination_metadata_definition_ids
    if selected_ids is None:
        return
    destination_ids = [uuid.UUID(value) for value in selected_ids]
    _validated_destination_ids(
        db,
        matter_id=task.matter_id,
        destination_metadata_definition_ids=destination_ids,
    )
    destination_keys = set(
        db.scalars(
            select(MetadataDefinition.key).where(
                MetadataDefinition.matter_id == task.matter_id,
                MetadataDefinition.id.in_(destination_ids),
            )
        )
    )
    mapped_keys = {
        question.field_mapping.metadata_definition_key
        for question in specification.questions.values()
        if question.field_mapping is not None
    }
    unselected = sorted(mapped_keys - destination_keys)
    if unselected:
        raise MatterAnalysisTaskError(
            "Decision Specification maps fields outside this task version's destinations: "
            + ", ".join(unselected)
        )


def create_analysis_task(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    key: str,
    name: str,
    description: str | None,
    task_type: str,
    decision_provider: str,
    definition_markdown: str,
    destination_metadata_definition_ids: list[uuid.UUID],
    classification_model_version_id: uuid.UUID | None = None,
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
    destination_ids = _validated_destination_ids(
        db,
        matter_id=matter.id,
        destination_metadata_definition_ids=destination_metadata_definition_ids,
    )
    classification_model_id: uuid.UUID | None = None
    pinned_model_version_id: uuid.UUID | None = None
    decision_model = decision_provider_model(decision_provider)
    if task_type == "CLASSIFICATION":
        if decision_provider != "JEV":
            raise MatterAnalysisTaskError("Classification tasks do not use a decision provider")
        classification_model_id, pinned_model_version_id = _classification_binding(
            db,
            matter_id=matter.id,
            classification_model_version_id=classification_model_version_id,
            destination_ids=destination_ids,
        )
    elif classification_model_version_id is not None:
        raise MatterAnalysisTaskError("Question-answering tasks cannot pin a classification model")

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
        compilation_status="READY" if task_type == "CLASSIFICATION" else "NOT_GENERATED",
        definition_markdown=definition_markdown,
        definition_content_hash=content_hash(definition_markdown),
        destination_metadata_definition_ids=destination_ids,
        decision_provider=decision_provider,
        decision_model=decision_model,
        classification_model_id=classification_model_id,
        classification_model_version_id=pinned_model_version_id,
        created_by_user_id=actor_user_id,
        source_provenance={
            "origin": "USER_CREATED",
            "decision_provider": decision_provider,
            "decision_model": decision_model,
            **(
                {
                    "classification_model_id": str(classification_model_id),
                    "classification_model_version_id": str(pinned_model_version_id),
                }
                if classification_model_id and pinned_model_version_id
                else {}
            ),
        },
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
    decision_provider: str | None,
    destination_metadata_definition_ids: list[uuid.UUID] | None,
    classification_model_version_id: uuid.UUID | None = None,
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
    destination_ids = (
        prior_version.destination_metadata_definition_ids
        if destination_metadata_definition_ids is None
        else _validated_destination_ids(
            db,
            matter_id=task.matter_id,
            destination_metadata_definition_ids=destination_metadata_definition_ids,
        )
    )
    selected_decision_provider = decision_provider or prior_version.decision_provider
    decision_model = decision_provider_model(selected_decision_provider)
    if task.task_type == "CLASSIFICATION" and selected_decision_provider != "JEV":
        raise MatterAnalysisTaskError("Classification tasks do not use a decision provider")
    pinned_model_id = prior_version.classification_model_id
    pinned_model_version_id = prior_version.classification_model_version_id
    if task.task_type == "CLASSIFICATION":
        requested_version_id = classification_model_version_id or pinned_model_version_id
        pinned_model_id, pinned_model_version_id = _classification_binding(
            db,
            matter_id=task.matter_id,
            classification_model_version_id=requested_version_id,
            destination_ids=destination_ids or [],
        )
    elif classification_model_version_id is not None:
        raise MatterAnalysisTaskError("Question-answering tasks cannot pin a classification model")
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
        compilation_status="READY" if task.task_type == "CLASSIFICATION" else "NOT_GENERATED",
        definition_markdown=definition_markdown,
        definition_content_hash=content_hash(definition_markdown),
        destination_metadata_definition_ids=destination_ids,
        decision_provider=selected_decision_provider,
        decision_model=decision_model,
        classification_model_id=pinned_model_id,
        classification_model_version_id=pinned_model_version_id,
        created_by_user_id=actor_user_id,
        source_provenance={
            "origin": "USER_EDIT",
            "based_on_task_version_id": str(prior_version.id),
            "based_on_version": prior_version.version,
            "decision_provider": selected_decision_provider,
            "decision_model": decision_model,
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
    _validate_specification_destinations(
        db,
        task=task,
        task_version=task_version,
        specification=specification,
    )

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
    if task.task_type == "QUESTION_ANSWERING":
        if task_version.compilation_status != "READY" or task_version.decision_specification is None:
            raise MatterAnalysisTaskConflict("The Decision Specification must be valid and READY before publication")
    elif task.task_type == "CLASSIFICATION":
        _classification_binding(
            db,
            matter_id=task.matter_id,
            classification_model_version_id=task_version.classification_model_version_id,
            destination_ids=task_version.destination_metadata_definition_ids or [],
        )
        task_version.compilation_status = "READY"
    else:
        raise MatterAnalysisTaskConflict(f"Unsupported analysis task type: {task.task_type}")

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
