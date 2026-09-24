import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_models import resolve_agent_model
from app.audit import record_audit
from app.config import get_settings
from app.matter_definitions import MatterDefinitionError, append_matter_definition_revision
from app.models import (
    Matter,
    MatterDefinitionAssessmentQuestion,
    MatterDefinitionAssessmentRun,
    MatterDefinitionRevision,
    SkillDefinitionVersion,
    WorkflowRun,
    WorkflowStepRun,
)
from app.skill_execution import execute_skill_run
from app.workflow_specs import (
    MATTER_DEFINITION_ASSESSMENT_SPEC,
    binding_snapshot,
    resolve_workflow_skill_bindings,
)
from app.workflows.dispatcher import enqueue_definition_assessment_guidance_refinement


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def queue_guidance_refinement(
    db: Session,
    assessment_id: uuid.UUID,
    *,
    initiated_by_user_id: uuid.UUID,
) -> MatterDefinitionAssessmentRun:
    assessment = db.scalar(
        select(MatterDefinitionAssessmentRun)
        .where(MatterDefinitionAssessmentRun.id == assessment_id)
        .with_for_update()
    )
    if assessment is None:
        raise ValueError("Assessment not found")
    if assessment.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise ValueError("The assessment must be complete before guidance can be refined")
    if assessment.guidance_refinement_status in {"QUEUED", "RUNNING", "COMPLETED", "NOT_REQUIRED"}:
        return assessment

    questions = list(
        db.scalars(
            select(MatterDefinitionAssessmentQuestion)
            .where(MatterDefinitionAssessmentQuestion.assessment_run_id == assessment.id)
            .order_by(MatterDefinitionAssessmentQuestion.created_at, MatterDefinitionAssessmentQuestion.id)
        )
    )
    if not questions or any(question.status == "OPEN" for question in questions):
        return assessment
    if not any(question.status == "ANSWERED" for question in questions):
        assessment.guidance_refinement_status = "NOT_REQUIRED"
        assessment.guidance_refinement_error_message = None
        return assessment

    matter = db.get(Matter, assessment.matter_id)
    if matter is None:
        raise ValueError("Assessment matter is unavailable")
    resolved = resolve_workflow_skill_bindings(
        db,
        workflow_key=MATTER_DEFINITION_ASSESSMENT_SPEC.key,
        tenant_id=matter.client.tenant_id,
    )
    snapshots = binding_snapshot(resolved)
    refinement_binding = {"guidance_refinement": snapshots["guidance_refinement"]}
    refinement_model = resolve_agent_model(resolved["guidance_refinement"][2].model_key, get_settings())
    attempt_id = uuid.uuid4()
    workflow_id = f"definition-assessment:{assessment.id}:guidance-refinement:{attempt_id}"
    resolved_questions = [
        {
            "id": str(question.id),
            "question": question.question,
            "status": question.status,
            "answer": question.answer,
            "rationale": question.rationale,
        }
        for question in questions
    ]
    workflow = WorkflowRun(
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        matter_id=matter.id,
        workflow_key=MATTER_DEFINITION_ASSESSMENT_SPEC.key,
        code_version=MATTER_DEFINITION_ASSESSMENT_SPEC.code_version,
        dbos_workflow_id=workflow_id,
        status="QUEUED",
        input_snapshot={
            "assessment_id": str(assessment.id),
            "matter_definition_revision_id": str(assessment.matter_definition_revision_id),
            "resolved_questions": resolved_questions,
        },
        binding_snapshot=refinement_binding,
        configuration_snapshot={"resolved_models": {"guidance_refinement": refinement_model}},
        progress={},
        initiated_by_user_id=initiated_by_user_id,
    )
    db.add(workflow)
    db.flush()
    db.add(
        WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key="guidance_refinement",
            ordinal=1,
            status="QUEUED",
            total_count=1,
        )
    )
    assessment.guidance_refinement_status = "QUEUED"
    assessment.guidance_refinement_workflow_run_id = workflow.id
    assessment.guidance_refinement_error_message = None
    db.flush()
    enqueue_definition_assessment_guidance_refinement(db, workflow_id, str(assessment.id))
    return assessment


def _validate_refined_guidance(output: dict) -> None:
    content = output.get("content_markdown")
    changes = output.get("change_summary")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Refined guidance must contain Markdown")
    if not isinstance(changes, list) or not changes or any(not str(value).strip() for value in changes):
        raise ValueError("Refined guidance must include a change summary")


