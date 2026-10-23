from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analysis_input_manifests import (
    AnalysisInputContract,
    AnalysisInputDocument,
    AnalysisInputExtensionField,
    prepare_analysis_input_manifest,
    publish_analysis_input_manifest,
)
from app.analysis_task_batch_set_runs import (
    AnalysisTaskBatchSetRunError,
    cancel_batch_set_run,
    create_batch_set_run,
)
from app.analysis_task_batch_sets import (
    AnalysisTaskBatchSetError,
    cancel_batch_set,
    create_batch_set,
    search_document_ids,
)
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
from app.job_executions import REVIEW_BATCH_MATERIALIZATION_JOB_TYPE, cancel_job_execution
from app.matter_definition_assessments import AssessmentError, start_assessment
from app.matter_definitions import (
    MatterDefinitionError,
    clone_guidance,
    create_guidance,
    get_guidance,
)
from app.models import (
    AnalysisTaskBatchSet,
    AnalysisTaskBatchSetRun,
    AnalysisTaskBatchSetRunChild,
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
    MatterDocument,
    MetadataDefinition,
    ReviewBatch,
    ReviewBatchCodingField,
    ReviewBatchCodingGroup,
    ReviewBatchDocument,
    ReviewBatchReviewerCoding,
    ReviewBatchReviewerDocument,
    ReviewBatchRun,
    ReviewProgram,
    ReviewProgramAction,
    ReviewProgramChild,
    ReviewProgramEvent,
    ReviewProgramRun,
    ReviewProgramStageAttempt,
    ReviewProgramVersion,
    SearchIndexGeneration,
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
from app.review_batch_comparisons import comparison_document_ids, iter_comparison_documents
from app.review_batches import create_review_batch_execution
from app.review_workflow_templates import (
    BatchingPolicy,
    ReviewWorkflowStageSpec,
    ReviewWorkflowTemplateSpec,
    get_review_workflow_template,
)
from app.reviewer_coding import resolve_reviewer_coding_as_of
from app.schemas import ReviewBatchComparisonSourceOptionRead
from app.workflows.dispatcher import (
    enqueue_analysis_task_batch_set,
    enqueue_analysis_task_batch_set_run,
    enqueue_review_batch,
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
    support_state: str | None = None,
    training_label_state: str | None = None,
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
    if stage.key == "DISCOVERY_QA":
        return (
            "Start discovery Q&A",
            (
                "Freeze a training-oriented population from the accepted retrieval plan, split it into "
                "task-specific batches, and run both pinned Q&A profiles over exactly the same documents."
            ),
            [
                {
                    "key": "START",
                    "label": "Start discovery Q&A",
                    "consequence": (
                        "Create one immutable population and launch both model profiles without promoting "
                        "their values to matter metadata."
                    ),
                },
                stop,
            ],
        )
    if stage.key == "ISSUE_SUPPORT_DECISION":
        if support_state == "INSUFFICIENT":
            return (
                "Resolve low-support coding categories",
                (
                    "One or more coding categories lack the minimum positive or negative examples. Run another "
                    "discovery round or explicitly continue with those limitations."
                ),
                [
                    {
                        "key": "SUPPLEMENT",
                        "label": "Review more documents",
                        "consequence": "Return to Discovery Q&A and freeze a new population excluding prior rounds.",
                    },
                    {
                        "key": "IGNORE_LOW_SUPPORT",
                        "label": "Accept limitations",
                        "consequence": "Record the low-support categories and proceed to label resolution.",
                    },
                    {
                        "key": "REFINE_DEFINITION",
                        "label": "Refine Matter Definition",
                        "consequence": (
                            "Return to definition refinement, reassess the revised guidance, and regenerate Q&A."
                        ),
                    },
                    stop,
                ],
            )
        return (
            "Review model alignment and issue support",
            (
                "Review per-field agreement, disagreement, incomplete results, and low-support categories before "
                "moving into training-label resolution."
            ),
            [
                {
                    "key": "CONTINUE",
                    "label": "Accept current support",
                    "consequence": "Proceed to difference review using the frozen discovery results.",
                },
                stop,
            ],
        )
    if stage.key == "DISAGREEMENT_REVIEW":
        if training_label_state == "AWAITING_ADJUDICATION":
            return (
                "Complete the difference review",
                (
                    "Open each ad hoc difference batch and assign the correct coding. Continue only after every "
                    "document has a completed human coding; skipped documents remain unresolved."
                ),
                [
                    {
                        "key": "CONFIRM_ADJUDICATION",
                        "label": "Confirm difference review complete",
                        "consequence": (
                            "Validate every assigned difference batch and freeze the reviewer coding revisions."
                        ),
                    },
                    stop,
                ],
            )
        return (
            "Choose the classifier training labels",
            (
                "Choose how the two Q&A results become classifier labels. Agreement measures consistency, not "
                "correctness; each option remains pinned to the completed discovery runs."
            ),
            [
                {
                    "key": "USE_ALL_MODEL_CODES",
                    "label": "Use all model codings",
                    "consequence": (
                        "Include both model outputs as equal-authority label claims; conflicting documents are "
                        "excluded from training."
                    ),
                },
                {
                    "key": "USE_EXACT_AGREEMENT",
                    "label": "Use exact model agreements",
                    "consequence": (
                        "Use only documents where both models assigned the same complete coding set."
                    ),
                },
                {
                    "key": "REVIEW_DISAGREEMENTS",
                    "label": "Review model differences",
                    "consequence": (
                        "Create ad hoc difference batches and use completed human coding for disagreements."
                    ),
                },
                stop,
            ],
        )
    if stage.key == "TRAINING_LABEL_FINALIZATION":
        return (
            "Finalize the classifier training labels",
            (
                "Freeze the selected label policy, source runs, human adjudication revisions, exclusions, and "
                "accepted limitations before classifier training begins."
            ),
            [
                {
                    "key": "FINALIZE",
                    "label": "Finalize training labels",
                    "consequence": "Create an immutable training-label provenance snapshot and begin training.",
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


def _accepted_discovery_queries(
    db: Session,
    *,
    run: ReviewProgramRun,
) -> tuple[MatterDefinitionAssessmentRun, list[MatterDefinitionAssessmentQuery]]:
    accepted = (run.progress_summary or {}).get("accepted_matter_definition") or {}
    assessment_ids = accepted.get("assessment_ids") or []
    if not assessment_ids:
        raise ReviewProgramError("Accepted Matter Definition assessment provenance is unavailable")
    try:
        assessment_id = uuid.UUID(str(assessment_ids[-1]))
    except (TypeError, ValueError) as exc:
        raise ReviewProgramError("Accepted assessment provenance is invalid") from exc
    assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
    if assessment is None or assessment.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise ReviewProgramError("Accepted Matter Definition assessment is unavailable")
    queries = list(
        db.scalars(
            select(MatterDefinitionAssessmentQuery)
            .where(MatterDefinitionAssessmentQuery.assessment_run_id == assessment.id)
            .order_by(MatterDefinitionAssessmentQuery.ordinal)
        )
    )
    if not queries:
        raise ReviewProgramError("Accepted assessment has no retrieval queries")
    return assessment, queries


def _discovery_stage_context(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
) -> dict[str, Any]:
    assessment, queries = _accepted_discovery_queries(db, run=run)
    generation = db.scalar(
        select(SearchIndexGeneration).where(
            SearchIndexGeneration.matter_id == program.matter_id,
            SearchIndexGeneration.status == "ACTIVE",
        )
    )
    accepted_task = (run.progress_summary or {}).get("accepted_qa_task") or {}
    return {
        "retrieval_plan": {
            "source": "ACCEPTED_ASSESSMENT_PLAN",
            "policy_version": "review-program-discovery-retrieval-v1",
            "assessment_id": str(assessment.id),
            "query_count": len(queries),
            "queries": [
                {
                    "ordinal": query.ordinal,
                    "criterion_key": query.criterion_key,
                    "criterion_label": query.criterion_label,
                    "rationale": query.rationale,
                    "quota": query.quota,
                    "search_mode": query.search_request.get("search_mode"),
                }
                for query in queries
            ],
            "diagnostic_batch_ids": (
                (run.progress_summary or {}).get("accepted_matter_definition") or {}
            ).get("diagnostic_batch_ids", []),
        },
        "search_index_generation": (
            {
                "id": str(generation.id),
                "generation": generation.generation,
                "document_count": generation.document_count,
                "schema_hash": generation.schema_hash,
            }
            if generation is not None
            else None
        ),
        "qa_task": {
            "task_id": accepted_task.get("task_id"),
            "task_version_id": accepted_task.get("task_version_id"),
            "execution_profiles": accepted_task.get("execution_profiles", []),
        },
        "defaults": {
            "target_document_count": 500,
            "control_sample_size": 50,
            "target_batch_size": 100,
            "keep_families_together": True,
        },
    }


def _training_label_stage_context(db: Session, *, run: ReviewProgramRun) -> dict[str, Any]:
    resolution = dict((run.progress_summary or {}).get("training_label_resolution") or {})
    raw_batch_ids = resolution.get("difference_batch_ids") or []
    try:
        batch_ids = [uuid.UUID(str(value)) for value in raw_batch_ids]
    except (TypeError, ValueError) as exc:
        raise ReviewProgramError("Difference-batch provenance is invalid") from exc
    batches = {
        batch.id: batch
        for batch in db.scalars(select(ReviewBatch).where(ReviewBatch.id.in_(batch_ids)))
    }
    batch_reads = []
    for batch_id in batch_ids:
        batch = batches.get(batch_id)
        if batch is None:
            continue
        coding = db.scalar(
            select(ReviewBatchReviewerCoding).where(
                ReviewBatchReviewerCoding.review_batch_id == batch.id,
                ReviewBatchReviewerCoding.reviewer_user_id == batch.assigned_user_id,
            )
        )
        completed_count = (
            int(
                db.scalar(
                    select(func.count())
                    .select_from(ReviewBatchReviewerDocument)
                    .where(
                        ReviewBatchReviewerDocument.reviewer_coding_id == coding.id,
                        ReviewBatchReviewerDocument.status == "COMPLETED",
                    )
                )
                or 0
            )
            if coding is not None
            else 0
        )
        batch_reads.append(
            {
                "id": str(batch.id),
                "name": batch.name,
                "status": batch.status,
                "document_count": batch.document_count,
                "assigned_user_id": str(batch.assigned_user_id) if batch.assigned_user_id else None,
                "reviewer_coding_id": str(coding.id) if coding is not None else None,
                "reviewer_coding_revision": coding.revision if coding is not None else None,
                "completed_document_count": completed_count,
            }
        )
    resolution["difference_batches"] = batch_reads
    return {
        "discovery_analysis": (run.progress_summary or {}).get("discovery_analysis", {}),
        "training_label_resolution": resolution,
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
    support_state = str(
        ((run.progress_summary or {}).get("discovery_analysis") or {}).get("support_status") or ""
    )
    training_label_state = str(
        ((run.progress_summary or {}).get("training_label_resolution") or {}).get("state") or ""
    )
    title, explanation, choices = _action_contract(
        template,
        stage,
        qa_setup_state=qa_setup_state,
        support_state=support_state,
        training_label_state=training_label_state,
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
            else _discovery_stage_context(db, program=program, run=run)
            if stage.key == "DISCOVERY_QA"
            else {"discovery_analysis": (run.progress_summary or {}).get("discovery_analysis", {})}
            if stage.key == "ISSUE_SUPPORT_DECISION"
            else _training_label_stage_context(db, run=run)
            if stage.key in {"DISAGREEMENT_REVIEW", "TRAINING_LABEL_FINALIZATION"}
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
            "DISCOVERY_QA": "DISCOVERY_QA_SETUP",
            "DISAGREEMENT_REVIEW": (
                "DISAGREEMENT_REVIEW_COMPLETION"
                if training_label_state == "AWAITING_ADJUDICATION"
                else "TRAINING_LABEL_POLICY"
            ),
            "TRAINING_LABEL_FINALIZATION": "TRAINING_LABEL_FINALIZATION",
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


def setup_review_program_discovery(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    action_id: uuid.UUID,
    actor: User,
    preview_hash: str,
    target_document_count: int,
    control_sample_size: int,
    target_batch_size: int,
    keep_families_together: bool,
) -> ReviewProgramRun:
    action = db.scalar(
        select(ReviewProgramAction)
        .where(ReviewProgramAction.id == action_id, ReviewProgramAction.run_id == run.id)
        .with_for_update()
    )
    if action is None or action.action_type != "DISCOVERY_QA_SETUP":
        raise ReviewProgramError("Discovery Q&A setup action not found")
    if action.status == "RESOLVED":
        if (action.resolution_payload or {}).get("choice_key") != "START":
            raise ReviewProgramError("Discovery Q&A setup was already resolved differently")
        return run
    if run.current_stage != "DISCOVERY_QA" or run.status != "WAITING_FOR_USER":
        raise ReviewProgramError("Review Program is not waiting for Discovery Q&A setup")
    if action.preview_hash != preview_hash:
        raise ReviewProgramError("Discovery Q&A setup preview is stale")
    if control_sample_size > target_document_count:
        raise ReviewProgramError("Control sample size cannot exceed the discovery target")

    matter = db.get(Matter, program.matter_id)
    if matter is None:
        raise ReviewProgramError("Review Program matter is unavailable")
    generation = db.scalar(
        select(SearchIndexGeneration).where(
            SearchIndexGeneration.matter_id == matter.id,
            SearchIndexGeneration.status == "ACTIVE",
        )
    )
    if generation is None:
        raise ReviewProgramError("The matter search index must be ready before Discovery Q&A")
    assessment, queries = _accepted_discovery_queries(db, run=run)
    task, task_version, profiles = _qa_task_records(db, run)
    if task_version.status != "PUBLISHED" or len(profiles) != 2:
        raise ReviewProgramError("Discovery Q&A requires one published task and two pinned profiles")

    accepted_definition = (run.progress_summary or {}).get("accepted_matter_definition") or {}
    try:
        diagnostic_batch_ids = [
            uuid.UUID(str(value)) for value in accepted_definition.get("diagnostic_batch_ids", [])
        ]
    except (TypeError, ValueError) as exc:
        raise ReviewProgramError("Diagnostic batch provenance is invalid") from exc
    excluded_document_ids = (
        set(
            db.scalars(
                select(ReviewBatchDocument.matter_document_id).where(
                    ReviewBatchDocument.review_batch_id.in_(diagnostic_batch_ids)
                )
            )
        )
        if diagnostic_batch_ids
        else set()
    )
    diagnostic_excluded_count = len(excluded_document_ids)
    prior_batch_set_ids = list(
        db.scalars(
            select(ReviewProgramChild.child_id).where(
                ReviewProgramChild.run_id == run.id,
                ReviewProgramChild.child_type == "ANALYSIS_TASK_BATCH_SET",
            )
        )
    )
    if prior_batch_set_ids:
        excluded_document_ids.update(
            db.scalars(
                select(ReviewBatchDocument.matter_document_id).where(
                    ReviewBatchDocument.analysis_task_batch_set_id.in_(prior_batch_set_ids)
                )
            )
        )

    targeted_limit = target_document_count - control_sample_size
    selected_ids: list[uuid.UUID] = []
    selected_set: set[uuid.UUID] = set()
    selection_reasons: dict[uuid.UUID, list[str]] = {}
    query_yields: list[dict[str, Any]] = []
    for query in queries:
        remaining = targeted_limit - len(selected_ids)
        if remaining <= 0:
            break
        requested = min(query.quota, remaining)
        hits = search_document_ids(
            db,
            matter=matter,
            generation=generation,
            search_definition=query.search_request,
            settings=get_settings(),
            limit=max(requested + len(excluded_document_ids) + len(selected_set), requested),
            relevance_order=True,
        )
        accepted_hits: list[uuid.UUID] = []
        for document_id in hits:
            if document_id in excluded_document_ids:
                continue
            reasons = selection_reasons.setdefault(document_id, [])
            if query.criterion_key not in reasons:
                reasons.append(query.criterion_key)
            if document_id not in selected_set and len(accepted_hits) < requested:
                selected_ids.append(document_id)
                selected_set.add(document_id)
                accepted_hits.append(document_id)
        query_yields.append(
            {
                "ordinal": query.ordinal,
                "criterion_key": query.criterion_key,
                "quota": query.quota,
                "requested": requested,
                "search_hit_count": len(hits),
                "selected_count": len(accepted_hits),
            }
        )

    control_ids: list[uuid.UUID] = []
    if control_sample_size:
        excluded_for_control = excluded_document_ids | selected_set
        control_candidates = db.scalars(
            select(MatterDocument.id)
            .where(MatterDocument.matter_id == matter.id)
            .order_by(MatterDocument.created_at, MatterDocument.id)
        ).yield_per(1_000)
        for document_id in control_candidates:
            if document_id in excluded_for_control:
                continue
            control_ids.append(document_id)
            if len(control_ids) >= control_sample_size:
                break
        for document_id in control_ids:
            selected_ids.append(document_id)
            selected_set.add(document_id)
            selection_reasons[document_id] = ["CONTROL"]
    if not selected_ids:
        raise ReviewProgramError("The discovery retrieval plan selected no documents")

    documents_by_id = {
        document.id: document
        for document in db.scalars(
            select(MatterDocument).where(
                MatterDocument.matter_id == matter.id,
                MatterDocument.id.in_(selected_ids),
            )
        )
    }
    if len(documents_by_id) != len(selected_ids):
        raise ReviewProgramError("Discovery retrieval returned documents outside the matter")
    round_number = run.current_discovery_round + 1
    contract = AnalysisInputContract(
        key="review-program.discovery-population",
        version=1,
        resolver_version="review-program-discovery-retrieval-v1",
        extension_fields=[
            AnalysisInputExtensionField(name="retrieval_lane", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="selection_reasons", type="STRING", nullable=False),
        ],
        options={
            "review_program_id": str(program.id),
            "review_program_run_id": str(run.id),
            "round": round_number,
            "assessment_id": str(assessment.id),
            "search_index_generation_id": str(generation.id),
            "policy_version": "review-program-discovery-retrieval-v1",
        },
    )
    attempt = _current_stage_attempt(db, run, for_update=True)
    prepared = prepare_analysis_input_manifest(
        [
            AnalysisInputDocument(
                matter_document_id=document_id,
                collection_item_id=documents_by_id[document_id].collection_item_id,
                source_collection_id=documents_by_id[document_id].source_collection_id,
                scope_ordinal=ordinal,
                extensions={
                    "retrieval_lane": (
                        "CONTROL" if selection_reasons[document_id] == ["CONTROL"] else "TARGETED"
                    ),
                    "selection_reasons": ",".join(selection_reasons[document_id]),
                },
            )
            for ordinal, document_id in enumerate(selected_ids, start=1)
        ],
        contract=contract,
        scope_type="REVIEW_PROGRAM_STAGE",
        scope_id=attempt.id,
    )
    manifest, _ = publish_analysis_input_manifest(
        db,
        prepared=prepared,
        tenant_id=program.tenant_id,
        client_id=matter.client_id,
        matter_id=matter.id,
        owner_type="REVIEW_PROGRAM_DISCOVERY",
        owner_id=attempt.id,
        producer_kind="REVIEW_PROGRAM_DISCOVERY",
        producer_run_id=run.id,
        actor_user_id=actor.id,
    )
    try:
        batch_set = create_batch_set(
            db,
            matter=matter,
            task=task,
            task_version=task_version,
            name=f"{program.name} · discovery round {round_number}"[:200],
            description="Frozen training-oriented discovery population for dual-model Q&A.",
            population_source_type="INPUT_MANIFEST",
            source_review_batch_id=None,
            input_manifest_id=manifest.id,
            search=None,
            saved_search_id=None,
            saved_search_name=None,
            saved_search_updated_at=None,
            target_batch_size=target_batch_size,
            keep_together=["FAMILY"] if keep_families_together else [],
            random_seed=str(run.id),
            actor_user_id=actor.id,
            notification_parent_type="REVIEW_PROGRAM_RUN",
            notification_parent_id=run.id,
        )
    except AnalysisTaskBatchSetError as exc:
        raise ReviewProgramError(str(exc)) from exc
    batch_set.selection_definition = {
        **batch_set.selection_definition,
        "review_program": {
            "program_id": str(program.id),
            "run_id": str(run.id),
            "stage_attempt_id": str(attempt.id),
            "discovery_round": round_number,
            "assessment_id": str(assessment.id),
            "accepted_definition_content_hash": accepted_definition.get("content_hash"),
            "task_version_id": str(task_version.id),
            "profile_ids": [str(profile.id) for profile in profiles],
            "retrieval_policy_version": "review-program-discovery-retrieval-v1",
            "query_yields": query_yields,
            "control_selected_count": len(control_ids),
            "excluded_diagnostic_document_count": diagnostic_excluded_count,
            "excluded_prior_program_document_count": len(excluded_document_ids)
            - diagnostic_excluded_count,
            "prior_discovery_batch_set_ids": [str(value) for value in prior_batch_set_ids],
        },
    }
    link_review_program_child(
        db,
        program=program,
        run=run,
        stage_attempt=attempt,
        purpose="DISCOVERY_POPULATION",
        child_type="ANALYSIS_TASK_BATCH_SET",
        child_id=batch_set.id,
        derivation_key=f"discovery-batch-set:{attempt.id}",
        batching_policy="PARTITIONED_SET",
        context={
            "input_manifest_id": str(manifest.id),
            "population_count": len(selected_ids),
            "targeted_count": len(selected_ids) - len(control_ids),
            "control_count": len(control_ids),
            "round": round_number,
        },
    )
    discovery_snapshot = {
        "schema_version": 1,
        "round": round_number,
        "assessment_id": str(assessment.id),
        "search_index_generation_id": str(generation.id),
        "input_manifest_id": str(manifest.id),
        "input_manifest_content_hash": manifest.artifact_content_hash,
        "batch_set_id": str(batch_set.id),
        "task_id": str(task.id),
        "task_version_id": str(task_version.id),
        "execution_profile_ids": [str(profile.id) for profile in profiles],
        "target_document_count": target_document_count,
        "selected_document_count": len(selected_ids),
        "targeted_document_count": len(selected_ids) - len(control_ids),
        "control_document_count": len(control_ids),
        "target_batch_size": target_batch_size,
        "keep_families_together": keep_families_together,
        "query_yields": query_yields,
    }
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=actor.id,
        configuration_stage="DISCOVERY_QA",
        configuration_updates={"discovery_qa": discovery_snapshot},
        source_hash_updates={
            "discovery_input_manifest_content_hash": manifest.artifact_content_hash,
            "discovery_search_index_schema_hash": generation.schema_hash,
        },
    )
    run.current_discovery_round = round_number
    run.progress_summary = {
        **run.progress_summary,
        "discovery_qa_state": "MATERIALIZING_BATCH_SET",
        "discovery_qa": {
            **discovery_snapshot,
            "program_version_id": str(version.id),
            "batch_set_run_ids": [],
        },
    }
    resolve_review_program_action(
        db,
        program=program,
        run=run,
        action_id=action.id,
        actor=actor,
        preview_hash=preview_hash,
        choice_key="START",
        reason="Discovery Q&A population accepted",
        input_payload={
            "input_manifest_id": str(manifest.id),
            "batch_set_id": str(batch_set.id),
            "population_count": len(selected_ids),
        },
    )
    enqueue_analysis_task_batch_set(db, batch_set.workflow_id, str(batch_set.id))
    record_audit(
        db,
        tenant_id=program.tenant_id,
        actor_user_id=actor.id,
        action="review_program.discovery_qa_started",
        target_type="review_program_run",
        target_id=run.id,
        details={
            "program_id": str(program.id),
            "batch_set_id": str(batch_set.id),
            "input_manifest_id": str(manifest.id),
            "population_count": len(selected_ids),
            "profile_ids": [str(profile.id) for profile in profiles],
        },
    )
    return run


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
        elif action.stage_key == "DISCOVERY_QA" and choice == "START":
            attempt.status = "RUNNING"
            run.status = "RUNNING"
        elif action.stage_key == "ISSUE_SUPPORT_DECISION" and choice == "SUPPLEMENT":
            _complete_action_attempt(
                attempt,
                action,
                result={"decision": "SUPPLEMENT", "discovery_round": run.current_discovery_round},
            )
            _activate_stage_by_key(
                db,
                program=program,
                run=run,
                stage_key="DISCOVERY_QA",
            )
        elif action.stage_key == "ISSUE_SUPPORT_DECISION" and choice == "IGNORE_LOW_SUPPORT":
            analysis = (run.progress_summary or {}).get("discovery_analysis") or {}
            version = _create_review_program_version(
                db,
                program=program,
                run=run,
                actor_user_id=action.resolved_by_user_id or run.initiated_by_user_id,
                configuration_stage="ISSUE_SUPPORT_DECISION",
                configuration_updates={
                    "accepted_low_support": {
                        "discovery_round": run.current_discovery_round,
                        "categories": analysis.get("low_support", []),
                        "accepted_by_user_id": str(action.resolved_by_user_id),
                        "accepted_at": action.resolved_at.isoformat() if action.resolved_at else None,
                    }
                },
                source_hash_updates={},
            )
            _complete_action_attempt(
                attempt,
                action,
                result={"decision": "IGNORE_LOW_SUPPORT", "program_version_id": str(version.id)},
            )
            _advance_after_stage(db, program=program, run=run, attempt=attempt)
        elif action.stage_key == "ISSUE_SUPPORT_DECISION" and choice == "REFINE_DEFINITION":
            _complete_action_attempt(
                attempt,
                action,
                result={"decision": "REFINE_DEFINITION", "discovery_round": run.current_discovery_round},
            )
            _activate_stage_by_key(
                db,
                program=program,
                run=run,
                stage_key="DEFINITION_REFINEMENT",
            )
        elif action.stage_key == "DISAGREEMENT_REVIEW" and choice == "USE_ALL_MODEL_CODES":
            _select_training_label_policy(
                db,
                program=program,
                run=run,
                attempt=attempt,
                action=action,
                policy="ALL_MODEL_CODES",
            )
        elif action.stage_key == "DISAGREEMENT_REVIEW" and choice == "USE_EXACT_AGREEMENT":
            _select_training_label_policy(
                db,
                program=program,
                run=run,
                attempt=attempt,
                action=action,
                policy="EXACT_AGREEMENT",
            )
        elif action.stage_key == "DISAGREEMENT_REVIEW" and choice == "REVIEW_DISAGREEMENTS":
            _start_disagreement_review(
                db,
                program=program,
                run=run,
                attempt=attempt,
                action=action,
            )
        elif action.stage_key == "DISAGREEMENT_REVIEW" and choice == "CONFIRM_ADJUDICATION":
            _confirm_disagreement_review(
                db,
                program=program,
                run=run,
                attempt=attempt,
                action=action,
            )
        elif action.stage_key == "TRAINING_LABEL_FINALIZATION" and choice == "FINALIZE":
            _finalize_training_labels(
                db,
                program=program,
                run=run,
                attempt=attempt,
                action=action,
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


def project_review_program_discovery_batch_set_terminal(
    db: Session,
    *,
    batch_set: AnalysisTaskBatchSet,
    workflow: WorkflowRun,
) -> ReviewProgramEvent | None:
    if (
        workflow.notification_policy != "PARENT_MANAGED"
        or workflow.notification_parent_type != "REVIEW_PROGRAM_RUN"
        or workflow.notification_parent_id is None
    ):
        return None
    child = db.scalar(
        select(ReviewProgramChild).where(
            ReviewProgramChild.run_id == workflow.notification_parent_id,
            ReviewProgramChild.child_type == "ANALYSIS_TASK_BATCH_SET",
            ReviewProgramChild.child_id == batch_set.id,
        )
    )
    if child is None:
        return None
    child.status = batch_set.status
    child.context = {
        **child.context,
        "workflow_status": workflow.status,
        "population_count": batch_set.population_count,
        "batch_count": batch_set.batch_count,
        "population_hash": batch_set.population_hash,
        "warnings": batch_set.warning_details,
        "error_message": batch_set.error_message,
    }
    run = db.get(ReviewProgramRun, child.run_id)
    attempt = db.get(ReviewProgramStageAttempt, child.stage_attempt_id)
    program = db.get(ReviewProgram, run.program_id) if run is not None else None
    if run is None or attempt is None or program is None:
        raise ReviewProgramError("Review Program discovery Batch Set link is incomplete")
    if (
        run.status in TERMINAL_RUN_STATUSES
        or run.current_stage != "DISCOVERY_QA"
        or attempt.id != _current_stage_attempt(db, run).id
    ):
        return None
    if workflow.status in {"FAILED", "CANCELED"}:
        run.status = "RUNNING"
        attempt.status = "RUNNING"
        return fail_review_program_stage(
            db,
            program=program,
            run=run,
            error_code=f"DISCOVERY_BATCH_SET_{workflow.status}",
            error_summary=batch_set.error_message or f"Discovery Batch Set {workflow.status.lower()}",
            retriable=True,
        )
    if workflow.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        return None

    task, task_version, profiles = _qa_task_records(db, run)
    matter = db.get(Matter, program.matter_id)
    if matter is None:
        raise ReviewProgramError("Review Program matter is unavailable")
    launched_runs: list[AnalysisTaskBatchSetRun] = []
    try:
        for profile in profiles:
            batch_run, _ = create_batch_set_run(
                db,
                matter=matter,
                batch_set=batch_set,
                task=task,
                task_version=task_version,
                configuration={"max_concurrency": 2, "result_policy": "ISOLATED"},
                idempotency_key=f"review-program:{run.id}:discovery:{attempt.attempt}:profile:{profile.id}",
                actor_user_id=run.initiated_by_user_id,
                execution_profile=profile,
                notification_parent_type="REVIEW_PROGRAM_RUN",
                notification_parent_id=run.id,
            )
            launched_runs.append(batch_run)
            link_review_program_child(
                db,
                program=program,
                run=run,
                stage_attempt=attempt,
                purpose="DISCOVERY_QA_PROFILE",
                child_type="ANALYSIS_TASK_BATCH_SET_RUN",
                child_id=batch_run.id,
                derivation_key=f"discovery-profile-run:{attempt.id}:{profile.id}",
                batching_policy="PARTITIONED_SET",
                context={
                    "batch_set_id": str(batch_set.id),
                    "execution_profile_id": str(profile.id),
                    "execution_profile_key": profile.key,
                    "execution_profile_content_hash": profile.content_hash,
                },
            )
    except AnalysisTaskBatchSetRunError as exc:
        run.status = "RUNNING"
        attempt.status = "RUNNING"
        return fail_review_program_stage(
            db,
            program=program,
            run=run,
            error_code="DISCOVERY_QA_LAUNCH_FAILED",
            error_summary=str(exc),
            retriable=True,
        )
    discovery = dict((run.progress_summary or {}).get("discovery_qa") or {})
    discovery["batch_set_run_ids"] = [str(item.id) for item in launched_runs]
    discovery["batch_set_population_hash"] = batch_set.population_hash
    discovery["batch_count"] = batch_set.batch_count
    run.progress_summary = {
        **run.progress_summary,
        "discovery_qa_state": "RUNNING_PROFILES",
        "discovery_qa": discovery,
    }
    for batch_run in launched_runs:
        enqueue_analysis_task_batch_set_run(db, batch_run.workflow_id, str(batch_run.id))
    return None


def _discovery_alignment_snapshot(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    profile_runs: list[AnalysisTaskBatchSetRun],
) -> dict[str, Any]:
    runs_by_batch_set: dict[uuid.UUID, list[AnalysisTaskBatchSetRun]] = {}
    for profile_run in profile_runs:
        runs_by_batch_set.setdefault(profile_run.analysis_task_batch_set_id, []).append(profile_run)
    if not runs_by_batch_set or any(len(items) != 2 for items in runs_by_batch_set.values()):
        raise ReviewProgramError("Discovery alignment requires exactly two profile runs per frozen population")
    accepted_task = (run.progress_summary or {}).get("accepted_qa_task") or {}
    definition_ids = [
        uuid.UUID(str(value)) for value in accepted_task.get("destination_metadata_definition_ids", [])
    ]
    definitions = {
        definition.id: definition
        for definition in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == program.matter_id,
                MetadataDefinition.id.in_(definition_ids),
            )
        )
    }
    paired_children: list[
        tuple[
            AnalysisTaskBatchSetRun,
            AnalysisTaskBatchSetRun,
            dict[uuid.UUID, AnalysisTaskBatchSetRunChild],
            dict[uuid.UUID, AnalysisTaskBatchSetRunChild],
            list[uuid.UUID],
        ]
    ] = []
    execution_metrics: dict[uuid.UUID | None, Counter] = {}
    for batch_set_id in sorted(runs_by_batch_set, key=str):
        left_parent, right_parent = sorted(
            runs_by_batch_set[batch_set_id],
            key=lambda item: str(item.execution_profile_id),
        )
        children_by_parent = []
        for profile_run in (left_parent, right_parent):
            children = list(
                db.scalars(
                    select(AnalysisTaskBatchSetRunChild).where(
                        AnalysisTaskBatchSetRunChild.batch_set_run_id == profile_run.id
                    )
                )
            )
            children_by_parent.append({child.review_batch_id: child for child in children})
            metrics = execution_metrics.setdefault(profile_run.execution_profile_id, Counter())
            for child in children:
                review_run = db.get(ReviewBatchRun, child.child_run_id)
                workflow = (
                    db.get(WorkflowRun, review_run.current_workflow_run_id)
                    if review_run is not None and review_run.current_workflow_run_id is not None
                    else None
                )
                if workflow is None:
                    continue
                metrics["request_count"] += workflow.request_count
                metrics["input_tokens"] += workflow.input_tokens
                metrics["cached_input_tokens"] += workflow.cached_input_tokens
                metrics["output_tokens"] += workflow.output_tokens
                if workflow.started_at is not None and workflow.completed_at is not None:
                    metrics["elapsed_seconds"] += int(
                        (workflow.completed_at - workflow.started_at).total_seconds()
                    )
        batch_ids = sorted(
            set(children_by_parent[0]).intersection(children_by_parent[1]),
            key=str,
        )
        paired_children.append(
            (left_parent, right_parent, children_by_parent[0], children_by_parent[1], batch_ids)
        )
    field_snapshots: list[dict[str, Any]] = []
    overall = Counter()
    minimum_positive_support = 25
    minimum_negative_support = 25
    for definition_id in definition_ids:
        definition = definitions.get(definition_id)
        if definition is None:
            continue
        counts = Counter()
        class_counts: dict[str, Counter] = {}
        for left_parent, right_parent, left_children, right_children, batch_ids in paired_children:
            for batch_id in batch_ids:
                left_child = left_children[batch_id]
                right_child = right_children[batch_id]
                left_run = db.get(ReviewBatchRun, left_child.child_run_id)
                right_run = db.get(ReviewBatchRun, right_child.child_run_id)
                if left_run is None or right_run is None:
                    raise ReviewProgramError("Discovery Q&A child-run provenance is incomplete")
                options = [
                    ReviewBatchComparisonSourceOptionRead(
                        id=source.id,
                        source_type="RUN",
                        run_type=source.run_type,
                        purpose=source.purpose,
                        status=source.status,
                        label=str(
                            (parent.configuration_snapshot or {}).get(
                                "execution_profile_id", f"Profile {index + 1}"
                            )
                        ),
                        detail=(source.configuration_snapshot or {}).get("model_key"),
                        metadata_definition_ids=definition_ids,
                        created_at=source.created_at,
                    )
                    for index, (parent, source) in enumerate(
                        ((left_parent, left_run), (right_parent, right_run))
                    )
                ]
                for document in iter_comparison_documents(
                    db,
                    batch_id=batch_id,
                    definition_id=definition_id,
                    source_options=options,
                ):
                    counts[document.agreement_status] += 1
                    overall[document.agreement_status] += 1
                    left_values = set(document.sources[left_run.id].value_keys)
                    right_values = set(document.sources[right_run.id].value_keys)
                    for value_key in left_values | right_values:
                        value_counts = class_counts.setdefault(value_key, Counter())
                        value_counts["left"] += value_key in left_values
                        value_counts["right"] += value_key in right_values
                        value_counts["intersection"] += value_key in left_values and value_key in right_values
                        value_counts["union"] += 1
                        value_counts["disagreement"] += (value_key in left_values) != (
                            value_key in right_values
                        )
        declared_keys = [
            str(value.get("key"))
            for value in definition.allowed_values or []
            if value.get("key") is not None
        ]
        if definition.type == "BOOLEAN":
            declared_keys = ["true", "false"]
        observed_keys = sorted(set(class_counts).union(declared_keys))
        if not observed_keys:
            observed_keys = ["__NO_DECLARED_CLASS__"]
        evaluated_count = counts["EXACT"] + counts["PARTIAL"] + counts["DISAGREEMENT"]
        classes = [
            {
                "value_key": value_key,
                "left_selected_count": class_counts.get(value_key, Counter())["left"],
                "right_selected_count": class_counts.get(value_key, Counter())["right"],
                "intersection_count": class_counts.get(value_key, Counter())["intersection"],
                "union_count": class_counts.get(value_key, Counter())["union"],
                "negative_count": max(
                    0,
                    evaluated_count - class_counts.get(value_key, Counter())["union"],
                ),
                "disagreement_count": class_counts.get(value_key, Counter())["disagreement"],
                "support_status": (
                    "SUFFICIENT"
                    if (
                        class_counts.get(value_key, Counter())["union"] >= minimum_positive_support
                        and evaluated_count - class_counts.get(value_key, Counter())["union"]
                        >= minimum_negative_support
                    )
                    else "LOW_SUPPORT"
                ),
            }
            for value_key in observed_keys
        ]
        field_snapshots.append(
            {
                "metadata_definition_id": str(definition.id),
                "key": definition.key,
                "display_name": definition.display_name,
                "exact_count": counts["EXACT"],
                "partial_count": counts["PARTIAL"],
                "disagreement_count": counts["DISAGREEMENT"],
                "incomplete_count": counts["INCOMPLETE"],
                "classes": classes,
            }
        )
    low_support = [
        {
            "field_key": field["key"],
            "value_key": value["value_key"],
            "positive_count": value["union_count"],
            "negative_count": value["negative_count"],
        }
        for field in field_snapshots
        for value in field["classes"]
        if value["support_status"] == "LOW_SUPPORT"
    ]
    return {
        "schema_version": 1,
        "policy": {
            "version": "review-program-issue-support-v1",
            "minimum_positive_support": minimum_positive_support,
            "minimum_negative_support": minimum_negative_support,
        },
        "profile_run_ids": [str(item.id) for item in profile_runs],
        "execution_profiles": [
            {
                "execution_profile_id": str(profile_id),
                "request_count": metrics["request_count"],
                "input_tokens": metrics["input_tokens"],
                "cached_input_tokens": metrics["cached_input_tokens"],
                "output_tokens": metrics["output_tokens"],
                "elapsed_seconds": metrics["elapsed_seconds"],
            }
            for profile_id, metrics in sorted(execution_metrics.items(), key=lambda item: str(item[0]))
        ],
        "batch_count": sum(len(item[4]) for item in paired_children),
        "overall": {
            "exact_count": overall["EXACT"],
            "partial_count": overall["PARTIAL"],
            "disagreement_count": overall["DISAGREEMENT"],
            "incomplete_count": overall["INCOMPLETE"],
        },
        "fields": field_snapshots,
        "low_support": low_support,
        "support_status": "SUFFICIENT" if not low_support else "INSUFFICIENT",
    }


def _completed_discovery_profile_runs(
    db: Session,
    *,
    run: ReviewProgramRun,
) -> list[AnalysisTaskBatchSetRun]:
    child_ids = list(
        db.scalars(
            select(ReviewProgramChild.child_id).where(
                ReviewProgramChild.run_id == run.id,
                ReviewProgramChild.purpose == "DISCOVERY_QA_PROFILE",
                ReviewProgramChild.child_type == "ANALYSIS_TASK_BATCH_SET_RUN",
            )
        )
    )
    if not child_ids:
        raise ReviewProgramError("Discovery Q&A profile provenance is unavailable")
    profile_runs = list(
        db.scalars(
            select(AnalysisTaskBatchSetRun)
            .where(
                AnalysisTaskBatchSetRun.id.in_(child_ids),
                AnalysisTaskBatchSetRun.status.in_({"COMPLETED", "COMPLETED_WITH_ERRORS"}),
            )
            .order_by(AnalysisTaskBatchSetRun.created_at, AnalysisTaskBatchSetRun.id)
        )
    )
    if len(profile_runs) != len(child_ids):
        raise ReviewProgramError("Every Discovery Q&A profile run must be complete before label resolution")
    return profile_runs


def _create_disagreement_review_batches(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    actor_user_id: uuid.UUID,
) -> list[ReviewBatch]:
    profile_runs = _completed_discovery_profile_runs(db, run=run)
    runs_by_batch_set: dict[uuid.UUID, list[AnalysisTaskBatchSetRun]] = {}
    for profile_run in profile_runs:
        runs_by_batch_set.setdefault(profile_run.analysis_task_batch_set_id, []).append(profile_run)
    accepted_task = (run.progress_summary or {}).get("accepted_qa_task") or {}
    try:
        definition_ids = list(
            dict.fromkeys(
                uuid.UUID(str(value))
                for value in accepted_task.get("destination_metadata_definition_ids", [])
            )
        )
    except (TypeError, ValueError) as exc:
        raise ReviewProgramError("Q&A destination-field provenance is invalid") from exc
    if not definition_ids:
        raise ReviewProgramError("Q&A destination-field provenance is unavailable")
    definitions = {
        definition.id: definition
        for definition in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == program.matter_id,
                MetadataDefinition.id.in_(definition_ids),
            )
        )
    }
    created: list[ReviewBatch] = []
    for batch_set_id, paired_runs in sorted(runs_by_batch_set.items(), key=lambda item: str(item[0])):
        if len(paired_runs) != 2:
            raise ReviewProgramError("Difference review requires exactly two profiles per discovery population")
        left_parent, right_parent = sorted(paired_runs, key=lambda item: str(item.execution_profile_id))
        left_profile = db.get(AnalysisTaskExecutionProfile, left_parent.execution_profile_id)
        right_profile = db.get(AnalysisTaskExecutionProfile, right_parent.execution_profile_id)
        if left_profile is None or right_profile is None:
            raise ReviewProgramError("Discovery execution-profile provenance is unavailable")
        child_maps = []
        for parent in (left_parent, right_parent):
            child_maps.append(
                {
                    child.review_batch_id: child
                    for child in db.scalars(
                        select(AnalysisTaskBatchSetRunChild).where(
                            AnalysisTaskBatchSetRunChild.batch_set_run_id == parent.id
                        )
                    )
                }
            )
        if not child_maps[0] or set(child_maps[0]) != set(child_maps[1]):
            raise ReviewProgramError("Discovery profiles do not cover the same source batches")
        for source_batch_id in sorted(set(child_maps[0]).intersection(child_maps[1]), key=str):
            source_batch = db.get(ReviewBatch, source_batch_id)
            left_run = db.get(ReviewBatchRun, child_maps[0][source_batch_id].child_run_id)
            right_run = db.get(ReviewBatchRun, child_maps[1][source_batch_id].child_run_id)
            if source_batch is None or left_run is None or right_run is None:
                raise ReviewProgramError("Discovery child-run provenance is incomplete")
            source_options = [
                ReviewBatchComparisonSourceOptionRead(
                    id=source.id,
                    source_type="RUN",
                    run_type=source.run_type,
                    purpose=source.purpose,
                    status=source.status,
                    label=profile.name,
                    detail=profile.resolved_model,
                    metadata_definition_ids=definition_ids,
                    created_at=source.created_at,
                )
                for profile, source in ((left_profile, left_run), (right_profile, right_run))
            ]
            source_snapshots = [
                {**source.model_dump(mode="json"), "reviewer_revision": None}
                for source in source_options
            ]
            for definition_id in definition_ids:
                definition = definitions.get(definition_id)
                if definition is None:
                    raise ReviewProgramError("A Q&A destination field is unavailable")
                candidate_ids = comparison_document_ids(
                    db,
                    batch_id=source_batch.id,
                    definition_id=definition.id,
                    source_options=source_options,
                    row_filter="DISAGREEMENT",
                )
                if not candidate_ids:
                    continue
                batch_id = uuid.uuid4()
                batch = ReviewBatch(
                    id=batch_id,
                    matter_id=program.matter_id,
                    name=(
                        f"{program.name} · {definition.display_name} differences · {len(created) + 1}"
                    )[:200],
                    description=(
                        "Ad hoc human review of differences between the two pinned Discovery Q&A profiles."
                    ),
                    selection_type="COMPARISON_RESULT",
                    selection_definition={
                        "type": "COMPARISON_RESULT",
                        "review_program_id": str(program.id),
                        "review_program_run_id": str(run.id),
                        "stage_attempt_id": str(attempt.id),
                        "batching_policy": "AD_HOC_BATCH",
                        "source_batch_id": str(source_batch.id),
                        "source_batch_name": source_batch.name,
                        "source_batch_set_id": str(batch_set_id),
                        "metadata_definition_id": str(definition.id),
                        "field_display_name": definition.display_name,
                        "row_filter": "DISAGREEMENT",
                        "sources": source_snapshots,
                        "candidate_count": len(candidate_ids),
                    },
                    source_batch_id=source_batch.id,
                    assigned_user_id=program.owner_user_id,
                    assigned_by_user_id=actor_user_id,
                    assigned_at=utcnow(),
                    reviewer_value_visibility="OWN_VALUES",
                    status="QUEUED",
                    workflow_id=f"review-batch:{batch_id}",
                    created_by_user_id=actor_user_id,
                )
                db.add(batch)
                db.flush()
                group = ReviewBatchCodingGroup(
                    review_batch_id=batch.id,
                    source_metadata_group_id=None,
                    display_name="Difference review",
                    description="Assign the final coding for the field where the Q&A profiles differ.",
                    sort_order=0,
                )
                db.add(group)
                db.flush()
                db.add(
                    ReviewBatchCodingField(
                        review_batch_coding_group_id=group.id,
                        metadata_definition_id=definition.id,
                        sort_order=0,
                        definition_snapshot={
                            "key": definition.key,
                            "display_name": definition.display_name,
                            "description": definition.description,
                            "type": definition.type,
                            "cardinality": definition.cardinality,
                            "allowed_values": definition.allowed_values,
                        },
                    )
                )
                execution = create_review_batch_execution(
                    db,
                    batch,
                    initiated_by_user_id=actor_user_id,
                    notification_parent_type="REVIEW_PROGRAM_RUN",
                    notification_parent_id=run.id,
                )
                link_review_program_child(
                    db,
                    program=program,
                    run=run,
                    stage_attempt=attempt,
                    purpose="DISCOVERY_DISAGREEMENT_REVIEW",
                    child_type="REVIEW_BATCH",
                    child_id=batch.id,
                    derivation_key=(
                        f"disagreement-review:{attempt.id}:{source_batch.id}:{definition.id}"
                    ),
                    batching_policy="AD_HOC_BATCH",
                    context={
                        "source_batch_id": str(source_batch.id),
                        "metadata_definition_id": str(definition.id),
                        "candidate_count": len(candidate_ids),
                        "assigned_user_id": str(program.owner_user_id),
                    },
                )
                enqueue_review_batch(db, execution.dbos_workflow_id, str(batch.id))
                record_audit(
                    db,
                    tenant_id=program.tenant_id,
                    actor_user_id=actor_user_id,
                    action="review_program.disagreement_batch_created",
                    target_type="review_batch",
                    target_id=batch.id,
                    details={
                        "program_id": str(program.id),
                        "run_id": str(run.id),
                        "source_batch_id": str(source_batch.id),
                        "metadata_definition_id": str(definition.id),
                        "candidate_count": len(candidate_ids),
                    },
                )
                created.append(batch)
    return created


def _select_training_label_policy(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    action: ReviewProgramAction,
    policy: str,
) -> None:
    profile_runs = _completed_discovery_profile_runs(db, run=run)
    resolution = {
        "schema_version": 1,
        "state": "POLICY_SELECTED",
        "policy": policy,
        "profile_run_ids": [str(item.id) for item in profile_runs],
        "difference_batch_ids": [],
        "adjudication_sources": [],
        "selected_by_user_id": str(action.resolved_by_user_id),
        "selected_at": action.resolved_at.isoformat() if action.resolved_at else None,
    }
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=action.resolved_by_user_id or run.initiated_by_user_id,
        configuration_stage="TRAINING_LABEL_FINALIZATION",
        configuration_updates={"training_label_resolution": resolution},
        source_hash_updates={"training_label_resolution_hash": canonical_hash(resolution)},
    )
    run.progress_summary = {**run.progress_summary, "training_label_resolution": resolution}
    _complete_action_attempt(
        attempt,
        action,
        result={"training_label_resolution": resolution, "program_version_id": str(version.id)},
    )
    _advance_after_stage(db, program=program, run=run, attempt=attempt)


def _start_disagreement_review(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    action: ReviewProgramAction,
) -> None:
    batches = _create_disagreement_review_batches(
        db,
        program=program,
        run=run,
        attempt=attempt,
        actor_user_id=action.resolved_by_user_id or run.initiated_by_user_id,
    )
    if not batches:
        _select_training_label_policy(
            db,
            program=program,
            run=run,
            attempt=attempt,
            action=action,
            policy="EXACT_AGREEMENT",
        )
        return
    profile_runs = _completed_discovery_profile_runs(db, run=run)
    resolution = {
        "schema_version": 1,
        "state": "AWAITING_ADJUDICATION",
        "policy": "ADJUDICATED_DISAGREEMENTS",
        "profile_run_ids": [str(item.id) for item in profile_runs],
        "difference_batch_ids": [str(batch.id) for batch in batches],
        "difference_document_count": sum(
            int(batch.selection_definition.get("candidate_count", 0)) for batch in batches
        ),
        "adjudication_sources": [],
        "selected_by_user_id": str(action.resolved_by_user_id),
        "selected_at": action.resolved_at.isoformat() if action.resolved_at else None,
    }
    _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=action.resolved_by_user_id or run.initiated_by_user_id,
        configuration_stage="DISAGREEMENT_REVIEW",
        configuration_updates={"training_label_resolution": resolution},
        source_hash_updates={"training_label_resolution_hash": canonical_hash(resolution)},
    )
    run.progress_summary = {**run.progress_summary, "training_label_resolution": resolution}
    attempt.status = "RUNNING"
    _create_action_for_stage(
        db,
        program=program,
        run=run,
        attempt=attempt,
        stage=_stage_for_run(program, run),
        template=_template_for_program(program),
    )


def _confirm_disagreement_review(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    action: ReviewProgramAction,
) -> None:
    resolution = dict((run.progress_summary or {}).get("training_label_resolution") or {})
    if resolution.get("state") != "AWAITING_ADJUDICATION":
        raise ReviewProgramError("Difference review is not awaiting adjudication")
    try:
        batch_ids = [uuid.UUID(str(value)) for value in resolution.get("difference_batch_ids", [])]
    except (TypeError, ValueError) as exc:
        raise ReviewProgramError("Difference-batch provenance is invalid") from exc
    if not batch_ids:
        raise ReviewProgramError("Difference-batch provenance is unavailable")
    adjudication_sources: list[dict[str, Any]] = []
    for batch_id in batch_ids:
        batch = db.get(ReviewBatch, batch_id)
        if batch is None or batch.matter_id != program.matter_id:
            raise ReviewProgramError("A difference batch is unavailable")
        if batch.source_batch_id is None:
            raise ReviewProgramError(f"Difference batch {batch.name} has no source batch")
        if batch.status != "READY":
            raise ReviewProgramError(f"Difference batch {batch.name} is not ready")
        if batch.assigned_user_id is None:
            raise ReviewProgramError(f"Difference batch {batch.name} has no assigned reviewer")
        coding = db.scalar(
            select(ReviewBatchReviewerCoding).where(
                ReviewBatchReviewerCoding.review_batch_id == batch.id,
                ReviewBatchReviewerCoding.reviewer_user_id == batch.assigned_user_id,
            )
        )
        if coding is None:
            raise ReviewProgramError(f"Difference batch {batch.name} has not been reviewed")
        document_ids = list(
            db.scalars(
                select(ReviewBatchDocument.matter_document_id)
                .where(ReviewBatchDocument.review_batch_id == batch.id)
                .order_by(ReviewBatchDocument.sequence_number)
            )
        )
        if len(document_ids) != batch.document_count:
            raise ReviewProgramError(f"Difference batch {batch.name} membership is incomplete")
        try:
            definition_id = uuid.UUID(str(batch.selection_definition["metadata_definition_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewProgramError(
                f"Difference batch {batch.name} has invalid coding-field provenance"
            ) from exc
        resolved = resolve_reviewer_coding_as_of(
            db,
            reviewer_coding_id=coding.id,
            revision=coding.revision,
            document_ids=document_ids,
            metadata_definition_ids=[definition_id],
        )
        completed_document_ids = {
            item.matter_document_id for item in resolved.documents if item.status == "COMPLETED"
        }
        if len(completed_document_ids) != batch.document_count:
            raise ReviewProgramError(
                f"Difference batch {batch.name} has "
                f"{batch.document_count - len(completed_document_ids)} unresolved documents"
            )
        coded_document_ids = {
            item.matter_document_id
            for item in resolved.fields
            if item.metadata_definition_id == definition_id
        }
        if coded_document_ids != set(document_ids):
            raise ReviewProgramError(
                f"Difference batch {batch.name} has incomplete field coding"
            )
        adjudication_sources.append(
            {
                "review_batch_id": str(batch.id),
                "source_batch_id": str(batch.source_batch_id),
                "metadata_definition_id": str(definition_id),
                "reviewer_coding_id": str(coding.id),
                "reviewer_user_id": str(coding.reviewer_user_id),
                "reviewer_coding_revision": coding.revision,
                "document_count": batch.document_count,
            }
        )
    resolution = {
        **resolution,
        "state": "POLICY_SELECTED",
        "adjudication_sources": adjudication_sources,
        "confirmed_by_user_id": str(action.resolved_by_user_id),
        "confirmed_at": action.resolved_at.isoformat() if action.resolved_at else None,
    }
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=action.resolved_by_user_id or run.initiated_by_user_id,
        configuration_stage="TRAINING_LABEL_FINALIZATION",
        configuration_updates={"training_label_resolution": resolution},
        source_hash_updates={"training_label_resolution_hash": canonical_hash(resolution)},
    )
    run.progress_summary = {**run.progress_summary, "training_label_resolution": resolution}
    _complete_action_attempt(
        attempt,
        action,
        result={"training_label_resolution": resolution, "program_version_id": str(version.id)},
    )
    _advance_after_stage(db, program=program, run=run, attempt=attempt)


def _finalize_training_labels(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    action: ReviewProgramAction,
) -> None:
    resolution = dict((run.progress_summary or {}).get("training_label_resolution") or {})
    if resolution.get("state") != "POLICY_SELECTED":
        raise ReviewProgramError("Training-label policy has not been selected")
    snapshot = {
        **resolution,
        "state": "FROZEN",
        "frozen_by_user_id": str(action.resolved_by_user_id),
        "frozen_at": action.resolved_at.isoformat() if action.resolved_at else None,
        "discovery_analysis_hash": canonical_hash(
            (run.progress_summary or {}).get("discovery_analysis") or {}
        ),
    }
    snapshot["content_hash"] = canonical_hash(snapshot)
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=action.resolved_by_user_id or run.initiated_by_user_id,
        configuration_stage="CLASSIFIER_TRAINING",
        configuration_updates={"training_labels": snapshot},
        source_hash_updates={"training_labels_content_hash": snapshot["content_hash"]},
    )
    run.progress_summary = {
        **run.progress_summary,
        "training_label_resolution": {**resolution, "state": "FROZEN"},
        "training_labels": snapshot,
    }
    _complete_action_attempt(
        attempt,
        action,
        result={"training_labels": snapshot, "program_version_id": str(version.id)},
    )
    _advance_after_stage(db, program=program, run=run, attempt=attempt)


def project_review_program_discovery_qa_run_terminal(
    db: Session,
    *,
    batch_run: AnalysisTaskBatchSetRun,
    workflow: WorkflowRun,
) -> ReviewProgramEvent | None:
    if (
        workflow.notification_policy != "PARENT_MANAGED"
        or workflow.notification_parent_type != "REVIEW_PROGRAM_RUN"
        or workflow.notification_parent_id is None
    ):
        return None
    child = db.scalar(
        select(ReviewProgramChild).where(
            ReviewProgramChild.run_id == workflow.notification_parent_id,
            ReviewProgramChild.child_type == "ANALYSIS_TASK_BATCH_SET_RUN",
            ReviewProgramChild.child_id == batch_run.id,
        )
    )
    if child is None:
        return None
    child.status = batch_run.status
    child.context = {
        **child.context,
        "workflow_status": workflow.status,
        "completed_child_count": batch_run.completed_child_count,
        "failed_child_count": batch_run.failed_child_count,
    }
    run = db.get(ReviewProgramRun, child.run_id)
    attempt = db.get(ReviewProgramStageAttempt, child.stage_attempt_id)
    program = db.get(ReviewProgram, run.program_id) if run is not None else None
    if run is None or attempt is None or program is None:
        raise ReviewProgramError("Review Program discovery Q&A link is incomplete")
    recovering_failed_profile = (
        run.status == "FAILED"
        and run.failure_code == "DISCOVERY_QA_PROFILE_FAILED"
        and run.current_stage == "DISCOVERY_QA"
        and attempt.id == _current_stage_attempt(db, run).id
    )
    if (
        (run.status in TERMINAL_RUN_STATUSES and not recovering_failed_profile)
        or run.current_stage != "DISCOVERY_QA"
        or attempt.id != _current_stage_attempt(db, run).id
    ):
        return None
    profile_children = list(
        db.scalars(
            select(ReviewProgramChild)
            .where(
                ReviewProgramChild.run_id == run.id,
                ReviewProgramChild.stage_attempt_id == attempt.id,
                ReviewProgramChild.purpose == "DISCOVERY_QA_PROFILE",
                ReviewProgramChild.child_type == "ANALYSIS_TASK_BATCH_SET_RUN",
            )
            .order_by(ReviewProgramChild.created_at, ReviewProgramChild.id)
        )
    )
    profile_runs = [db.get(AnalysisTaskBatchSetRun, item.child_id) for item in profile_children]
    if len(profile_runs) != 2 or any(item is None for item in profile_runs):
        return None
    resolved_runs = [item for item in profile_runs if item is not None]
    terminal = {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}
    if any(item.status not in terminal for item in resolved_runs):
        return None
    has_failed_profile = any(
        item.status in {"FAILED", "CANCELED"} or item.failed_child_count > 0
        for item in resolved_runs
    )
    if has_failed_profile:
        if recovering_failed_profile:
            return None
        run.status = "RUNNING"
        attempt.status = "RUNNING"
        return fail_review_program_stage(
            db,
            program=program,
            run=run,
            error_code="DISCOVERY_QA_PROFILE_FAILED",
            error_summary="One or more Discovery Q&A profile runs failed",
            retriable=True,
        )

    if recovering_failed_profile:
        run.status = "RUNNING"
        run.failure_code = None
        run.failure_summary = None
        run.failure_retriable = False
        run.completed_at = None
        attempt.status = "RUNNING"
        attempt.error_code = None
        attempt.error_summary = None
        attempt.completed_at = None
        resolve_source_notifications(
            db,
            source=NotificationSource("review_program_run", run.id),
            resolution_code="RECOVERED",
        )

    discovery = dict((run.progress_summary or {}).get("discovery_qa") or {})
    discovery["profile_runs"] = [
        {
            "id": str(item.id),
            "execution_profile_id": str(item.execution_profile_id),
            "status": item.status,
            "completed_child_count": item.completed_child_count,
            "failed_child_count": item.failed_child_count,
        }
        for item in resolved_runs
    ]
    run.progress_summary = {
        **run.progress_summary,
        "discovery_qa_state": "COMPLETED",
        "discovery_qa": discovery,
    }
    complete_review_program_stage(
        db,
        program=program,
        run=run,
        result_summary={"discovery_qa": discovery},
        idempotency_key=f"discovery-qa-terminal:{attempt.id}",
    )
    if run.current_stage != "DISCOVERY_ANALYSIS" or run.status != "RUNNING":
        return None
    all_profile_child_ids = list(
        db.scalars(
            select(ReviewProgramChild.child_id).where(
                ReviewProgramChild.run_id == run.id,
                ReviewProgramChild.purpose == "DISCOVERY_QA_PROFILE",
                ReviewProgramChild.child_type == "ANALYSIS_TASK_BATCH_SET_RUN",
            )
        )
    )
    accumulated_profile_runs = list(
        db.scalars(
            select(AnalysisTaskBatchSetRun)
            .where(
                AnalysisTaskBatchSetRun.id.in_(all_profile_child_ids),
                AnalysisTaskBatchSetRun.status.in_({"COMPLETED", "COMPLETED_WITH_ERRORS"}),
            )
            .order_by(AnalysisTaskBatchSetRun.created_at, AnalysisTaskBatchSetRun.id)
        )
    )
    alignment = _discovery_alignment_snapshot(
        db,
        program=program,
        run=run,
        profile_runs=accumulated_profile_runs,
    )
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=run.initiated_by_user_id,
        configuration_stage="ISSUE_SUPPORT_DECISION",
        configuration_updates={"discovery_analysis": alignment},
        source_hash_updates={"discovery_analysis_hash": canonical_hash(alignment)},
    )
    run.progress_summary = {
        **run.progress_summary,
        "discovery_analysis": alignment,
        "discovery_analysis_program_version_id": str(version.id),
    }
    return complete_review_program_stage(
        db,
        program=program,
        run=run,
        result_summary=alignment,
        idempotency_key=f"discovery-analysis-terminal:{run.id}:{run.current_discovery_round}",
    )


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
    if (
        action.action_type == "DISCOVERY_QA_SETUP"
        and choice_key != "STOP"
        and not input_payload.get("batch_set_id")
    ):
        raise ReviewProgramError("Use the Discovery Q&A setup command for this choice")
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


