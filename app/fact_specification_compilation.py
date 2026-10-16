"""Compile a pinned Review Guidance revision into a draft Fact Development Specification.

Mirrors the Decision Specification compiler: queueing records a WorkflowRun and step, the DBOS worker calls the
pinned managed skill, every candidate output is validated deterministically (source hashes and coding mappings), and
only a valid specification is attached to the draft task version as READY. A human reviews and may edit it before
publication.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_models import resolve_agent_model
from app.analysis_tasks import (
    MatterAnalysisTaskConflict,
    json_content_hash,
    set_fact_development_specification,
)
from app.config import Settings, get_settings
from app.fact_development_skills import FACT_SPECIFICATION_COMPILER_ROLE
from app.fact_specifications import (
    FactDevelopmentSpecification,
    FactDevelopmentSpecificationCompilationOutput,
    FactSpecificationError,
    mappable_metadata_definitions,
    metadata_definition_catalog,
    pinned_guidance_revision,
    task_version_source_catalog,
    validate_fact_development_specification,
)
from app.models import (
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    MatterDefinition,
    SkillDefinitionVersion,
    WorkflowRun,
    WorkflowStepRun,
)
from app.skill_execution import execute_skill_run
from app.workflow_specs import FACT_SPECIFICATION_COMPILATION_SPEC, binding_snapshot, resolve_workflow_skill_bindings
from app.workflows.dispatcher import enqueue_fact_specification_compilation

COMPILER_REPAIR_RULES = (
    "compiler repair rules: copy source, heading, and excerpt_hash exactly from source_reference_catalog; map each "
    "issue only to fields in metadata_definitions using that field's match_type and an active value key; omit an "
    "issue rather than guessing a mapping; issue keys must be unique and match ^[a-z][a-z0-9_]{0,99}$."
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_compilable_version(task: MatterAnalysisTask, task_version: MatterAnalysisTaskVersion) -> None:
    if task.task_type != "FACT_DEVELOPMENT":
        raise MatterAnalysisTaskConflict("Only fact-development tasks compile a Fact Development Specification")
    if task_version.status != "DRAFT":
        raise MatterAnalysisTaskConflict("Only a draft task version can be compiled")


def _progress_context(catalog: list[dict[str, Any]], definitions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "source_reference_count": len(catalog),
        "guidance_reference_count": sum(entry["source"] == "REVIEW_GUIDANCE" for entry in catalog),
        "mappable_field_count": len(definitions),
    }


def queue_fact_specification_compilation(
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
    if task_version is None:
        raise MatterAnalysisTaskConflict("Matter Analysis Task version not found")
    _require_compilable_version(locked_task, task_version)
    if task_version.compilation_status == "GENERATING":
        raise MatterAnalysisTaskConflict("Fact Development Specification compilation is already running")
    try:
        catalog = task_version_source_catalog(db, task_version)
    except FactSpecificationError as exc:
        raise MatterAnalysisTaskConflict(str(exc)) from exc
    if not any(entry["source"] == "REVIEW_GUIDANCE" for entry in catalog):
        raise MatterAnalysisTaskConflict("The pinned Review Guidance revision has no substantive guidance")
    definitions = metadata_definition_catalog(mappable_metadata_definitions(db, matter.id).values())
    if not definitions:
        raise MatterAnalysisTaskConflict("The matter has no active coding fields that could hold issue coding")
    try:
        resolved = resolve_workflow_skill_bindings(
            db,
            workflow_key=FACT_SPECIFICATION_COMPILATION_SPEC.key,
            tenant_id=matter.client.tenant_id,
        )
    except ValueError as exc:
        raise MatterAnalysisTaskConflict(f"Fact specification compiler is not configured: {exc}") from exc
    skill_version = resolved[FACT_SPECIFICATION_COMPILER_ROLE][2]
    resolved_model = resolve_agent_model(skill_version.model_key, settings)
    workflow_id = f"analysis-task:{task.id}:version:{version_number}:fact-compile:{uuid.uuid4()}"
    workflow = WorkflowRun(
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        matter_id=matter.id,
        workflow_key=FACT_SPECIFICATION_COMPILATION_SPEC.key,
        code_version=FACT_SPECIFICATION_COMPILATION_SPEC.code_version,
        dbos_workflow_id=workflow_id,
        status="QUEUED",
        input_snapshot={
            "task_id": str(task.id),
            "task_version_id": str(task_version.id),
            "version": task_version.version,
            "definition_content_hash": task_version.definition_content_hash,
            "review_guidance_revision_id": str(task_version.review_guidance_revision_id),
            "review_guidance_content_hash": task_version.review_guidance_content_hash,
            "source_reference_catalog_hash": json_content_hash(catalog),
        },
        binding_snapshot=binding_snapshot(resolved),
        configuration_snapshot={"resolved_models": {FACT_SPECIFICATION_COMPILER_ROLE: resolved_model}},
        progress={
            "stage": "QUEUED",
            "message": "Compilation is queued for the workflow worker.",
            "validation_attempt": 0,
            "validation_failures": [],
            **_progress_context(catalog, definitions),
        },
        initiated_by_user_id=initiated_by_user_id,
    )
    db.add(workflow)
    db.flush()
    db.add(
        WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key=FACT_SPECIFICATION_COMPILER_ROLE,
            ordinal=1,
            status="QUEUED",
            total_count=1,
        )
    )
    task_version.compilation_status = "GENERATING"
    task_version.compiler_workflow_run_id = workflow.id
    task_version.validation_report = {"status": "RUNNING", "errors": [], "warnings": []}
    db.flush()
    enqueue_fact_specification_compilation(db, workflow_id, str(task_version.id))
    return task_version, workflow


def parse_fact_compiler_wire_output(output: dict[str, Any]) -> FactDevelopmentSpecificationCompilationOutput:
    raw = output.get("compiled_output_json")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"compiled_output_json must contain a non-empty JSON string; {COMPILER_REPAIR_RULES}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"compiled_output_json is not valid JSON: {exc.msg}; {COMPILER_REPAIR_RULES}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"compiled_output_json must decode to a JSON object; {COMPILER_REPAIR_RULES}")  # noqa: TRY004
    try:
        return FactDevelopmentSpecificationCompilationOutput.model_validate(parsed)
    except ValueError as exc:
        raise ValueError(f"{exc}; {COMPILER_REPAIR_RULES}") from exc


def fact_compiler_output_validator(
    *,
    source_catalog: list[dict[str, Any]],
    definitions: dict[str, Any],
):
    def validate(output: dict[str, Any]) -> None:
        compiled = parse_fact_compiler_wire_output(output)
        valid_references = {
            (entry["source"], entry["heading"], entry["excerpt_hash"]) for entry in source_catalog
        }
        for omission in compiled.omissions:
            for reference in omission.source_refs:
                if (reference.source, reference.heading, reference.excerpt_hash) not in valid_references:
                    raise ValueError(
                        f"Omission {omission.subject} cites a source reference outside the supplied catalog; "
                        f"{COMPILER_REPAIR_RULES}"
                    )
        missing_rationales = sorted(
            issue.key
            for issue in compiled.fact_development_specification.issues
            if not compiled.issue_rationales.get(issue.key, "").strip()
        )
        if missing_rationales:
            raise ValueError("issue_rationales is missing: " + ", ".join(missing_rationales))
        try:
            validate_fact_development_specification(
                compiled.fact_development_specification,
                source_catalog=source_catalog,
                definitions=definitions,
            )
        except FactSpecificationError as exc:
            raise ValueError(f"{exc}; {COMPILER_REPAIR_RULES}") from exc

    return validate


def compile_fact_specification_version(
    db: Session,
    task_version_id: uuid.UUID,
    *,
    model: Any | None = None,
) -> dict[str, Any]:
    task_version = db.get(MatterAnalysisTaskVersion, task_version_id)
    if task_version is None or task_version.compiler_workflow_run_id is None:
        raise MatterAnalysisTaskConflict("Fact specification compilation is not queued")
    task = db.get(MatterAnalysisTask, task_version.matter_analysis_task_id)
    workflow = db.get(WorkflowRun, task_version.compiler_workflow_run_id)
    if task is None or workflow is None:
        raise MatterAnalysisTaskConflict("Fact specification compilation references are incomplete")
    _require_compilable_version(task, task_version)
    if task.current_version != task_version.version or task_version.compilation_status != "GENERATING":
        raise MatterAnalysisTaskConflict("Fact specification compilation is stale")
    step = db.scalar(
        select(WorkflowStepRun).where(
            WorkflowStepRun.workflow_run_id == workflow.id,
            WorkflowStepRun.ordinal == 1,
        )
    )
    role_snapshot = workflow.binding_snapshot.get(FACT_SPECIFICATION_COMPILER_ROLE) or {}
    skill_version_id = role_snapshot.get("skill_definition_version_id")
    skill_version = db.get(SkillDefinitionVersion, uuid.UUID(skill_version_id)) if skill_version_id else None
    matter = db.get(Matter, task.matter_id)
    if step is None or skill_version is None or matter is None:
        raise MatterAnalysisTaskConflict("Pinned compiler workflow configuration is unavailable")

    guidance_revision = pinned_guidance_revision(db, task_version)
    guidance = db.get(MatterDefinition, guidance_revision.matter_definition_id)
    catalog = task_version_source_catalog(db, task_version)
    if json_content_hash(catalog) != workflow.input_snapshot.get("source_reference_catalog_hash"):
        raise MatterAnalysisTaskConflict("The pinned guidance or task definition changed after compilation was queued")
    definitions = mappable_metadata_definitions(db, matter.id)
    definition_catalog = metadata_definition_catalog(definitions.values())
    prior = _prior_published_specification(db, task)
    validator = fact_compiler_output_validator(source_catalog=catalog, definitions=definitions)

    now = utcnow()
    workflow.status = "RUNNING"
    workflow.started_at = workflow.started_at or now
    workflow.progress = {
        **_progress_context(catalog, definition_catalog),
        "stage": "REQUESTING_MODEL",
        "message": "The compiler is drafting the issue catalog.",
        "validation_attempt": 0,
        "validation_failures": [],
    }
    step.status = "RUNNING"
    step.started_at = step.started_at or now
    db.commit()

    validation_attempt = 0
    validation_failures: list[dict[str, Any]] = []

    def validate_with_progress(output: dict[str, Any]) -> None:
        nonlocal validation_attempt
        validation_attempt += 1
        workflow.progress = {
            **workflow.progress,
            "stage": "VALIDATING_OUTPUT",
            "message": f"Validating candidate {validation_attempt}.",
            "validation_attempt": validation_attempt,
        }
        db.commit()
        try:
            validator(output)
        except ValueError as exc:
            validation_failures.append({"attempt": validation_attempt, "message": " ".join(str(exc).split())[:1200]})
            workflow.progress = {
                **workflow.progress,
                "stage": "REPAIRING_OUTPUT",
                "message": f"Candidate {validation_attempt} failed validation; the compiler is repairing its output.",
                "validation_failures": validation_failures[-4:],
            }
            db.commit()
            raise

    pinned_model = workflow.configuration_snapshot.get("resolved_models", {}).get(FACT_SPECIFICATION_COMPILER_ROLE)
    output, skill_run = asyncio.run(
        execute_skill_run(
            db,
            workflow=workflow,
            step=step,
            skill_version=skill_version,
            scope_type="MATTER_ANALYSIS_TASK_VERSION",
            scope_id=task_version.id,
            stable_context={
                "source_reference_catalog": catalog,
                "metadata_definitions": definition_catalog,
                "specification_schema": FactDevelopmentSpecification.model_json_schema(mode="validation"),
                "compiled_output_schema": FactDevelopmentSpecificationCompilationOutput.model_json_schema(
                    mode="validation"
                ),
            },
            dynamic_input={
                "task": {
                    "task_id": str(task.id),
                    "task_version_id": str(task_version.id),
                    "version": task_version.version,
                    "key": task.key,
                    "name": task.name,
                    "description": task.description,
                    "definition_content_hash": task_version.definition_content_hash,
                },
                "review_guidance": {
                    "review_guidance_id": str(guidance_revision.matter_definition_id),
                    "name": guidance.name if guidance is not None else None,
                    "revision": guidance_revision.revision,
                    "content_hash": task_version.review_guidance_content_hash,
                },
                "prior_published_specification": prior,
            },
            cache_identity={
                "tenant_id": str(workflow.tenant_id),
                "matter_id": str(matter.id),
                "task_id": str(task.id),
                "task_version_id": str(task_version.id),
                "definition_content_hash": task_version.definition_content_hash,
                "review_guidance_content_hash": task_version.review_guidance_content_hash,
                "skill_version_id": str(skill_version.id),
                "output_schema_key": skill_version.output_schema_key,
            },
            output_validators=(validate_with_progress,),
            usage_job_type="FACT_SPECIFICATION_COMPILATION",
            model=model if model is not None else pinned_model,
        )
    )
    compiled = parse_fact_compiler_wire_output(output)
    set_fact_development_specification(
        db,
        task_id=task.id,
        version_number=task_version.version,
        specification=compiled.fact_development_specification,
        compiler_skill_definition_version_id=skill_version.id,
        compiler_skill_run_id=skill_run.id,
        compiler_model_configuration={
            "model_key": skill_version.model_key,
            "resolved_model": pinned_model,
            "model_policy": skill_version.model_policy,
            "limits": skill_version.limits,
        },
        validation_report={
            "warnings": [warning.model_dump(mode="json") for warning in compiled.warnings],
            "issue_rationales": compiled.issue_rationales,
            "omissions": [omission.model_dump(mode="json") for omission in compiled.omissions],
        },
        source_provenance={
            "specification_origin": "COMPILER",
            "compiler_workflow_run_id": str(workflow.id),
            "review_guidance_revision_id": str(task_version.review_guidance_revision_id),
            "review_guidance_content_hash": task_version.review_guidance_content_hash,
            "source_reference_catalog_hash": json_content_hash(catalog),
        },
    )
    step.status = "COMPLETED"
    step.completed_count = 1
    step.completed_at = utcnow()
    workflow.status = "COMPLETED"
    workflow.progress = {
        **workflow.progress,
        "stage": "COMPLETED",
        "message": "The Fact Development Specification passed validation and was saved.",
        "issue_count": len(compiled.fact_development_specification.issues),
        "omission_count": len(compiled.omissions),
        "warning_count": len(task_version.validation_report.get("warnings", [])),
    }
    workflow.completed_at = utcnow()
    db.commit()
    return compiled.fact_development_specification.model_dump(mode="json")


def fail_fact_specification_compilation(db: Session, task_version_id: uuid.UUID, message: str) -> None:
    task_version = db.get(MatterAnalysisTaskVersion, task_version_id)
    if task_version is None or task_version.compiler_workflow_run_id is None:
        return
    workflow = db.get(WorkflowRun, task_version.compiler_workflow_run_id)
    if workflow is not None:
        workflow.status = "FAILED"
        workflow.error_message = message[:4000]
        workflow.progress = {
            **(workflow.progress or {}),
            "stage": "FAILED",
            "message": "Fact Development Specification compilation failed.",
            "error": " ".join(message.split())[:1200],
        }
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


def _prior_published_specification(db: Session, task: MatterAnalysisTask) -> dict[str, Any] | None:
    if task.published_version is None:
        return None
    prior = db.scalar(
        select(MatterAnalysisTaskVersion).where(
            MatterAnalysisTaskVersion.matter_analysis_task_id == task.id,
            MatterAnalysisTaskVersion.version == task.published_version,
        )
    )
    return prior.fact_development_specification if prior is not None else None
