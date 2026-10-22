from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analysis_task_compilation import queue_analysis_task_compilation
from app.analysis_task_execution_profiles import (
    AnalysisTaskExecutionProfileError,
    create_execution_profile,
    execution_profiles_for_version,
    validate_dual_execution_profiles,
)
from app.analysis_tasks import (
    MatterAnalysisTaskError,
    create_analysis_task,
    publish_analysis_task_version,
)
from app.audit import record_audit
from app.authorization import ResourceRef, user_can
from app.config import get_settings
from app.embeddings.configuration import canonical_hash
from app.matter_definition_assessments import AssessmentError, start_assessment
from app.matter_definitions import (
    MatterDefinitionError,
    clone_guidance,
    create_guidance,
    get_guidance,
)
from app.models import (
    AnalysisTaskExecutionProfile,
    Client,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    MatterDefinition,
    MatterDefinitionAssessmentQuery,
    MatterDefinitionAssessmentQuestion,
    MatterDefinitionAssessmentRun,
    MatterDefinitionRevision,
    ReviewProgram,
    ReviewProgramAction,
    ReviewProgramChild,
    ReviewProgramEvent,
    ReviewProgramRun,
    ReviewProgramStageAttempt,
    ReviewProgramVersion,
    User,
    WorkflowRun,
    utcnow,
)
from app.notifications import (
    NotificationSource,
    PermissionAudience,
    UserAudience,
    publish_notification,
    resolve_source_notifications,
)
from app.review_workflow_templates import (
    BatchingPolicy,
    ReviewWorkflowStageSpec,
    ReviewWorkflowTemplateSpec,
    get_review_workflow_template,
)


class ReviewProgramError(ValueError):
    pass


TERMINAL_RUN_STATUSES = frozenset({"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"})
ACTIVE_RUN_STATUSES = frozenset({"QUEUED", "RUNNING", "WAITING_FOR_USER"})


def _validate_idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 500:
        raise ReviewProgramError("Idempotency key is invalid")
    return normalized


def _template_for_program(program: ReviewProgram) -> ReviewWorkflowTemplateSpec:
    template = get_review_workflow_template(program.template_key, version=program.template_version)
    if template.content_hash != program.template_content_hash:
        raise ReviewProgramError("Review Program template content has drifted")
    return template


def _stage_for_run(program: ReviewProgram, run: ReviewProgramRun) -> ReviewWorkflowStageSpec:
    template = _template_for_program(program)
    try:
        stage = template.stages[run.current_stage_ordinal - 1]
    except IndexError as exc:
        raise ReviewProgramError("Review Program run stage is invalid") from exc
    if stage.key != run.current_stage or stage.ordinal != run.current_stage_ordinal:
        raise ReviewProgramError("Review Program run stage does not match its template")
    return stage


def _current_stage_attempt(
    db: Session,
    run: ReviewProgramRun,
    *,
    for_update: bool = False,
) -> ReviewProgramStageAttempt:
    statement = (
        select(ReviewProgramStageAttempt)
        .where(
            ReviewProgramStageAttempt.run_id == run.id,
            ReviewProgramStageAttempt.stage_key == run.current_stage,
        )
        .order_by(ReviewProgramStageAttempt.attempt.desc())
        .limit(1)
    )
    if for_update:
        statement = statement.with_for_update()
    attempt = db.scalar(statement)
    if attempt is None:
        raise ReviewProgramError("Review Program run has no current stage attempt")
    return attempt


def get_review_program_run(
    db: Session,
    *,
    program: ReviewProgram,
    run_id: uuid.UUID,
    for_update: bool = False,
) -> ReviewProgramRun:
    statement = select(ReviewProgramRun).where(
        ReviewProgramRun.id == run_id,
        ReviewProgramRun.program_id == program.id,
    )
    if for_update:
        statement = statement.with_for_update()
    run = db.scalar(statement)
    if run is None:
        raise ReviewProgramError("Review Program run not found")
    return run


def _notification_route(db: Session, program: ReviewProgram, run: ReviewProgramRun) -> dict[str, str]:
    client_id = db.scalar(select(Matter.client_id).where(Matter.id == program.matter_id))
    if client_id is None:
        raise ReviewProgramError("Review Program matter is unavailable")
    return {
        "client_id": str(client_id),
        "matter_id": str(program.matter_id),
        "program_id": str(program.id),
        "run_id": str(run.id),
    }


def _run_audiences(program: ReviewProgram, run: ReviewProgramRun) -> tuple[UserAudience, ...]:
    return tuple(
        UserAudience(user_id)
        for user_id in dict.fromkeys((program.owner_user_id, run.initiated_by_user_id))
    )


def _notification_program_name(program: ReviewProgram) -> str:
    return " ".join(program.name.split())[:100]


def _publish_run_notification(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    type_key: str,
    template_data: dict[str, str],
    occurrence_key: str | None = None,
) -> None:
    publish_notification(
        db,
        type_key=type_key,
        scope=ResourceRef.matter(program.matter_id),
        source=NotificationSource("review_program_run", run.id),
        audiences=_run_audiences(program, run),
        template_data=template_data,
        route_params=_notification_route(db, program, run),
        idempotency_key=f"review-program:{run.id}:{type_key}:{occurrence_key or 'terminal'}",
        created_by_kind="WORKFLOW",
    )


def _action_contract(
    template: ReviewWorkflowTemplateSpec,
    stage: ReviewWorkflowStageSpec,
    *,
    qa_setup_state: str | None = None,
) -> tuple[str, str, list[dict[str, Any]]]:
    stop = {
        "key": "STOP",
        "label": "Stop program",
        "consequence": "Cancel this run while preserving its history and completed work.",
    }
    if stage.key == "MATTER_DEFINITION":
        return (
            "Set up the Matter Definition",
            (
                "Create a new definition, clone an existing definition into an independent lineage, or adopt an "
                "existing published definition. The selected revision will be assessed before it can be accepted."
            ),
            [
                {
                    "key": "CREATE",
                    "label": "Create definition",
                    "consequence": "Create and publish a new program-specific Matter Definition.",
                },
                {
                    "key": "CLONE",
                    "label": "Clone definition",
                    "consequence": "Create and publish an independent copy of an existing definition revision.",
                },
                {
                    "key": "ADOPT",
                    "label": "Adopt published definition",
                    "consequence": (
                        "Pin an existing published revision into an automatically named, independent program lineage."
                    ),
                },
                stop,
            ],
        )
    if stage.key == "DEFINITION_REFINEMENT":
        return (
            "Review assessment findings",
            (
                "Resolve every blocking assessment question. If a refinement draft was generated, publish a "
                "revision before continuing; the coordinator will assess that new revision in another round."
            ),
            [
                {
                    "key": "CONTINUE",
                    "label": "Check readiness and continue",
                    "consequence": (
                        "Proceed to acceptance when the assessed revision remains current, or assess a newly "
                        "published revision."
                    ),
                },
                stop,
            ],
        )
    if stage.key == "DEFINITION_ACCEPTANCE":
        return (
            "Accept the Matter Definition",
            (
                "Accept the assessed published revision and freeze its assessment provenance for Q&A setup, "
                "or request another assessment round."
            ),
            [
                {
                    "key": "ACCEPT",
                    "label": "Accept for first-pass review",
                    "consequence": "Freeze this definition revision and proceed to Q&A task setup.",
                },
                {
                    "key": "REASSESS",
                    "label": "Run another assessment",
                    "consequence": "Return to definition assessment using the current published revision.",
                },
                stop,
            ],
        )
    if stage.key == "QA_TASK_SETUP":
        if qa_setup_state == "READY_FOR_PUBLICATION":
            return (
                "Review and publish the generated Q&A task",
                (
                    "The generated Decision Specification passed deterministic validation. Review its mappings, "
                    "warnings, omissions, and two pinned execution profiles before publishing it."
                ),
                [
                    {
                        "key": "PUBLISH",
                        "label": "Publish Q&A task",
                        "consequence": "Freeze the task and both execution profiles for discovery Q&A.",
                    },
                    {
                        "key": "RECOMPILE",
                        "label": "Generate another draft",
                        "consequence": "Run the compiler again against the same accepted definition and fields.",
                    },
                    stop,
                ],
            )
        return (
            "Generate the first-pass Q&A task",
            (
                "Choose the issue and responsiveness fields the Q&A task may write. The coordinator will generate "
                "one task from the accepted Matter Definition and pin both existing model paths."
            ),
            [
                {
                    "key": "GENERATE",
                    "label": "Generate Q&A task",
                    "consequence": "Create the draft task, pin two execution profiles, and compile its specification.",
                },
                stop,
            ],
        )
    next_stage = template.stages[stage.ordinal] if stage.ordinal < len(template.stages) else None
    next_name = next_stage.name if next_stage is not None else "completion"
    return (
        f"Review {stage.name}",
        (
            f"Review the current {stage.name.lower()} state. Continuing completes this gate and moves the "
            f"program to {next_name.lower()}; stopping preserves all completed work and ends this run."
        ),
        [
            {
                "key": "CONTINUE",
                "label": f"Continue to {next_name}",
                "consequence": "Complete this gate and advance the program.",
            },
            stop,
        ],
    )


def _definition_stage_context(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "matter_definition_id": str(program.matter_definition_id) if program.matter_definition_id else None,
    }
    child = db.scalar(
        select(ReviewProgramChild)
        .where(
            ReviewProgramChild.run_id == run.id,
            ReviewProgramChild.child_type == "MATTER_DEFINITION_ASSESSMENT",
        )
        .order_by(ReviewProgramChild.created_at.desc(), ReviewProgramChild.id.desc())
        .limit(1)
    )
    if child is None:
        return context
    assessment = db.get(MatterDefinitionAssessmentRun, child.child_id)
    if assessment is None:
        return context
    questions = list(
        db.scalars(
            select(MatterDefinitionAssessmentQuestion).where(
                MatterDefinitionAssessmentQuestion.assessment_run_id == assessment.id
            )
        )
    )
    context["assessment"] = {
        "id": str(assessment.id),
        "status": assessment.status,
        "matter_definition_revision_id": str(assessment.matter_definition_revision_id),
        "review_batch_id": str(assessment.review_batch_id) if assessment.review_batch_id else None,
        "review_batch_run_id": str(assessment.review_batch_run_id) if assessment.review_batch_run_id else None,
        "requested_document_count": assessment.requested_document_count,
        "selected_count": assessment.selected_count,
        "summarized_count": assessment.summarized_count,
        "failed_count": assessment.failed_count,
        "synthesis_status": (assessment.synthesis_result or {}).get("status"),
        "guidance_refinement_status": assessment.guidance_refinement_status,
        "refined_matter_definition_revision_id": (
            str(assessment.refined_matter_definition_revision_id)
            if assessment.refined_matter_definition_revision_id
            else None
        ),
        "question_counts": {
            "open": sum(question.status == "OPEN" for question in questions),
            "open_blocking": sum(question.status == "OPEN" and question.blocking for question in questions),
            "answered": sum(question.status == "ANSWERED" for question in questions),
            "dismissed": sum(question.status == "DISMISSED" for question in questions),
        },
    }
    return context