def cancel_review_program_discovery_children(
    db: Session,
    *,
    run: ReviewProgramRun,
    actor_user_id: uuid.UUID,
) -> tuple[list[str], list[str], list[str], list[str]]:
    batch_set_workflow_ids: list[str] = []
    batch_run_workflow_ids: list[str] = []
    child_workflow_ids: list[str] = []
    review_batch_workflow_ids: list[str] = []
    children = list(db.scalars(select(ReviewProgramChild).where(ReviewProgramChild.run_id == run.id)))
    for child in children:
        if child.child_type == "ANALYSIS_TASK_BATCH_SET_RUN":
            batch_run = db.get(AnalysisTaskBatchSetRun, child.child_id)
            if batch_run is None or batch_run.status not in {"QUEUED", "RUNNING"}:
                continue
            execution, canceled, child_ids = cancel_batch_set_run(
                db,
                batch_run,
                actor_user_id=actor_user_id,
            )
            if canceled:
                child.status = "CANCELED"
                batch_run_workflow_ids.append(execution.dbos_workflow_id)
                child_workflow_ids.extend(child_ids)
        elif child.child_type == "ANALYSIS_TASK_BATCH_SET":
            batch_set = db.get(AnalysisTaskBatchSet, child.child_id)
            if batch_set is None or batch_set.status not in {"QUEUED", "BUILDING"}:
                continue
            execution, canceled = cancel_batch_set(
                db,
                batch_set,
                actor_user_id=actor_user_id,
            )
            if canceled:
                child.status = "CANCELED"
                batch_set_workflow_ids.append(execution.dbos_workflow_id)
        elif child.child_type == "REVIEW_BATCH":
            batch = db.get(ReviewBatch, child.child_id)
            if batch is None or batch.status not in {"QUEUED", "BUILDING"}:
                continue
            execution, canceled = cancel_job_execution(
                db,
                job_type=REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
                job_id=batch.id,
                actor_user_id=actor_user_id,
            )
            if canceled:
                child.status = "CANCELED"
                review_batch_workflow_ids.append(execution.dbos_workflow_id)
    return (
        batch_set_workflow_ids,
        batch_run_workflow_ids,
        child_workflow_ids,
        review_batch_workflow_ids,
    )


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


def review_program_action_decision_context(
    db: Session,
    *,
    run: ReviewProgramRun,
    action: ReviewProgramAction,
) -> dict[str, Any]:
    """Return live operational detail without changing the action's frozen decision preview."""

    context = dict(action.decision_context)
    if action.status == "OPEN" and action.stage_key in {
        "DISAGREEMENT_REVIEW",
        "TRAINING_LABEL_FINALIZATION",
    }:
        context.update(_training_label_stage_context(db, run=run))
    return context