def create_guidance_revision(
    db: Session,
    assessment_id: uuid.UUID,
    *,
    model: Any | None = None,
) -> uuid.UUID:
    assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
    if assessment is None or assessment.guidance_refinement_workflow_run_id is None:
        raise ValueError("Guidance refinement is not queued")
    workflow = db.get(WorkflowRun, assessment.guidance_refinement_workflow_run_id)
    source_revision = db.get(MatterDefinitionRevision, assessment.matter_definition_revision_id)
    matter = db.get(Matter, assessment.matter_id)
    if workflow is None or source_revision is None or matter is None:
        raise ValueError("Guidance refinement references are incomplete")
    if assessment.refined_matter_definition_revision_id is not None:
        return assessment.refined_matter_definition_revision_id

    resolved_questions = workflow.input_snapshot.get("resolved_questions")
    if not isinstance(resolved_questions, list) or not resolved_questions:
        raise ValueError("Guidance refinement has no frozen question decisions")
    if any(
        not isinstance(question, dict) or question.get("status") not in {"ANSWERED", "DISMISSED"}
        for question in resolved_questions
    ):
        raise ValueError("Every frozen refinement question must be resolved")
    actor_user_id = workflow.initiated_by_user_id

    role = workflow.binding_snapshot.get("guidance_refinement") or {}
    version_id = role.get("skill_definition_version_id")
    if not version_id:
        raise ValueError("Guidance refinement has no pinned skill binding")
    version = db.get(SkillDefinitionVersion, uuid.UUID(version_id))
    step = db.scalar(
        select(WorkflowStepRun).where(
            WorkflowStepRun.workflow_run_id == workflow.id,
            WorkflowStepRun.ordinal == 1,
        )
    )
    if version is None or step is None:
        raise ValueError("Guidance refinement skill is unavailable")

    now = utcnow()
    assessment.guidance_refinement_status = "RUNNING"
    workflow.status = "RUNNING"
    workflow.started_at = workflow.started_at or now
    step.status = "RUNNING"
    step.started_at = step.started_at or now
    pinned_model = workflow.configuration_snapshot.get("resolved_models", {}).get("guidance_refinement")
    output, skill_run = asyncio.run(
        execute_skill_run(
            db,
            workflow=workflow,
            step=step,
            skill_version=version,
            scope_type="MATTER_DEFINITION_ASSESSMENT",
            scope_id=assessment.id,
            stable_context={"matter_definition": source_revision.content_markdown},
            dynamic_input={
                "resolved_questions": [
                    {
                        "question": question["question"],
                        "status": question["status"],
                        "answer": question.get("answer"),
                        "rationale": question["rationale"],
                    }
                    for question in resolved_questions
                ]
            },
            cache_identity={
                "tenant_id": str(workflow.tenant_id),
                "matter_id": str(matter.id),
                "assessment_id": str(assessment.id),
                "definition_content_hash": assessment.definition_content_hash,
                "skill_version_id": str(version.id),
            },
            output_validators=(_validate_refined_guidance,),
            model=model if model is not None else pinned_model if isinstance(pinned_model, str) else None,
        )
    )
    try:
        _, revision = append_matter_definition_revision(
            db,
            matter=matter,
            actor_user_id=actor_user_id,
            content_markdown=str(output["content_markdown"]).strip(),
            source_kind="ASSESSMENT_REFINEMENT",
            based_on_revision=source_revision.revision,
            source_skill_run_id=skill_run.id,
        )
    except MatterDefinitionError as exc:
        raise ValueError(str(exc)) from exc

    completed_at = utcnow()
    assessment.guidance_refinement_status = "COMPLETED"
    assessment.refined_matter_definition_revision_id = revision.id
    assessment.guidance_refinement_error_message = None
    workflow.status = "COMPLETED"
    workflow.completed_at = completed_at
    workflow.progress = {
        "matter_definition_revision_id": str(revision.id),
        "revision": revision.revision,
        "change_summary": output["change_summary"],
    }
    step.status = "COMPLETED"
    step.completed_count = 1
    step.completed_at = completed_at
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=actor_user_id,
        action="matter_definition.assessment_guidance_refined",
        target_type="matter_definition_assessment_run",
        target_id=assessment.id,
        details={
            "matter_id": str(matter.id),
            "source_revision": source_revision.revision,
            "created_revision": revision.revision,
            "skill_run_id": str(skill_run.id),
        },
    )
    db.commit()
    return revision.id


def fail_guidance_refinement(db: Session, assessment_id: uuid.UUID, message: str) -> None:
    assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
    if assessment is None:
        return
    assessment.guidance_refinement_status = "FAILED"
    assessment.guidance_refinement_error_message = message[:4000]
    workflow = (
        db.get(WorkflowRun, assessment.guidance_refinement_workflow_run_id)
        if assessment.guidance_refinement_workflow_run_id
        else None
    )
    if workflow is not None:
        workflow.status = "FAILED"
        workflow.error_message = message[:4000]
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
    db.commit()