def _qa_stage_context(db: Session, *, run: ReviewProgramRun) -> dict[str, Any]:
    setup = (run.progress_summary or {}).get("qa_task_setup") or {}
    task_id = setup.get("task_id")
    task_version_id = setup.get("task_version_id")
    workflow_id = setup.get("compiler_workflow_run_id")
    task = db.get(MatterAnalysisTask, uuid.UUID(task_id)) if task_id else None
    task_version = (
        db.get(MatterAnalysisTaskVersion, uuid.UUID(task_version_id)) if task_version_id else None
    )
    workflow = db.get(WorkflowRun, uuid.UUID(workflow_id)) if workflow_id else None
    profiles = (
        execution_profiles_for_version(db, task_version.id) if task_version is not None else []
    )
    return {
        "qa_task_setup_state": run.progress_summary.get("qa_task_setup_state"),
        "qa_task": {
            "id": str(task.id) if task is not None else None,
            "name": task.name if task is not None else None,
            "task_version_id": str(task_version.id) if task_version is not None else None,
            "version": task_version.version if task_version is not None else None,
            "status": task_version.status if task_version is not None else None,
            "compilation_status": task_version.compilation_status if task_version is not None else None,
            "definition_content_hash": (
                task_version.definition_content_hash if task_version is not None else None
            ),
            "specification_content_hash": (
                task_version.specification_content_hash if task_version is not None else None
            ),
            "destination_metadata_definition_ids": (
                task_version.destination_metadata_definition_ids if task_version is not None else []
            ),
            "validation_report": task_version.validation_report if task_version is not None else {},
        },
        "compiler": {
            "workflow_run_id": str(workflow.id) if workflow is not None else None,
            "status": workflow.status if workflow is not None else None,
            "progress": workflow.progress if workflow is not None else {},
            "error_message": workflow.error_message if workflow is not None else None,
        },
        "execution_profiles": [
            {
                "id": str(profile.id),
                "key": profile.key,
                "name": profile.name,
                "decision_provider": profile.decision_provider,
                "resolved_model": profile.resolved_model,
                "provider_adapter_version": profile.provider_adapter_version,
                "content_hash": profile.content_hash,
            }
            for profile in profiles
        ],
    }


def _create_action_for_stage(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    stage: ReviewWorkflowStageSpec,
    template: ReviewWorkflowTemplateSpec,
) -> ReviewProgramAction:
    qa_setup_state = str(run.progress_summary.get("qa_task_setup_state") or "")
    title, explanation, choices = _action_contract(
        template,
        stage,
        qa_setup_state=qa_setup_state,
    )
    decision_context = {
        "schema_version": 1,
        "program_version_id": str(run.current_program_version_id),
        "stage_key": stage.key,
        "stage_attempt_id": str(attempt.id),
        "stage_input": attempt.input_snapshot,
        **(
            _definition_stage_context(db, program=program, run=run)
            if stage.key in {"MATTER_DEFINITION", "DEFINITION_REFINEMENT", "DEFINITION_ACCEPTANCE"}
            else _qa_stage_context(db, run=run)
            if stage.key == "QA_TASK_SETUP"
            else {}
        ),
    }
    action = ReviewProgramAction(
        run_id=run.id,
        stage_attempt_id=attempt.id,
        stage_key=stage.key,
        action_type={
            "MATTER_DEFINITION": "MATTER_DEFINITION_SETUP",
            "DEFINITION_REFINEMENT": "MATTER_DEFINITION_REFINEMENT",
            "DEFINITION_ACCEPTANCE": "MATTER_DEFINITION_ACCEPTANCE",
            "QA_TASK_SETUP": (
                "QA_TASK_PUBLICATION" if qa_setup_state == "READY_FOR_PUBLICATION" else "QA_TASK_SETUP"
            ),
        }.get(stage.key, "STAGE_GATE"),
        title=title,
        explanation=explanation,
        decision_context=decision_context,
        preview_hash=canonical_hash({"context": decision_context, "choices": choices}),
        allowed_choices=choices,
        status="OPEN",
        authorization_context={"permission": "review_program.manage", "matter_id": str(program.matter_id)},
    )
    db.add(action)
    db.flush()
    run.pending_action_id = action.id
    run.status = "WAITING_FOR_USER"
    attempt.status = "WAITING"
    route = _notification_route(db, program, run)
    publish_notification(
        db,
        type_key="review_program.action_required",
        scope=ResourceRef.matter(program.matter_id),
        source=NotificationSource("review_program_action", action.id),
        audiences=(PermissionAudience("review_program.manage"),),
        template_data={"program_name": _notification_program_name(program), "action_title": action.title},
        route_params={**route, "action_id": str(action.id)},
        idempotency_key=f"review-program-action:{action.id}",
        created_by_kind="WORKFLOW",
    )
    return action


def _create_stage_attempt(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    stage: ReviewWorkflowStageSpec,
    attempt_number: int,
) -> ReviewProgramStageAttempt:
    template = _template_for_program(program)
    attempt = ReviewProgramStageAttempt(
        run_id=run.id,
        stage_key=stage.key,
        stage_ordinal=stage.ordinal,
        attempt=attempt_number,
        status="RUNNING",
        idempotency_key=f"run:{run.id}:stage:{stage.key}:attempt:{attempt_number}",
        input_snapshot={
            "schema_version": 1,
            "program_version_id": str(run.current_program_version_id),
            "batching_policy": stage.batching_policy,
        },
        policy_hashes={
            "template_content_hash": template.content_hash,
            "program_configuration_hash": run.frozen_configuration_hash,
        },
        started_at=utcnow(),
    )
    db.add(attempt)
    db.flush()
    if stage.requires_human_gate:
        _create_action_for_stage(
            db,
            program=program,
            run=run,
            attempt=attempt,
            stage=stage,
            template=template,
        )
    else:
        run.status = "RUNNING"
        run.pending_action_id = None
        if stage.key == "DEFINITION_ASSESSMENT":
            _launch_definition_assessment(db, program=program, run=run, attempt=attempt)
    return attempt


def tenant_id_for_matter(db: Session, matter: Matter) -> uuid.UUID:
    tenant_id = db.scalar(select(Client.tenant_id).where(Client.id == matter.client_id))
    if tenant_id is None:
        raise ReviewProgramError("Matter client is unavailable")
    return tenant_id


def get_review_program(
    db: Session,
    *,
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    for_update: bool = False,
) -> ReviewProgram:
    statement = select(ReviewProgram).where(
        ReviewProgram.id == program_id,
        ReviewProgram.matter_id == matter_id,
    )
    if for_update:
        statement = statement.with_for_update()
    program = db.scalar(statement)
    if program is None:
        raise ReviewProgramError("Review Program not found")
    return program


def current_review_program_version(db: Session, program: ReviewProgram) -> ReviewProgramVersion:
    if program.current_version_id is None:
        raise ReviewProgramError("Review Program has no current version")
    version = db.get(ReviewProgramVersion, program.current_version_id)
    if version is None or version.program_id != program.id:
        raise ReviewProgramError("Review Program current version is unavailable")
    return version


def _create_review_program_version(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    actor_user_id: uuid.UUID,
    configuration_stage: str,
    configuration_updates: dict[str, Any],
    source_hash_updates: dict[str, Any],
) -> ReviewProgramVersion:
    current = current_review_program_version(db, program)
    configuration = {**current.configuration_snapshot, **configuration_updates}
    source_hashes = {**current.source_hashes, **source_hash_updates}
    next_version = (db.scalar(select(func.max(ReviewProgramVersion.version)).where(
        ReviewProgramVersion.program_id == program.id
    )) or 0) + 1
    version = ReviewProgramVersion(
        program_id=program.id,
        version=next_version,
        predecessor_version_id=current.id,
        template_key=program.template_key,
        template_version=program.template_version,
        template_content_hash=program.template_content_hash,
        configuration_stage=configuration_stage,
        configuration_snapshot=configuration,
        source_hashes=source_hashes,
        content_hash=canonical_hash({"configuration": configuration, "source_hashes": source_hashes}),
        created_by_user_id=actor_user_id,
    )
    db.add(version)
    db.flush()
    program.current_version_id = version.id
    program.row_version += 1
    run.current_program_version_id = version.id
    run.frozen_configuration_hash = version.content_hash
    return version


