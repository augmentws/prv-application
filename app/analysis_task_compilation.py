from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_models import resolve_agent_model
from app.analysis_tasks import (
    MatterAnalysisTaskConflict,
    content_hash,
    json_content_hash,
    set_analysis_task_specification,
)
from app.config import Settings, get_settings
from app.decision_specifications import (
    ChoiceDecisionQuestion,
    DecisionSpecification,
    DecisionSpecificationCompilationOutput,
)
from app.models import (
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    MatterAnalysisTaskVersionDependency,
    MetadataDefinition,
    SkillDefinitionVersion,
    WorkflowRun,
    WorkflowStepRun,
)
from app.skill_execution import execute_skill_run
from app.workflow_specs import (
    MATTER_ANALYSIS_TASK_COMPILATION_SPEC,
    binding_snapshot,
    resolve_workflow_skill_bindings,
)
from app.workflows.dispatcher import enqueue_analysis_task_compilation

HEADING_PATTERN = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
FALLBACK_OPTION_KEYS = frozenset({"other", "unclear", "insufficient_evidence"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_source_reference_catalog(
    task_version_id: uuid.UUID,
    definition_markdown: str,
) -> list[dict[str, Any]]:
    heading: str | None = None
    blocks: list[tuple[str | None, str]] = []
    paragraph_lines: list[str] = []

    def flush() -> None:
        if not paragraph_lines:
            return
        excerpt = " ".join(line.strip() for line in paragraph_lines if line.strip()).strip()
        paragraph_lines.clear()
        if excerpt:
            blocks.append((heading, excerpt))

    for raw_line in definition_markdown.splitlines():
        line = raw_line.strip()
        match = HEADING_PATTERN.match(line)
        if match:
            flush()
            heading = match.group(1).strip()
            continue
        if not line:
            flush()
            continue
        paragraph_lines.append(line)
    flush()
    if not blocks:
        fallback = " ".join(definition_markdown.split()).strip()
        if fallback:
            blocks.append((heading, fallback))
    return [
        {
            "task_version_id": str(task_version_id),
            "heading": block_heading,
            "excerpt": excerpt,
            "excerpt_hash": content_hash(excerpt),
        }
        for block_heading, excerpt in blocks
    ]


def metadata_definition_snapshot(db: Session, matter_id: uuid.UUID) -> list[dict[str, Any]]:
    definitions = db.scalars(
        select(MetadataDefinition)
        .where(
            MetadataDefinition.matter_id == matter_id,
            MetadataDefinition.status == "ACTIVE",
            MetadataDefinition.ai_assignable.is_(True),
        )
        .order_by(MetadataDefinition.key)
    ).all()
    return [
        {
            "id": str(definition.id),
            "key": definition.key,
            "display_name": definition.display_name,
            "description": definition.description,
            "type": definition.type,
            "cardinality": definition.cardinality,
            "allowed_values": definition.allowed_values,
            "assertion_policy": definition.assertion_policy,
            "resolution_policy": definition.resolution_policy,
        }
        for definition in definitions
    ]


def dependency_snapshot(
    db: Session,
    task_version_id: uuid.UUID,
) -> list[dict[str, Any]]:
    rows = db.execute(
        select(
            MatterAnalysisTaskVersionDependency,
            MatterAnalysisTaskVersion,
            MatterAnalysisTask,
        )
        .join(
            MatterAnalysisTaskVersion,
            MatterAnalysisTaskVersion.id == MatterAnalysisTaskVersionDependency.dependency_task_version_id,
        )
        .join(
            MatterAnalysisTask,
            MatterAnalysisTask.id == MatterAnalysisTaskVersion.matter_analysis_task_id,
        )
        .where(MatterAnalysisTaskVersionDependency.matter_analysis_task_version_id == task_version_id)
        .order_by(MatterAnalysisTaskVersionDependency.role, MatterAnalysisTask.key)
    ).all()
    return [
        {
            "role": dependency.role,
            "task_id": str(task.id),
            "task_key": task.key,
            "task_type": task.task_type,
            "task_version_id": str(version.id),
            "version": version.version,
            "definition_markdown": version.definition_markdown,
            "definition_content_hash": version.definition_content_hash,
            "decision_specification": version.decision_specification,
            "specification_content_hash": version.specification_content_hash,
            "pinned_content_hash": dependency.dependency_content_hash,
        }
        for dependency, version, task in rows
    ]


def queue_analysis_task_compilation(
    db: Session,
    *,
    matter: Matter,
    task: MatterAnalysisTask,
    version_number: int,
    initiated_by_user_id: uuid.UUID,
    settings: Settings | None = None,
) -> tuple[MatterAnalysisTaskVersion, WorkflowRun]:
    settings = settings or get_settings()
    locked_task = db.scalar(select(MatterAnalysisTask).where(MatterAnalysisTask.id == task.id).with_for_update())
    if locked_task is None or locked_task.matter_id != matter.id:
        raise MatterAnalysisTaskConflict("Matter Analysis Task not found")
    if locked_task.current_version != version_number:
        raise MatterAnalysisTaskConflict("Only the current task version can be compiled")
    task_version = db.scalar(
        select(MatterAnalysisTaskVersion)
        .where(
            MatterAnalysisTaskVersion.matter_analysis_task_id == locked_task.id,
            MatterAnalysisTaskVersion.version == version_number,
        )
        .with_for_update()
    )
    if task_version is None or task_version.status != "DRAFT":
        raise MatterAnalysisTaskConflict("Only a draft task version can be compiled")
    if task_version.compilation_status == "GENERATING":
        raise MatterAnalysisTaskConflict("Decision Specification compilation is already running")

    references = build_source_reference_catalog(task_version.id, task_version.definition_markdown)
    if not references:
        raise MatterAnalysisTaskConflict("The Task Definition must contain substantive guidance")
    try:
        resolved = resolve_workflow_skill_bindings(
            db,
            workflow_key=MATTER_ANALYSIS_TASK_COMPILATION_SPEC.key,
            tenant_id=matter.client.tenant_id,
        )
    except ValueError as exc:
        raise MatterAnalysisTaskConflict(f"Compiler managed skill is not configured: {exc}") from exc
    bindings = binding_snapshot(resolved)
    skill_version = resolved["decision_specification_compiler"][2]
    resolved_model = resolve_agent_model(skill_version.model_key, settings)
    workflow_id = f"analysis-task:{task.id}:version:{version_number}:compile:{uuid.uuid4()}"
    workflow = WorkflowRun(
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        matter_id=matter.id,
        workflow_key=MATTER_ANALYSIS_TASK_COMPILATION_SPEC.key,
        code_version=MATTER_ANALYSIS_TASK_COMPILATION_SPEC.code_version,
        dbos_workflow_id=workflow_id,
        status="QUEUED",
        input_snapshot={
            "task_id": str(task.id),
            "task_version_id": str(task_version.id),
            "version": task_version.version,
            "definition_content_hash": task_version.definition_content_hash,
            "source_reference_catalog_hash": json_content_hash(references),
        },
        binding_snapshot=bindings,
        configuration_snapshot={
            "resolved_models": {"decision_specification_compiler": resolved_model},
        },
        progress={"stage": "QUEUED"},
        initiated_by_user_id=initiated_by_user_id,
    )
    db.add(workflow)
    db.flush()
    db.add(
        WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key="decision_specification_compiler",
            ordinal=1,
            status="QUEUED",
            total_count=1,
        )
    )
    task_version.compilation_status = "GENERATING"
    task_version.compiler_workflow_run_id = workflow.id
    task_version.validation_report = {"status": "RUNNING", "errors": [], "warnings": []}
    db.flush()
    enqueue_analysis_task_compilation(db, workflow_id, str(task_version.id))
    return task_version, workflow


def compile_analysis_task_version(
    db: Session,
    task_version_id: uuid.UUID,
    *,
    model: Any | None = None,
) -> dict[str, Any]:
    task_version = db.get(MatterAnalysisTaskVersion, task_version_id)
    if task_version is None or task_version.compiler_workflow_run_id is None:
        raise MatterAnalysisTaskConflict("Analysis task compilation is not queued")
    task = db.get(MatterAnalysisTask, task_version.matter_analysis_task_id)
    workflow = db.get(WorkflowRun, task_version.compiler_workflow_run_id)
    if task is None or workflow is None:
        raise MatterAnalysisTaskConflict("Analysis task compilation references are incomplete")
    if (
        task.current_version != task_version.version
        or task_version.status != "DRAFT"
        or task_version.compilation_status != "GENERATING"
    ):
        raise MatterAnalysisTaskConflict("Analysis task compilation is stale")
    step = db.scalar(
        select(WorkflowStepRun).where(
            WorkflowStepRun.workflow_run_id == workflow.id,
            WorkflowStepRun.ordinal == 1,
        )
    )
    role_snapshot = workflow.binding_snapshot.get("decision_specification_compiler") or {}
    skill_version_id = role_snapshot.get("skill_definition_version_id")
    skill_version = db.get(SkillDefinitionVersion, uuid.UUID(skill_version_id)) if skill_version_id else None
    matter = db.get(Matter, task.matter_id)
    if step is None or skill_version is None or matter is None:
        raise MatterAnalysisTaskConflict("Pinned compiler workflow configuration is unavailable")

    references = build_source_reference_catalog(task_version.id, task_version.definition_markdown)
    metadata_definitions = metadata_definition_snapshot(db, matter.id)
    dependencies = dependency_snapshot(db, task_version.id)
    prior = _prior_published_specification(db, task)
    validator = _compiler_output_validator(
        task_version_id=task_version.id,
        references=references,
        metadata_definitions=metadata_definitions,
    )
    now = utcnow()
    workflow.status = "RUNNING"
    workflow.started_at = workflow.started_at or now
    workflow.progress = {"stage": "COMPILING"}
    step.status = "RUNNING"
    step.started_at = step.started_at or now
    pinned_model = workflow.configuration_snapshot.get("resolved_models", {}).get("decision_specification_compiler")
    output, skill_run = asyncio.run(
        execute_skill_run(
            db,
            workflow=workflow,
            step=step,
            skill_version=skill_version,
            scope_type="MATTER_ANALYSIS_TASK_VERSION",
            scope_id=task_version.id,
            stable_context={
                "task_definition": task_version.definition_markdown,
                "source_reference_catalog": references,
                "metadata_definitions": metadata_definitions,
                "dependency_versions": dependencies,
                "specification_schema": DecisionSpecification.model_json_schema(mode="validation"),
            },
            dynamic_input={
                "task": {
                    "task_id": str(task.id),
                    "task_version_id": str(task_version.id),
                    "version": task_version.version,
                    "key": task.key,
                    "name": task.name,
                    "description": task.description,
                    "task_type": task.task_type,
                    "workflow_key": task.workflow_key,
                    "definition_content_hash": task_version.definition_content_hash,
                },
                "prior_published_specification": prior,
            },
            cache_identity={
                "tenant_id": str(workflow.tenant_id),
                "matter_id": str(matter.id),
                "task_id": str(task.id),
                "task_version_id": str(task_version.id),
                "definition_content_hash": task_version.definition_content_hash,
                "skill_version_id": str(skill_version.id),
                "output_schema_key": skill_version.output_schema_key,
            },
            output_validators=(validator,),
            usage_job_type="MATTER_ANALYSIS_TASK_COMPILATION",
            model=model if model is not None else pinned_model,
        )
    )
    compiled = DecisionSpecificationCompilationOutput.model_validate(output)
    deterministic_warnings = _deterministic_warnings(compiled.decision_specification)
    validation_report = {
        "status": "VALID",
        "errors": [],
        "warnings": [
            *[warning.model_dump(mode="json") for warning in compiled.warnings],
            *deterministic_warnings,
        ],
        "question_rationales": compiled.question_rationales,
        "omissions": [omission.model_dump(mode="json") for omission in compiled.omissions],
    }
    specification = compiled.decision_specification
    task_version.compiler_workflow_run_id = workflow.id
    set_analysis_task_specification(
        db,
        task_id=task.id,
        version_number=task_version.version,
        specification=specification,
        input_contract=specification.state_contract.model_dump(mode="json"),
        output_contract={
            "schema_version": specification.schema_version,
            "questions": {
                key: {
                    "type": question.type,
                    "field_mapping": (
                        question.field_mapping.model_dump(mode="json") if question.field_mapping else None
                    ),
                }
                for key, question in specification.questions.items()
            },
        },
        evidence_policy={
            key: question.evidence.model_dump(mode="json") for key, question in specification.questions.items()
        },
        routing_policy=specification.decision_policy.model_dump(mode="json"),
        compiler_skill_definition_version_id=skill_version.id,
        compiler_skill_run_id=skill_run.id,
        compiler_model_configuration={
            "model_key": skill_version.model_key,
            "resolved_model": pinned_model,
            "model_policy": skill_version.model_policy,
            "limits": skill_version.limits,
        },
        validation_report=validation_report,
        source_provenance={
            "compiler_workflow_run_id": str(workflow.id),
            "definition_content_hash": task_version.definition_content_hash,
            "source_reference_catalog_hash": json_content_hash(references),
        },
    )
    step.status = "COMPLETED"
    step.completed_count = 1
    step.completed_at = utcnow()
    workflow.status = "COMPLETED"
    workflow.progress = {"stage": "COMPLETED", "question_count": len(specification.questions)}
    workflow.completed_at = utcnow()
    db.commit()
    return specification.model_dump(mode="json")


def fail_analysis_task_compilation(
    db: Session,
    task_version_id: uuid.UUID,
    message: str,
) -> None:
    task_version = db.get(MatterAnalysisTaskVersion, task_version_id)
    if task_version is None or task_version.compiler_workflow_run_id is None:
        return
    workflow = db.get(WorkflowRun, task_version.compiler_workflow_run_id)
    if workflow is not None:
        workflow.status = "FAILED"
        workflow.error_message = message[:4000]
        workflow.progress = {"stage": "FAILED"}
        workflow.completed_at = utcnow()
        step = db.scalar(
            select(WorkflowStepRun).where(
                WorkflowStepRun.workflow_run_id == workflow.id,
                WorkflowStepRun.ordinal == 1,
            )
        )
        if step is not None:
            step.status = "FAILED"
            step.failed_count = 1
            step.error_message = message[:4000]
            step.completed_at = utcnow()
    if task_version.status == "DRAFT" and task_version.compilation_status == "GENERATING":
        task_version.compilation_status = "FAILED"
        task_version.validation_report = {
            "status": "INVALID",
            "errors": [{"code": "COMPILATION_FAILED", "message": message[:4000]}],
            "warnings": [],
        }
    db.commit()


def _prior_published_specification(
    db: Session,
    task: MatterAnalysisTask,
) -> dict[str, Any] | None:
    if task.published_version is None:
        return None
    prior = db.scalar(
        select(MatterAnalysisTaskVersion).where(
            MatterAnalysisTaskVersion.matter_analysis_task_id == task.id,
            MatterAnalysisTaskVersion.version == task.published_version,
        )
    )
    return prior.decision_specification if prior is not None else None


def _compiler_output_validator(
    *,
    task_version_id: uuid.UUID,
    references: list[dict[str, Any]],
    metadata_definitions: list[dict[str, Any]],
):
    valid_references = {(reference["heading"], reference["excerpt_hash"]) for reference in references}
    definitions = {definition["key"]: definition for definition in metadata_definitions}

    def validate(output: dict[str, Any]) -> None:
        compiled = DecisionSpecificationCompilationOutput.model_validate(output)
        specification = compiled.decision_specification
        for key, question in specification.questions.items():
            for reference in question.source_refs:
                if reference.task_version_id != task_version_id:
                    raise ValueError(f"{key} cites a different task version")
                if (reference.heading, reference.excerpt_hash) not in valid_references:
                    raise ValueError(f"{key} cites a source reference outside the supplied catalog")
            if question.field_mapping is not None:
                definition = definitions.get(question.field_mapping.metadata_definition_key)
                if definition is None:
                    raise ValueError(f"{key} maps to an unavailable metadata definition")
                _validate_mapped_value(key, question.field_mapping.value, definition)
        for omission in compiled.omissions:
            for reference in omission.source_refs:
                if reference.task_version_id != task_version_id:
                    raise ValueError(f"Omission {omission.subject} cites a different task version")
                if (reference.heading, reference.excerpt_hash) not in valid_references:
                    raise ValueError(
                        f"Omission {omission.subject} cites a source reference outside the supplied catalog"
                    )

    return validate


def _validate_mapped_value(question_key: str, value: Any, definition: dict[str, Any]) -> None:
    field_type = definition["type"]
    if field_type == "BOOLEAN" and not isinstance(value, bool):
        raise ValueError(f"{question_key} must map a boolean value to {definition['key']}")
    if field_type == "INTEGER" and (not isinstance(value, int) or isinstance(value, bool)):
        raise ValueError(f"{question_key} must map an integer value to {definition['key']}")
    if field_type == "DECIMAL" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise ValueError(f"{question_key} must map a numeric value to {definition['key']}")
    if field_type in {"TEXT", "LONG_TEXT", "DATE", "DATETIME"} and not isinstance(value, str):
        raise ValueError(f"{question_key} must map a string value to {definition['key']}")
    if field_type == "ENUM":
        allowed = {
            str(item.get("key"))
            for item in definition.get("allowed_values") or []
            if isinstance(item, dict) and item.get("status", "ACTIVE") == "ACTIVE" and item.get("key")
        }
        values = value if isinstance(value, list) else [value]
        if any(str(item) not in allowed for item in values):
            raise ValueError(f"{question_key} maps an unknown enum value to {definition['key']}")


def _deterministic_warnings(specification: DecisionSpecification) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for key, question in specification.questions.items():
        if isinstance(question, ChoiceDecisionQuestion) and not FALLBACK_OPTION_KEYS.intersection(question.criteria):
            warnings.append(
                {
                    "code": "CHOICE_WITHOUT_FALLBACK",
                    "message": "Confirm that the Choice options are exhaustive or add a fallback option.",
                    "question_key": key,
                }
            )
    return warnings