def _launch_definition_assessment(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
) -> MatterDefinitionAssessmentRun | None:
    try:
        if program.matter_definition_id is None:
            raise ReviewProgramError("Review Program has no Matter Definition")
        definition = get_guidance(
            db,
            matter_id=program.matter_id,
            guidance_id=program.matter_definition_id,
        )
        if definition.published_revision is None:
            raise ReviewProgramError("Matter Definition must have a published revision before assessment")
        matter = db.get(Matter, program.matter_id)
        if matter is None:
            raise ReviewProgramError("Review Program matter is unavailable")
        configuration = current_review_program_version(db, program).configuration_snapshot
        policy = configuration.get("matter_definition_policy") or {}
        round_number = int(run.progress_summary.get("definition_assessment_round", 0)) + 1
        maximum_rounds = int(policy.get("maximum_assessment_rounds", 3))
        if round_number > maximum_rounds:
            raise ReviewProgramError(
                f"Matter Definition assessment limit of {maximum_rounds} rounds has been reached"
            )
        assessment = start_assessment(
            db,
            matter=matter,
            matter_definition_id=definition.id,
            initiated_by_user_id=run.initiated_by_user_id,
            settings=get_settings(),
            name=f"{program.name} · definition assessment {round_number}",
            revision_number=definition.published_revision,
            target_document_count=int(policy.get("assessment_target_document_count", 500)),
            control_sample_size=int(policy.get("assessment_control_sample_size", 0)),
            use_batching=bool(policy.get("assessment_use_batching", True)),
            acknowledge_large_run_warning=bool(policy.get("acknowledge_large_run_warning", False)),
            notification_parent_type="REVIEW_PROGRAM_RUN",
            notification_parent_id=run.id,
        )
        link_review_program_child(
            db,
            program=program,
            run=run,
            stage_attempt=attempt,
            purpose="DEFINITION_ASSESSMENT",
            child_type="MATTER_DEFINITION_ASSESSMENT",
            child_id=assessment.id,
            derivation_key=f"definition-assessment:{attempt.id}",
            batching_policy="AD_HOC_BATCH",
            context={
                "round": round_number,
                "matter_definition_id": str(definition.id),
                "matter_definition_revision_id": str(assessment.matter_definition_revision_id),
                "workflow_run_id": str(assessment.workflow_run_id),
            },
        )
        attempt.input_snapshot = {
            **attempt.input_snapshot,
            "assessment_id": str(assessment.id),
            "matter_definition_id": str(definition.id),
            "matter_definition_revision_id": str(assessment.matter_definition_revision_id),
        }
        run.progress_summary = {
            **run.progress_summary,
            "definition_assessment_round": round_number,
            "current_assessment_id": str(assessment.id),
        }
        return assessment
    except (AssessmentError, MatterDefinitionError, ReviewProgramError) as exc:
        fail_review_program_stage(
            db,
            program=program,
            run=run,
            error_code="DEFINITION_ASSESSMENT_LAUNCH_FAILED",
            error_summary=str(exc),
            retriable=True,
        )
        return None


def _starting_configuration(
    *,
    template: ReviewWorkflowTemplateSpec,
    definition: MatterDefinition | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    configuration = {
        "schema_version": 1,
        "template_key": template.key,
        "template_version": template.version,
        "starting_stage": "MATTER_DEFINITION",
        "matter_definition": (
            {
                "id": str(definition.id),
                "current_revision": definition.current_revision,
                "published_revision": definition.published_revision,
            }
            if definition is not None
            else None
        ),
        "matter_definition_policy": {
            "assessment_target_document_count": 500,
            "assessment_control_sample_size": 0,
            "assessment_use_batching": True,
            "acknowledge_large_run_warning": False,
            "maximum_assessment_rounds": 3,
        },
    }
    return configuration, {"template_content_hash": template.content_hash}


def create_review_program(
    db: Session,
    *,
    matter: Matter,
    actor: User,
    template_key: str,
    key: str,
    name: str,
    description: str | None,
    owner_user_id: uuid.UUID | None,
    matter_definition_id: uuid.UUID | None,
) -> tuple[ReviewProgram, ReviewProgramVersion]:
    if matter.status != "ACTIVE":
        raise ReviewProgramError("Review Programs can be created only in an active matter")
    template = get_review_workflow_template(template_key)
    tenant_id = tenant_id_for_matter(db, matter)
    owner = db.get(User, owner_user_id or actor.id)
    if owner is None or owner.tenant_id != tenant_id or owner.status != "ACTIVE":
        raise ReviewProgramError("Review Program owner is unavailable")
    if not user_can(db, owner, "review_program.view", ResourceRef.matter(matter.id)):
        raise ReviewProgramError("Review Program owner cannot view this matter")

    definition = None
    if matter_definition_id is not None:
        definition = db.get(MatterDefinition, matter_definition_id)
        if definition is None or definition.matter_id != matter.id or definition.status != "ACTIVE":
            raise ReviewProgramError("Matter Definition is unavailable")

    existing = db.scalar(
        select(ReviewProgram.id).where(
            ReviewProgram.matter_id == matter.id,
            ReviewProgram.key == key,
        )
    )
    if existing is not None:
        raise ReviewProgramError("A Review Program with this key already exists in the matter")

    program = ReviewProgram(
        tenant_id=tenant_id,
        matter_id=matter.id,
        template_key=template.key,
        template_version=template.version,
        template_content_hash=template.content_hash,
        key=key,
        name=name.strip(),
        description=description.strip() if description else None,
        status="DRAFT",
        matter_definition_id=definition.id if definition is not None else None,
        owner_user_id=owner.id,
        created_by_user_id=actor.id,
    )
    db.add(program)
    db.flush()

    configuration, source_hashes = _starting_configuration(template=template, definition=definition)
    version = ReviewProgramVersion(
        program_id=program.id,
        version=1,
        predecessor_version_id=None,
        template_key=template.key,
        template_version=template.version,
        template_content_hash=template.content_hash,
        configuration_stage="MATTER_DEFINITION",
        configuration_snapshot=configuration,
        source_hashes=source_hashes,
        content_hash=canonical_hash(
            {
                "configuration": configuration,
                "source_hashes": source_hashes,
            }
        ),
        created_by_user_id=actor.id,
    )
    db.add(version)
    db.flush()
    program.current_version_id = version.id

    record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor.id,
        action="review_program.created",
        target_type="review_program",
        target_id=program.id,
        details={
            "matter_id": str(matter.id),
            "template_key": template.key,
            "template_version": template.version,
            "program_key": program.key,
            "owner_user_id": str(owner.id),
            "matter_definition_id": str(definition.id) if definition is not None else None,
            "program_version_id": str(version.id),
        },
    )
    return program, version


def start_review_program_run(
    db: Session,
    *,
    program: ReviewProgram,
    actor: User,
    idempotency_key: str,
) -> ReviewProgramRun:
    key = _validate_idempotency_key(idempotency_key)
    deterministic_id = uuid.uuid5(program.id, f"review-program-run:{key}")
    existing = db.get(ReviewProgramRun, deterministic_id)
    if existing is not None:
        if existing.program_id != program.id:
            raise ReviewProgramError("Review Program run idempotency conflict")
        return existing
    if program.status in {"SUSPENDED", "ARCHIVED"}:
        raise ReviewProgramError("Review Program cannot be started in its current status")

    active_run = db.scalar(
        select(ReviewProgramRun).where(
            ReviewProgramRun.program_id == program.id,
            ReviewProgramRun.status.in_(ACTIVE_RUN_STATUSES),
        )
    )
    if active_run is not None:
        raise ReviewProgramError("Review Program already has an active run")

    version = current_review_program_version(db, program)
    template = _template_for_program(program)
    first_stage = template.stages[0]
    now = utcnow()
    run = ReviewProgramRun(
        id=deterministic_id,
        tenant_id=program.tenant_id,
        matter_id=program.matter_id,
        program_id=program.id,
        starting_program_version_id=version.id,
        current_program_version_id=version.id,
        status="RUNNING",
        current_stage=first_stage.key,
        current_stage_ordinal=first_stage.ordinal,
        progress_summary={"schema_version": 1, "completed_stages": [], "start_key": key},
        frozen_configuration_hash=version.content_hash,
        initiated_by_user_id=actor.id,
        started_at=now,
    )
    db.add(run)
    db.flush()
    _create_stage_attempt(
        db,
        program=program,
        run=run,
        stage=first_stage,
        attempt_number=1,
    )
    program.status = "ACTIVE"
    program.current_run_id = run.id
    program.row_version += 1
    record_audit(
        db,
        tenant_id=program.tenant_id,
        actor_user_id=actor.id,
        action="review_program.run_started",
        target_type="review_program_run",
        target_id=run.id,
        details={"program_id": str(program.id), "program_version_id": str(version.id)},
    )
    return run


def setup_review_program_definition(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    action_id: uuid.UUID,
    actor: User,
    preview_hash: str,
    mode: str,
    existing_definition_id: uuid.UUID | None,
    source_revision: int | None,
    definition_key: str | None,
    name: str | None,
    description: str | None,
    content_markdown: str | None,
    assessment_target_document_count: int,
    assessment_control_sample_size: int,
    assessment_use_batching: bool,
    acknowledge_large_run_warning: bool,
) -> ReviewProgramRun:
    action = db.scalar(
        select(ReviewProgramAction)
        .where(ReviewProgramAction.id == action_id, ReviewProgramAction.run_id == run.id)
        .with_for_update()
    )
    if action is None or action.action_type != "MATTER_DEFINITION_SETUP":
        raise ReviewProgramError("Matter Definition setup action not found")
    if action.status == "RESOLVED":
        resolution = action.resolution_payload or {}
        if resolution.get("choice_key") != mode:
            raise ReviewProgramError("Matter Definition setup was already resolved differently")
        return run
    if run.current_stage != "MATTER_DEFINITION" or run.status != "WAITING_FOR_USER":
        raise ReviewProgramError("Review Program is not waiting for Matter Definition setup")
    if action.preview_hash != preview_hash:
        raise ReviewProgramError("Matter Definition setup preview is stale")
    if mode not in {"CREATE", "CLONE", "ADOPT"}:
        raise ReviewProgramError("Matter Definition setup mode is unsupported")
    matter = db.get(Matter, program.matter_id)
    if matter is None:
        raise ReviewProgramError("Review Program matter is unavailable")
    source_definition_id: uuid.UUID | None = None
    try:
        if mode == "CREATE":
            if not definition_key or not name or not (content_markdown or "").strip():
                raise ReviewProgramError("New Matter Definition requires a key, name, and guidance content")
            definition, revision = create_guidance(
                db,
                matter=matter,
                actor_user_id=actor.id,
                key=definition_key,
                name=name.strip(),
                description=description.strip() if description else None,
                content_markdown=str(content_markdown).strip(),
                source_kind="USER_EDIT",
            )
        else:
            source_id = existing_definition_id or program.matter_definition_id
            if source_id is None:
                raise ReviewProgramError("Select an existing Matter Definition")
            source = get_guidance(db, matter_id=matter.id, guidance_id=source_id)
            source_definition_id = source.id
            selected_revision = source_revision or source.published_revision
            if selected_revision is None:
                raise ReviewProgramError("Selected Matter Definition has no published revision")
            if mode == "CLONE":
                if not definition_key or not name:
                    raise ReviewProgramError("Cloned Matter Definition requires a key and name")
                clone_key = definition_key
                clone_name = name.strip()
                clone_description = description.strip() if description else None
            else:
                base = f"{program.key}-definition"[:88].rstrip("-_")
                clone_key = f"{base}-{str(program.id)[:8]}"
                clone_name = f"{program.name} definition"
                clone_description = f"Program-owned lineage adopted from {source.name}."
            definition, revision = clone_guidance(
                db,
                matter=matter,
                source=source,
                actor_user_id=actor.id,
                key=clone_key,
                name=clone_name,
                description=clone_description,
                source_revision_number=selected_revision,
            )
    except MatterDefinitionError as exc:
        raise ReviewProgramError(str(exc)) from exc
    definition.published_revision = revision.revision
    program.matter_definition_id = definition.id
    content_hash = hashlib.sha256(revision.content_markdown.encode()).hexdigest()
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=actor.id,
        configuration_stage="DEFINITION_ASSESSMENT",
        configuration_updates={
            "matter_definition": {
                "id": str(definition.id),
                "current_revision": definition.current_revision,
                "published_revision": definition.published_revision,
                "setup_mode": mode,
                "source_definition_id": str(source_definition_id) if source_definition_id else None,
            },
            "matter_definition_policy": {
                "assessment_target_document_count": assessment_target_document_count,
                "assessment_control_sample_size": assessment_control_sample_size,
                "assessment_use_batching": assessment_use_batching,
                "acknowledge_large_run_warning": acknowledge_large_run_warning,
                "maximum_assessment_rounds": 3,
            },
        },
        source_hash_updates={"matter_definition_content_hash": content_hash},
    )
    current_attempt = _current_stage_attempt(db, run, for_update=True)
    current_attempt.policy_hashes = {
        **current_attempt.policy_hashes,
        "program_configuration_hash": version.content_hash,
    }
    resolve_review_program_action(
        db,
        program=program,
        run=run,
        action_id=action.id,
        actor=actor,
        preview_hash=preview_hash,
        choice_key=mode,
        reason="Matter Definition setup completed",
        input_payload={
            "matter_definition_id": str(definition.id),
            "matter_definition_revision_id": str(revision.id),
            "revision": revision.revision,
            "content_hash": content_hash,
            "program_version_id": str(version.id),
        },
    )
    record_audit(
        db,
        tenant_id=program.tenant_id,
        actor_user_id=actor.id,
        action="review_program.matter_definition_configured",
        target_type="review_program",
        target_id=program.id,
        details={
            "run_id": str(run.id),
            "setup_mode": mode,
            "matter_definition_id": str(definition.id),
            "matter_definition_revision_id": str(revision.id),
            "program_version_id": str(version.id),
        },
    )
    return run


def setup_review_program_qa_task(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    action_id: uuid.UUID,
    actor: User,
    preview_hash: str,
    task_key: str,
    name: str,
    description: str | None,
    destination_metadata_definition_ids: list[uuid.UUID],
) -> ReviewProgramRun:
    action = db.scalar(
        select(ReviewProgramAction)
        .where(ReviewProgramAction.id == action_id, ReviewProgramAction.run_id == run.id)
        .with_for_update()
    )
    if action is None or action.action_type != "QA_TASK_SETUP":
        raise ReviewProgramError("Q&A task setup action not found")
    if action.status == "RESOLVED":
        resolution = action.resolution_payload or {}
        if resolution.get("choice_key") != "GENERATE":
            raise ReviewProgramError("Q&A task setup was already resolved differently")
        return run
    if run.current_stage != "QA_TASK_SETUP" or run.status != "WAITING_FOR_USER":
        raise ReviewProgramError("Review Program is not waiting for Q&A task setup")
    if action.preview_hash != preview_hash:
        raise ReviewProgramError("Q&A task setup preview is stale")
    configuration = current_review_program_version(db, program).configuration_snapshot
    accepted = configuration.get("accepted_matter_definition") or {}
    revision_id = accepted.get("revision_id")
    content_hash = accepted.get("content_hash")
    revision = db.get(MatterDefinitionRevision, uuid.UUID(str(revision_id))) if revision_id else None
    if (
        revision is None
        or str(revision.matter_definition_id) != str(program.matter_definition_id)
        or hashlib.sha256(revision.content_markdown.encode()).hexdigest() != content_hash
    ):
        raise ReviewProgramError("Accepted Matter Definition provenance is unavailable or stale")
    matter = db.get(Matter, program.matter_id)
    if matter is None:
        raise ReviewProgramError("Review Program matter is unavailable")
    try:
        task, task_version = create_analysis_task(
            db,
            matter=matter,
            actor_user_id=actor.id,
            key=task_key,
            name=name,
            description=description,
            task_type="QUESTION_ANSWERING",
            decision_provider="JEV",
            definition_markdown=revision.content_markdown,
            destination_metadata_definition_ids=destination_metadata_definition_ids,
            scope_policy={"allowed_modes": ["PARTITIONED_SET"], "default_mode": "PARTITIONED_SET"},
        )
        task_version.source_provenance = {
            **task_version.source_provenance,
            "origin": "REVIEW_PROGRAM",
            "review_program_id": str(program.id),
            "review_program_run_id": str(run.id),
            "review_program_version_id": str(run.current_program_version_id),
            "accepted_matter_definition": accepted,
        }
        profiles = [
            create_execution_profile(
                db,
                matter=matter,
                task=task,
                task_version=task_version,
                key="jev",
                name="Jev review model",
                decision_provider="JEV",
                actor_user_id=actor.id,
            ),
            create_execution_profile(
                db,
                matter=matter,
                task=task,
                task_version=task_version,
                key="openai_decisions",
                name="OpenAI decisions model",
                decision_provider="OPENAI_DECISIONS",
                actor_user_id=actor.id,
            ),
        ]
        task_version, workflow = queue_analysis_task_compilation(
            db,
            matter=matter,
            task=task,
            version_number=task_version.version,
            initiated_by_user_id=actor.id,
            notification_parent_type="REVIEW_PROGRAM_RUN",
            notification_parent_id=run.id,
        )
    except (MatterAnalysisTaskError, AnalysisTaskExecutionProfileError) as exc:
        raise ReviewProgramError(str(exc)) from exc
    attempt = _current_stage_attempt(db, run, for_update=True)
    link_review_program_child(
        db,
        program=program,
        run=run,
        stage_attempt=attempt,
        purpose="QA_TASK",
        child_type="MATTER_ANALYSIS_TASK",
        child_id=task.id,
        derivation_key=f"qa-task:{attempt.id}",
        batching_policy="NONE",
        context={"task_version_id": str(task_version.id), "version": task_version.version},
    )
    link_review_program_child(
        db,
        program=program,
        run=run,
        stage_attempt=attempt,
        purpose="QA_TASK_COMPILATION",
        child_type="ANALYSIS_TASK_COMPILATION",
        child_id=workflow.id,
        derivation_key=f"qa-task-compilation:{workflow.id}",
        batching_policy="NONE",
        context={"task_id": str(task.id), "task_version_id": str(task_version.id)},
    )
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=actor.id,
        configuration_stage="QA_TASK_SETUP",
        configuration_updates={
            "qa_task_draft": {
                "task_id": str(task.id),
                "task_version_id": str(task_version.id),
                "version": task_version.version,
                "destination_metadata_definition_ids": task_version.destination_metadata_definition_ids,
                "execution_profile_ids": [str(profile.id) for profile in profiles],
                "compiler_workflow_run_id": str(workflow.id),
            }
        },
        source_hash_updates={
            "qa_task_definition_content_hash": task_version.definition_content_hash,
            "qa_execution_profile_hashes": [profile.content_hash for profile in profiles],
        },
    )
    run.progress_summary = {
        **run.progress_summary,
        "qa_task_setup_state": "COMPILING",
        "qa_task_setup": {
            "task_id": str(task.id),
            "task_version_id": str(task_version.id),
            "execution_profile_ids": [str(profile.id) for profile in profiles],
            "compiler_workflow_run_id": str(workflow.id),
        },
    }
    resolve_review_program_action(
        db,
        program=program,
        run=run,
        action_id=action.id,
        actor=actor,
        preview_hash=preview_hash,
        choice_key="GENERATE",
        reason="Q&A task generation started",
        input_payload={
            "task_id": str(task.id),
            "task_version_id": str(task_version.id),
            "execution_profile_ids": [str(profile.id) for profile in profiles],
            "compiler_workflow_run_id": str(workflow.id),
            "program_version_id": str(version.id),
        },
    )
    record_audit(
        db,
        tenant_id=program.tenant_id,
        actor_user_id=actor.id,
        action="review_program.qa_task_generation_started",
        target_type="review_program",
        target_id=program.id,
        details={
            "run_id": str(run.id),
            "task_id": str(task.id),
            "task_version_id": str(task_version.id),
            "execution_profile_ids": [str(profile.id) for profile in profiles],
            "compiler_workflow_run_id": str(workflow.id),
        },
    )
    return run


def publish_review_program_event(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    event_type: str,
    source_type: str,
    source_id: uuid.UUID,
    idempotency_key: str,
    stage_attempt_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> ReviewProgramEvent:
    key = _validate_idempotency_key(idempotency_key)
    existing = db.scalar(
        select(ReviewProgramEvent).where(
            ReviewProgramEvent.program_id == program.id,
            ReviewProgramEvent.idempotency_key == key,
        )
    )
    if existing is not None:
        if (
            existing.run_id != run.id
            or existing.event_type != event_type
            or existing.source_type != source_type
            or existing.source_id != source_id
        ):
            raise ReviewProgramError("Review Program event idempotency conflict")
        return existing
    event = ReviewProgramEvent(
        tenant_id=program.tenant_id,
        matter_id=program.matter_id,
        program_id=program.id,
        program_version_id=run.current_program_version_id,
        run_id=run.id,
        stage_attempt_id=stage_attempt_id,
        event_type=event_type,
        schema_version=1,
        source_type=source_type,
        source_id=source_id,
        payload=payload or {},
        idempotency_key=key,
        status="PENDING",
        occurred_at=occurred_at or utcnow(),
    )
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(ReviewProgramEvent).where(
                ReviewProgramEvent.program_id == program.id,
                ReviewProgramEvent.idempotency_key == key,
            )
        )
        if existing is None:
            raise
        return existing
    return event


def _advance_after_stage(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
) -> None:
    template = _template_for_program(program)
    stage = _stage_for_run(program, run)
    completed = list(run.progress_summary.get("completed_stages", []))
    if stage.key not in completed:
        completed.append(stage.key)
    run.progress_summary = {
        **run.progress_summary,
        "completed_stages": completed,
        "last_completed_stage": stage.key,
    }
    if stage.ordinal == len(template.stages):
        run.status = "COMPLETED"
        run.completed_at = utcnow()
        run.pending_action_id = None
        program.current_run_id = None
        program.row_version += 1
        _publish_run_notification(
            db,
            program=program,
            run=run,
            type_key="review_program.completed",
            template_data={"program_name": _notification_program_name(program)},
        )
        return
    _activate_stage(db, program=program, run=run, stage=template.stages[stage.ordinal])


def _activate_stage(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    stage: ReviewWorkflowStageSpec,
) -> ReviewProgramStageAttempt:
    previous_attempt = db.scalar(
        select(func.max(ReviewProgramStageAttempt.attempt)).where(
            ReviewProgramStageAttempt.run_id == run.id,
            ReviewProgramStageAttempt.stage_key == stage.key,
        )
    )
    run.current_stage = stage.key
    run.current_stage_ordinal = stage.ordinal
    return _create_stage_attempt(
        db,
        program=program,
        run=run,
        stage=stage,
        attempt_number=(previous_attempt or 0) + 1,
    )


def _activate_stage_by_key(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    stage_key: str,
) -> ReviewProgramStageAttempt:
    template = _template_for_program(program)
    stage = next((item for item in template.stages if item.key == stage_key), None)
    if stage is None:
        raise ReviewProgramError("Review Program target stage is unavailable")
    return _activate_stage(db, program=program, run=run, stage=stage)


def _latest_definition_assessment(
    db: Session,
    run: ReviewProgramRun,
) -> tuple[ReviewProgramChild, MatterDefinitionAssessmentRun]:
    child = db.scalar(
        select(ReviewProgramChild)
        .where(
            ReviewProgramChild.run_id == run.id,
            ReviewProgramChild.child_type == "MATTER_DEFINITION_ASSESSMENT",
        )
        .order_by(ReviewProgramChild.created_at.desc(), ReviewProgramChild.id.desc())
        .limit(1)
    )
    assessment = db.get(MatterDefinitionAssessmentRun, child.child_id) if child else None
    if child is None or assessment is None:
        raise ReviewProgramError("Review Program has no Matter Definition assessment")
    return child, assessment


def _assessment_questions(
    db: Session,
    assessment: MatterDefinitionAssessmentRun,
) -> list[MatterDefinitionAssessmentQuestion]:
    return list(
        db.scalars(
            select(MatterDefinitionAssessmentQuestion)
            .where(MatterDefinitionAssessmentQuestion.assessment_run_id == assessment.id)
            .order_by(MatterDefinitionAssessmentQuestion.created_at, MatterDefinitionAssessmentQuestion.id)
        )
    )


def _complete_action_attempt(
    attempt: ReviewProgramStageAttempt,
    action: ReviewProgramAction,
    *,
    result: dict[str, Any] | None = None,
) -> None:
    attempt.status = "COMPLETED"
    attempt.completed_at = action.resolved_at
    attempt.result_summary = {
        **attempt.result_summary,
        "gate_choice": (action.resolution_payload or {}).get("choice_key"),
        "action_id": str(action.id),
        **(result or {}),
    }


def _transition_definition_refinement(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    action: ReviewProgramAction,
) -> None:
    _, assessment = _latest_definition_assessment(db, run)
    if assessment.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise ReviewProgramError("Matter Definition assessment is not complete")
    questions = _assessment_questions(db, assessment)
    blocking = [question for question in questions if question.blocking and question.status == "OPEN"]
    if blocking:
        raise ReviewProgramError(
            f"Resolve {len(blocking)} blocking assessment question(s) before continuing"
        )
    answered = [question for question in questions if question.status == "ANSWERED"]
    if answered and assessment.guidance_refinement_status in {"NOT_READY", "QUEUED", "RUNNING"}:
        raise ReviewProgramError(
            "Resolve the remaining assessment questions and wait for the definition refinement draft"
        )
    if assessment.guidance_refinement_status == "FAILED":
        raise ReviewProgramError(
            assessment.guidance_refinement_error_message or "Matter Definition refinement failed"
        )
    if program.matter_definition_id is None:
        raise ReviewProgramError("Review Program Matter Definition is unavailable")
    definition = get_guidance(
        db,
        matter_id=program.matter_id,
        guidance_id=program.matter_definition_id,
    )
    if definition.published_revision is None:
        raise ReviewProgramError("Publish a Matter Definition revision before continuing")
    published = db.scalar(
        select(MatterDefinitionRevision).where(
            MatterDefinitionRevision.matter_definition_id == definition.id,
            MatterDefinitionRevision.revision == definition.published_revision,
        )
    )
    if published is None:
        raise ReviewProgramError("Published Matter Definition revision is unavailable")
    if (
        assessment.refined_matter_definition_revision_id is not None
        and published.id == assessment.matter_definition_revision_id
    ):
        raise ReviewProgramError("Review and publish a refinement revision before continuing")
    _complete_action_attempt(
        attempt,
        action,
        result={
            "assessment_id": str(assessment.id),
            "assessed_revision_id": str(assessment.matter_definition_revision_id),
            "published_revision_id": str(published.id),
            "open_nonblocking_question_ids": [
                str(question.id) for question in questions if not question.blocking and question.status == "OPEN"
            ],
        },
    )
    if published.id != assessment.matter_definition_revision_id:
        _activate_stage_by_key(
            db,
            program=program,
            run=run,
            stage_key="DEFINITION_ASSESSMENT",
        )
    else:
        _advance_after_stage(db, program=program, run=run, attempt=attempt)


def _accept_matter_definition(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    action: ReviewProgramAction,
) -> None:
    if program.matter_definition_id is None:
        raise ReviewProgramError("Review Program Matter Definition is unavailable")
    _, assessment = _latest_definition_assessment(db, run)
    definition = get_guidance(
        db,
        matter_id=program.matter_id,
        guidance_id=program.matter_definition_id,
    )
    if definition.published_revision is None:
        raise ReviewProgramError("Matter Definition has no published revision")
    revision = db.scalar(
        select(MatterDefinitionRevision).where(
            MatterDefinitionRevision.matter_definition_id == definition.id,
            MatterDefinitionRevision.revision == definition.published_revision,
        )
    )
    if revision is None or revision.id != assessment.matter_definition_revision_id:
        raise ReviewProgramError("The current published Matter Definition revision has not been assessed")
    questions = _assessment_questions(db, assessment)
    if any(question.blocking and question.status == "OPEN" for question in questions):
        raise ReviewProgramError("Blocking assessment questions remain unresolved")
    content_hash = hashlib.sha256(revision.content_markdown.encode()).hexdigest()
    if content_hash != assessment.definition_content_hash:
        raise ReviewProgramError("Matter Definition content no longer matches the assessed revision")
    query_keys = list(
        dict.fromkeys(
            db.scalars(
                select(MatterDefinitionAssessmentQuery.criterion_key)
                .where(MatterDefinitionAssessmentQuery.assessment_run_id == assessment.id)
                .order_by(MatterDefinitionAssessmentQuery.ordinal)
            )
        )
    )
    children = list(
        db.scalars(
            select(ReviewProgramChild)
            .where(
                ReviewProgramChild.run_id == run.id,
                ReviewProgramChild.child_type == "MATTER_DEFINITION_ASSESSMENT",
            )
            .order_by(ReviewProgramChild.created_at, ReviewProgramChild.id)
        )
    )
    assessments = [db.get(MatterDefinitionAssessmentRun, child.child_id) for child in children]
    accepted = {
        "schema_version": 1,
        "matter_definition_id": str(definition.id),
        "revision": revision.revision,
        "revision_id": str(revision.id),
        "content_hash": content_hash,
        "issue_keys": query_keys,
        "assessment_ids": [str(item.id) for item in assessments if item is not None],
        "diagnostic_batch_ids": [
            str(item.review_batch_id) for item in assessments if item is not None and item.review_batch_id is not None
        ],
        "questions": [
            {
                "id": str(question.id),
                "status": question.status,
                "blocking": question.blocking,
                "priority": question.priority,
                "answer": question.answer,
            }
            for question in questions
        ],
        "accepted_known_limitations": [
            {"question_id": str(question.id), "question": question.question}
            for question in questions
            if question.status == "OPEN" and not question.blocking
        ],
        "accepted_by_user_id": str(action.resolved_by_user_id),
        "accepted_at": action.resolved_at.isoformat() if action.resolved_at else None,
    }
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=action.resolved_by_user_id or run.initiated_by_user_id,
        configuration_stage="QA_TASK_SETUP",
        configuration_updates={
            "matter_definition": {
                "id": str(definition.id),
                "current_revision": definition.current_revision,
                "published_revision": definition.published_revision,
            },
            "accepted_matter_definition": accepted,
        },
        source_hash_updates={"accepted_matter_definition_content_hash": content_hash},
    )
    _complete_action_attempt(
        attempt,
        action,
        result={"accepted_matter_definition": accepted, "program_version_id": str(version.id)},
    )
    run.progress_summary = {
        **run.progress_summary,
        "accepted_matter_definition": accepted,
    }
    _advance_after_stage(db, program=program, run=run, attempt=attempt)


def _qa_task_records(
    db: Session,
    run: ReviewProgramRun,
) -> tuple[MatterAnalysisTask, MatterAnalysisTaskVersion, list[AnalysisTaskExecutionProfile]]:
    setup = (run.progress_summary or {}).get("qa_task_setup") or {}
    try:
        task_id = uuid.UUID(str(setup["task_id"]))
        task_version_id = uuid.UUID(str(setup["task_version_id"]))
        profile_ids = [uuid.UUID(str(value)) for value in setup["execution_profile_ids"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise ReviewProgramError("Q&A task setup provenance is incomplete") from exc
    task = db.get(MatterAnalysisTask, task_id)
    task_version = db.get(MatterAnalysisTaskVersion, task_version_id)
    if task is None or task_version is None or task_version.matter_analysis_task_id != task.id:
        raise ReviewProgramError("Q&A task setup records are unavailable")
    try:
        profiles = validate_dual_execution_profiles(
            db,
            task_version=task_version,
            profile_ids=profile_ids,
        )
    except AnalysisTaskExecutionProfileError as exc:
        raise ReviewProgramError(str(exc)) from exc
    if {profile.decision_provider for profile in profiles} != {"JEV", "OPENAI_DECISIONS"}:
        raise ReviewProgramError("Q&A task must pin one Jev and one OpenAI decisions profile")
    return task, task_version, profiles


def _publish_review_program_qa_task(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    action: ReviewProgramAction,
) -> None:
    task, task_version, profiles = _qa_task_records(db, run)
    if task_version.compilation_status != "READY" or task_version.decision_specification is None:
        raise ReviewProgramError("Q&A task Decision Specification is not ready for publication")
    validation = task_version.validation_report or {}
    if validation.get("errors"):
        raise ReviewProgramError("Q&A task has unresolved validation errors")
    try:
        task, task_version = publish_analysis_task_version(
            db,
            task_id=task.id,
            version_number=task_version.version,
            actor_user_id=action.resolved_by_user_id or run.initiated_by_user_id,
        )
    except MatterAnalysisTaskError as exc:
        raise ReviewProgramError(str(exc)) from exc
    accepted_task = {
        "schema_version": 1,
        "task_id": str(task.id),
        "task_version_id": str(task_version.id),
        "version": task_version.version,
        "definition_content_hash": task_version.definition_content_hash,
        "specification_content_hash": task_version.specification_content_hash,
        "destination_metadata_definition_ids": task_version.destination_metadata_definition_ids,
        "scope_policy": task_version.scope_policy,
        "input_contract": task_version.input_contract,
        "output_contract": task_version.output_contract,
        "evidence_policy": task_version.evidence_policy,
        "routing_policy": task_version.routing_policy,
        "validation_report": validation,
        "execution_profiles": [
            {
                "id": str(profile.id),
                "key": profile.key,
                "decision_provider": profile.decision_provider,
                "model_key": profile.model_key,
                "resolved_model": profile.resolved_model,
                "model_policy": profile.model_policy,
                "required_capabilities": profile.required_capabilities,
                "skill_definition_version_id": str(profile.skill_definition_version_id),
                "provider_adapter_version": profile.provider_adapter_version,
                "content_hash": profile.content_hash,
            }
            for profile in profiles
        ],
        "published_by_user_id": str(action.resolved_by_user_id),
        "published_at": action.resolved_at.isoformat() if action.resolved_at else None,
    }
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=action.resolved_by_user_id or run.initiated_by_user_id,
        configuration_stage="DISCOVERY_QA",
        configuration_updates={"accepted_qa_task": accepted_task},
        source_hash_updates={
            "qa_task_specification_content_hash": task_version.specification_content_hash,
            "qa_execution_profile_hashes": [profile.content_hash for profile in profiles],
        },
    )
    _complete_action_attempt(
        attempt,
        action,
        result={"accepted_qa_task": accepted_task, "program_version_id": str(version.id)},
    )
    run.progress_summary = {
        **run.progress_summary,
        "qa_task_setup_state": "PUBLISHED",
        "accepted_qa_task": accepted_task,
    }
    _advance_after_stage(db, program=program, run=run, attempt=attempt)


def _recompile_review_program_qa_task(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
) -> None:
    task, task_version, _ = _qa_task_records(db, run)
    matter = db.get(Matter, program.matter_id)
    if matter is None:
        raise ReviewProgramError("Review Program matter is unavailable")
    try:
        _, workflow = queue_analysis_task_compilation(
            db,
            matter=matter,
            task=task,
            version_number=task_version.version,
            initiated_by_user_id=run.initiated_by_user_id,
            notification_parent_type="REVIEW_PROGRAM_RUN",
            notification_parent_id=run.id,
        )
    except MatterAnalysisTaskError as exc:
        raise ReviewProgramError(str(exc)) from exc
    link_review_program_child(
        db,
        program=program,
        run=run,
        stage_attempt=attempt,
        purpose="QA_TASK_COMPILATION",
        child_type="ANALYSIS_TASK_COMPILATION",
        child_id=workflow.id,
        derivation_key=f"qa-task-compilation:{workflow.id}",
        batching_policy="NONE",
        context={"task_id": str(task.id), "task_version_id": str(task_version.id)},
    )
    setup = dict(run.progress_summary.get("qa_task_setup") or {})
    setup["compiler_workflow_run_id"] = str(workflow.id)
    run.progress_summary = {
        **run.progress_summary,
        "qa_task_setup_state": "COMPILING",
        "qa_task_setup": setup,
    }
    attempt.status = "RUNNING"
    run.status = "RUNNING"


def process_review_program_event(
    db: Session,
    *,
    event: ReviewProgramEvent,
) -> ReviewProgramEvent:
    if event.status == "PROCESSED":
        return event
    statement = select(ReviewProgramEvent).where(ReviewProgramEvent.id == event.id).with_for_update()
    locked = db.scalar(statement)
    if locked is None:
        raise ReviewProgramError("Review Program event not found")
    if locked.status == "PROCESSED":
        return locked
    program = db.scalar(select(ReviewProgram).where(ReviewProgram.id == locked.program_id).with_for_update())
    run = db.scalar(select(ReviewProgramRun).where(ReviewProgramRun.id == locked.run_id).with_for_update())
    if program is None or run is None or run.program_id != program.id:
        raise ReviewProgramError("Review Program event target is unavailable")
    if locked.program_version_id != run.current_program_version_id:
        raise ReviewProgramError("Review Program event version is stale")

    locked.status = "PROCESSING"
    locked.processing_attempt += 1
    locked.failure_code = None
    locked.failure_summary = None
    locked.next_retry_at = None

    if locked.event_type == "STAGE_COMPLETED":
        attempt = _current_stage_attempt(db, run, for_update=True)
        if locked.stage_attempt_id != attempt.id or locked.source_id != attempt.id:
            raise ReviewProgramError("Review Program completion event is stale")
        if attempt.status != "COMPLETED":
            raise ReviewProgramError("Review Program stage is not complete")
        _advance_after_stage(db, program=program, run=run, attempt=attempt)
    elif locked.event_type == "STAGE_FAILED":
        attempt = _current_stage_attempt(db, run, for_update=True)
        if locked.stage_attempt_id != attempt.id or locked.source_id != attempt.id:
            raise ReviewProgramError("Review Program failure event is stale")
        if attempt.status != "FAILED":
            raise ReviewProgramError("Review Program stage has not failed")
        run.status = "FAILED"
        run.failure_code = attempt.error_code or "STAGE_FAILED"
        run.failure_summary = attempt.error_summary or "The current stage failed"
        run.failure_retriable = bool(locked.payload.get("retriable", False))
        run.pending_action_id = None
        _publish_run_notification(
            db,
            program=program,
            run=run,
            type_key="review_program.attention_required",
            template_data={
                "program_name": _notification_program_name(program),
                "stage_name": _stage_for_run(program, run).name,
            },
            occurrence_key=str(attempt.id),
        )
    elif locked.event_type == "ACTION_RESOLVED":
        action = db.scalar(
            select(ReviewProgramAction).where(ReviewProgramAction.id == locked.source_id).with_for_update()
        )
        if action is None or action.run_id != run.id or action.status != "RESOLVED":
            raise ReviewProgramError("Review Program action resolution is unavailable")
        if run.pending_action_id not in {action.id, None}:
            raise ReviewProgramError("Review Program action resolution is stale")
        attempt = _current_stage_attempt(db, run, for_update=True)
        if action.stage_attempt_id != attempt.id:
            raise ReviewProgramError("Review Program action stage is stale")
        choice = (action.resolution_payload or {}).get("choice_key")
        resolve_source_notifications(
            db,
            source=NotificationSource("review_program_action", action.id),
            resolution_code=str(choice or "RESOLVED"),
            resolved_at=action.resolved_at,
        )
        run.pending_action_id = None
        if choice == "STOP":
            attempt.status = "CANCELED"
            attempt.completed_at = action.resolved_at
            _cancel_run_state(db, program=program, run=run)
        elif action.stage_key == "MATTER_DEFINITION" and choice in {"CREATE", "CLONE", "ADOPT"}:
            _complete_action_attempt(attempt, action)
            _advance_after_stage(db, program=program, run=run, attempt=attempt)
        elif action.stage_key == "DEFINITION_REFINEMENT" and choice == "CONTINUE":
            _transition_definition_refinement(
                db,
                program=program,
                run=run,
                attempt=attempt,
                action=action,
            )
        elif action.stage_key == "DEFINITION_ACCEPTANCE" and choice == "ACCEPT":
            _accept_matter_definition(
                db,
                program=program,
                run=run,
                attempt=attempt,
                action=action,
            )
        elif action.stage_key == "DEFINITION_ACCEPTANCE" and choice == "REASSESS":
            _complete_action_attempt(attempt, action)
            _activate_stage_by_key(
                db,
                program=program,
                run=run,
                stage_key="DEFINITION_ASSESSMENT",
            )
        elif action.stage_key == "QA_TASK_SETUP" and choice == "GENERATE":
            attempt.status = "RUNNING"
            run.status = "RUNNING"
        elif action.stage_key == "QA_TASK_SETUP" and choice == "PUBLISH":
            _publish_review_program_qa_task(
                db,
                program=program,
                run=run,
                attempt=attempt,
                action=action,
            )
        elif action.stage_key == "QA_TASK_SETUP" and choice == "RECOMPILE":
            _recompile_review_program_qa_task(
                db,
                program=program,
                run=run,
                attempt=attempt,
            )
        elif choice == "CONTINUE":
            _complete_action_attempt(attempt, action)
            _advance_after_stage(db, program=program, run=run, attempt=attempt)
        else:
            raise ReviewProgramError("Review Program action choice is unsupported")
    elif locked.event_type == "RUN_CANCELED":
        _cancel_run_state(db, program=program, run=run)
    else:
        raise ReviewProgramError("Review Program event type is unsupported")

    locked.status = "PROCESSED"
    locked.processed_at = utcnow()
    return locked


def complete_review_program_stage(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    result_summary: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> ReviewProgramEvent:
    if run.status != "RUNNING":
        raise ReviewProgramError("Review Program run is not running")
    attempt = _current_stage_attempt(db, run, for_update=True)
    if attempt.status not in {"RUNNING", "COMPLETED"}:
        raise ReviewProgramError("Review Program stage cannot be completed")
    attempt.status = "COMPLETED"
    attempt.result_summary = result_summary or attempt.result_summary
    attempt.completed_at = attempt.completed_at or utcnow()
    event = publish_review_program_event(
        db,
        program=program,
        run=run,
        event_type="STAGE_COMPLETED",
        source_type="review_program_stage_attempt",
        source_id=attempt.id,
        stage_attempt_id=attempt.id,
        idempotency_key=idempotency_key or f"stage-completed:{attempt.id}",
        payload={"stage_key": attempt.stage_key},
    )
    return process_review_program_event(db, event=event)


def fail_review_program_stage(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    error_code: str,
    error_summary: str,
    retriable: bool,
) -> ReviewProgramEvent:
    if run.status != "RUNNING":
        raise ReviewProgramError("Review Program run is not running")
    attempt = _current_stage_attempt(db, run, for_update=True)
    attempt.status = "FAILED"
    attempt.error_code = error_code
    attempt.error_summary = error_summary
    attempt.completed_at = utcnow()
    event = publish_review_program_event(
        db,
        program=program,
        run=run,
        event_type="STAGE_FAILED",
        source_type="review_program_stage_attempt",
        source_id=attempt.id,
        stage_attempt_id=attempt.id,
        idempotency_key=f"stage-failed:{attempt.id}",
        payload={"stage_key": attempt.stage_key, "retriable": retriable},
    )
    return process_review_program_event(db, event=event)


def project_review_program_assessment_terminal(
    db: Session,
    *,
    assessment: MatterDefinitionAssessmentRun,
) -> ReviewProgramEvent | None:
    child = db.scalar(
        select(ReviewProgramChild).where(
            ReviewProgramChild.child_type == "MATTER_DEFINITION_ASSESSMENT",
            ReviewProgramChild.child_id == assessment.id,
        )
    )
    if child is None:
        return None
    child.status = assessment.status
    child.context = {
        **child.context,
        "review_batch_id": str(assessment.review_batch_id) if assessment.review_batch_id else None,
        "review_batch_run_id": str(assessment.review_batch_run_id) if assessment.review_batch_run_id else None,
        "selected_count": assessment.selected_count,
        "summarized_count": assessment.summarized_count,
        "failed_count": assessment.failed_count,
        "synthesis_status": (assessment.synthesis_result or {}).get("status"),
    }
    run = db.get(ReviewProgramRun, child.run_id)
    attempt = db.get(ReviewProgramStageAttempt, child.stage_attempt_id)
    program = db.get(ReviewProgram, run.program_id) if run else None
    if run is None or attempt is None or program is None:
        raise ReviewProgramError("Review Program assessment link is incomplete")
    if run.status in TERMINAL_RUN_STATUSES or run.current_stage != "DEFINITION_ASSESSMENT":
        return None
    if attempt.id != _current_stage_attempt(db, run).id:
        return None
    if assessment.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        attempt.status = "COMPLETED"
        attempt.completed_at = assessment.completed_at or utcnow()
        attempt.result_summary = {
            "assessment_id": str(assessment.id),
            "status": assessment.status,
            "matter_definition_revision_id": str(assessment.matter_definition_revision_id),
            "review_batch_id": str(assessment.review_batch_id) if assessment.review_batch_id else None,
            "review_batch_run_id": str(assessment.review_batch_run_id) if assessment.review_batch_run_id else None,
            "selected_count": assessment.selected_count,
            "summarized_count": assessment.summarized_count,
            "failed_count": assessment.failed_count,
            "coverage": assessment.coverage_snapshot,
        }
        event = publish_review_program_event(
            db,
            program=program,
            run=run,
            event_type="STAGE_COMPLETED",
            source_type="review_program_stage_attempt",
            source_id=attempt.id,
            stage_attempt_id=attempt.id,
            idempotency_key=f"assessment-terminal:{assessment.id}:{assessment.status}",
            payload={"assessment_id": str(assessment.id), "status": assessment.status},
            occurred_at=assessment.completed_at,
        )
    elif assessment.status in {"FAILED", "CANCELED"}:
        attempt.status = "FAILED"
        attempt.error_code = f"DEFINITION_ASSESSMENT_{assessment.status}"
        attempt.error_summary = assessment.error_message or f"Matter Definition assessment {assessment.status.lower()}"
        attempt.completed_at = assessment.completed_at or utcnow()
        event = publish_review_program_event(
            db,
            program=program,
            run=run,
            event_type="STAGE_FAILED",
            source_type="review_program_stage_attempt",
            source_id=attempt.id,
            stage_attempt_id=attempt.id,
            idempotency_key=f"assessment-terminal:{assessment.id}:{assessment.status}",
            payload={
                "assessment_id": str(assessment.id),
                "status": assessment.status,
                "retriable": True,
            },
            occurred_at=assessment.completed_at,
        )
    else:
        return None
    return process_review_program_event(db, event=event)


def project_review_program_qa_compilation_terminal(
    db: Session,
    *,
    workflow: WorkflowRun,
) -> ReviewProgramAction | ReviewProgramEvent | None:
    if (
        workflow.notification_policy != "PARENT_MANAGED"
        or workflow.notification_parent_type != "REVIEW_PROGRAM_RUN"
        or workflow.notification_parent_id is None
    ):
        return None
    child = db.scalar(
        select(ReviewProgramChild).where(
            ReviewProgramChild.run_id == workflow.notification_parent_id,
            ReviewProgramChild.child_type == "ANALYSIS_TASK_COMPILATION",
            ReviewProgramChild.child_id == workflow.id,
        )
    )
    if child is None:
        return None
    child.status = workflow.status
    child.context = {
        **child.context,
        "workflow_status": workflow.status,
        "progress": workflow.progress or {},
        "error_message": workflow.error_message,
    }
    run = db.get(ReviewProgramRun, child.run_id)
    attempt = db.get(ReviewProgramStageAttempt, child.stage_attempt_id)
    program = db.get(ReviewProgram, run.program_id) if run is not None else None
    if run is None or attempt is None or program is None:
        raise ReviewProgramError("Review Program Q&A compilation link is incomplete")
    if (
        run.status in TERMINAL_RUN_STATUSES
        or run.current_stage != "QA_TASK_SETUP"
        or attempt.id != _current_stage_attempt(db, run).id
    ):
        return None
    setup = run.progress_summary.get("qa_task_setup") or {}
    if setup.get("compiler_workflow_run_id") != str(workflow.id):
        return None
    if workflow.status == "COMPLETED":
        _, task_version, _ = _qa_task_records(db, run)
        if task_version.compilation_status != "READY" or task_version.decision_specification is None:
            raise ReviewProgramError("Completed compiler did not produce a ready Decision Specification")
        existing = db.scalar(
            select(ReviewProgramAction).where(
                ReviewProgramAction.run_id == run.id,
                ReviewProgramAction.stage_attempt_id == attempt.id,
                ReviewProgramAction.action_type == "QA_TASK_PUBLICATION",
                ReviewProgramAction.status == "OPEN",
            )
        )
        if existing is not None:
            return existing
        run.progress_summary = {
            **run.progress_summary,
            "qa_task_setup_state": "READY_FOR_PUBLICATION",
        }
        return _create_action_for_stage(
            db,
            program=program,
            run=run,
            attempt=attempt,
            stage=_stage_for_run(program, run),
            template=_template_for_program(program),
        )
    if workflow.status in {"FAILED", "CANCELED"}:
        run.status = "RUNNING"
        attempt.status = "RUNNING"
        return fail_review_program_stage(
            db,
            program=program,
            run=run,
            error_code=f"QA_TASK_COMPILATION_{workflow.status}",
            error_summary=workflow.error_message or f"Q&A task compilation {workflow.status.lower()}",
            retriable=True,
        )
    return None


def resolve_review_program_action(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    action_id: uuid.UUID,
    actor: User,
    preview_hash: str,
    choice_key: str,
    reason: str | None,
    input_payload: dict[str, Any],
) -> ReviewProgramAction:
    action = db.scalar(
        select(ReviewProgramAction)
        .where(ReviewProgramAction.id == action_id, ReviewProgramAction.run_id == run.id)
        .with_for_update()
    )
    if action is None:
        raise ReviewProgramError("Review Program action not found")
    if action.status == "RESOLVED":
        resolution = action.resolution_payload or {}
        if action.preview_hash == preview_hash and resolution.get("choice_key") == choice_key:
            return action
        raise ReviewProgramError("Review Program action was already resolved differently")
    if action.status != "OPEN" or run.pending_action_id != action.id or run.status != "WAITING_FOR_USER":
        raise ReviewProgramError("Review Program action is no longer open")
    if action.preview_hash != preview_hash:
        raise ReviewProgramError("Review Program action preview is stale")
    allowed = {choice.get("key") for choice in action.allowed_choices}
    if choice_key not in allowed:
        raise ReviewProgramError("Review Program action choice is not allowed")
    if (
        action.action_type == "MATTER_DEFINITION_SETUP"
        and choice_key != "STOP"
        and not input_payload.get("matter_definition_id")
    ):
        raise ReviewProgramError("Use the Matter Definition setup command for this choice")
    if (
        action.action_type == "QA_TASK_SETUP"
        and choice_key != "STOP"
        and not input_payload.get("task_id")
    ):
        raise ReviewProgramError("Use the Q&A task setup command for this choice")
    now = utcnow()
    action.status = "RESOLVED"
    action.resolution_payload = {"choice_key": choice_key, "input": input_payload}
    action.resolution_reason = reason.strip() if reason else None
    action.resolved_by_user_id = actor.id
    action.resolved_at = now
    event = publish_review_program_event(
        db,
        program=program,
        run=run,
        event_type="ACTION_RESOLVED",
        source_type="review_program_action",
        source_id=action.id,
        stage_attempt_id=action.stage_attempt_id,
        idempotency_key=f"action-resolved:{action.id}",
        payload={"choice_key": choice_key},
        occurred_at=now,
    )
    process_review_program_event(db, event=event)
    record_audit(
        db,
        tenant_id=program.tenant_id,
        actor_user_id=actor.id,
        action="review_program.action_resolved",
        target_type="review_program_action",
        target_id=action.id,
        details={"program_id": str(program.id), "run_id": str(run.id), "choice_key": choice_key},
    )
    return action


def _cancel_run_state(db: Session, *, program: ReviewProgram, run: ReviewProgramRun) -> None:
    if run.status == "CANCELED":
        return
    now = utcnow()
    if run.pending_action_id is not None:
        action = db.get(ReviewProgramAction, run.pending_action_id)
        if action is not None and action.status == "OPEN":
            action.status = "CANCELED"
            action.resolved_at = now
            resolve_source_notifications(
                db,
                source=NotificationSource("review_program_action", action.id),
                resolution_code="CANCELED",
                resolved_at=now,
            )
    attempt = _current_stage_attempt(db, run, for_update=True)
    if attempt.status in {"QUEUED", "RUNNING", "WAITING"}:
        attempt.status = "CANCELED"
        attempt.completed_at = now
    for assessment, workflow in review_program_assessment_cancellation_targets(db, run):
        assessment.status = "CANCELED"
        assessment.canceled_at = now
        assessment.completed_at = now
        if workflow is not None:
            workflow.status = "CANCELED"
            workflow.cancel_requested_at = now
            workflow.completed_at = now
        child = db.scalar(
            select(ReviewProgramChild).where(
                ReviewProgramChild.run_id == run.id,
                ReviewProgramChild.child_type == "MATTER_DEFINITION_ASSESSMENT",
                ReviewProgramChild.child_id == assessment.id,
            )
        )
        if child is not None:
            child.status = "CANCELED"
    run.status = "CANCELED"
    run.pending_action_id = None
    run.canceled_at = now
    run.completed_at = now
    run.failure_retriable = False
    program.current_run_id = None
    program.row_version += 1
    resolve_source_notifications(
        db,
        source=NotificationSource("review_program_run", run.id),
        resolution_code="CANCELED",
        resolved_at=now,
    )
    _publish_run_notification(
        db,
        program=program,
        run=run,
        type_key="review_program.stopped",
        template_data={"program_name": _notification_program_name(program)},
    )


def review_program_assessment_cancellation_targets(
    db: Session,
    run: ReviewProgramRun,
) -> list[tuple[MatterDefinitionAssessmentRun, WorkflowRun | None]]:
    assessment_ids = list(
        db.scalars(
            select(ReviewProgramChild.child_id).where(
                ReviewProgramChild.run_id == run.id,
                ReviewProgramChild.child_type == "MATTER_DEFINITION_ASSESSMENT",
            )
        )
    )
    if not assessment_ids:
        return []
    assessments = list(
        db.scalars(
            select(MatterDefinitionAssessmentRun).where(
                MatterDefinitionAssessmentRun.id.in_(assessment_ids),
                ~MatterDefinitionAssessmentRun.status.in_(
                    {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}
                ),
            )
        )
    )
    return [(assessment, db.get(WorkflowRun, assessment.workflow_run_id)) for assessment in assessments]


def cancel_review_program_run(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    actor: User,
    idempotency_key: str,
    reason: str | None,
) -> ReviewProgramRun:
    if run.status == "CANCELED":
        return run
    if run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise ReviewProgramError("Completed Review Program run cannot be canceled")
    event = publish_review_program_event(
        db,
        program=program,
        run=run,
        event_type="RUN_CANCELED",
        source_type="review_program_run",
        source_id=run.id,
        stage_attempt_id=_current_stage_attempt(db, run).id,
        idempotency_key=f"cancel:{_validate_idempotency_key(idempotency_key)}",
        payload={"reason": reason.strip() if reason else None},
    )
    process_review_program_event(db, event=event)
    record_audit(
        db,
        tenant_id=program.tenant_id,
        actor_user_id=actor.id,
        action="review_program.run_canceled",
        target_type="review_program_run",
        target_id=run.id,
        details={"program_id": str(program.id), "reason": reason.strip() if reason else None},
    )
    return run


def retry_review_program_run(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    actor: User,
    idempotency_key: str,
) -> ReviewProgramRun:
    key = _validate_idempotency_key(idempotency_key)
    if run.status == "RUNNING" and run.progress_summary.get("last_retry_key") == key:
        return run
    if run.status != "FAILED" or not run.failure_retriable:
        raise ReviewProgramError("Review Program run is not retriable")
    previous = _current_stage_attempt(db, run, for_update=True)
    next_attempt_number = previous.attempt + 1
    stage = _stage_for_run(program, run)
    run.status = "RUNNING"
    run.failure_code = None
    run.failure_summary = None
    run.failure_retriable = False
    run.completed_at = None
    run.progress_summary = {**run.progress_summary, "last_retry_key": key}
    resolve_source_notifications(
        db,
        source=NotificationSource("review_program_run", run.id),
        resolution_code="RETRIED",
    )
    _create_stage_attempt(
        db,
        program=program,
        run=run,
        stage=stage,
        attempt_number=next_attempt_number,
    )
    record_audit(
        db,
        tenant_id=program.tenant_id,
        actor_user_id=actor.id,
        action="review_program.run_retried",
        target_type="review_program_run",
        target_id=run.id,
        details={
            "program_id": str(program.id),
            "stage_key": stage.key,
            "attempt": next_attempt_number,
        },
    )
    return run


def reconcile_review_program_run(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    actor: User,
) -> ReviewProgramRun:
    pending_events = list(
        db.scalars(
            select(ReviewProgramEvent)
            .where(
                ReviewProgramEvent.run_id == run.id,
                ReviewProgramEvent.status.in_({"PENDING", "PROCESSING"}),
            )
            .order_by(ReviewProgramEvent.created_at, ReviewProgramEvent.id)
        )
    )
    for event in pending_events:
        process_review_program_event(db, event=event)
    if run.status == "RUNNING" and run.current_stage == "DEFINITION_ASSESSMENT":
        current_attempt = _current_stage_attempt(db, run, for_update=True)
        child = db.scalar(
            select(ReviewProgramChild).where(
                ReviewProgramChild.stage_attempt_id == current_attempt.id,
                ReviewProgramChild.child_type == "MATTER_DEFINITION_ASSESSMENT",
            )
        )
        assessment = db.get(MatterDefinitionAssessmentRun, child.child_id) if child else None
        if assessment is not None and assessment.status in {
            "COMPLETED",
            "COMPLETED_WITH_ERRORS",
            "FAILED",
            "CANCELED",
        }:
            project_review_program_assessment_terminal(db, assessment=assessment)
    if run.status in {"RUNNING", "FAILED"}:
        attempt = _current_stage_attempt(db, run, for_update=True)
        if attempt.status == "COMPLETED":
            event = publish_review_program_event(
                db,
                program=program,
                run=run,
                event_type="STAGE_COMPLETED",
                source_type="review_program_stage_attempt",
                source_id=attempt.id,
                stage_attempt_id=attempt.id,
                idempotency_key=f"stage-completed:{attempt.id}",
                payload={"stage_key": attempt.stage_key, "reconciled": True},
            )
            process_review_program_event(db, event=event)
        elif attempt.status == "FAILED" and run.status != "FAILED":
            event = publish_review_program_event(
                db,
                program=program,
                run=run,
                event_type="STAGE_FAILED",
                source_type="review_program_stage_attempt",
                source_id=attempt.id,
                stage_attempt_id=attempt.id,
                idempotency_key=f"stage-failed:{attempt.id}",
                payload={"stage_key": attempt.stage_key, "retriable": True, "reconciled": True},
            )
            process_review_program_event(db, event=event)
    record_audit(
        db,
        tenant_id=program.tenant_id,
        actor_user_id=actor.id,
        action="review_program.run_reconciled",
        target_type="review_program_run",
        target_id=run.id,
        details={"program_id": str(program.id)},
    )
    return run


def link_review_program_child(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    stage_attempt: ReviewProgramStageAttempt,
    purpose: str,
    child_type: str,
    child_id: uuid.UUID,
    derivation_key: str,
    batching_policy: BatchingPolicy,
    context: dict[str, Any] | None = None,
) -> ReviewProgramChild:
    derivation_key = _validate_idempotency_key(derivation_key)
    if stage_attempt.run_id != run.id or stage_attempt.stage_key != run.current_stage:
        raise ReviewProgramError("Review Program child stage is stale")
    expected = _stage_for_run(program, run).batching_policy
    if batching_policy != expected:
        raise ReviewProgramError(
            f"Review Program stage requires {expected} batching, not {batching_policy}"
        )
    existing = db.scalar(
        select(ReviewProgramChild).where(
            ReviewProgramChild.run_id == run.id,
            ReviewProgramChild.derivation_key == derivation_key,
        )
    )
    if existing is not None:
        return existing
    child = ReviewProgramChild(
        run_id=run.id,
        stage_attempt_id=stage_attempt.id,
        purpose=purpose,
        child_type=child_type,
        child_id=child_id,
        status="LINKED",
        derivation_key=derivation_key,
        context={**(context or {}), "batching_policy": batching_policy},
    )
    db.add(child)
    db.flush()
    return child


def review_program_run_details(
    db: Session,
    run: ReviewProgramRun,
) -> tuple[list[ReviewProgramStageAttempt], list[ReviewProgramAction], list[ReviewProgramEvent]]:
    attempts = list(
        db.scalars(
            select(ReviewProgramStageAttempt)
            .where(ReviewProgramStageAttempt.run_id == run.id)
            .order_by(ReviewProgramStageAttempt.stage_ordinal, ReviewProgramStageAttempt.attempt)
        )
    )
    actions = list(
        db.scalars(
            select(ReviewProgramAction)
            .where(ReviewProgramAction.run_id == run.id)
            .order_by(ReviewProgramAction.created_at, ReviewProgramAction.id)
        )
    )
    events = list(
        db.scalars(
            select(ReviewProgramEvent)
            .where(ReviewProgramEvent.run_id == run.id)
            .order_by(ReviewProgramEvent.created_at, ReviewProgramEvent.id)
        )
    )
    return attempts, actions, events
