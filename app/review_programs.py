from __future__ import annotations

import hashlib
import io
import json
import uuid
from collections import Counter
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analysis_input_manifests import (
    AnalysisInputContract,
    AnalysisInputDocument,
    AnalysisInputExtensionField,
    AnalysisInputManifestError,
    AnalysisInputManifestReader,
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
from app.artifact_gateway import (
    copy_workflow_artifact_content,
    get_collection_item_analysis_input_snapshots,
    store_workflow_artifact,
)
from app.audit import record_audit
from app.authorization import ResourceRef, user_can
from app.classification_datasets import (
    ClassificationDatasetError,
    begin_dataset_snapshot,
    materialize_dataset_snapshot,
    prepare_dataset_snapshot,
)
from app.classification_features import (
    ClassificationFeatureSplitError,
    embedding_job_output_is_usable,
    materialize_scoring_features,
    prepare_scoring_features,
)
from app.classification_lifecycle import ClassificationLifecycleError, decide_candidate
from app.classification_models import (
    ClassificationModelCommandError,
    active_value_keys,
    create_classification_model,
    create_classification_model_configuration,
)
from app.classification_scoring import (
    ClassificationScoringError,
    create_scoring_run,
    prediction_preview,
    prepare_scoring,
    retry_scoring_run,
)
from app.classification_training_jobs import (
    ClassificationTrainingJobError,
    create_training_job,
)
from app.config import get_settings
from app.document_metadata import event_value
from app.embeddings.configuration import canonical_hash
from app.job_executions import (
    CLASSIFICATION_SCORING_RUN_JOB_TYPE,
    CLASSIFICATION_TRAINING_JOB_TYPE,
    REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
    cancel_job_execution,
    current_job_execution,
)
from app.matter_definition_assessments import AssessmentError, start_assessment
from app.matter_definitions import (
    MatterDefinitionError,
    clone_guidance,
    create_guidance,
    get_guidance,
)
from app.models import (
    AnalysisInputManifest,
    AnalysisTaskBatchSet,
    AnalysisTaskBatchSetRun,
    AnalysisTaskBatchSetRunChild,
    AnalysisTaskExecutionProfile,
    ClassificationDatasetSnapshot,
    ClassificationModel,
    ClassificationModelConfiguration,
    ClassificationModelVersion,
    ClassificationScoringRun,
    ClassificationTrainingJob,
    ClassificationTrainingRun,
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
    MatterEmbeddingJob,
    MetadataDefinition,
    ReviewBatch,
    ReviewBatchCodingField,
    ReviewBatchCodingGroup,
    ReviewBatchDocument,
    ReviewBatchReviewerCoding,
    ReviewBatchReviewerDocument,
    ReviewBatchRun,
    ReviewDecisionResult,
    ReviewProgram,
    ReviewProgramAction,
    ReviewProgramChild,
    ReviewProgramCompletionReport,
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
from app.schemas import (
    ClassificationDatasetSnapshotRequest,
    ClassificationModelConfigurationCreate,
    ClassificationModelCreate,
    ClassificationTrainingJobCreate,
    ReviewBatchComparisonSourceOptionRead,
)
from app.workflows.dispatcher import (
    enqueue_analysis_task_batch_set,
    enqueue_analysis_task_batch_set_run,
    enqueue_classification_model_scoring,
    enqueue_classification_training_job,
    enqueue_review_batch,
)
from artifact_service.schemas import WorkflowArtifactLineageInput
from artifact_service.workflow_artifact_types import ArtifactType
from classification_service.schemas import (
    ClassificationEvaluationReport,
    ClassificationStrategy,
    FeatureNormalization,
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
    return tuple(UserAudience(user_id) for user_id in dict.fromkeys((program.owner_user_id, run.initiated_by_user_id)))


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
    negative_qc_state: str | None = None,
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
            "Resolve findings and publish the revised Matter Definition",
            (
                "Resolve every assessment question. When the refinement draft is ready, review and publish it "
                "before continuing. The coordinator will assess the newly published revision in another round."
            ),
            [
                {
                    "key": "CONTINUE",
                    "label": "Continue after publication",
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
                    "consequence": ("Use only documents where both models assigned the same complete coding set."),
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
    if stage.key == "CLASSIFIER_REVIEW":
        return (
            "Review the trained classifiers",
            (
                "Review cross-fit development metrics, class support, excluded categories, and quality warnings "
                "for every coding field. Accepting approves these candidate versions for the next scoring gate; "
                "requesting improvement keeps whole-collection scoring blocked."
            ),
            [
                {
                    "key": "ACCEPT_CLASSIFIERS",
                    "label": "Accept classifiers",
                    "consequence": (
                        "Approve the candidate model versions and proceed to whole-collection scoring setup."
                    ),
                },
                {
                    "key": "IMPROVE_CLASSIFIERS",
                    "label": "Improve weak categories",
                    "consequence": ("Proceed to targeted classifier improvement using task-specific review batches."),
                },
                stop,
            ],
        )
    if stage.key == "CLASSIFIER_IMPROVEMENT":
        return (
            "Target weak classifier categories",
            (
                "Review and edit the suggested searches for weak categories. The coordinator will freeze a new "
                "task-specific population, run both pinned Q&A profiles, and return the accumulated evidence "
                "through support and label review before retraining."
            ),
            [
                {
                    "key": "START",
                    "label": "Start targeted review",
                    "consequence": (
                        "Freeze the edited retrieval plan and run both Q&A profiles on documents not used by "
                        "earlier program populations."
                    ),
                },
                stop,
            ],
        )
    if stage.key == "WHOLE_COLLECTION_SCORING":
        return (
            "Run the accepted classifiers across the collection",
            (
                "Launch one immutable, matter-wide scoring run for each accepted classifier version. "
                "Predictions remain pinned to their model, dataset snapshot, and resolution policy for the "
                "next Q&A coverage and alignment stages."
            ),
            [
                {
                    "key": "START_SCORING",
                    "label": "Score the collection",
                    "consequence": ("Run every accepted classifier over its frozen whole-matter feature population."),
                },
                stop,
            ],
        )
    if stage.key == "CLASSIFIER_POSITIVE_COVERAGE":
        return (
            "Verify classifier-positive documents with both Q&A reviewers",
            (
                "Build a frozen manifest of classifier-positive documents that do not already have successful, "
                "current-content results from both pinned Q&A profiles, then run the existing task-specific "
                "batch workflow over that population. Classifier predictions are not included in the Q&A prompt."
            ),
            [
                {
                    "key": "RUN_COVERAGE",
                    "label": "Run dual-Q&A coverage",
                    "consequence": (
                        "Freeze the uncovered classifier-positive population and run both pinned Q&A profiles."
                    ),
                },
                stop,
            ],
        )
    if stage.key == "NO_CODE_QC":
        if negative_qc_state == "AWAITING_MANUAL_REVIEW":
            return (
                "Complete the QC findings review",
                (
                    "Open the assigned ad hoc batch and code every QC-positive, disputed, or unresolved document. "
                    "The original blind QC report remains unchanged."
                ),
                [
                    {
                        "key": "CONFIRM_QC_REVIEW",
                        "label": "Confirm QC review complete",
                        "consequence": "Validate the human coding and return to the QC decision.",
                    },
                    stop,
                ],
            )
        if negative_qc_state == "REPORT_READY":
            return (
                "Review Negative-population QC findings",
                (
                    "Review the blind dual-Q&A audit results, random-audit estimate, targeted-lane yield, and "
                    "unresolved mandatory documents before accepting first-pass QC."
                ),
                [
                    {
                        "key": "ACCEPT_QC",
                        "label": "Accept QC results",
                        "consequence": "Freeze the QC decision and proceed to the completion summary.",
                    },
                    {
                        "key": "REVIEW_QC_FINDINGS",
                        "label": "Review QC findings",
                        "consequence": (
                            "Create an assigned ad hoc batch for QC-positive, disputed, and unresolved documents."
                        ),
                    },
                    {
                        "key": "RETRIEVE_AND_RETRAIN",
                        "label": "Retrieve more and retrain",
                        "consequence": (
                            "Preserve this QC round and reopen targeted classifier improvement before rescoring."
                        ),
                    },
                    {
                        "key": "REVISE_GUIDANCE",
                        "label": "Revise guidance",
                        "consequence": (
                            "Preserve this QC round and reopen Matter Definition refinement and Q&A setup."
                        ),
                    },
                    stop,
                ],
            )
        return (
            "Configure Negative-population QC",
            (
                "Review and adjust the random-audit, near-threshold, cohort-control, and mandatory-review sample "
                "sizes. The selected documents will be frozen into one task-specific set and reviewed blindly by "
                "both pinned Q&A profiles."
            ),
            [
                {
                    "key": "START_QC",
                    "label": "Start Negative-population QC",
                    "consequence": "Freeze the QC population and run both Q&A profiles.",
                },
                stop,
            ],
        )
    if stage.key == "COMPLETION":
        return (
            "Complete first-pass review",
            (
                "Review the final checklist and accepted limitations. Completing freezes a provenance report; "
                "it does not publish Q&A or classifier values into matter metadata."
            ),
            [
                {
                    "key": "COMPLETE",
                    "label": "Complete first-pass review",
                    "consequence": "Create the immutable completion report and close this run.",
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
        "final_unassessed_refinement": (run.progress_summary or {}).get("final_unassessed_refinement"),
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
    definition = db.get(MatterDefinition, program.matter_definition_id) if program.matter_definition_id else None
    refined_revision = (
        db.get(MatterDefinitionRevision, assessment.refined_matter_definition_revision_id)
        if assessment.refined_matter_definition_revision_id
        else None
    )
    assessed_revision = db.get(MatterDefinitionRevision, assessment.matter_definition_revision_id)
    publication_required = refined_revision is not None
    publication_ready = bool(
        not publication_required
        or (
            definition is not None
            and definition.published_revision is not None
            and assessed_revision is not None
            and definition.published_revision != assessed_revision.revision
        )
    )
    configuration = current_review_program_version(db, program).configuration_snapshot
    policy = configuration.get("matter_definition_policy") or {}
    configured_round_limit = int(policy.get("maximum_assessment_rounds", 3))
    round_limit = max(
        configured_round_limit,
        int((run.progress_summary or {}).get("definition_assessment_round_limit_override") or 0),
    )
    assessment_round = int((run.progress_summary or {}).get("definition_assessment_round") or 1)
    context["matter_definition"] = {
        "current_revision": definition.current_revision if definition is not None else None,
        "published_revision": definition.published_revision if definition is not None else None,
    }
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
        "refined_matter_definition_revision": refined_revision.revision if refined_revision is not None else None,
        "publication_required": publication_required,
        "publication_ready": publication_ready,
        "assessment_round": assessment_round,
        "assessment_round_limit": round_limit,
        "assessment_round_limit_reached": assessment_round >= round_limit,
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
    task_version = db.get(MatterAnalysisTaskVersion, uuid.UUID(task_version_id)) if task_version_id else None
    workflow = db.get(WorkflowRun, uuid.UUID(workflow_id)) if workflow_id else None
    profiles = execution_profiles_for_version(db, task_version.id) if task_version is not None else []
    return {
        "qa_task_setup_state": run.progress_summary.get("qa_task_setup_state"),
        "qa_task": {
            "id": str(task.id) if task is not None else None,
            "name": task.name if task is not None else None,
            "task_version_id": str(task_version.id) if task_version is not None else None,
            "version": task_version.version if task_version is not None else None,
            "status": task_version.status if task_version is not None else None,
            "compilation_status": task_version.compilation_status if task_version is not None else None,
            "definition_content_hash": (task_version.definition_content_hash if task_version is not None else None),
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
            "policy_version": "review-program-discovery-retrieval-v2",
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
            "diagnostic_batch_ids": ((run.progress_summary or {}).get("accepted_matter_definition") or {}).get(
                "diagnostic_batch_ids", []
            ),
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
    batches = {batch.id: batch for batch in db.scalars(select(ReviewBatch).where(ReviewBatch.id.in_(batch_ids)))}
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


def _classifier_review_stage_context(*, run: ReviewProgramRun) -> dict[str, Any]:
    return {
        "classifier_results": (run.progress_summary or {}).get("classifier_results", {}),
        "training_labels": (run.progress_summary or {}).get("training_labels", {}),
    }


def _classifier_improvement_stage_context(
    db: Session,
    *,
    run: ReviewProgramRun,
) -> dict[str, Any]:
    results = dict((run.progress_summary or {}).get("classifier_results") or {})
    raw_models = [item for item in results.get("models", []) if isinstance(item, dict)]
    definition_ids: list[uuid.UUID] = []
    for item in raw_models:
        try:
            definition_ids.append(uuid.UUID(str(item.get("metadata_definition_id"))))
        except (TypeError, ValueError):
            continue
    definitions = {
        item.id: item
        for item in db.scalars(select(MetadataDefinition).where(MetadataDefinition.id.in_(definition_ids)))
    }
    targets: list[dict[str, Any]] = []
    for model in raw_models:
        if model.get("quality") != "WEAK":
            continue
        try:
            definition_id = uuid.UUID(str(model.get("metadata_definition_id")))
        except (TypeError, ValueError):
            continue
        definition = definitions.get(definition_id)
        if definition is None:
            continue
        value_labels = {
            str(value.get("key")): str(value.get("label") or value.get("key"))
            for value in definition.allowed_values or []
            if value.get("key") is not None
        }
        weak_categories = [item for item in model.get("weak_categories", []) if isinstance(item, dict)]
        class_keys = [str(item.get("class_key")) for item in weak_categories if item.get("class_key") is not None]
        if not class_keys:
            class_keys = [str(value) for value in model.get("trained_class_keys", [])]
        if not class_keys and definition.type == "BOOLEAN":
            class_keys = ["true", "false"]
        for class_key in dict.fromkeys(class_keys):
            category = next(
                (item for item in weak_categories if str(item.get("class_key")) == class_key),
                {},
            )
            class_label = value_labels.get(class_key, class_key)
            targets.append(
                {
                    "metadata_definition_id": str(definition.id),
                    "field_key": definition.key,
                    "field_name": definition.display_name,
                    "class_key": class_key,
                    "class_label": class_label,
                    "quality_reasons": category.get("reasons", model.get("quality_reasons", [])),
                    "current_positive_support": category.get("positive_support"),
                    "suggested_query": f"{definition.display_name} {class_label}",
                    "suggested_quota": 50,
                }
            )
    return {
        "classifier_results": results,
        "retrieval_plan": {
            "policy_version": "review-program-classifier-improvement-retrieval-v1",
            "targets": targets,
        },
        "defaults": {
            "control_sample_size": 25,
            "target_batch_size": 100,
            "keep_families_together": True,
        },
    }


def _whole_collection_scoring_stage_context(*, run: ReviewProgramRun) -> dict[str, Any]:
    classifier_results = dict((run.progress_summary or {}).get("classifier_results") or {})
    accepted = dict((run.progress_summary or {}).get("accepted_classifiers") or {})
    accepted_version_by_model = {
        str(item.get("classification_model_id")): str(item.get("model_version_id"))
        for item in accepted.get("approvals", [])
        if isinstance(item, dict)
    }
    models = [
        {
            "classification_model_id": item.get("classification_model_id"),
            "model_version_id": accepted_version_by_model.get(str(item.get("classification_model_id"))),
            "field_key": item.get("field_key"),
            "field_name": item.get("field_name"),
            "training_eligible_count": item.get("training_eligible_count"),
            "quality": item.get("quality"),
        }
        for item in classifier_results.get("models", [])
        if isinstance(item, dict) and str(item.get("classification_model_id")) in accepted_version_by_model
    ]
    return {
        "accepted_classifiers": accepted,
        "scoring_plan": {
            "scope_type": "MATTER",
            "model_count": len(models),
            "models": models,
        },
    }


def _classifier_positive_coverage_stage_context(*, run: ReviewProgramRun) -> dict[str, Any]:
    scoring = dict((run.progress_summary or {}).get("whole_collection_scoring") or {})
    setup = dict((run.progress_summary or {}).get("qa_task_setup") or {})
    profile_ids = [str(value) for value in setup.get("execution_profile_ids", [])]
    aggregate = dict(scoring.get("aggregate_model_document_counts") or {})
    return {
        "whole_collection_scoring": scoring,
        "coverage_plan": {
            "scope_type": "CLASSIFIER_POSITIVE_UNCOVERED",
            "required_profile_count": len(profile_ids),
            "required_profile_ids": profile_ids,
            "maximum_candidate_model_document_count": int(aggregate.get("scored_document_count") or 0),
            "batching_policy": "PARTITIONED_SET",
            "result_policy": "ISOLATED",
        },
    }


def _negative_population_qc_stage_context(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
) -> dict[str, Any]:
    policy = dict(
        current_review_program_version(db, program).configuration_snapshot.get("negative_population_qc_policy")
        or {}
    )
    return {
        "final_alignment": dict((run.progress_summary or {}).get("final_alignment") or {}),
        "negative_population_qc": dict((run.progress_summary or {}).get("negative_population_qc") or {}),
        "defaults": policy,
    }


def _completion_stage_context(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
) -> dict[str, Any]:
    children = list(db.scalars(select(ReviewProgramChild).where(ReviewProgramChild.run_id == run.id)))
    actions = list(db.scalars(select(ReviewProgramAction).where(ReviewProgramAction.run_id == run.id)))
    progress = run.progress_summary or {}
    qc = dict(progress.get("negative_population_qc") or {})
    qc_report = dict(qc.get("report") or {})
    final_alignment = dict(progress.get("final_alignment") or {})
    return {
        "completion_preview": {
            "schema_version": 1,
            "checklist": {
                "matter_definition_accepted": bool(progress.get("accepted_matter_definition")),
                "qa_task_published": bool(progress.get("accepted_qa_task")),
                "training_labels_finalized": bool(progress.get("training_labels")),
                "classifiers_accepted": bool(progress.get("classifier_results")),
                "whole_collection_scored": bool(progress.get("whole_collection_scoring")),
                "classifier_positives_covered": bool(progress.get("classifier_positive_coverage")),
                "final_alignment_ready": bool(final_alignment),
                "negative_population_qc_accepted": progress.get("negative_population_qc_state") == "ACCEPTED",
            },
            "child_record_count": len(children),
            "resolved_decision_count": sum(action.status == "RESOLVED" for action in actions),
            "unresolved_qc_document_count": int(qc_report.get("unresolved_document_count") or 0),
            "manual_resolution_document_count": int(qc_report.get("mandatory_unreviewable_count") or 0),
            "accepted_low_support": list(
                (
                    current_review_program_version(db, program).configuration_snapshot.get(
                        "accepted_low_support", {}
                    )
                    or {}
                ).get("categories", [])
            ),
            "publication_effect": "NONE",
        }
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
    support_state = str(((run.progress_summary or {}).get("discovery_analysis") or {}).get("support_status") or "")
    training_label_state = str(((run.progress_summary or {}).get("training_label_resolution") or {}).get("state") or "")
    negative_qc_state = str(((run.progress_summary or {}).get("negative_population_qc") or {}).get("state") or "")
    title, explanation, choices = _action_contract(
        template,
        stage,
        qa_setup_state=qa_setup_state,
        support_state=support_state,
        training_label_state=training_label_state,
        negative_qc_state=negative_qc_state,
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
            else _classifier_review_stage_context(run=run)
            if stage.key == "CLASSIFIER_REVIEW"
            else _classifier_improvement_stage_context(db, run=run)
            if stage.key == "CLASSIFIER_IMPROVEMENT"
            else _whole_collection_scoring_stage_context(run=run)
            if stage.key == "WHOLE_COLLECTION_SCORING"
            else _classifier_positive_coverage_stage_context(run=run)
            if stage.key == "CLASSIFIER_POSITIVE_COVERAGE"
            else _negative_population_qc_stage_context(db, program=program, run=run)
            if stage.key == "NO_CODE_QC"
            else _completion_stage_context(db, program=program, run=run)
            if stage.key == "COMPLETION"
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
            "QA_TASK_SETUP": ("QA_TASK_PUBLICATION" if qa_setup_state == "READY_FOR_PUBLICATION" else "QA_TASK_SETUP"),
            "DISCOVERY_QA": "DISCOVERY_QA_SETUP",
            "DISAGREEMENT_REVIEW": (
                "DISAGREEMENT_REVIEW_COMPLETION"
                if training_label_state == "AWAITING_ADJUDICATION"
                else "TRAINING_LABEL_POLICY"
            ),
            "TRAINING_LABEL_FINALIZATION": "TRAINING_LABEL_FINALIZATION",
            "CLASSIFIER_REVIEW": "CLASSIFIER_REVIEW",
            "CLASSIFIER_IMPROVEMENT": "CLASSIFIER_IMPROVEMENT_SETUP",
            "WHOLE_COLLECTION_SCORING": "WHOLE_COLLECTION_SCORING",
            "CLASSIFIER_POSITIVE_COVERAGE": "CLASSIFIER_POSITIVE_COVERAGE",
            "NO_CODE_QC": (
                "NEGATIVE_POPULATION_QC_FINDINGS_REVIEW"
                if negative_qc_state == "AWAITING_MANUAL_REVIEW"
                else "NEGATIVE_POPULATION_QC_REPORT"
                if negative_qc_state == "REPORT_READY"
                else "NEGATIVE_POPULATION_QC_SETUP"
            ),
            "COMPLETION": "REVIEW_PROGRAM_COMPLETION",
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
        elif stage.key == "CLASSIFIER_TRAINING":
            _launch_classifier_training(db, program=program, run=run, attempt=attempt)
        elif stage.key == "FINAL_ALIGNMENT":
            _launch_final_alignment_report(db, program=program, run=run, attempt=attempt)
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
    next_version = (
        db.scalar(select(func.max(ReviewProgramVersion.version)).where(ReviewProgramVersion.program_id == program.id))
        or 0
    ) + 1
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
        completed_rounds = int(
            db.scalar(
                select(func.count())
                .select_from(ReviewProgramChild)
                .join(
                    MatterDefinitionAssessmentRun,
                    MatterDefinitionAssessmentRun.id == ReviewProgramChild.child_id,
                )
                .where(
                    ReviewProgramChild.run_id == run.id,
                    ReviewProgramChild.child_type == "MATTER_DEFINITION_ASSESSMENT",
                    MatterDefinitionAssessmentRun.status.in_({"COMPLETED", "COMPLETED_WITH_ERRORS"}),
                )
            )
            or 0
        )
        round_number = completed_rounds + 1
        maximum_rounds = max(
            int(policy.get("maximum_assessment_rounds", 3)),
            int((run.progress_summary or {}).get("definition_assessment_round_limit_override") or 0),
        )
        if round_number > maximum_rounds:
            raise ReviewProgramError(f"Matter Definition assessment limit of {maximum_rounds} rounds has been reached")
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
        "classifier_positive_coverage_policy": {
            "target_batch_size": 100,
            "keep_families_together": True,
            "required_result_status": "COMPLETED",
            "require_current_source_content": True,
        },
        "negative_population_qc_policy": {
            "random_sample_size": 200,
            "near_threshold_sample_size": 100,
            "cohort_control_sample_size": 50,
            "mandatory_review_limit": 100,
            "target_batch_size": 100,
            "keep_families_together": True,
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


def _completion_lineage_artifact_ids(db: Session, *, run: ReviewProgramRun) -> list[uuid.UUID]:
    progress = run.progress_summary or {}
    artifact_ids: list[uuid.UUID] = []
    for section_key in ("classifier_positive_coverage", "final_alignment", "negative_population_qc"):
        manifest_id = dict(progress.get(section_key) or {}).get("input_manifest_id")
        if not manifest_id:
            continue
        try:
            manifest = db.get(AnalysisInputManifest, uuid.UUID(str(manifest_id)))
        except ValueError:
            manifest = None
        if manifest is not None and manifest.artifact_id is not None:
            artifact_ids.append(manifest.artifact_id)
    for model in dict(progress.get("whole_collection_scoring") or {}).get("models", []):
        if not isinstance(model, dict):
            continue
        for key in ("prediction_artifact_id", "result_manifest_artifact_id"):
            value = model.get(key)
            if value:
                try:
                    artifact_ids.append(uuid.UUID(str(value)))
                except ValueError:
                    continue
    return list(dict.fromkeys(artifact_ids))


def _build_review_program_completion_report(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    action: ReviewProgramAction,
) -> dict[str, Any]:
    progress = run.progress_summary or {}
    children = list(
        db.scalars(
            select(ReviewProgramChild)
            .where(ReviewProgramChild.run_id == run.id)
            .order_by(ReviewProgramChild.created_at, ReviewProgramChild.id)
        )
    )
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
            .where(ReviewProgramAction.run_id == run.id, ReviewProgramAction.status == "RESOLVED")
            .order_by(ReviewProgramAction.created_at, ReviewProgramAction.id)
        )
    )
    current_version = current_review_program_version(db, program)
    qc = dict(progress.get("negative_population_qc") or {})
    qc_report = dict(qc.get("report") or {})
    final_alignment = dict(progress.get("final_alignment") or {})
    report = {
        "schema_version": 1,
        "report_type": "FIRST_PASS_REVIEW_COMPLETION",
        "program": {
            "id": str(program.id),
            "key": program.key,
            "name": program.name,
            "template_key": program.template_key,
            "template_version": program.template_version,
            "template_content_hash": program.template_content_hash,
        },
        "run": {
            "id": str(run.id),
            "starting_program_version_id": str(run.starting_program_version_id),
            "completed_program_version_id": str(current_version.id),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "discovery_rounds": run.current_discovery_round,
            "training_rounds": run.current_training_round,
        },
        "definition": progress.get("accepted_matter_definition") or {},
        "qa_task": progress.get("accepted_qa_task") or {},
        "discovery": {
            "analysis": progress.get("discovery_analysis") or {},
            "accepted_low_support": current_version.configuration_snapshot.get("accepted_low_support") or {},
        },
        "training_labels": progress.get("training_labels") or {},
        "classifier": {
            "results": progress.get("classifier_results") or {},
            "whole_collection_scoring": progress.get("whole_collection_scoring") or {},
        },
        "coverage": progress.get("classifier_positive_coverage") or {},
        "final_alignment": final_alignment,
        "negative_population_qc": qc,
        "negative_population_qc_acceptance": progress.get("negative_population_qc_acceptance") or {},
        "negative_population_qc_history": progress.get("negative_population_qc_history") or [],
        "unresolved": {
            "final_alignment_incomplete_count": int(
                dict(final_alignment.get("alignment_counts") or {}).get("INCOMPLETE") or 0
            ),
            "qc_unresolved_document_count": int(qc_report.get("unresolved_document_count") or 0),
            "manual_resolution_document_count": int(qc_report.get("mandatory_unreviewable_count") or 0),
        },
        "stage_attempts": [
            {
                "id": str(attempt.id),
                "stage_key": attempt.stage_key,
                "ordinal": attempt.stage_ordinal,
                "attempt": attempt.attempt,
                "status": attempt.status,
                "result_summary": attempt.result_summary,
                "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
                "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
            }
            for attempt in attempts
        ],
        "decisions": [
            {
                "id": str(item.id),
                "stage_key": item.stage_key,
                "action_type": item.action_type,
                "preview_hash": item.preview_hash,
                "choice_key": dict(item.resolution_payload or {}).get("choice_key"),
                "reason": item.resolution_reason,
                "resolved_by_user_id": str(item.resolved_by_user_id) if item.resolved_by_user_id else None,
                "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
            }
            for item in actions
        ],
        "children": [
            {
                "id": str(child.id),
                "stage_attempt_id": str(child.stage_attempt_id),
                "purpose": child.purpose,
                "child_type": child.child_type,
                "child_id": str(child.child_id),
                "status": child.status,
                "derivation_key": child.derivation_key,
                "batching_policy": child.context.get("batching_policy"),
            }
            for child in children
        ],
        "completion": {
            "completed_by_user_id": str(action.resolved_by_user_id or run.initiated_by_user_id),
            "completed_at": action.resolved_at.isoformat() if action.resolved_at else utcnow().isoformat(),
            "reason": action.resolution_reason,
            "publication_effect": "NONE",
            "accepted_limitations": {
                "low_support": current_version.configuration_snapshot.get("accepted_low_support") or {},
                "qc_limitations": qc_report.get("limitations") or [],
                "unresolved": {
                    "qc": int(qc_report.get("unresolved_document_count") or 0),
                    "manual_resolution": int(qc_report.get("mandatory_unreviewable_count") or 0),
                },
            },
        },
    }
    report["checklist"] = {
        "matter_definition_accepted": bool(report["definition"]),
        "qa_task_published": bool(report["qa_task"]),
        "training_labels_finalized": bool(report["training_labels"]),
        "classifiers_accepted": bool(report["classifier"]["results"]),
        "whole_collection_scored": bool(report["classifier"]["whole_collection_scoring"]),
        "classifier_positives_covered": bool(report["coverage"]),
        "final_alignment_ready": bool(report["final_alignment"]),
        "negative_population_qc_accepted": progress.get("negative_population_qc_state") == "ACCEPTED",
    }
    return report


def _finalize_review_program_completion(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    action: ReviewProgramAction,
) -> None:
    if db.scalar(select(ReviewProgramCompletionReport).where(ReviewProgramCompletionReport.run_id == run.id)):
        raise ReviewProgramError("Review Program completion report already exists")
    _complete_action_attempt(attempt, action)
    report = _build_review_program_completion_report(db, program=program, run=run, action=action)
    incomplete = [key for key, value in report["checklist"].items() if not value]
    if incomplete:
        raise ReviewProgramError(f"First-pass completion checklist is incomplete: {', '.join(incomplete)}")
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    content_hash = hashlib.sha256(payload).hexdigest()
    derivation_key = canonical_hash(
        {
            "contract": "review-program-completion-report-v1",
            "run_id": str(run.id),
            "program_version_id": str(run.current_program_version_id),
            "content_hash": content_hash,
        }
    )
    matter = db.get(Matter, program.matter_id)
    if matter is None:
        raise ReviewProgramError("Review Program matter is unavailable")
    try:
        reference, _ = store_workflow_artifact(
            content=io.BytesIO(payload),
            artifact_type=ArtifactType.REVIEW_PROGRAM_COMPLETION_REPORT,
            core_scope_kind="MATTER",
            core_scope_id=matter.id,
            schema_version=1,
            derivation_key=derivation_key,
            content_hash=content_hash,
            byte_length=len(payload),
            media_type="application/json",
            producer_kind="CORE",
            producer_run_id=run.id,
            artifact_metadata={
                "contract_version": "review-program-completion-report-v1",
                "program_id": str(program.id),
                "run_id": str(run.id),
                "program_version_id": str(run.current_program_version_id),
                "content_hash": content_hash,
                "byte_length": len(payload),
                "filename": f"{program.key}-first-pass-completion.json",
            },
            lineage=[
                WorkflowArtifactLineageInput(
                    source_kind="WORKFLOW_ARTIFACT",
                    source_artifact_id=artifact_id,
                )
                for artifact_id in _completion_lineage_artifact_ids(db, run=run)
            ],
            actor_user_id=action.resolved_by_user_id or run.initiated_by_user_id,
            tenant_id=program.tenant_id,
            client_id=matter.client_id,
            filename=f"{program.key}-first-pass-completion.json",
        )
    except (httpx.HTTPError, PermissionError, ValueError) as exc:
        raise ReviewProgramError(f"Completion report artifact could not be stored: {exc}") from exc
    completed_at = action.resolved_at or utcnow()
    record = ReviewProgramCompletionReport(
        tenant_id=program.tenant_id,
        matter_id=program.matter_id,
        program_id=program.id,
        program_version_id=run.current_program_version_id,
        run_id=run.id,
        schema_version=1,
        report=report,
        content_hash=content_hash,
        artifact_id=reference.artifact_id,
        artifact_content_hash=reference.content_hash,
        completion_reason=action.resolution_reason,
        completed_by_user_id=action.resolved_by_user_id or run.initiated_by_user_id,
        completed_at=completed_at,
    )
    db.add(record)
    db.flush()
    summary = {
        "id": str(record.id),
        "state": "FINALIZED",
        "content_hash": content_hash,
        "artifact_id": str(reference.artifact_id),
        "artifact_content_hash": reference.content_hash,
        "completed_by_user_id": str(record.completed_by_user_id),
        "completed_at": completed_at.isoformat(),
        "publication_effect": "NONE",
        "unresolved": report["unresolved"],
    }
    run.progress_summary = {**run.progress_summary, "completion_report": summary}
    _complete_action_attempt(
        attempt,
        action,
        result={"completion_report": summary},
    )
    _advance_after_stage(db, program=program, run=run, attempt=attempt)


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
        raise ReviewProgramError(f"Resolve {len(blocking)} blocking assessment question(s) before continuing")
    answered = [question for question in questions if question.status == "ANSWERED"]
    if answered and assessment.guidance_refinement_status in {"NOT_READY", "QUEUED", "RUNNING"}:
        raise ReviewProgramError(
            "Resolve the remaining assessment questions and wait for the definition refinement draft"
        )
    if assessment.guidance_refinement_status == "FAILED":
        raise ReviewProgramError(assessment.guidance_refinement_error_message or "Matter Definition refinement failed")
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
    configuration = current_review_program_version(db, program).configuration_snapshot
    policy = configuration.get("matter_definition_policy") or {}
    round_limit = max(
        int(policy.get("maximum_assessment_rounds", 3)),
        int((run.progress_summary or {}).get("definition_assessment_round_limit_override") or 0),
    )
    assessment_round = int((run.progress_summary or {}).get("definition_assessment_round") or 1)
    limit_reached = assessment_round >= round_limit
    decision = str(((action.resolution_payload or {}).get("input") or {}).get("final_round_decision") or "")
    published_changed = published.id != assessment.matter_definition_revision_id
    if published_changed and limit_reached and decision not in {"ACCEPT_REFINEMENT", "EXTEND_AND_REASSESS"}:
        raise ReviewProgramError(
            "Choose whether to accept the final refinement or run an additional validation assessment"
        )
    _complete_action_attempt(
        attempt,
        action,
        result={
            "assessment_id": str(assessment.id),
            "assessed_revision_id": str(assessment.matter_definition_revision_id),
            "published_revision_id": str(published.id),
            "final_round_decision": decision or None,
            "open_nonblocking_question_ids": [
                str(question.id) for question in questions if not question.blocking and question.status == "OPEN"
            ],
        },
    )
    if published_changed and limit_reached and decision == "ACCEPT_REFINEMENT":
        run.progress_summary = {
            **run.progress_summary,
            "final_unassessed_refinement": {
                "assessment_id": str(assessment.id),
                "assessed_revision_id": str(assessment.matter_definition_revision_id),
                "accepted_revision_id": str(published.id),
                "accepted_revision": published.revision,
                "assessment_round": assessment_round,
                "assessment_round_limit": round_limit,
            },
        }
        _advance_after_stage(db, program=program, run=run, attempt=attempt)
        _carry_forward_final_refinement_acceptance(
            db,
            program=program,
            run=run,
            source_action=action,
        )
    elif published_changed and limit_reached and decision == "EXTEND_AND_REASSESS":
        run.progress_summary = {
            **run.progress_summary,
            "definition_assessment_round_limit_override": assessment_round + 1,
            "final_unassessed_refinement": None,
        }
        _activate_stage_by_key(
            db,
            program=program,
            run=run,
            stage_key="DEFINITION_ASSESSMENT",
        )
    elif published_changed:
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
    final_refinement = (run.progress_summary or {}).get("final_unassessed_refinement") or {}
    accepts_final_refinement = bool(
        revision is not None
        and final_refinement.get("assessment_id") == str(assessment.id)
        and final_refinement.get("accepted_revision_id") == str(revision.id)
        and assessment.refined_matter_definition_revision_id == revision.id
    )
    if revision is None or (
        revision.id != assessment.matter_definition_revision_id and not accepts_final_refinement
    ):
        raise ReviewProgramError("The current published Matter Definition revision has not been assessed")
    questions = _assessment_questions(db, assessment)
    if any(question.blocking and question.status == "OPEN" for question in questions):
        raise ReviewProgramError("Blocking assessment questions remain unresolved")
    content_hash = hashlib.sha256(revision.content_markdown.encode()).hexdigest()
    if not accepts_final_refinement and content_hash != assessment.definition_content_hash:
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
        "assessment_status": (
            "FINAL_REFINEMENT_ACCEPTED_WITHOUT_REASSESSMENT"
            if accepts_final_refinement
            else "ASSESSED"
        ),
        "assessed_revision_id": str(assessment.matter_definition_revision_id),
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


def _carry_forward_final_refinement_acceptance(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    source_action: ReviewProgramAction,
) -> None:
    """Apply the user's final-round acceptance to the acceptance audit stage.

    The final-round choice is already an explicit acceptance decision. The
    separate DEFINITION_ACCEPTANCE stage is retained in history, but its action
    is resolved from that choice instead of presenting the user with the same
    decision twice.
    """
    if run.current_stage != "DEFINITION_ACCEPTANCE" or run.pending_action_id is None:
        raise ReviewProgramError("Matter Definition acceptance stage is unavailable")
    attempt = _current_stage_attempt(db, run, for_update=True)
    action = db.scalar(
        select(ReviewProgramAction)
        .where(
            ReviewProgramAction.id == run.pending_action_id,
            ReviewProgramAction.run_id == run.id,
        )
        .with_for_update()
    )
    if action is None or action.status != "OPEN" or action.stage_attempt_id != attempt.id:
        raise ReviewProgramError("Matter Definition acceptance action is unavailable")
    accepted_at = source_action.resolved_at or utcnow()
    action.status = "RESOLVED"
    action.resolution_payload = {
        "choice_key": "ACCEPT",
        "input": {
            "carried_forward_from_action_id": str(source_action.id),
            "final_round_decision": "ACCEPT_REFINEMENT",
        },
    }
    action.resolution_reason = "Acceptance carried forward from the final assessment-round decision."
    action.resolved_by_user_id = source_action.resolved_by_user_id or run.initiated_by_user_id
    action.resolved_at = accepted_at
    resolve_source_notifications(
        db,
        source=NotificationSource("review_program_action", action.id),
        resolution_code="ACCEPT",
        resolved_at=accepted_at,
    )
    run.pending_action_id = None
    _accept_matter_definition(
        db,
        program=program,
        run=run,
        attempt=attempt,
        action=action,
    )


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
        diagnostic_batch_ids = [uuid.UUID(str(value)) for value in accepted_definition.get("diagnostic_batch_ids", [])]
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

    selected_ids: list[uuid.UUID] = []
    selected_set: set[uuid.UUID] = set()
    selection_reasons: dict[uuid.UUID, list[str]] = {}
    query_yields: list[dict[str, Any]] = []
    for query in queries:
        remaining = target_document_count - len(selected_ids)
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
        version=2,
        resolver_version="review-program-discovery-retrieval-v2",
        extension_fields=[
            AnalysisInputExtensionField(name="selection_reasons", type="STRING", nullable=False),
        ],
        options={
            "review_program_id": str(program.id),
            "review_program_run_id": str(run.id),
            "round": round_number,
            "assessment_id": str(assessment.id),
            "search_index_generation_id": str(generation.id),
            "policy_version": "review-program-discovery-retrieval-v2",
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
        producer_kind="CORE",
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
            "retrieval_policy_version": "review-program-discovery-retrieval-v2",
            "query_yields": query_yields,
            "excluded_diagnostic_document_count": diagnostic_excluded_count,
            "excluded_prior_program_document_count": len(excluded_document_ids) - diagnostic_excluded_count,
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
            "round": round_number,
        },
    )
    discovery_snapshot = {
        "schema_version": 2,
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


def setup_review_program_classifier_improvement(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    action_id: uuid.UUID,
    actor: User,
    preview_hash: str,
    queries: list[dict[str, Any]],
    control_sample_size: int,
    target_batch_size: int,
    keep_families_together: bool,
) -> ReviewProgramRun:
    action = db.scalar(
        select(ReviewProgramAction)
        .where(ReviewProgramAction.id == action_id, ReviewProgramAction.run_id == run.id)
        .with_for_update()
    )
    if action is None or action.action_type != "CLASSIFIER_IMPROVEMENT_SETUP":
        raise ReviewProgramError("Classifier improvement setup action not found")
    if action.status == "RESOLVED":
        if (action.resolution_payload or {}).get("choice_key") != "START":
            raise ReviewProgramError("Classifier improvement setup was already resolved differently")
        return run
    if run.current_stage != "CLASSIFIER_IMPROVEMENT" or run.status != "WAITING_FOR_USER":
        raise ReviewProgramError("Review Program is not waiting for classifier improvement setup")
    if action.preview_hash != preview_hash:
        raise ReviewProgramError("Classifier improvement setup preview is stale")

    allowed_targets = {
        (str(item.get("metadata_definition_id")), str(item.get("class_key")))
        for item in (((action.decision_context or {}).get("retrieval_plan") or {}).get("targets") or [])
        if isinstance(item, dict)
    }
    submitted_targets = [(str(item.get("metadata_definition_id")), str(item.get("class_key"))) for item in queries]
    if not submitted_targets or len(submitted_targets) != len(set(submitted_targets)):
        raise ReviewProgramError("Classifier improvement targets must be present and unique")
    if any(target not in allowed_targets for target in submitted_targets):
        raise ReviewProgramError("Classifier improvement plan contains an unexpected target")

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
        raise ReviewProgramError("The matter search index must be ready before classifier improvement")
    task, task_version, profiles = _qa_task_records(db, run)
    if task_version.status != "PUBLISHED" or len(profiles) != 2:
        raise ReviewProgramError("Classifier improvement requires one published task and two pinned profiles")

    prior_batch_set_ids = list(
        db.scalars(
            select(ReviewProgramChild.child_id).where(
                ReviewProgramChild.run_id == run.id,
                ReviewProgramChild.child_type == "ANALYSIS_TASK_BATCH_SET",
            )
        )
    )
    excluded_document_ids: set[uuid.UUID] = set()
    accepted_definition = (run.progress_summary or {}).get("accepted_matter_definition") or {}
    try:
        diagnostic_batch_ids = [uuid.UUID(str(value)) for value in accepted_definition.get("diagnostic_batch_ids", [])]
    except (TypeError, ValueError) as exc:
        raise ReviewProgramError("Diagnostic batch provenance is invalid") from exc
    if diagnostic_batch_ids:
        excluded_document_ids.update(
            db.scalars(
                select(ReviewBatchDocument.matter_document_id).where(
                    ReviewBatchDocument.review_batch_id.in_(diagnostic_batch_ids)
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

    selected_ids: list[uuid.UUID] = []
    selected_set: set[uuid.UUID] = set()
    selection_reasons: dict[uuid.UUID, list[str]] = {}
    query_yields: list[dict[str, Any]] = []
    frozen_queries: list[dict[str, Any]] = []
    for ordinal, query in enumerate(queries, start=1):
        definition_id = str(query["metadata_definition_id"])
        class_key = str(query["class_key"])
        query_text = " ".join(str(query["query"]).split())
        quota = int(query["quota"])
        reason = f"{definition_id}:{class_key}"
        hits = search_document_ids(
            db,
            matter=matter,
            generation=generation,
            search_definition={"query": query_text, "search_mode": "KEYWORD"},
            settings=get_settings(),
            limit=max(quota + len(excluded_document_ids) + len(selected_set), quota),
            relevance_order=True,
        )
        accepted_hits: list[uuid.UUID] = []
        for document_id in hits:
            if document_id in excluded_document_ids:
                continue
            reasons = selection_reasons.setdefault(document_id, [])
            if reason not in reasons:
                reasons.append(reason)
            if document_id not in selected_set and len(accepted_hits) < quota:
                selected_ids.append(document_id)
                selected_set.add(document_id)
                accepted_hits.append(document_id)
        frozen = {
            "ordinal": ordinal,
            "metadata_definition_id": definition_id,
            "class_key": class_key,
            "query": query_text,
            "quota": quota,
        }
        frozen_queries.append(frozen)
        query_yields.append(
            {
                **frozen,
                "search_hit_count": len(hits),
                "selected_count": len(accepted_hits),
            }
        )

    control_ids: list[uuid.UUID] = []
    if control_sample_size:
        excluded_for_control = excluded_document_ids | selected_set
        for document_id in db.scalars(
            select(MatterDocument.id)
            .where(MatterDocument.matter_id == matter.id)
            .order_by(MatterDocument.created_at, MatterDocument.id)
        ).yield_per(1_000):
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
        raise ReviewProgramError("The classifier improvement retrieval plan selected no documents")

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
        raise ReviewProgramError("Classifier improvement retrieval returned documents outside the matter")
    attempt = _current_stage_attempt(db, run, for_update=True)
    round_number = int((run.progress_summary or {}).get("classifier_improvement_round") or 0) + 1
    contract = AnalysisInputContract(
        key="review-program.classifier-improvement-population",
        version=1,
        resolver_version="review-program-classifier-improvement-retrieval-v1",
        extension_fields=[
            AnalysisInputExtensionField(name="retrieval_lane", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="selection_reasons", type="STRING", nullable=False),
        ],
        options={
            "review_program_id": str(program.id),
            "review_program_run_id": str(run.id),
            "round": round_number,
            "search_index_generation_id": str(generation.id),
            "policy_version": "review-program-classifier-improvement-retrieval-v1",
        },
    )
    prepared = prepare_analysis_input_manifest(
        [
            AnalysisInputDocument(
                matter_document_id=document_id,
                collection_item_id=documents_by_id[document_id].collection_item_id,
                source_collection_id=documents_by_id[document_id].source_collection_id,
                scope_ordinal=ordinal,
                extensions={
                    "retrieval_lane": ("CONTROL" if selection_reasons[document_id] == ["CONTROL"] else "TARGETED"),
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
        owner_type="REVIEW_PROGRAM_CLASSIFIER_IMPROVEMENT",
        owner_id=attempt.id,
        producer_kind="CORE",
        producer_run_id=run.id,
        actor_user_id=actor.id,
    )
    try:
        batch_set = create_batch_set(
            db,
            matter=matter,
            task=task,
            task_version=task_version,
            name=f"{program.name} · classifier improvement {round_number}"[:200],
            description="Frozen targeted population for dual-model classifier improvement Q&A.",
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
            "classifier_improvement_round": round_number,
            "task_version_id": str(task_version.id),
            "profile_ids": [str(profile.id) for profile in profiles],
            "retrieval_policy_version": "review-program-classifier-improvement-retrieval-v1",
            "queries": frozen_queries,
            "query_yields": query_yields,
            "control_selected_count": len(control_ids),
            "excluded_prior_program_document_count": len(excluded_document_ids),
            "prior_batch_set_ids": [str(value) for value in prior_batch_set_ids],
        },
    }
    link_review_program_child(
        db,
        program=program,
        run=run,
        stage_attempt=attempt,
        purpose="CLASSIFIER_IMPROVEMENT_POPULATION",
        child_type="ANALYSIS_TASK_BATCH_SET",
        child_id=batch_set.id,
        derivation_key=f"classifier-improvement-batch-set:{attempt.id}",
        batching_policy="PARTITIONED_SET",
        context={
            "input_manifest_id": str(manifest.id),
            "population_count": len(selected_ids),
            "targeted_count": len(selected_ids) - len(control_ids),
            "control_count": len(control_ids),
            "round": round_number,
        },
    )
    improvement_snapshot = {
        "schema_version": 1,
        "round": round_number,
        "search_index_generation_id": str(generation.id),
        "input_manifest_id": str(manifest.id),
        "input_manifest_content_hash": manifest.artifact_content_hash,
        "batch_set_id": str(batch_set.id),
        "task_id": str(task.id),
        "task_version_id": str(task_version.id),
        "execution_profile_ids": [str(profile.id) for profile in profiles],
        "selected_document_count": len(selected_ids),
        "targeted_document_count": len(selected_ids) - len(control_ids),
        "control_document_count": len(control_ids),
        "target_batch_size": target_batch_size,
        "keep_families_together": keep_families_together,
        "queries": frozen_queries,
        "query_yields": query_yields,
    }
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=actor.id,
        configuration_stage="CLASSIFIER_IMPROVEMENT",
        configuration_updates={"classifier_improvement": improvement_snapshot},
        source_hash_updates={
            "classifier_improvement_input_manifest_content_hash": manifest.artifact_content_hash,
            "classifier_improvement_retrieval_plan_hash": canonical_hash(frozen_queries),
            "classifier_improvement_search_index_schema_hash": generation.schema_hash,
        },
    )
    run.progress_summary = {
        **run.progress_summary,
        "classifier_improvement_round": round_number,
        "classifier_improvement_state": "MATERIALIZING_BATCH_SET",
        "classifier_improvement": {
            **improvement_snapshot,
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
        reason="Classifier improvement population accepted",
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
        action="review_program.classifier_improvement_started",
        target_type="review_program_run",
        target_id=run.id,
        details={
            "program_id": str(program.id),
            "batch_set_id": str(batch_set.id),
            "input_manifest_id": str(manifest.id),
            "population_count": len(selected_ids),
            "targets": submitted_targets,
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
        elif (
            action.stage_key in {"DISCOVERY_QA", "CLASSIFIER_IMPROVEMENT"} and choice == "START"
        ) or (action.stage_key == "NO_CODE_QC" and choice == "START_QC"):
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
        elif action.stage_key == "CLASSIFIER_REVIEW" and choice == "ACCEPT_CLASSIFIERS":
            _accept_trained_classifiers(
                db,
                program=program,
                run=run,
                attempt=attempt,
                action=action,
            )
        elif action.stage_key == "CLASSIFIER_REVIEW" and choice == "IMPROVE_CLASSIFIERS":
            _complete_action_attempt(
                attempt,
                action,
                result={"decision": "IMPROVE_CLASSIFIERS"},
            )
            _advance_after_stage(db, program=program, run=run, attempt=attempt)
        elif action.stage_key == "WHOLE_COLLECTION_SCORING" and choice == "START_SCORING":
            _launch_whole_collection_scoring(
                db,
                program=program,
                run=run,
                attempt=attempt,
                action=action,
            )
        elif action.stage_key == "CLASSIFIER_POSITIVE_COVERAGE" and choice == "RUN_COVERAGE":
            _launch_classifier_positive_coverage(
                db,
                program=program,
                run=run,
                attempt=attempt,
                action=action,
            )
        elif action.stage_key == "NO_CODE_QC" and choice == "REVIEW_QC_FINDINGS":
            _start_negative_population_qc_findings_review(
                db,
                program=program,
                run=run,
                attempt=attempt,
                action=action,
            )
        elif action.stage_key == "NO_CODE_QC" and choice == "CONFIRM_QC_REVIEW":
            _confirm_negative_population_qc_findings_review(
                db,
                program=program,
                run=run,
                attempt=attempt,
                action=action,
            )
        elif action.stage_key == "NO_CODE_QC" and choice == "ACCEPT_QC":
            qc = dict((run.progress_summary or {}).get("negative_population_qc") or {})
            if qc.get("state") != "REPORT_READY":
                raise ReviewProgramError("Negative-population QC report is not ready for acceptance")
            accepted = {
                "schema_version": 1,
                "state": "ACCEPTED",
                "report_hash": canonical_hash(qc.get("report") or {}),
                "accepted_by_user_id": str(action.resolved_by_user_id or run.initiated_by_user_id),
                "accepted_at": action.resolved_at.isoformat() if action.resolved_at else None,
            }
            version = _create_review_program_version(
                db,
                program=program,
                run=run,
                actor_user_id=action.resolved_by_user_id or run.initiated_by_user_id,
                configuration_stage="COMPLETION",
                configuration_updates={"negative_population_qc_acceptance": accepted},
                source_hash_updates={"negative_population_qc_acceptance_hash": canonical_hash(accepted)},
            )
            _complete_action_attempt(
                attempt,
                action,
                result={"negative_population_qc_acceptance": accepted, "program_version_id": str(version.id)},
            )
            run.progress_summary = {
                **run.progress_summary,
                "negative_population_qc_state": "ACCEPTED",
                "negative_population_qc_acceptance": accepted,
            }
            _advance_after_stage(db, program=program, run=run, attempt=attempt)
        elif action.stage_key == "NO_CODE_QC" and choice in {"RETRIEVE_AND_RETRAIN", "REVISE_GUIDANCE"}:
            qc = dict((run.progress_summary or {}).get("negative_population_qc") or {})
            if qc.get("state") != "REPORT_READY":
                raise ReviewProgramError("Negative-population QC report is not ready for remediation")
            target_stage = "CLASSIFIER_IMPROVEMENT" if choice == "RETRIEVE_AND_RETRAIN" else "DEFINITION_REFINEMENT"
            history = list((run.progress_summary or {}).get("negative_population_qc_history") or [])
            history.append(
                {
                    **qc,
                    "remediation_choice": choice,
                    "remediation_reason": action.resolution_reason,
                    "reopened_by_user_id": str(action.resolved_by_user_id or run.initiated_by_user_id),
                    "reopened_at": action.resolved_at.isoformat() if action.resolved_at else None,
                    "target_stage": target_stage,
                }
            )
            remediation = {
                "schema_version": 1,
                "choice": choice,
                "source_qc_report_hash": canonical_hash(qc.get("report") or {}),
                "target_stage": target_stage,
                "requested_by_user_id": str(action.resolved_by_user_id or run.initiated_by_user_id),
                "requested_at": action.resolved_at.isoformat() if action.resolved_at else None,
                "reason": action.resolution_reason,
            }
            version = _create_review_program_version(
                db,
                program=program,
                run=run,
                actor_user_id=action.resolved_by_user_id or run.initiated_by_user_id,
                configuration_stage=target_stage,
                configuration_updates={
                    "negative_population_qc_history": history,
                    "negative_population_qc_remediation": remediation,
                },
                source_hash_updates={"negative_population_qc_remediation_hash": canonical_hash(remediation)},
            )
            _complete_action_attempt(
                attempt,
                action,
                result={"remediation": remediation, "program_version_id": str(version.id)},
            )
            run.progress_summary = {
                **run.progress_summary,
                "negative_population_qc_state": "REOPENED",
                "negative_population_qc_history": history,
                "negative_population_qc": {},
                "negative_population_qc_remediation": remediation,
            }
            _activate_stage_by_key(db, program=program, run=run, stage_key=target_stage)
        elif action.stage_key == "COMPLETION" and choice == "COMPLETE":
            _finalize_review_program_completion(
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
    population_stage = {
        "DISCOVERY_POPULATION": (
            "DISCOVERY_QA",
            "DISCOVERY_QA_PROFILE",
            "discovery",
            "discovery_qa",
        ),
        "CLASSIFIER_IMPROVEMENT_POPULATION": (
            "CLASSIFIER_IMPROVEMENT",
            "CLASSIFIER_IMPROVEMENT_QA_PROFILE",
            "classifier-improvement",
            "classifier_improvement",
        ),
        "CLASSIFIER_POSITIVE_COVERAGE_POPULATION": (
            "CLASSIFIER_POSITIVE_COVERAGE",
            "CLASSIFIER_POSITIVE_COVERAGE_QA_PROFILE",
            "classifier-positive-coverage",
            "classifier_positive_coverage",
        ),
        "NEGATIVE_POPULATION_QC_POPULATION": (
            "NO_CODE_QC",
            "NEGATIVE_POPULATION_QC_QA_PROFILE",
            "negative-population-qc",
            "negative_population_qc",
        ),
    }.get(child.purpose)
    if population_stage is None:
        return None
    stage_key, profile_purpose, idempotency_scope, progress_key = population_stage
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
        raise ReviewProgramError("Review Program Q&A Batch Set link is incomplete")
    if (
        run.status in TERMINAL_RUN_STATUSES
        or run.current_stage != stage_key
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
            error_code=f"{stage_key}_BATCH_SET_{workflow.status}",
            error_summary=batch_set.error_message
            or f"{stage_key.replace('_', ' ').title()} Batch Set {workflow.status.lower()}",
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
                idempotency_key=(f"review-program:{run.id}:{idempotency_scope}:{attempt.attempt}:profile:{profile.id}"),
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
                purpose=profile_purpose,
                child_type="ANALYSIS_TASK_BATCH_SET_RUN",
                child_id=batch_run.id,
                derivation_key=f"{idempotency_scope}-profile-run:{attempt.id}:{profile.id}",
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
            error_code=f"{stage_key}_LAUNCH_FAILED",
            error_summary=str(exc),
            retriable=True,
        )
    progress = dict((run.progress_summary or {}).get(progress_key) or {})
    progress["batch_set_run_ids"] = [str(item.id) for item in launched_runs]
    progress["batch_set_population_hash"] = batch_set.population_hash
    progress["batch_count"] = batch_set.batch_count
    run.progress_summary = {
        **run.progress_summary,
        f"{progress_key}_state": "RUNNING_PROFILES",
        progress_key: progress,
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
    definition_ids = [uuid.UUID(str(value)) for value in accepted_task.get("destination_metadata_definition_ids", [])]
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
                    metrics["elapsed_seconds"] += int((workflow.completed_at - workflow.started_at).total_seconds())
        batch_ids = sorted(
            set(children_by_parent[0]).intersection(children_by_parent[1]),
            key=str,
        )
        paired_children.append((left_parent, right_parent, children_by_parent[0], children_by_parent[1], batch_ids))
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
                            (parent.configuration_snapshot or {}).get("execution_profile_id", f"Profile {index + 1}")
                        ),
                        detail=(source.configuration_snapshot or {}).get("model_key"),
                        metadata_definition_ids=definition_ids,
                        created_at=source.created_at,
                    )
                    for index, (parent, source) in enumerate(((left_parent, left_run), (right_parent, right_run)))
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
                        value_counts["disagreement"] += (value_key in left_values) != (value_key in right_values)
        declared_keys = [
            str(value.get("key")) for value in definition.allowed_values or [] if value.get("key") is not None
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
        dict.fromkeys(
            db.scalars(
                select(ReviewProgramChild.child_id).where(
                    ReviewProgramChild.run_id == run.id,
                    ReviewProgramChild.purpose.in_({"DISCOVERY_QA_PROFILE", "CLASSIFIER_IMPROVEMENT_QA_PROFILE"}),
                    ReviewProgramChild.child_type == "ANALYSIS_TASK_BATCH_SET_RUN",
                )
            )
        )
    )
    if not child_ids:
        raise ReviewProgramError("Discovery Q&A profile provenance is unavailable")
    profile_runs = list(
        db.scalars(
            select(AnalysisTaskBatchSetRun)
            .where(AnalysisTaskBatchSetRun.id.in_(child_ids))
            .order_by(AnalysisTaskBatchSetRun.created_at, AnalysisTaskBatchSetRun.id)
        )
    )
    if {item.id for item in profile_runs} != set(child_ids) or any(
        item.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"} for item in profile_runs
    ):
        raise ReviewProgramError("Every Discovery Q&A profile run must be complete before label resolution")
    return profile_runs


def _completed_discovery_label_runs(
    db: Session,
    *,
    profile_runs: list[AnalysisTaskBatchSetRun],
) -> list[ReviewBatchRun]:
    """Resolve the immutable child runs that actually contain Q&A label values."""

    if not profile_runs:
        raise ReviewProgramError("Discovery Q&A profile provenance is unavailable")
    child_run_ids = list(
        db.scalars(
            select(AnalysisTaskBatchSetRunChild.child_run_id)
            .where(
                AnalysisTaskBatchSetRunChild.batch_set_run_id.in_([profile_run.id for profile_run in profile_runs]),
                AnalysisTaskBatchSetRunChild.child_run_id.is_not(None),
            )
            .order_by(
                AnalysisTaskBatchSetRunChild.batch_set_run_id,
                AnalysisTaskBatchSetRunChild.ordinal,
            )
        )
    )
    ordered_ids = list(dict.fromkeys(child_run_id for child_run_id in child_run_ids if child_run_id))
    label_runs_by_id = {
        label_run.id: label_run
        for label_run in db.scalars(select(ReviewBatchRun).where(ReviewBatchRun.id.in_(ordered_ids)))
    }
    if not ordered_ids or len(label_runs_by_id) != len(ordered_ids):
        raise ReviewProgramError("Discovery Q&A child-run provenance is incomplete")
    label_runs = [label_runs_by_id[label_run_id] for label_run_id in ordered_ids]
    if any(
        label_run.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
        or label_run.run_type not in {"AGENT", "WORKFLOW"}
        for label_run in label_runs
    ):
        raise ReviewProgramError("Every Discovery Q&A label run must be complete before training")
    return label_runs


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
                uuid.UUID(str(value)) for value in accepted_task.get("destination_metadata_definition_ids", [])
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
                {**source.model_dump(mode="json"), "reviewer_revision": None} for source in source_options
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
                    name=(f"{program.name} · {definition.display_name} differences · {len(created) + 1}")[:200],
                    description=("Ad hoc human review of differences between the two pinned Discovery Q&A profiles."),
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
                    derivation_key=(f"disagreement-review:{attempt.id}:{source_batch.id}:{definition.id}"),
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
            raise ReviewProgramError(f"Difference batch {batch.name} has invalid coding-field provenance") from exc
        resolved = resolve_reviewer_coding_as_of(
            db,
            reviewer_coding_id=coding.id,
            revision=coding.revision,
            document_ids=document_ids,
            metadata_definition_ids=[definition_id],
        )
        completed_document_ids = {item.matter_document_id for item in resolved.documents if item.status == "COMPLETED"}
        if len(completed_document_ids) != batch.document_count:
            raise ReviewProgramError(
                f"Difference batch {batch.name} has "
                f"{batch.document_count - len(completed_document_ids)} unresolved documents"
            )
        coded_document_ids = {
            item.matter_document_id for item in resolved.fields if item.metadata_definition_id == definition_id
        }
        if coded_document_ids != set(document_ids):
            raise ReviewProgramError(f"Difference batch {batch.name} has incomplete field coding")
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
    profile_runs = _completed_discovery_profile_runs(db, run=run)
    label_runs = _completed_discovery_label_runs(db, profile_runs=profile_runs)
    snapshot = {
        **resolution,
        "state": "FROZEN",
        "profile_run_ids": [str(profile_run.id) for profile_run in profile_runs],
        "label_run_ids": [str(label_run.id) for label_run in label_runs],
        "frozen_by_user_id": str(action.resolved_by_user_id),
        "frozen_at": action.resolved_at.isoformat() if action.resolved_at else None,
        "discovery_analysis_hash": canonical_hash((run.progress_summary or {}).get("discovery_analysis") or {}),
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


def _accept_trained_classifiers(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    action: ReviewProgramAction,
) -> None:
    classifier_results = dict((run.progress_summary or {}).get("classifier_results") or {})
    models = classifier_results.get("models") or []
    if classifier_results.get("state") != "COMPLETED" or not models:
        raise ReviewProgramError("Classifier training results are unavailable")
    actor_user_id = action.resolved_by_user_id or run.initiated_by_user_id
    approvals: list[dict[str, str]] = []
    try:
        for item in models:
            decision = decide_candidate(
                db,
                matter_id=program.matter_id,
                classification_model_id=uuid.UUID(str(item["classification_model_id"])),
                model_version_id=uuid.UUID(str(item["model_version_id"])),
                decision_type="APPROVAL",
                reason=f"Accepted through First Pass Review program {program.name}",
                actor_user_id=actor_user_id,
            )
            approvals.append(
                {
                    "classification_model_id": str(decision.classification_model_id),
                    "model_version_id": str(decision.model_version_id),
                    "decision_id": str(decision.id),
                }
            )
    except (ClassificationLifecycleError, KeyError, TypeError, ValueError) as exc:
        raise ReviewProgramError(str(exc)) from exc
    accepted = {
        "schema_version": 1,
        "classifier_results_hash": canonical_hash(classifier_results),
        "accepted_by_user_id": str(actor_user_id),
        "accepted_at": action.resolved_at.isoformat() if action.resolved_at else None,
        "approvals": approvals,
    }
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=actor_user_id,
        configuration_stage="WHOLE_COLLECTION_SCORING",
        configuration_updates={"accepted_classifiers": accepted},
        source_hash_updates={"accepted_classifiers_hash": canonical_hash(accepted)},
    )
    _complete_action_attempt(
        attempt,
        action,
        result={"accepted_classifiers": accepted, "program_version_id": str(version.id)},
    )
    completed = list(run.progress_summary.get("completed_stages", []))
    if "CLASSIFIER_REVIEW" not in completed:
        completed.append("CLASSIFIER_REVIEW")
    run.progress_summary = {
        **run.progress_summary,
        "completed_stages": completed,
        "last_completed_stage": "CLASSIFIER_REVIEW",
        "accepted_classifiers": accepted,
    }
    _activate_stage_by_key(
        db,
        program=program,
        run=run,
        stage_key="WHOLE_COLLECTION_SCORING",
    )


def _prepare_whole_collection_scoring_snapshot(
    db: Session,
    *,
    matter: Matter,
    model: ClassificationModel,
    model_version: ClassificationModelVersion,
    embedding_job: MatterEmbeddingJob,
    actor_user_id: uuid.UUID,
) -> ClassificationDatasetSnapshot:
    snapshot_request = ClassificationDatasetSnapshotRequest.model_validate(
        {
            "configuration_id": model_version.configuration_id,
            "purpose": "SCORING",
            "training_scope": {"scope_type": "MATTER", "scope_id": matter.id},
            "embedding_job_id": embedding_job.id,
        }
    )
    prepared_snapshot = prepare_dataset_snapshot(
        db,
        matter=matter,
        classification_model_id=model.id,
        actor_user_id=actor_user_id,
        request=snapshot_request,
    )
    snapshot, should_materialize, _ = begin_dataset_snapshot(
        db,
        prepared=prepared_snapshot,
        actor_user_id=actor_user_id,
    )
    if should_materialize:
        materialize_dataset_snapshot(
            db,
            snapshot=snapshot,
            prepared=prepared_snapshot,
            actor_user_id=actor_user_id,
        )
    if not (
        snapshot.status == "READY_FOR_SCORING"
        and (snapshot.feature_specification or {}).get("embedding_job_id") == str(embedding_job.id)
    ):
        snapshot.status = "BUILDING"
        prepared_features = prepare_scoring_features(
            db,
            snapshot=snapshot,
            embedding_job_id=embedding_job.id,
            actor_user_id=actor_user_id,
        )
        materialize_scoring_features(
            db,
            snapshot=snapshot,
            prepared=prepared_features,
            actor_user_id=actor_user_id,
        )
    if snapshot.status != "READY_FOR_SCORING":
        raise ReviewProgramError("Whole-collection scoring snapshot is not ready")
    return snapshot


def _launch_whole_collection_scoring(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    action: ReviewProgramAction,
) -> None:
    accepted = dict((run.progress_summary or {}).get("accepted_classifiers") or {})
    classifier_results = dict((run.progress_summary or {}).get("classifier_results") or {})
    approvals = [item for item in accepted.get("approvals", []) if isinstance(item, dict)]
    result_models = {
        str(item.get("classification_model_id")): item
        for item in classifier_results.get("models", [])
        if isinstance(item, dict) and item.get("classification_model_id")
    }
    if not approvals or len(approvals) != len(result_models):
        raise ReviewProgramError("Accepted classifier provenance is incomplete")
    matter = db.get(Matter, program.matter_id)
    if matter is None:
        raise ReviewProgramError("Review Program matter is unavailable")
    actor_user_id = action.resolved_by_user_id or run.initiated_by_user_id
    scoring_states: list[dict[str, Any]] = []
    created_runs: list[ClassificationScoringRun] = []
    terminal_runs: list[tuple[ClassificationScoringRun, WorkflowRun]] = []
    try:
        for approval in approvals:
            model_id = uuid.UUID(str(approval["classification_model_id"]))
            model_version_id = uuid.UUID(str(approval["model_version_id"]))
            result_model = result_models.get(str(model_id))
            if result_model is None:
                raise ReviewProgramError("Accepted classifier result is unavailable")
            model = db.get(ClassificationModel, model_id)
            model_version = db.get(ClassificationModelVersion, model_version_id)
            training_job = db.get(
                ClassificationTrainingJob,
                uuid.UUID(str(result_model["training_job_id"])),
            )
            embedding_job = (
                db.get(MatterEmbeddingJob, training_job.embedding_job_id) if training_job is not None else None
            )
            if (
                model is None
                or model.matter_id != matter.id
                or model_version is None
                or model_version.classification_model_id != model.id
                or model_version.status != "APPROVED"
                or training_job is None
                or embedding_job is None
                or not embedding_job_output_is_usable(embedding_job)
            ):
                raise ReviewProgramError("Accepted classifier scoring inputs are unavailable")
            snapshot = _prepare_whole_collection_scoring_snapshot(
                db,
                matter=matter,
                model=model,
                model_version=model_version,
                embedding_job=embedding_job,
                actor_user_id=actor_user_id,
            )
            prepared = prepare_scoring(
                db,
                matter_id=matter.id,
                classification_model_id=model.id,
                dataset_snapshot_id=snapshot.id,
                model_version_id=model_version.id,
            )
            scoring_run, created = create_scoring_run(
                db,
                prepared=prepared,
                idempotency_key=(
                    f"review-program:{run.id}:whole-collection:attempt:{attempt.attempt}:model:{model.id}"
                ),
                actor_user_id=actor_user_id,
                notification_parent_type="REVIEW_PROGRAM_RUN",
                notification_parent_id=run.id,
            )
            workflow = current_job_execution(
                db,
                job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
                job_id=scoring_run.id,
            )
            if workflow is None:
                raise ReviewProgramError("Classifier scoring workflow is unavailable")
            if not created and scoring_run.status not in {
                "COMPLETED",
                "COMPLETED_WITH_ERRORS",
            }:
                if workflow.notification_policy != "PARENT_MANAGED" or workflow.notification_parent_id != run.id:
                    raise ReviewProgramError(
                        "An identical classifier scoring run is already active outside this Review Program"
                    )
                if scoring_run.status in {"FAILED", "CANCELED"}:
                    workflow = retry_scoring_run(
                        db,
                        run=scoring_run,
                        actor_user_id=actor_user_id,
                    )
                    created = True
            child_context = {
                "classification_model_id": str(model.id),
                "model_version_id": str(model_version.id),
                "dataset_snapshot_id": str(snapshot.id),
                "dataset_snapshot_aggregate_hash": snapshot.aggregate_hash,
                "population_document_count": snapshot.population_count,
                "batching_policy": "WHOLE_MATTER",
            }
            existing_child = db.scalar(
                select(ReviewProgramChild).where(
                    ReviewProgramChild.run_id == run.id,
                    ReviewProgramChild.child_type == "CLASSIFICATION_SCORING_RUN",
                    ReviewProgramChild.child_id == scoring_run.id,
                )
            )
            if existing_child is None:
                link_review_program_child(
                    db,
                    program=program,
                    run=run,
                    stage_attempt=attempt,
                    purpose="WHOLE_COLLECTION_SCORING",
                    child_type="CLASSIFICATION_SCORING_RUN",
                    child_id=scoring_run.id,
                    derivation_key=f"whole-collection-scoring:{attempt.id}:{model.id}",
                    batching_policy="WHOLE_MATTER",
                    context=child_context,
                )
            else:
                existing_child.stage_attempt_id = attempt.id
                existing_child.status = "LINKED"
                existing_child.derivation_key = f"whole-collection-scoring:{attempt.id}:{model.id}"
                existing_child.context = child_context
            scoring_states.append(
                {
                    "classification_model_id": str(model.id),
                    "model_version_id": str(model_version.id),
                    "field_key": result_model.get("field_key"),
                    "field_name": result_model.get("field_name"),
                    "dataset_snapshot_id": str(snapshot.id),
                    "dataset_snapshot_aggregate_hash": snapshot.aggregate_hash,
                    "scoring_run_id": str(scoring_run.id),
                    "population_document_count": scoring_run.population_document_count,
                    "status": scoring_run.status,
                }
            )
            if created:
                created_runs.append(scoring_run)
            elif scoring_run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
                terminal_runs.append((scoring_run, workflow))
    except (
        ClassificationDatasetError,
        ClassificationFeatureSplitError,
        ClassificationScoringError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise ReviewProgramError(str(exc)) from exc

    scoring_snapshot = {
        "schema_version": 1,
        "state": "RUNNING",
        "scope_type": "MATTER",
        "stage_attempt_id": str(attempt.id),
        "models": scoring_states,
    }
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=actor_user_id,
        configuration_stage="WHOLE_COLLECTION_SCORING",
        configuration_updates={"whole_collection_scoring": scoring_snapshot},
        source_hash_updates={"whole_collection_scoring_plan_hash": canonical_hash(scoring_snapshot["models"])},
    )
    attempt.status = "RUNNING"
    attempt.result_summary = {
        "gate_choice": "START_SCORING",
        "action_id": str(action.id),
        "program_version_id": str(version.id),
    }
    run.status = "RUNNING"
    run.progress_summary = {
        **run.progress_summary,
        "whole_collection_scoring": scoring_snapshot,
    }
    for scoring_run in created_runs:
        enqueue_classification_model_scoring(db, scoring_run.workflow_id, str(scoring_run.id))
    for scoring_run, workflow in terminal_runs:
        project_review_program_classifier_scoring_terminal(
            db,
            scoring_run=scoring_run,
            workflow=workflow,
            review_program_run_id=run.id,
        )


_QA_COVERAGE_PROFILE_PURPOSES = {
    "DISCOVERY_QA_PROFILE",
    "CLASSIFIER_IMPROVEMENT_QA_PROFILE",
    "CLASSIFIER_POSITIVE_COVERAGE_QA_PROFILE",
    "NEGATIVE_POPULATION_QC_QA_PROFILE",
}


def _classifier_positive_class_keys(
    configuration: ClassificationModelConfiguration,
    predicted_class_keys: list[str],
) -> list[str]:
    selected = list(dict.fromkeys(predicted_class_keys))
    if configuration.classification_strategy != "BINARY":
        return selected
    positive_key = (configuration.binary_mapping or {}).get("positive_class_key")
    if not isinstance(positive_key, str) or not positive_key:
        raise ReviewProgramError("Binary classifier positive-class provenance is unavailable")
    return [positive_key] if positive_key in selected else []


def _classifier_positive_documents(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    matter: Matter,
    actor_user_id: uuid.UUID,
) -> tuple[dict[uuid.UUID, dict[str, Any]], list[ClassificationScoringRun]]:
    scoring = dict((run.progress_summary or {}).get("whole_collection_scoring") or {})
    models = [item for item in scoring.get("models", []) if isinstance(item, dict)]
    if scoring.get("state") not in {"COMPLETED", "COMPLETED_WITH_ERRORS"} or not models:
        raise ReviewProgramError("Whole-collection classifier results are unavailable")
    positives: dict[uuid.UUID, dict[str, Any]] = {}
    scoring_runs: list[ClassificationScoringRun] = []
    for model_state in models:
        try:
            scoring_run_id = uuid.UUID(str(model_state["scoring_run_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewProgramError("Classifier scoring provenance is invalid") from exc
        scoring_run = db.get(ClassificationScoringRun, scoring_run_id)
        if (
            scoring_run is None
            or scoring_run.matter_id != program.matter_id
            or scoring_run.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
        ):
            raise ReviewProgramError("Classifier scoring output is unavailable")
        version = db.get(ClassificationModelVersion, scoring_run.model_version_id)
        configuration = (
            db.get(ClassificationModelConfiguration, version.configuration_id) if version is not None else None
        )
        if configuration is None:
            raise ReviewProgramError("Classifier scoring configuration is unavailable")
        preview = prediction_preview(
            scoring_run,
            matter=matter,
            actor_user_id=actor_user_id,
            offset=0,
            limit=scoring_run.population_document_count,
        )
        probability_ordinals = {class_key: ordinal for ordinal, class_key in enumerate(preview.ordered_class_keys)}
        for row in preview.items:
            if row.result_status != "SUCCESS" or row.abstained:
                continue
            positive_keys = _classifier_positive_class_keys(
                configuration,
                list(row.predicted_class_keys),
            )
            if not positive_keys:
                continue
            selected_probabilities = [
                float(row.probabilities[probability_ordinals[class_key]])
                for class_key in positive_keys
                if class_key in probability_ordinals and probability_ordinals[class_key] < len(row.probabilities)
            ]
            document = positives.setdefault(
                row.matter_document_id,
                {"issues": [], "maximum_probability": None},
            )
            document["issues"].extend(
                {
                    "classification_model_id": str(scoring_run.classification_model_id),
                    "model_version_id": str(scoring_run.model_version_id),
                    "scoring_run_id": str(scoring_run.id),
                    "field_key": model_state.get("field_key"),
                    "field_name": model_state.get("field_name"),
                    "class_key": class_key,
                }
                for class_key in positive_keys
            )
            if selected_probabilities:
                maximum_probability = max(selected_probabilities)
                current = document["maximum_probability"]
                document["maximum_probability"] = (
                    maximum_probability if current is None else max(float(current), maximum_probability)
                )
        scoring_runs.append(scoring_run)
    for document in positives.values():
        document["issues"] = sorted(
            document["issues"],
            key=lambda item: (
                str(item.get("field_key") or ""),
                str(item.get("class_key") or ""),
                str(item.get("scoring_run_id") or ""),
            ),
        )
    return positives, scoring_runs


def _program_qa_results_by_profile(
    db: Session,
    *,
    run: ReviewProgramRun,
    task_version_id: uuid.UUID,
    profile_ids: set[uuid.UUID],
    completed_only: bool = True,
) -> dict[uuid.UUID, dict[uuid.UUID, ReviewDecisionResult]]:
    linked_run_ids = list(
        db.scalars(
            select(ReviewProgramChild.child_id).where(
                ReviewProgramChild.run_id == run.id,
                ReviewProgramChild.purpose.in_(_QA_COVERAGE_PROFILE_PURPOSES),
                ReviewProgramChild.child_type == "ANALYSIS_TASK_BATCH_SET_RUN",
            )
        )
    )
    if not linked_run_ids:
        return {}
    profile_runs = list(
        db.scalars(
            select(AnalysisTaskBatchSetRun)
            .where(
                AnalysisTaskBatchSetRun.id.in_(linked_run_ids),
                AnalysisTaskBatchSetRun.matter_analysis_task_version_id == task_version_id,
                AnalysisTaskBatchSetRun.execution_profile_id.in_(list(profile_ids)),
            )
            .order_by(AnalysisTaskBatchSetRun.created_at, AnalysisTaskBatchSetRun.id)
        )
    )
    by_document: dict[uuid.UUID, dict[uuid.UUID, ReviewDecisionResult]] = {}
    for profile_run in profile_runs:
        if profile_run.execution_profile_id is None:
            continue
        label_run_ids = list(
            db.scalars(
                select(AnalysisTaskBatchSetRunChild.child_run_id).where(
                    AnalysisTaskBatchSetRunChild.batch_set_run_id == profile_run.id,
                    AnalysisTaskBatchSetRunChild.child_run_id.is_not(None),
                )
            )
        )
        if not label_run_ids:
            continue
        statement = select(ReviewDecisionResult).where(
            ReviewDecisionResult.review_batch_run_id.in_(label_run_ids),
            ReviewDecisionResult.matter_analysis_task_version_id == task_version_id,
        )
        if completed_only:
            statement = statement.where(ReviewDecisionResult.status == "COMPLETED")
        results = db.scalars(statement.order_by(ReviewDecisionResult.created_at, ReviewDecisionResult.id))
        for result in results:
            by_document.setdefault(result.matter_document_id, {})[profile_run.execution_profile_id] = result
    return by_document


def _coverage_confidence_band(value: float | None) -> str:
    if value is None:
        return "UNAVAILABLE"
    if value >= 0.9:
        return "HIGH"
    if value >= 0.7:
        return "MEDIUM"
    return "LOW"


def _prepare_classifier_positive_coverage(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    actor_user_id: uuid.UUID,
) -> tuple[AnalysisInputManifest, dict[str, Any]]:
    matter = db.get(Matter, program.matter_id)
    if matter is None:
        raise ReviewProgramError("Review Program matter is unavailable")
    _, task_version, profiles = _qa_task_records(db, run)
    positives, scoring_runs = _classifier_positive_documents(
        db,
        program=program,
        run=run,
        matter=matter,
        actor_user_id=actor_user_id,
    )
    documents_by_id = {
        document.id: document
        for document in db.scalars(
            select(MatterDocument).where(
                MatterDocument.matter_id == matter.id,
                MatterDocument.id.in_(list(positives)),
            )
        )
    }
    if len(documents_by_id) != len(positives):
        raise ReviewProgramError("Classifier-positive documents are outside the matter")
    try:
        source_snapshots = get_collection_item_analysis_input_snapshots(
            item_ids=[document.collection_item_id for document in documents_by_id.values()],
            actor_user_id=actor_user_id,
            tenant_id=program.tenant_id,
            client_id=matter.client_id,
        )
    except (httpx.HTTPError, PermissionError, ValueError) as exc:
        raise ReviewProgramError(f"Classifier-positive source content is unavailable: {exc}") from exc
    profile_ids = {profile.id for profile in profiles}
    results_by_document = _program_qa_results_by_profile(
        db,
        run=run,
        task_version_id=task_version.id,
        profile_ids=profile_ids,
    )
    issue_counts: dict[tuple[str, str], Counter] = {}
    confidence_counts = Counter()
    missing_profile_counts = Counter()
    uncovered_documents: list[AnalysisInputDocument] = []
    already_covered_count = 0
    stale_source_count = 0
    source_unavailable_count = 0
    for document_id in sorted(positives, key=str):
        prediction = positives[document_id]
        matter_document = documents_by_id[document_id]
        source_snapshot = source_snapshots.get(matter_document.collection_item_id)
        source = source_snapshot.text_source if source_snapshot is not None else None
        expected_source_hash = source.content_hash if source is not None else None
        profile_results = results_by_document.get(document_id, {})
        current_profile_ids = {
            profile_id
            for profile_id, result in profile_results.items()
            if expected_source_hash is not None and result.source_content_hash == expected_source_hash
        }
        missing_profile_ids = profile_ids - current_profile_ids
        has_stale_result = bool(profile_results) and bool(missing_profile_ids)
        if not missing_profile_ids:
            already_covered_count += 1
        else:
            if expected_source_hash is None:
                source_unavailable_count += 1
                coverage_status = "SOURCE_UNAVAILABLE"
            elif has_stale_result:
                stale_source_count += 1
                coverage_status = "STALE_OR_PARTIAL_QA"
            else:
                coverage_status = "MISSING_QA"
            for profile in profiles:
                if profile.id in missing_profile_ids:
                    missing_profile_counts[profile.key] += 1
            maximum_probability = prediction.get("maximum_probability")
            uncovered_documents.append(
                AnalysisInputDocument(
                    matter_document_id=document_id,
                    collection_item_id=matter_document.collection_item_id,
                    source_collection_id=matter_document.source_collection_id,
                    scope_ordinal=len(uncovered_documents) + 1,
                    source_artifact_id=source.artifact_id if source is not None else None,
                    source_content_hash=expected_source_hash,
                    resolved_inputs=(
                        {
                            "text_source": {
                                "role": source.role,
                                "media_type": source.media_type,
                                "original_filename": source.original_filename,
                                "byte_length": source.byte_length,
                                "record_type": source_snapshot.record_type,
                            }
                        }
                        if source is not None and source_snapshot is not None
                        else {}
                    ),
                    extensions={
                        "classifier_issue_keys": json.dumps(
                            [f"{item.get('field_key')}:{item.get('class_key')}" for item in prediction["issues"]],
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        "scoring_run_ids": json.dumps(
                            sorted({item["scoring_run_id"] for item in prediction["issues"]}),
                            separators=(",", ":"),
                        ),
                        "missing_profile_keys": json.dumps(
                            sorted(profile.key for profile in profiles if profile.id in missing_profile_ids),
                            separators=(",", ":"),
                        ),
                        "coverage_status": coverage_status,
                        "maximum_probability": maximum_probability,
                    },
                )
            )
        confidence_counts[_coverage_confidence_band(prediction.get("maximum_probability"))] += 1
        for issue in prediction["issues"]:
            key = (str(issue.get("field_key") or ""), str(issue.get("class_key") or ""))
            counts = issue_counts.setdefault(key, Counter())
            counts["positive"] += 1
            counts["uncovered"] += bool(missing_profile_ids)
    contract = AnalysisInputContract(
        key="review-program.classifier-positive-coverage",
        version=1,
        resolver_version="review-program-classifier-positive-coverage-v1",
        extension_fields=[
            AnalysisInputExtensionField(name="classifier_issue_keys", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="scoring_run_ids", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="missing_profile_keys", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="coverage_status", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="maximum_probability", type="FLOAT64", nullable=True),
        ],
        options={
            "review_program_id": str(program.id),
            "review_program_run_id": str(run.id),
            "stage_attempt_id": str(attempt.id),
            "task_version_id": str(task_version.id),
            "required_profile_ids": [str(profile.id) for profile in profiles],
            "scoring_run_ids": [str(item.id) for item in scoring_runs],
            "policy_version": "review-program-classifier-positive-coverage-v1",
        },
    )
    prepared = prepare_analysis_input_manifest(
        uncovered_documents,
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
        owner_type="REVIEW_PROGRAM_CLASSIFIER_POSITIVE_COVERAGE",
        owner_id=attempt.id,
        producer_kind="CORE",
        producer_run_id=run.id,
        actor_user_id=actor_user_id,
        lineage=[
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=item.prediction_artifact_id,
            )
            for item in scoring_runs
            if item.prediction_artifact_id is not None
        ],
    )
    summary = {
        "schema_version": 1,
        "state": "READY_TO_RUN" if uncovered_documents else "COMPLETED",
        "policy_version": "review-program-classifier-positive-coverage-v1",
        "task_version_id": str(task_version.id),
        "required_profiles": [
            {
                "id": str(profile.id),
                "key": profile.key,
                "name": profile.name,
                "decision_provider": profile.decision_provider,
                "content_hash": profile.content_hash,
            }
            for profile in profiles
        ],
        "scoring_run_ids": [str(item.id) for item in scoring_runs],
        "positive_document_count": len(positives),
        "already_covered_document_count": already_covered_count,
        "uncovered_document_count": len(uncovered_documents),
        "stale_or_partial_document_count": stale_source_count,
        "source_unavailable_document_count": source_unavailable_count,
        "estimated_qa_request_count": len(uncovered_documents) * len(profiles),
        "input_manifest_id": str(manifest.id),
        "input_manifest_content_hash": manifest.artifact_content_hash,
        "by_issue": [
            {
                "field_key": field_key,
                "class_key": class_key,
                "positive_document_count": counts["positive"],
                "uncovered_document_count": counts["uncovered"],
            }
            for (field_key, class_key), counts in sorted(issue_counts.items())
        ],
        "by_confidence_band": dict(sorted(confidence_counts.items())),
        "missing_by_profile": dict(sorted(missing_profile_counts.items())),
    }
    return manifest, summary


def _launch_classifier_positive_coverage(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    action: ReviewProgramAction,
) -> None:
    actor_user_id = action.resolved_by_user_id or run.initiated_by_user_id
    attempt.status = "RUNNING"
    run.status = "RUNNING"
    manifest, coverage = _prepare_classifier_positive_coverage(
        db,
        program=program,
        run=run,
        attempt=attempt,
        actor_user_id=actor_user_id,
    )
    matter = db.get(Matter, program.matter_id)
    task, task_version, profiles = _qa_task_records(db, run)
    if matter is None:
        raise ReviewProgramError("Review Program matter is unavailable")
    policy = dict(
        current_review_program_version(db, program).configuration_snapshot.get("classifier_positive_coverage_policy")
        or {}
    )
    batch_set = None
    if coverage["uncovered_document_count"]:
        try:
            batch_set = create_batch_set(
                db,
                matter=matter,
                task=task,
                task_version=task_version,
                name=f"{program.name} · classifier-positive Q&A coverage"[:200],
                description=("Frozen classifier-positive population lacking current results from both Q&A profiles."),
                population_source_type="INPUT_MANIFEST",
                source_review_batch_id=None,
                input_manifest_id=manifest.id,
                search=None,
                saved_search_id=None,
                saved_search_name=None,
                saved_search_updated_at=None,
                target_batch_size=int(policy.get("target_batch_size", 100)),
                keep_together=(["FAMILY"] if bool(policy.get("keep_families_together", True)) else []),
                random_seed=str(run.id),
                actor_user_id=actor_user_id,
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
                "task_version_id": str(task_version.id),
                "profile_ids": [str(profile.id) for profile in profiles],
                "coverage_policy_version": coverage["policy_version"],
                "scoring_run_ids": coverage["scoring_run_ids"],
            },
        }
        link_review_program_child(
            db,
            program=program,
            run=run,
            stage_attempt=attempt,
            purpose="CLASSIFIER_POSITIVE_COVERAGE_POPULATION",
            child_type="ANALYSIS_TASK_BATCH_SET",
            child_id=batch_set.id,
            derivation_key=f"classifier-positive-coverage-batch-set:{attempt.id}",
            batching_policy="PARTITIONED_SET",
            context={
                "input_manifest_id": str(manifest.id),
                "population_count": coverage["uncovered_document_count"],
                "positive_document_count": coverage["positive_document_count"],
                "already_covered_document_count": coverage["already_covered_document_count"],
            },
        )
        coverage["batch_set_id"] = str(batch_set.id)
        coverage["target_batch_size"] = batch_set.target_batch_size
        coverage["keep_families_together"] = bool(policy.get("keep_families_together", True))
        coverage["batch_set_run_ids"] = []
        coverage["state"] = "MATERIALIZING_BATCH_SET"
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=actor_user_id,
        configuration_stage="CLASSIFIER_POSITIVE_COVERAGE",
        configuration_updates={"classifier_positive_coverage": coverage},
        source_hash_updates={
            "classifier_positive_coverage_manifest_hash": manifest.artifact_content_hash,
            "classifier_positive_coverage_policy_hash": canonical_hash(
                {
                    "policy_version": coverage["policy_version"],
                    "required_profiles": coverage["required_profiles"],
                }
            ),
        },
    )
    coverage["program_version_id"] = str(version.id)
    run.progress_summary = {
        **run.progress_summary,
        "classifier_positive_coverage_state": coverage["state"],
        "classifier_positive_coverage": coverage,
    }
    attempt.result_summary = {
        **attempt.result_summary,
        "gate_choice": "RUN_COVERAGE",
        "action_id": str(action.id),
        "program_version_id": str(version.id),
        "input_manifest_id": str(manifest.id),
    }
    if batch_set is None:
        _complete_action_attempt(
            attempt,
            action,
            result={
                "classifier_positive_coverage": coverage,
                "program_version_id": str(version.id),
            },
        )
        _advance_after_stage(db, program=program, run=run, attempt=attempt)
        return
    enqueue_analysis_task_batch_set(db, batch_set.workflow_id, str(batch_set.id))


def _alignment_value_key(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _decision_result_issue_keys(result: ReviewDecisionResult) -> set[str]:
    selected: set[str] = set()
    for recommendation in (result.recommendations or {}).values():
        if not isinstance(recommendation, dict) or recommendation.get("matched") is not True:
            continue
        field_key = recommendation.get("metadata_definition_key")
        if not isinstance(field_key, str) or not field_key or "value" not in recommendation:
            continue
        selected.add(f"{field_key}:{_alignment_value_key(recommendation.get('value'))}")
    return selected


def _alignment_status(
    classifier_issues: set[str],
    left_issues: set[str] | None,
    right_issues: set[str] | None,
) -> str:
    if left_issues is None or right_issues is None:
        return "INCOMPLETE"
    if classifier_issues == left_issues == right_issues:
        return "EXACT_ALL"
    if left_issues == right_issues:
        return "QA_PAIR_EXACT"
    source_sets = (classifier_issues, left_issues, right_issues)
    if any(source_sets[index].intersection(source_sets[other]) for index in range(3) for other in range(index + 1, 3)):
        return "PARTIAL"
    return "DISAGREEMENT"


def _program_reviewed_document_ids(db: Session, *, run: ReviewProgramRun) -> set[uuid.UUID]:
    batch_set_ids = list(
        db.scalars(
            select(ReviewProgramChild.child_id).where(
                ReviewProgramChild.run_id == run.id,
                ReviewProgramChild.child_type == "ANALYSIS_TASK_BATCH_SET",
            )
        )
    )
    if not batch_set_ids:
        return set()
    return set(
        db.scalars(
            select(ReviewBatchDocument.matter_document_id).where(
                ReviewBatchDocument.analysis_task_batch_set_id.in_(batch_set_ids)
            )
        )
    )


def _human_adjudicated_issue_keys(
    db: Session,
    *,
    run: ReviewProgramRun,
    definitions: dict[uuid.UUID, MetadataDefinition],
) -> tuple[dict[uuid.UUID, set[str]], list[dict[str, Any]]]:
    resolution = dict((run.progress_summary or {}).get("training_label_resolution") or {})
    by_document: dict[uuid.UUID, set[str]] = {}
    sources: list[dict[str, Any]] = []
    for raw_source in resolution.get("adjudication_sources", []):
        if not isinstance(raw_source, dict):
            continue
        try:
            coding_id = uuid.UUID(str(raw_source["reviewer_coding_id"]))
            revision = int(raw_source["reviewer_coding_revision"])
            definition_id = uuid.UUID(str(raw_source["metadata_definition_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewProgramError("Human adjudication provenance is invalid") from exc
        definition = definitions.get(definition_id)
        if definition is None:
            raise ReviewProgramError("Human adjudication field is unavailable")
        resolved = resolve_reviewer_coding_as_of(
            db,
            reviewer_coding_id=coding_id,
            revision=revision,
            metadata_definition_ids=[definition_id],
        )
        document_ids = {item.matter_document_id for item in resolved.documents}
        for document_id in document_ids:
            by_document.setdefault(document_id, set())
        for value in resolved.values:
            selected = event_value(value)
            if selected is not None:
                by_document.setdefault(value.matter_document_id, set()).add(
                    f"{definition.key}:{_alignment_value_key(selected)}"
                )
        sources.append(
            {
                "reviewer_coding_id": str(coding_id),
                "reviewer_coding_revision": revision,
                "metadata_definition_id": str(definition_id),
                "document_count": len(document_ids),
            }
        )
    return by_document, sources


def _classifier_alignment_predictions(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    matter: Matter,
    actor_user_id: uuid.UUID,
) -> tuple[dict[uuid.UUID, dict[str, Any]], list[ClassificationScoringRun], set[str]]:
    scoring = dict((run.progress_summary or {}).get("whole_collection_scoring") or {})
    model_states = [item for item in scoring.get("models", []) if isinstance(item, dict)]
    predictions: dict[uuid.UUID, dict[str, Any]] = {}
    scoring_runs: list[ClassificationScoringRun] = []
    issue_universe: set[str] = set()
    for model_state in model_states:
        try:
            scoring_run = db.get(ClassificationScoringRun, uuid.UUID(str(model_state["scoring_run_id"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewProgramError("Classifier scoring provenance is invalid") from exc
        if scoring_run is None or scoring_run.matter_id != program.matter_id:
            raise ReviewProgramError("Classifier scoring output is unavailable")
        version = db.get(ClassificationModelVersion, scoring_run.model_version_id)
        configuration = (
            db.get(ClassificationModelConfiguration, version.configuration_id) if version is not None else None
        )
        if configuration is None:
            raise ReviewProgramError("Classifier scoring configuration is unavailable")
        field_key = model_state.get("field_key")
        if not isinstance(field_key, str) or not field_key:
            raise ReviewProgramError("Classifier target-field provenance is unavailable")
        if configuration.classification_strategy == "BINARY":
            positive_key = (configuration.binary_mapping or {}).get("positive_class_key")
            if not isinstance(positive_key, str) or not positive_key:
                raise ReviewProgramError("Binary classifier positive-class provenance is unavailable")
            issue_universe.add(f"{field_key}:{positive_key}")
        else:
            issue_universe.update(f"{field_key}:{class_key}" for class_key in version.trained_class_keys)
        preview = prediction_preview(
            scoring_run,
            matter=matter,
            actor_user_id=actor_user_id,
            offset=0,
            limit=scoring_run.population_document_count,
        )
        probability_ordinals = {
            class_key: ordinal for ordinal, class_key in enumerate(preview.ordered_class_keys)
        }
        for row in preview.items:
            prediction = predictions.setdefault(
                row.matter_document_id,
                {
                    "issues": set(),
                    "maximum_probability": None,
                    "maximum_candidate_probability": None,
                    "statuses": [],
                    "abstained": False,
                },
            )
            prediction["statuses"].append(row.result_status)
            prediction["abstained"] = bool(prediction["abstained"] or row.abstained)
            if row.result_status != "SUCCESS" or row.abstained:
                continue
            positive_keys = _classifier_positive_class_keys(configuration, list(row.predicted_class_keys))
            prediction["issues"].update(f"{field_key}:{class_key}" for class_key in positive_keys)
            candidate_keys = (
                [(configuration.binary_mapping or {}).get("positive_class_key")]
                if configuration.classification_strategy == "BINARY"
                else list(version.trained_class_keys)
            )
            candidate_probabilities = [
                float(row.probabilities[probability_ordinals[class_key]])
                for class_key in candidate_keys
                if isinstance(class_key, str)
                and class_key in probability_ordinals
                and probability_ordinals[class_key] < len(row.probabilities)
            ]
            if candidate_probabilities:
                candidate_probability = max(candidate_probabilities)
                current_candidate = prediction["maximum_candidate_probability"]
                prediction["maximum_candidate_probability"] = (
                    candidate_probability
                    if current_candidate is None
                    else max(float(current_candidate), candidate_probability)
                )
            selected_probabilities = [
                float(row.probabilities[probability_ordinals[class_key]])
                for class_key in positive_keys
                if class_key in probability_ordinals
                and probability_ordinals[class_key] < len(row.probabilities)
            ]
            if selected_probabilities:
                maximum_probability = max(selected_probabilities)
                current = prediction["maximum_probability"]
                prediction["maximum_probability"] = (
                    maximum_probability if current is None else max(float(current), maximum_probability)
                )
        scoring_runs.append(scoring_run)
    return predictions, scoring_runs, issue_universe


def _launch_final_alignment_report(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
) -> None:
    try:
        with db.begin_nested():
            _build_final_alignment_report(
                db,
                program=program,
                run=run,
                attempt=attempt,
            )
    except (
        AnalysisInputManifestError,
        ReviewProgramError,
        httpx.HTTPError,
        PermissionError,
        ValueError,
    ) as exc:
        fail_review_program_stage(
            db,
            program=program,
            run=run,
            error_code="FINAL_ALIGNMENT_FAILED",
            error_summary=str(exc),
            retriable=True,
        )


def _build_final_alignment_report(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
) -> None:
    matter = db.get(Matter, program.matter_id)
    if matter is None:
        raise ReviewProgramError("Review Program matter is unavailable")
    _, task_version, profiles = _qa_task_records(db, run)
    profile_ids = {profile.id for profile in profiles}
    results_by_document = _program_qa_results_by_profile(
        db,
        run=run,
        task_version_id=task_version.id,
        profile_ids=profile_ids,
    )
    classifier_predictions, scoring_runs, issue_universe = _classifier_alignment_predictions(
        db,
        program=program,
        run=run,
        matter=matter,
        actor_user_id=run.initiated_by_user_id,
    )
    definition_ids = [uuid.UUID(str(value)) for value in task_version.destination_metadata_definition_ids or []]
    definitions = {
        item.id: item
        for item in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter.id,
                MetadataDefinition.id.in_(definition_ids),
            )
        )
    }
    human_by_document, adjudication_sources = _human_adjudicated_issue_keys(
        db,
        run=run,
        definitions=definitions,
    )
    classifier_positive_ids = {
        document_id for document_id, prediction in classifier_predictions.items() if prediction["issues"]
    }
    population_ids = (
        classifier_positive_ids
        | set(results_by_document)
        | _program_reviewed_document_ids(db, run=run)
        | set(human_by_document)
    )
    documents_by_id = {
        item.id: item
        for item in db.scalars(
            select(MatterDocument).where(
                MatterDocument.matter_id == matter.id,
                MatterDocument.id.in_(list(population_ids)),
            )
        )
    }
    if len(documents_by_id) != len(population_ids):
        raise ReviewProgramError("Final alignment population contains documents outside the matter")
    try:
        source_snapshots = get_collection_item_analysis_input_snapshots(
            item_ids=[item.collection_item_id for item in documents_by_id.values()],
            actor_user_id=run.initiated_by_user_id,
            tenant_id=program.tenant_id,
            client_id=matter.client_id,
        )
    except (httpx.HTTPError, PermissionError, ValueError) as exc:
        raise ReviewProgramError(f"Final alignment source content is unavailable: {exc}") from exc

    training_labels = dict((run.progress_summary or {}).get("training_labels") or {})
    training_run_ids = {uuid.UUID(str(value)) for value in training_labels.get("label_run_ids", [])}
    alignment_counts = Counter()
    source_counts = Counter()
    authority_counts = Counter()
    human_alignment_counts = Counter()
    confidence_counts = Counter()
    family_ids: set[uuid.UUID] = set()
    family_document_count = 0
    record_type_counts: dict[str, Counter] = {}
    issue_counts: dict[str, Counter] = {issue_key: Counter() for issue_key in issue_universe}
    manifest_documents: list[AnalysisInputDocument] = []
    for document_id in sorted(population_ids, key=str):
        document = documents_by_id[document_id]
        source_snapshot = source_snapshots.get(document.collection_item_id)
        source = source_snapshot.text_source if source_snapshot is not None else None
        source_hash = source.content_hash if source is not None else None
        prediction = classifier_predictions.get(document_id) or {
            "issues": set(),
            "maximum_probability": None,
            "statuses": [],
            "abstained": False,
        }
        classifier_issues = set(prediction["issues"])
        document_results = results_by_document.get(document_id, {})
        current_results = {
            profile_id: result
            for profile_id, result in document_results.items()
            if source_hash is not None and result.source_content_hash == source_hash
        }
        profile_issue_sets = [
            _decision_result_issue_keys(current_results[profile.id])
            if profile.id in current_results
            else None
            for profile in profiles
        ]
        left_issues, right_issues = profile_issue_sets
        left_issues = left_issues.intersection(issue_universe) if left_issues is not None else None
        right_issues = right_issues.intersection(issue_universe) if right_issues is not None else None
        human_adjudicated = document_id in human_by_document
        human_issues = human_by_document.get(document_id, set()).intersection(issue_universe)
        status = _alignment_status(classifier_issues, left_issues, right_issues)
        alignment_counts[status] += 1
        record_type = source_snapshot.record_type if source_snapshot is not None else "UNAVAILABLE"
        record_type_counts.setdefault(record_type, Counter())[status] += 1
        family_id = source_snapshot.family_id if source_snapshot is not None else None
        if family_id is not None:
            family_ids.add(family_id)
            family_document_count += 1
        if source_hash is None:
            source_status = "SOURCE_UNAVAILABLE"
        elif len(current_results) == len(profiles):
            source_status = "CURRENT"
        elif document_results:
            source_status = "STALE_OR_PARTIAL"
        else:
            source_status = "MISSING_QA"
        source_counts[source_status] += 1
        result_run_ids = {result.review_batch_run_id for result in document_results.values()}
        training_overlap = bool(result_run_ids.intersection(training_run_ids))
        authority = "ADJUDICATED" if human_adjudicated else "SILVER" if training_overlap else "UNLABELED"
        authority_counts[authority] += 1
        if human_adjudicated:
            compared_sources = []
            if document_id in classifier_predictions:
                compared_sources.append(classifier_issues)
            if left_issues is not None:
                compared_sources.append(left_issues)
            if right_issues is not None:
                compared_sources.append(right_issues)
            human_alignment_counts[
                "EXACT" if compared_sources and all(source == human_issues for source in compared_sources) else "CONFLICT"
            ] += 1
        confidence_band = _coverage_confidence_band(prediction.get("maximum_probability"))
        confidence_counts[confidence_band] += 1
        for issue_key in classifier_issues | (left_issues or set()) | (right_issues or set()) | human_issues:
            counts = issue_counts.setdefault(issue_key, Counter())
            counts["classifier"] += issue_key in classifier_issues
            counts["profile_a"] += left_issues is not None and issue_key in left_issues
            counts["profile_b"] += right_issues is not None and issue_key in right_issues
            counts["qa_pair"] += (
                left_issues is not None
                and right_issues is not None
                and issue_key in left_issues
                and issue_key in right_issues
            )
            counts["all_three"] += (
                issue_key in classifier_issues
                and left_issues is not None
                and right_issues is not None
                and issue_key in left_issues
                and issue_key in right_issues
            )
            counts["classifier_only"] += (
                issue_key in classifier_issues
                and (left_issues is None or issue_key not in left_issues)
                and (right_issues is None or issue_key not in right_issues)
            )
            counts["profile_a_only"] += (
                left_issues is not None
                and issue_key in left_issues
                and issue_key not in classifier_issues
                and (right_issues is None or issue_key not in right_issues)
            )
            counts["profile_b_only"] += (
                right_issues is not None
                and issue_key in right_issues
                and issue_key not in classifier_issues
                and (left_issues is None or issue_key not in left_issues)
            )
            counts["human"] += issue_key in human_issues
            if human_adjudicated:
                compared_values = []
                if document_id in classifier_predictions:
                    compared_values.append(issue_key in classifier_issues)
                if left_issues is not None:
                    compared_values.append(issue_key in left_issues)
                if right_issues is not None:
                    compared_values.append(issue_key in right_issues)
                counts["human_conflict"] += bool(compared_values) and any(
                    value != (issue_key in human_issues) for value in compared_values
                )
        manifest_documents.append(
            AnalysisInputDocument(
                matter_document_id=document_id,
                collection_item_id=document.collection_item_id,
                source_collection_id=document.source_collection_id,
                scope_ordinal=len(manifest_documents) + 1,
                source_artifact_id=source.artifact_id if source is not None else None,
                source_content_hash=source_hash,
                resolved_inputs=(
                    {
                        "text_source": {
                            "role": source.role,
                            "media_type": source.media_type,
                            "original_filename": source.original_filename,
                            "byte_length": source.byte_length,
                            "record_type": source_snapshot.record_type,
                        }
                    }
                    if source is not None and source_snapshot is not None
                    else {}
                ),
                extensions={
                    "classifier_issue_keys": json.dumps(sorted(classifier_issues), separators=(",", ":")),
                    "profile_a_issue_keys": json.dumps(sorted(left_issues or set()), separators=(",", ":")),
                    "profile_b_issue_keys": json.dumps(sorted(right_issues or set()), separators=(",", ":")),
                    "human_issue_keys": json.dumps(sorted(human_issues), separators=(",", ":")),
                    "alignment_status": status,
                    "source_status": source_status,
                    "confidence_band": confidence_band,
                    "training_overlap": training_overlap,
                    "label_authority": authority,
                    "family_id": str(family_id) if family_id is not None else None,
                    "record_type": record_type,
                },
            )
        )
    contract = AnalysisInputContract(
        key="review-program.final-alignment",
        version=1,
        resolver_version="review-program-final-alignment-v1",
        extension_fields=[
            AnalysisInputExtensionField(name="classifier_issue_keys", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="profile_a_issue_keys", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="profile_b_issue_keys", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="human_issue_keys", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="alignment_status", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="source_status", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="confidence_band", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="training_overlap", type="BOOLEAN", nullable=False),
            AnalysisInputExtensionField(name="label_authority", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="family_id", type="STRING", nullable=True),
            AnalysisInputExtensionField(name="record_type", type="STRING", nullable=False),
        ],
        options={
            "review_program_id": str(program.id),
            "review_program_run_id": str(run.id),
            "stage_attempt_id": str(attempt.id),
            "task_version_id": str(task_version.id),
            "profile_ids": [str(profile.id) for profile in profiles],
            "policy_version": "review-program-final-alignment-v1",
        },
    )
    prepared = prepare_analysis_input_manifest(
        manifest_documents,
        contract=contract,
        scope_type="REVIEW_PROGRAM_STAGE",
        scope_id=attempt.id,
    )
    coverage_manifest_id = ((run.progress_summary or {}).get("classifier_positive_coverage") or {}).get(
        "input_manifest_id"
    )
    coverage_manifest = (
        db.get(AnalysisInputManifest, uuid.UUID(str(coverage_manifest_id))) if coverage_manifest_id else None
    )
    lineage_ids = [
        item.prediction_artifact_id for item in scoring_runs if item.prediction_artifact_id is not None
    ]
    if coverage_manifest is not None and coverage_manifest.artifact_id is not None:
        lineage_ids.append(coverage_manifest.artifact_id)
    manifest, _ = publish_analysis_input_manifest(
        db,
        prepared=prepared,
        tenant_id=program.tenant_id,
        client_id=matter.client_id,
        matter_id=matter.id,
        owner_type="REVIEW_PROGRAM_FINAL_ALIGNMENT",
        owner_id=attempt.id,
        producer_kind="CORE",
        producer_run_id=run.id,
        actor_user_id=run.initiated_by_user_id,
        lineage=[
            WorkflowArtifactLineageInput(source_kind="WORKFLOW_ARTIFACT", source_artifact_id=artifact_id)
            for artifact_id in dict.fromkeys(lineage_ids)
        ],
    )
    scoring_summary = dict((run.progress_summary or {}).get("whole_collection_scoring") or {})
    report = {
        "schema_version": 1,
        "state": "COMPLETED",
        "policy_version": "review-program-final-alignment-v1",
        "population_document_count": len(population_ids),
        "classifier_positive_document_count": len(classifier_positive_ids),
        "classifier_scoring_counts": dict(scoring_summary.get("aggregate_model_document_counts") or {}),
        "alignment_counts": dict(sorted(alignment_counts.items())),
        "source_coverage_counts": dict(sorted(source_counts.items())),
        "label_authority_counts": dict(sorted(authority_counts.items())),
        "human_alignment_counts": dict(sorted(human_alignment_counts.items())),
        "confidence_band_counts": dict(sorted(confidence_counts.items())),
        "family_summary": {
            "family_count": len(family_ids),
            "family_document_count": family_document_count,
            "ungrouped_document_count": len(population_ids) - family_document_count,
        },
        "record_type_alignment": [
            {"record_type": record_type, **dict(sorted(counts.items()))}
            for record_type, counts in sorted(record_type_counts.items())
        ],
        "issues": [
            {
                "issue_key": issue_key,
                "classifier": counts["classifier"],
                "profile_a": counts["profile_a"],
                "profile_b": counts["profile_b"],
                "qa_pair": counts["qa_pair"],
                "all_three": counts["all_three"],
                "classifier_only": counts["classifier_only"],
                "profile_a_only": counts["profile_a_only"],
                "profile_b_only": counts["profile_b_only"],
                "human": counts["human"],
                "human_conflict": counts["human_conflict"],
            }
            for issue_key, counts in sorted(issue_counts.items())
        ],
        "adjudication_sources": adjudication_sources,
        "task_version_id": str(task_version.id),
        "profile_ids": [str(profile.id) for profile in profiles],
        "scoring_run_ids": [str(item.id) for item in scoring_runs],
        "manifest_id": str(manifest.id),
        "manifest_content_hash": manifest.artifact_content_hash,
        "limitations": [
            "Agreement is consistency evidence, not an accuracy estimate.",
            "Record type is reported as a built-in cohort; no additional custom cohort policy was configured.",
        ],
    }
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=run.initiated_by_user_id,
        configuration_stage="NO_CODE_QC",
        configuration_updates={"final_alignment": report},
        source_hash_updates={"final_alignment_content_hash": canonical_hash(report)},
    )
    report["program_version_id"] = str(version.id)
    run.progress_summary = {
        **run.progress_summary,
        "final_alignment_state": "COMPLETED",
        "final_alignment": report,
    }
    complete_review_program_stage(
        db,
        program=program,
        run=run,
        result_summary=report,
        idempotency_key=f"final-alignment-terminal:{attempt.id}",
    )


def _deterministic_qc_order(document_id: uuid.UUID, *, seed: str) -> str:
    return hashlib.sha256(f"{seed}:{document_id}".encode()).hexdigest()


def _qc_cohort_sample(
    candidates: list[uuid.UUID],
    *,
    snapshots: dict[uuid.UUID, Any],
    documents_by_id: dict[uuid.UUID, MatterDocument],
    limit: int,
) -> list[uuid.UUID]:
    if limit <= 0:
        return []
    by_record_type: dict[str, list[uuid.UUID]] = {}
    for document_id in candidates:
        document = documents_by_id[document_id]
        snapshot = snapshots.get(document.collection_item_id)
        record_type = snapshot.record_type if snapshot is not None else "UNAVAILABLE"
        by_record_type.setdefault(record_type, []).append(document_id)
    selected: list[uuid.UUID] = []
    record_types = sorted(by_record_type)
    while record_types and len(selected) < limit:
        remaining: list[str] = []
        for record_type in record_types:
            values = by_record_type[record_type]
            if values and len(selected) < limit:
                selected.append(values.pop(0))
            if values:
                remaining.append(record_type)
        record_types = remaining
    return selected


def setup_review_program_negative_population_qc(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    action_id: uuid.UUID,
    actor: User,
    preview_hash: str,
    random_sample_size: int,
    near_threshold_sample_size: int,
    cohort_control_sample_size: int,
    mandatory_review_limit: int,
    target_batch_size: int,
    keep_families_together: bool,
) -> None:
    action = db.scalar(
        select(ReviewProgramAction)
        .where(ReviewProgramAction.id == action_id, ReviewProgramAction.run_id == run.id)
        .with_for_update()
    )
    if (
        action is None
        or action.action_type != "NEGATIVE_POPULATION_QC_SETUP"
        or action.stage_key != "NO_CODE_QC"
        or action.status != "OPEN"
        or run.pending_action_id != action.id
    ):
        raise ReviewProgramError("Negative-population QC setup action is unavailable")
    if action.preview_hash != preview_hash:
        raise ReviewProgramError("Negative-population QC preview is stale")
    matter = db.get(Matter, program.matter_id)
    if matter is None:
        raise ReviewProgramError("Review Program matter is unavailable")
    task, task_version, profiles = _qa_task_records(db, run)
    attempt = _current_stage_attempt(db, run, for_update=True)
    try:
        predictions, scoring_runs, issue_universe = _classifier_alignment_predictions(
            db,
            program=program,
            run=run,
            matter=matter,
            actor_user_id=actor.id,
        )
    except (ClassificationScoringError, PermissionError, ValueError) as exc:
        raise ReviewProgramError(f"Negative-population classifier results are unavailable: {exc}") from exc
    document_ids = set(predictions)
    documents_by_id = {
        item.id: item
        for item in db.scalars(
            select(MatterDocument).where(
                MatterDocument.matter_id == matter.id,
                MatterDocument.id.in_(list(document_ids)),
            )
        )
    }
    if len(documents_by_id) != len(document_ids):
        raise ReviewProgramError("Negative-population scoring frame contains documents outside the matter")
    try:
        source_snapshots = get_collection_item_analysis_input_snapshots(
            item_ids=[item.collection_item_id for item in documents_by_id.values()],
            actor_user_id=actor.id,
            tenant_id=program.tenant_id,
            client_id=matter.client_id,
        )
    except (httpx.HTTPError, PermissionError, ValueError) as exc:
        raise ReviewProgramError(f"Negative-population source content is unavailable: {exc}") from exc
    profile_ids = {profile.id for profile in profiles}
    results_by_document = _program_qa_results_by_profile(
        db,
        run=run,
        task_version_id=task_version.id,
        profile_ids=profile_ids,
        completed_only=False,
    )
    definitions = {
        item.id: item
        for item in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter.id,
                MetadataDefinition.id.in_(
                    [uuid.UUID(str(value)) for value in task_version.destination_metadata_definition_ids or []]
                ),
            )
        )
    }
    human_by_document, _ = _human_adjudicated_issue_keys(db, run=run, definitions=definitions)
    reviewed_ids = _program_reviewed_document_ids(db, run=run)
    eligible: list[uuid.UUID] = []
    mandatory: dict[uuid.UUID, list[str]] = {}
    excluded_positive_count = 0
    model_count = len(scoring_runs)
    for document_id in sorted(document_ids, key=str):
        prediction = predictions[document_id]
        document = documents_by_id[document_id]
        snapshot = source_snapshots.get(document.collection_item_id)
        source = snapshot.text_source if snapshot is not None else None
        source_hash = source.content_hash if source is not None else None
        reasons: list[str] = []
        statuses = list(prediction.get("statuses") or [])
        if len(statuses) != model_count or any(status != "SUCCESS" for status in statuses):
            reasons.append("CLASSIFIER_INCOMPLETE_OR_FAILED")
        if prediction.get("abstained"):
            reasons.append("CLASSIFIER_ABSTAINED")
        if source_hash is None:
            reasons.append("SOURCE_UNAVAILABLE")
        document_results = results_by_document.get(document_id, {})
        current_completed_results = {
            profile_id: result
            for profile_id, result in document_results.items()
            if result.status == "COMPLETED" and source_hash is not None and result.source_content_hash == source_hash
        }
        if document_results and len(current_completed_results) != len(document_results):
            reasons.append("QA_FAILED_PARTIAL_OR_STALE")
        if document_id in reviewed_ids and len(current_completed_results) != len(profiles):
            reasons.append("PRIOR_QA_COVERAGE_INCOMPLETE")
        q_and_a_issues = set().union(
            *(
                _decision_result_issue_keys(result).intersection(issue_universe)
                for result in current_completed_results.values()
            )
        ) if current_completed_results else set()
        human_issues = human_by_document.get(document_id, set()).intersection(issue_universe)
        if prediction["issues"] or q_and_a_issues or human_issues:
            excluded_positive_count += 1
            continue
        if reasons:
            mandatory[document_id] = sorted(set(reasons))
        else:
            eligible.append(document_id)

    seed = str(run.id)
    random_order = sorted(eligible, key=lambda value: _deterministic_qc_order(value, seed=seed))
    random_ids = random_order[:random_sample_size]
    selected = set(random_ids)
    remaining = [value for value in eligible if value not in selected]
    near_threshold_ids = sorted(
        remaining,
        key=lambda value: (
            -(float(predictions[value].get("maximum_candidate_probability"))
              if predictions[value].get("maximum_candidate_probability") is not None else -1.0),
            str(value),
        ),
    )[:near_threshold_sample_size]
    selected.update(near_threshold_ids)
    remaining = [value for value in remaining if value not in selected]
    cohort_ids = _qc_cohort_sample(
        sorted(remaining, key=str),
        snapshots=source_snapshots,
        documents_by_id=documents_by_id,
        limit=cohort_control_sample_size,
    )
    selected.update(cohort_ids)
    reviewable_mandatory_ids = [
        document_id
        for document_id in mandatory
        if (
            (snapshot := source_snapshots.get(documents_by_id[document_id].collection_item_id))
            is not None
            and snapshot.text_source is not None
        )
    ]
    mandatory_ids = sorted(
        reviewable_mandatory_ids,
        key=lambda value: (mandatory[value], _deterministic_qc_order(value, seed=f"{seed}:mandatory")),
    )[:mandatory_review_limit]
    lane_by_document = {
        **{value: "RANDOM_AUDIT" for value in random_ids},
        **{value: "NEAR_THRESHOLD" for value in near_threshold_ids},
        **{value: "COHORT_CONTROL" for value in cohort_ids},
        **{value: "MANDATORY_REVIEW" for value in mandatory_ids},
    }
    if not lane_by_document:
        raise ReviewProgramError("The Negative-population QC plan selected no documents")
    random_inclusion_probability = min(1.0, len(random_ids) / len(eligible)) if eligible else None
    manifest_documents: list[AnalysisInputDocument] = []
    for document_id in sorted(lane_by_document, key=lambda value: (lane_by_document[value], str(value))):
        document = documents_by_id[document_id]
        snapshot = source_snapshots.get(document.collection_item_id)
        source = snapshot.text_source if snapshot is not None else None
        lane = lane_by_document[document_id]
        manifest_documents.append(
            AnalysisInputDocument(
                matter_document_id=document_id,
                collection_item_id=document.collection_item_id,
                source_collection_id=document.source_collection_id,
                scope_ordinal=len(manifest_documents) + 1,
                source_artifact_id=source.artifact_id if source is not None else None,
                source_content_hash=source.content_hash if source is not None else None,
                resolved_inputs=(
                    {
                        "text_source": {
                            "role": source.role,
                            "media_type": source.media_type,
                            "original_filename": source.original_filename,
                            "byte_length": source.byte_length,
                            "record_type": snapshot.record_type,
                        }
                    }
                    if source is not None and snapshot is not None
                    else {}
                ),
                extensions={
                    "selection_lane": lane,
                    "classifier_risk_score": predictions[document_id].get("maximum_candidate_probability"),
                    "random_inclusion_probability": (
                        random_inclusion_probability if lane == "RANDOM_AUDIT" else None
                    ),
                    "sampling_weight": (
                        1.0 / random_inclusion_probability
                        if lane == "RANDOM_AUDIT" and random_inclusion_probability
                        else None
                    ),
                    "mandatory_reasons": json.dumps(mandatory.get(document_id, []), separators=(",", ":")),
                    "record_type": snapshot.record_type if snapshot is not None else "UNAVAILABLE",
                    "family_id": str(snapshot.family_id) if snapshot is not None and snapshot.family_id else None,
                },
            )
        )
    contract = AnalysisInputContract(
        key="review-program.negative-population-qc",
        version=1,
        resolver_version="review-program-negative-population-qc-v1",
        extension_fields=[
            AnalysisInputExtensionField(name="selection_lane", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="classifier_risk_score", type="FLOAT64", nullable=True),
            AnalysisInputExtensionField(name="random_inclusion_probability", type="FLOAT64", nullable=True),
            AnalysisInputExtensionField(name="sampling_weight", type="FLOAT64", nullable=True),
            AnalysisInputExtensionField(name="mandatory_reasons", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="record_type", type="STRING", nullable=False),
            AnalysisInputExtensionField(name="family_id", type="STRING", nullable=True),
        ],
        options={
            "review_program_id": str(program.id),
            "review_program_run_id": str(run.id),
            "stage_attempt_id": str(attempt.id),
            "task_version_id": str(task_version.id),
            "profile_ids": [str(profile.id) for profile in profiles],
            "policy_version": "review-program-negative-population-qc-v1",
            "random_seed": seed,
        },
    )
    try:
        prepared = prepare_analysis_input_manifest(
            manifest_documents,
            contract=contract,
            scope_type="REVIEW_PROGRAM_STAGE",
            scope_id=attempt.id,
        )
    except AnalysisInputManifestError as exc:
        raise ReviewProgramError(str(exc)) from exc
    lineage_ids = [
        item.prediction_artifact_id for item in scoring_runs if item.prediction_artifact_id is not None
    ]
    final_manifest_id = ((run.progress_summary or {}).get("final_alignment") or {}).get("manifest_id")
    final_manifest = (
        db.get(AnalysisInputManifest, uuid.UUID(str(final_manifest_id))) if final_manifest_id else None
    )
    if final_manifest is not None and final_manifest.artifact_id is not None:
        lineage_ids.append(final_manifest.artifact_id)
    try:
        manifest, _ = publish_analysis_input_manifest(
            db,
            prepared=prepared,
            tenant_id=program.tenant_id,
            client_id=matter.client_id,
            matter_id=matter.id,
            owner_type="REVIEW_PROGRAM_NEGATIVE_POPULATION_QC",
            owner_id=attempt.id,
            producer_kind="CORE",
            producer_run_id=run.id,
            actor_user_id=actor.id,
            lineage=[
                WorkflowArtifactLineageInput(
                    source_kind="WORKFLOW_ARTIFACT", source_artifact_id=artifact_id
                )
                for artifact_id in dict.fromkeys(lineage_ids)
            ],
        )
    except AnalysisInputManifestError as exc:
        raise ReviewProgramError(str(exc)) from exc
    try:
        batch_set = create_batch_set(
            db,
            matter=matter,
            task=task,
            task_version=task_version,
            name=f"{program.name} · Negative-population QC"[:200],
            description="Frozen random, targeted, and mandatory Negative-population QC sample.",
            population_source_type="INPUT_MANIFEST",
            source_review_batch_id=None,
            input_manifest_id=manifest.id,
            search=None,
            saved_search_id=None,
            saved_search_name=None,
            saved_search_updated_at=None,
            target_batch_size=target_batch_size,
            keep_together=["FAMILY"] if keep_families_together else [],
            random_seed=seed,
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
            "task_version_id": str(task_version.id),
            "profile_ids": [str(profile.id) for profile in profiles],
            "policy_version": "review-program-negative-population-qc-v1",
        },
    }
    link_review_program_child(
        db,
        program=program,
        run=run,
        stage_attempt=attempt,
        purpose="NEGATIVE_POPULATION_QC_POPULATION",
        child_type="ANALYSIS_TASK_BATCH_SET",
        child_id=batch_set.id,
        derivation_key=f"negative-population-qc-batch-set:{attempt.id}",
        batching_policy="PARTITIONED_SET",
        context={
            "input_manifest_id": str(manifest.id),
            "population_count": len(manifest_documents),
            "random_frame_count": len(eligible),
            "mandatory_queue_count": len(mandatory),
        },
    )
    lane_counts = Counter(lane_by_document.values())
    qc = {
        "schema_version": 1,
        "state": "MATERIALIZING_BATCH_SET",
        "policy_version": "review-program-negative-population-qc-v1",
        "input_manifest_id": str(manifest.id),
        "input_manifest_content_hash": manifest.artifact_content_hash,
        "batch_set_id": str(batch_set.id),
        "batch_set_run_ids": [],
        "task_version_id": str(task_version.id),
        "profile_ids": [str(profile.id) for profile in profiles],
        "scoring_run_ids": [str(item.id) for item in scoring_runs],
        "scoring_population_document_count": len(document_ids),
        "eligible_negative_frame_count": len(eligible),
        "excluded_positive_document_count": excluded_positive_count,
        "mandatory_queue_count": len(mandatory),
        "mandatory_automated_reviewable_count": len(reviewable_mandatory_ids),
        "mandatory_unreviewable_count": len(mandatory) - len(reviewable_mandatory_ids),
        "selected_document_count": len(manifest_documents),
        "selection_lane_counts": dict(sorted(lane_counts.items())),
        "random_inclusion_probability": random_inclusion_probability,
        "random_seed": seed,
        "target_batch_size": target_batch_size,
        "keep_families_together": keep_families_together,
    }
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=actor.id,
        configuration_stage="NO_CODE_QC",
        configuration_updates={
            "negative_population_qc_policy": {
                "random_sample_size": random_sample_size,
                "near_threshold_sample_size": near_threshold_sample_size,
                "cohort_control_sample_size": cohort_control_sample_size,
                "mandatory_review_limit": mandatory_review_limit,
                "target_batch_size": target_batch_size,
                "keep_families_together": keep_families_together,
            },
            "negative_population_qc": qc,
        },
        source_hash_updates={"negative_population_qc_manifest_hash": manifest.artifact_content_hash},
    )
    qc["program_version_id"] = str(version.id)
    run.progress_summary = {
        **run.progress_summary,
        "negative_population_qc_state": qc["state"],
        "negative_population_qc": qc,
    }
    resolve_review_program_action(
        db,
        program=program,
        run=run,
        action_id=action.id,
        actor=actor,
        preview_hash=preview_hash,
        choice_key="START_QC",
        reason="Negative-population QC sampling plan accepted",
        input_payload={
            "input_manifest_id": str(manifest.id),
            "batch_set_id": str(batch_set.id),
            "selected_document_count": len(manifest_documents),
        },
    )
    enqueue_analysis_task_batch_set(db, batch_set.workflow_id, str(batch_set.id))


def _classifier_training_embedding_job(
    db: Session,
    *,
    matter_id: uuid.UUID,
) -> MatterEmbeddingJob:
    jobs = list(
        db.scalars(
            select(MatterEmbeddingJob)
            .where(MatterEmbeddingJob.matter_id == matter_id)
            .order_by(MatterEmbeddingJob.created_at.desc(), MatterEmbeddingJob.id.desc())
        )
    )
    job = next((item for item in jobs if embedding_job_output_is_usable(item)), None)
    if job is None:
        raise ReviewProgramError("Classifier training requires a completed reusable matter embedding job")
    return job


def _classifier_label_policy(
    *,
    training_labels: dict[str, Any],
    metadata_definition_id: uuid.UUID,
) -> dict[str, Any]:
    try:
        label_run_ids = [uuid.UUID(str(value)) for value in training_labels.get("label_run_ids", [])]
    except (TypeError, ValueError) as exc:
        raise ReviewProgramError("Frozen classifier label-run provenance is invalid") from exc
    if not label_run_ids:
        raise ReviewProgramError("Frozen classifier label-run provenance is unavailable")
    allowed_sources: list[dict[str, Any]] = [
        {
            "kind": "ACCEPTED_JEV",
            "authority": "SILVER",
            "source_weight": 1.0,
            "source_reference_id": str(label_run_id),
        }
        for label_run_id in label_run_ids
    ]
    for source in training_labels.get("adjudication_sources", []):
        if str(source.get("metadata_definition_id")) != str(metadata_definition_id):
            continue
        allowed_sources.append(
            {
                "kind": "ADJUDICATED_REVIEW",
                "authority": "ADJUDICATED",
                "source_weight": 1.0,
                "source_reference_id": str(source["reviewer_coding_id"]),
                "source_revision": int(source["reviewer_coding_revision"]),
            }
        )
    return {
        "allowed_sources": allowed_sources,
        "missing_label_action": "EXCLUDE",
        "source_conflict_action": "EXCLUDE",
        "target_value_conflict_action": "EXCLUDE",
    }


def _classifier_configuration_payload(
    *,
    definition: MetadataDefinition,
    embedding_job: MatterEmbeddingJob,
    training_labels: dict[str, Any],
) -> ClassificationModelConfigurationCreate:
    class_keys = active_value_keys(definition)
    if definition.type == "BOOLEAN":
        strategy = ClassificationStrategy.BINARY
        ordered_class_keys = ["false", "true"]
        binary_mapping: dict[str, Any] | None = {
            "negative_class_key": "false",
            "positive_class_key": "true",
            "negative_value_keys": ["false"],
            "positive_value_keys": ["true"],
        }
        prediction_policy: dict[str, Any] = {"binary_positive_threshold": 0.5}
    elif definition.cardinality == "SINGLE":
        strategy = ClassificationStrategy.MULTICLASS
        ordered_class_keys = class_keys if definition.type == "ENUM" else []
        binary_mapping = None
        prediction_policy = {"multiclass_minimum_confidence": 0.5}
    else:
        strategy = ClassificationStrategy.ONE_VS_REST
        ordered_class_keys = class_keys if definition.type == "ENUM" else []
        binary_mapping = None
        prediction_policy = {
            "one_vs_rest_thresholds": (
                {class_key: 0.5 for class_key in ordered_class_keys} if ordered_class_keys else None
            )
        }
    if definition.type == "ENUM" and len(ordered_class_keys) < 2:
        raise ReviewProgramError(f"Classifier field {definition.display_name} must have at least two active values")
    return ClassificationModelConfigurationCreate.model_validate(
        {
            "classification_strategy": strategy.value,
            "ordered_class_keys": ordered_class_keys,
            "binary_mapping": binary_mapping,
            "label_policy": _classifier_label_policy(
                training_labels=training_labels,
                metadata_definition_id=definition.id,
            ),
            "feature_contract": {
                "dimensions": embedding_job.embedding_dimensions,
                "data_type": "FLOAT32",
                "normalization": (
                    FeatureNormalization.L2.value
                    if embedding_job.embedding_normalized
                    else FeatureNormalization.NONE.value
                ),
                "pooling": "MEAN",
                "embedding_model": embedding_job.embedding_model,
                "embedding_model_revision": embedding_job.embedding_model_revision,
                "text_source_policy": "active_text",
            },
            "prediction_resolution_policy": prediction_policy,
            "trainer_specification": {
                "kind": "sklearn_logistic_regression",
                "parameters": {
                    "regularization": "l2",
                    "c_values": [0.1, 1.0, 10.0],
                    "maximum_iterations": 1000,
                },
            },
        }
    )


def _launch_classifier_training(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
) -> None:
    training_labels = dict((run.progress_summary or {}).get("training_labels") or {})
    if training_labels.get("state") != "FROZEN" or not training_labels.get("content_hash"):
        raise ReviewProgramError("Classifier training labels have not been frozen")
    accepted_task = dict((run.progress_summary or {}).get("accepted_qa_task") or {})
    try:
        definition_ids = list(
            dict.fromkeys(
                uuid.UUID(str(value)) for value in accepted_task.get("destination_metadata_definition_ids", [])
            )
        )
    except (TypeError, ValueError) as exc:
        raise ReviewProgramError("Q&A destination-field provenance is invalid") from exc
    if not definition_ids:
        raise ReviewProgramError("Q&A destination-field provenance is unavailable")
    definitions_by_id = {
        definition.id: definition
        for definition in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == program.matter_id,
                MetadataDefinition.id.in_(definition_ids),
            )
        )
    }
    if set(definitions_by_id) != set(definition_ids):
        raise ReviewProgramError("One or more classifier target fields are unavailable")
    matter = db.get(Matter, program.matter_id)
    if matter is None:
        raise ReviewProgramError("Review Program matter is unavailable")
    embedding_job = _classifier_training_embedding_job(db, matter_id=matter.id)
    previous = dict((run.progress_summary or {}).get("classifier_training") or {})
    prior_models = {
        str(item.get("metadata_definition_id")): item
        for item in previous.get("models", [])
        if isinstance(item, dict) and item.get("metadata_definition_id")
    }
    model_states: list[dict[str, Any]] = []
    launched_jobs: list[ClassificationTrainingJob] = []
    try:
        for definition_id in definition_ids:
            definition = definitions_by_id[definition_id]
            prior = prior_models.get(str(definition.id)) or {}
            model = (
                db.get(ClassificationModel, uuid.UUID(str(prior["classification_model_id"])))
                if prior.get("classification_model_id")
                else None
            )
            configuration = (
                db.get(
                    ClassificationModelConfiguration,
                    uuid.UUID(str(prior["configuration_id"])),
                )
                if prior.get("configuration_id")
                else None
            )
            if model is None or model.matter_id != matter.id or model.target_metadata_definition_id != definition.id:
                model = create_classification_model(
                    db,
                    matter=matter,
                    actor_user_id=run.initiated_by_user_id,
                    payload=ClassificationModelCreate(
                        target_metadata_definition_id=definition.id,
                        name=f"{program.name} — {definition.display_name}"[:200],
                        description=(f"Classifier managed by First Pass Review program {program.name}."),
                    ),
                )
                configuration = None
            same_label_snapshot = previous.get("training_labels_content_hash") == training_labels["content_hash"]
            if not same_label_snapshot or configuration is None or configuration.classification_model_id != model.id:
                configuration = create_classification_model_configuration(
                    db,
                    matter=matter,
                    classification_model_id=model.id,
                    actor_user_id=run.initiated_by_user_id,
                    payload=_classifier_configuration_payload(
                        definition=definition,
                        embedding_job=embedding_job,
                        training_labels=training_labels,
                    ),
                )
            job, created = create_training_job(
                db,
                matter=matter,
                classification_model_id=model.id,
                payload=ClassificationTrainingJobCreate.model_validate(
                    {
                        "configuration_id": configuration.id,
                        "training_scope": {
                            "scope_type": "MATTER",
                            "scope_id": matter.id,
                        },
                        "embedding_job_id": embedding_job.id,
                        "validation_policy": {
                            "fold_count": 5,
                            "minimum_labeled_count": 20,
                            "minimum_labeled_count_per_class": 5,
                        },
                        "random_seed": 42,
                        "idempotency_key": (
                            f"review-program:{run.id}:classifier:{definition.id}:attempt:{attempt.attempt}"
                        ),
                    }
                ),
                actor_user_id=run.initiated_by_user_id,
                notification_parent_type="REVIEW_PROGRAM_RUN",
                notification_parent_id=run.id,
            )
            link_review_program_child(
                db,
                program=program,
                run=run,
                stage_attempt=attempt,
                purpose="CLASSIFIER_TRAINING",
                child_type="CLASSIFICATION_TRAINING_JOB",
                child_id=job.id,
                derivation_key=f"classifier-training:{attempt.id}:{definition.id}",
                batching_policy="NONE",
                context={
                    "metadata_definition_id": str(definition.id),
                    "classification_model_id": str(model.id),
                    "configuration_id": str(configuration.id),
                    "embedding_job_id": str(embedding_job.id),
                    "training_labels_content_hash": training_labels["content_hash"],
                },
            )
            prior_job_ids = [str(value) for value in prior.get("training_job_ids", [])]
            if str(job.id) not in prior_job_ids:
                prior_job_ids.append(str(job.id))
            model_states.append(
                {
                    "metadata_definition_id": str(definition.id),
                    "field_key": definition.key,
                    "field_name": definition.display_name,
                    "classification_model_id": str(model.id),
                    "configuration_id": str(configuration.id),
                    "current_training_job_id": str(job.id),
                    "training_job_ids": prior_job_ids,
                }
            )
            if created:
                launched_jobs.append(job)
    except (ClassificationModelCommandError, ClassificationTrainingJobError, ValueError) as exc:
        raise ReviewProgramError(str(exc)) from exc

    training_state = {
        "schema_version": 1,
        "state": "RUNNING",
        "embedding_job_id": str(embedding_job.id),
        "training_labels_content_hash": training_labels["content_hash"],
        "stage_attempt_id": str(attempt.id),
        "models": model_states,
    }
    _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=run.initiated_by_user_id,
        configuration_stage="CLASSIFIER_TRAINING",
        configuration_updates={"classifier_training": training_state},
        source_hash_updates={
            "classifier_training_labels_content_hash": training_labels["content_hash"],
            "classifier_embedding_configuration_hash": embedding_job.configuration_hash,
        },
    )
    run.progress_summary = {**run.progress_summary, "classifier_training": training_state}
    for job in launched_jobs:
        enqueue_classification_training_job(db, job.workflow_id, str(job.id))


def _classifier_evaluation_report(
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    version: ClassificationModelVersion,
    matter: Matter,
) -> ClassificationEvaluationReport | None:
    destination = io.BytesIO()
    try:
        copy_workflow_artifact_content(
            artifact_id=version.evaluation_artifact_id,
            destination=destination,
            actor_user_id=run.initiated_by_user_id,
            tenant_id=program.tenant_id,
            client_id=matter.client_id,
        )
        payload = destination.getvalue()
        if hashlib.sha256(payload).hexdigest() != version.evaluation_content_hash:
            return None
        report = ClassificationEvaluationReport.model_validate_json(payload)
    except (OSError, PermissionError, ValueError):
        return None
    if (
        report.training_run_id != version.training_run_id
        or report.classification_model_id != version.classification_model_id
        or report.configuration_id != version.configuration_id
    ):
        return None
    return report


def _classifier_quality_snapshot(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    jobs: list[ClassificationTrainingJob],
) -> dict[str, Any]:
    matter = db.get(Matter, program.matter_id)
    if matter is None:
        raise ReviewProgramError("Review Program matter is unavailable")
    models: list[dict[str, Any]] = []
    weak_category_count = 0
    weak_model_count = 0
    for job in jobs:
        training_run = (
            db.get(ClassificationTrainingRun, job.training_run_id) if job.training_run_id is not None else None
        )
        version = (
            db.scalar(
                select(ClassificationModelVersion).where(ClassificationModelVersion.training_run_id == training_run.id)
            )
            if training_run is not None
            else None
        )
        snapshot = (
            db.get(ClassificationDatasetSnapshot, job.dataset_snapshot_id)
            if job.dataset_snapshot_id is not None
            else None
        )
        model = db.get(ClassificationModel, job.classification_model_id)
        if version is None or snapshot is None or model is None:
            raise ReviewProgramError("Completed classifier training output is incomplete")
        if training_run is None:
            raise ReviewProgramError("Completed classifier training run is unavailable")
        report = _classifier_evaluation_report(
            program=program,
            run=run,
            version=version,
            matter=matter,
        )
        weak_categories: list[dict[str, Any]] = []
        if report is not None:
            for class_key in report.ordered_class_keys:
                metrics = report.per_class_metrics.get(class_key, {})
                reasons: list[str] = []
                positive_support = metrics.get("positive_support")
                pr_auc = metrics.get("pr_auc")
                f1 = metrics.get("f1")
                if isinstance(positive_support, (int, float)) and positive_support < 10:
                    reasons.append("LOW_VALIDATION_SUPPORT")
                if isinstance(pr_auc, (int, float)) and pr_auc < 0.7:
                    reasons.append("LOW_PR_AUC")
                if isinstance(f1, (int, float)) and f1 < 0.6:
                    reasons.append("LOW_F1")
                if reasons:
                    weak_categories.append(
                        {
                            "class_key": class_key,
                            "reasons": reasons,
                            "positive_support": positive_support,
                            "pr_auc": pr_auc,
                            "f1": f1,
                        }
                    )
        for excluded in snapshot.excluded_classes or []:
            weak_categories.append(
                {
                    "class_key": excluded.get("class_key"),
                    "reasons": [excluded.get("reason_code") or "EXCLUDED_FROM_TRAINING"],
                    "positive_support": excluded.get("feature_ready_positive_count"),
                    "pr_auc": None,
                    "f1": None,
                }
            )
        aggregate_reasons: list[str] = []
        primary_pr_auc = version.metric_summary.get("primary_pr_auc")
        macro_f1 = version.metric_summary.get("macro_f1")
        if isinstance(primary_pr_auc, (int, float)) and primary_pr_auc < 0.7:
            aggregate_reasons.append("LOW_PRIMARY_PR_AUC")
        if isinstance(macro_f1, (int, float)) and macro_f1 < 0.6:
            aggregate_reasons.append("LOW_MACRO_F1")
        if report is None:
            aggregate_reasons.append("DETAILED_EVALUATION_UNAVAILABLE")
        if version.warnings:
            aggregate_reasons.append("TRAINING_WARNINGS")
        quality = "WEAK" if aggregate_reasons or weak_categories else "ACCEPTABLE"
        weak_category_count += len(weak_categories)
        weak_model_count += quality == "WEAK"
        target = version.target_field_snapshot
        models.append(
            {
                "metadata_definition_id": str(model.target_metadata_definition_id),
                "field_key": target.get("stable_key"),
                "field_name": target.get("display_name"),
                "classification_model_id": str(model.id),
                "configuration_id": str(job.configuration_id),
                "training_job_id": str(job.id),
                "training_run_id": str(training_run.id),
                "model_version_id": str(version.id),
                "model_version_number": version.version_number,
                "quality": quality,
                "quality_reasons": aggregate_reasons,
                "metrics": version.metric_summary,
                "labeled_document_count": snapshot.labeled_count,
                "training_eligible_count": snapshot.training_eligible_count,
                "class_support": snapshot.class_support,
                "trained_class_keys": version.trained_class_keys,
                "excluded_classes": snapshot.excluded_classes,
                "weak_categories": weak_categories,
                "warnings": version.warnings,
            }
        )
    return {
        "schema_version": 1,
        "state": "COMPLETED",
        "quality_policy_version": "review-program-classifier-quality-v1",
        "quality_thresholds": {
            "primary_pr_auc": 0.7,
            "macro_f1": 0.6,
            "class_pr_auc": 0.7,
            "class_f1": 0.6,
            "class_positive_support": 10,
        },
        "model_count": len(models),
        "weak_model_count": weak_model_count,
        "weak_category_count": weak_category_count,
        "overall_quality": "WEAK" if weak_model_count else "ACCEPTABLE",
        "models": models,
    }


def project_review_program_classifier_training_terminal(
    db: Session,
    *,
    training_job: ClassificationTrainingJob,
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
            ReviewProgramChild.child_type == "CLASSIFICATION_TRAINING_JOB",
            ReviewProgramChild.child_id == training_job.id,
        )
    )
    if child is None:
        return None
    child.status = training_job.status
    child.context = {
        **child.context,
        "workflow_status": workflow.status,
        "dataset_snapshot_id": (str(training_job.dataset_snapshot_id) if training_job.dataset_snapshot_id else None),
        "training_run_id": (str(training_job.training_run_id) if training_job.training_run_id else None),
        "failure_code": training_job.failure_code,
        "failure_summary": training_job.failure_summary,
    }
    run = db.get(ReviewProgramRun, child.run_id)
    attempt = db.get(ReviewProgramStageAttempt, child.stage_attempt_id)
    program = db.get(ReviewProgram, run.program_id) if run is not None else None
    if run is None or attempt is None or program is None:
        raise ReviewProgramError("Review Program classifier-training link is incomplete")
    if (
        run.status in TERMINAL_RUN_STATUSES
        or run.current_stage != "CLASSIFIER_TRAINING"
        or attempt.id != _current_stage_attempt(db, run).id
    ):
        return None
    children = list(
        db.scalars(
            select(ReviewProgramChild)
            .where(
                ReviewProgramChild.stage_attempt_id == attempt.id,
                ReviewProgramChild.child_type == "CLASSIFICATION_TRAINING_JOB",
            )
            .order_by(ReviewProgramChild.created_at, ReviewProgramChild.id)
        )
    )
    jobs = [db.get(ClassificationTrainingJob, item.child_id) for item in children]
    if not jobs or any(item is None for item in jobs):
        return None
    resolved_jobs = [item for item in jobs if item is not None]
    terminal = {"COMPLETED", "FAILED", "CANCELED"}
    if any(item.status not in terminal for item in resolved_jobs):
        return None
    failed_jobs = [item for item in resolved_jobs if item.status != "COMPLETED"]
    if failed_jobs:
        return fail_review_program_stage(
            db,
            program=program,
            run=run,
            error_code="CLASSIFIER_TRAINING_FAILED",
            error_summary="; ".join(
                item.failure_summary or f"Classifier training job {item.id} {item.status.lower()}"
                for item in failed_jobs
            )[:4000],
            retriable=any(item.failure_retriable for item in failed_jobs),
        )
    quality = _classifier_quality_snapshot(
        db,
        program=program,
        run=run,
        jobs=resolved_jobs,
    )
    _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=run.initiated_by_user_id,
        configuration_stage="CLASSIFIER_REVIEW",
        configuration_updates={"classifier_results": quality},
        source_hash_updates={"classifier_results_hash": canonical_hash(quality)},
    )
    run.progress_summary = {
        **run.progress_summary,
        "classifier_training": {
            **dict((run.progress_summary or {}).get("classifier_training") or {}),
            "state": "COMPLETED",
        },
        "classifier_results": quality,
    }
    return complete_review_program_stage(
        db,
        program=program,
        run=run,
        result_summary=quality,
        idempotency_key=f"classifier-training-terminal:{attempt.id}",
    )


def project_review_program_classifier_scoring_terminal(
    db: Session,
    *,
    scoring_run: ClassificationScoringRun,
    workflow: WorkflowRun,
    review_program_run_id: uuid.UUID | None = None,
) -> ReviewProgramEvent | None:
    if review_program_run_id is None:
        if (
            workflow.notification_policy != "PARENT_MANAGED"
            or workflow.notification_parent_type != "REVIEW_PROGRAM_RUN"
            or workflow.notification_parent_id is None
        ):
            return None
        review_program_run_id = workflow.notification_parent_id
    child = db.scalar(
        select(ReviewProgramChild).where(
            ReviewProgramChild.run_id == review_program_run_id,
            ReviewProgramChild.child_type == "CLASSIFICATION_SCORING_RUN",
            ReviewProgramChild.child_id == scoring_run.id,
        )
    )
    if child is None:
        return None
    child.status = scoring_run.status
    child.context = {
        **child.context,
        "workflow_status": workflow.status,
        "population_document_count": scoring_run.population_document_count,
        "scored_document_count": scoring_run.scored_document_count,
        "abstained_document_count": scoring_run.abstained_document_count,
        "excluded_document_count": scoring_run.excluded_document_count,
        "failed_document_count": scoring_run.failed_document_count,
        "prediction_artifact_id": (
            str(scoring_run.prediction_artifact_id) if scoring_run.prediction_artifact_id is not None else None
        ),
        "prediction_content_hash": scoring_run.prediction_content_hash,
        "failure_code": scoring_run.failure_code,
        "failure_summary": scoring_run.failure_summary,
    }
    run = db.get(ReviewProgramRun, child.run_id)
    attempt = db.get(ReviewProgramStageAttempt, child.stage_attempt_id)
    program = db.get(ReviewProgram, run.program_id) if run is not None else None
    if run is None or attempt is None or program is None:
        raise ReviewProgramError("Review Program classifier-scoring link is incomplete")
    if (
        run.status in TERMINAL_RUN_STATUSES
        or run.current_stage != "WHOLE_COLLECTION_SCORING"
        or attempt.id != _current_stage_attempt(db, run).id
    ):
        return None
    children = list(
        db.scalars(
            select(ReviewProgramChild)
            .where(
                ReviewProgramChild.stage_attempt_id == attempt.id,
                ReviewProgramChild.purpose == "WHOLE_COLLECTION_SCORING",
                ReviewProgramChild.child_type == "CLASSIFICATION_SCORING_RUN",
            )
            .order_by(ReviewProgramChild.created_at, ReviewProgramChild.id)
        )
    )
    scoring_runs = [db.get(ClassificationScoringRun, item.child_id) for item in children]
    if not scoring_runs or any(item is None for item in scoring_runs):
        return None
    resolved_runs = [item for item in scoring_runs if item is not None]
    terminal_statuses = {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}
    if any(item.status not in terminal_statuses for item in resolved_runs):
        return None
    failed_runs = [item for item in resolved_runs if item.status in {"FAILED", "CANCELED"}]
    if failed_runs:
        return fail_review_program_stage(
            db,
            program=program,
            run=run,
            error_code="WHOLE_COLLECTION_SCORING_FAILED",
            error_summary="; ".join(
                item.failure_summary or f"Classifier scoring run {item.id} {item.status.lower()}"
                for item in failed_runs
            )[:4000],
            retriable=any(item.failure_retriable for item in failed_runs),
        )
    existing_models = {
        str(item.get("scoring_run_id")): item
        for item in (((run.progress_summary or {}).get("whole_collection_scoring") or {}).get("models") or [])
        if isinstance(item, dict)
    }
    models = [
        {
            **existing_models.get(str(item.id), {}),
            "scoring_run_id": str(item.id),
            "classification_model_id": str(item.classification_model_id),
            "model_version_id": str(item.model_version_id),
            "dataset_snapshot_id": str(item.dataset_snapshot_id),
            "status": item.status,
            "population_document_count": item.population_document_count,
            "scored_document_count": item.scored_document_count,
            "abstained_document_count": item.abstained_document_count,
            "excluded_document_count": item.excluded_document_count,
            "failed_document_count": item.failed_document_count,
            "prediction_artifact_id": str(item.prediction_artifact_id),
            "prediction_content_hash": item.prediction_content_hash,
            "result_manifest_artifact_id": str(item.result_manifest_artifact_id),
            "result_manifest_content_hash": item.result_manifest_content_hash,
        }
        for item in resolved_runs
    ]
    aggregate = {
        "population_document_count": sum(item.population_document_count for item in resolved_runs),
        "scored_document_count": sum(item.scored_document_count for item in resolved_runs),
        "abstained_document_count": sum(item.abstained_document_count for item in resolved_runs),
        "excluded_document_count": sum(item.excluded_document_count for item in resolved_runs),
        "failed_document_count": sum(item.failed_document_count for item in resolved_runs),
    }
    summary = {
        "schema_version": 1,
        "state": (
            "COMPLETED_WITH_ERRORS"
            if any(item.status == "COMPLETED_WITH_ERRORS" for item in resolved_runs)
            else "COMPLETED"
        ),
        "scope_type": "MATTER",
        "model_count": len(models),
        "collection_document_count": int(
            db.scalar(
                select(func.count()).select_from(MatterDocument).where(MatterDocument.matter_id == program.matter_id)
            )
            or 0
        ),
        "aggregate_model_document_counts": aggregate,
        "models": models,
    }
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=run.initiated_by_user_id,
        configuration_stage="CLASSIFIER_POSITIVE_COVERAGE",
        configuration_updates={"whole_collection_scoring": summary},
        source_hash_updates={"whole_collection_scoring_results_hash": canonical_hash(summary)},
    )
    run.progress_summary = {
        **run.progress_summary,
        "whole_collection_scoring": summary,
        "whole_collection_scoring_program_version_id": str(version.id),
    }
    return complete_review_program_stage(
        db,
        program=program,
        run=run,
        result_summary=summary,
        idempotency_key=f"whole-collection-scoring-terminal:{attempt.id}",
    )


def _finalize_classifier_positive_coverage(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    progress: dict[str, Any],
) -> ReviewProgramEvent | None:
    try:
        manifest_id = uuid.UUID(str(progress["input_manifest_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ReviewProgramError("Classifier-positive coverage manifest is unavailable") from exc
    manifest = db.get(AnalysisInputManifest, manifest_id)
    if manifest is None or manifest.matter_id != program.matter_id:
        raise ReviewProgramError("Classifier-positive coverage manifest is unavailable")
    _, task_version, profiles = _qa_task_records(db, run)
    try:
        rows = AnalysisInputManifestReader(
            manifest,
            actor_user_id=run.initiated_by_user_id,
        ).rows(0, manifest.item_count)
    except AnalysisInputManifestError as exc:
        raise ReviewProgramError(str(exc)) from exc
    profile_ids = {profile.id for profile in profiles}
    results_by_document = _program_qa_results_by_profile(
        db,
        run=run,
        task_version_id=task_version.id,
        profile_ids=profile_ids,
    )
    missing_profile_counts = Counter()
    unresolved_document_count = 0
    stale_document_count = 0
    for row in rows:
        try:
            document_id = uuid.UUID(str(row["matter_document_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewProgramError("Classifier-positive coverage manifest is invalid") from exc
        expected_source_hash = row.get("source_content_hash")
        profile_results = results_by_document.get(document_id, {})
        if expected_source_hash:
            current_profile_ids = {
                profile_id
                for profile_id, result in profile_results.items()
                if result.source_content_hash == expected_source_hash
            }
        else:
            result_hashes = {
                result.source_content_hash
                for profile_id, result in profile_results.items()
                if profile_id in profile_ids
            }
            current_profile_ids = set(profile_results).intersection(profile_ids) if len(result_hashes) == 1 else set()
        missing_profile_ids = profile_ids - current_profile_ids
        if missing_profile_ids:
            unresolved_document_count += 1
            stale_document_count += bool(profile_results)
            for profile in profiles:
                if profile.id in missing_profile_ids:
                    missing_profile_counts[profile.key] += 1
    summary = {
        **progress,
        "state": "COMPLETED" if not unresolved_document_count else "INCOMPLETE",
        "covered_by_coverage_run_document_count": manifest.item_count - unresolved_document_count,
        "remaining_uncovered_document_count": unresolved_document_count,
        "remaining_stale_or_partial_document_count": stale_document_count,
        "remaining_missing_by_profile": dict(sorted(missing_profile_counts.items())),
    }
    run.progress_summary = {
        **run.progress_summary,
        "classifier_positive_coverage_state": summary["state"],
        "classifier_positive_coverage": summary,
    }
    if unresolved_document_count:
        return fail_review_program_stage(
            db,
            program=program,
            run=run,
            error_code="CLASSIFIER_POSITIVE_COVERAGE_INCOMPLETE",
            error_summary=(
                f"{unresolved_document_count} classifier-positive document(s) still lack "
                "current results from both Q&A profiles"
            ),
            retriable=True,
        )
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=run.initiated_by_user_id,
        configuration_stage="FINAL_ALIGNMENT",
        configuration_updates={"classifier_positive_coverage": summary},
        source_hash_updates={"classifier_positive_coverage_results_hash": canonical_hash(summary)},
    )
    run.progress_summary = {
        **run.progress_summary,
        "classifier_positive_coverage": summary,
        "classifier_positive_coverage_program_version_id": str(version.id),
    }
    return complete_review_program_stage(
        db,
        program=program,
        run=run,
        result_summary=summary,
        idempotency_key=f"classifier-positive-coverage-terminal:{attempt.id}",
    )


def _wilson_interval(successes: int, total: int, *, z: float = 1.959963984540054) -> tuple[float, float] | None:
    if total <= 0:
        return None
    proportion = successes / total
    denominator = 1 + (z * z / total)
    center = (proportion + (z * z / (2 * total))) / denominator
    margin = (
        z
        * ((proportion * (1 - proportion) / total + z * z / (4 * total * total)) ** 0.5)
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _negative_population_qc_findings_document_ids(
    db: Session,
    *,
    run: ReviewProgramRun,
    manifest: AnalysisInputManifest,
) -> list[uuid.UUID]:
    _, task_version, profiles = _qa_task_records(db, run)
    try:
        rows = AnalysisInputManifestReader(manifest, actor_user_id=run.initiated_by_user_id).rows(
            0, manifest.item_count
        )
    except AnalysisInputManifestError as exc:
        raise ReviewProgramError(str(exc)) from exc
    results_by_document = _program_qa_results_by_profile(
        db,
        run=run,
        task_version_id=task_version.id,
        profile_ids={profile.id for profile in profiles},
        completed_only=False,
    )
    selected: list[uuid.UUID] = []
    for row in rows:
        try:
            document_id = uuid.UUID(str(row["matter_document_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewProgramError("Negative-population QC manifest is invalid") from exc
        expected_hash = row.get("source_content_hash")
        current = {
            profile_id: result
            for profile_id, result in results_by_document.get(document_id, {}).items()
            if result.status == "COMPLETED"
            and expected_hash is not None
            and result.source_content_hash == expected_hash
        }
        if len(current) != len(profiles):
            selected.append(document_id)
            continue
        issue_sets = [_decision_result_issue_keys(current[profile.id]) for profile in profiles]
        if set().union(*issue_sets) or issue_sets[0] != issue_sets[1]:
            selected.append(document_id)
    return list(dict.fromkeys(selected))


def _start_negative_population_qc_findings_review(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    action: ReviewProgramAction,
) -> None:
    qc = dict((run.progress_summary or {}).get("negative_population_qc") or {})
    if qc.get("state") != "REPORT_READY":
        raise ReviewProgramError("Negative-population QC report is not ready for findings review")
    try:
        manifest_id = uuid.UUID(str(qc["input_manifest_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ReviewProgramError("Negative-population QC manifest is unavailable") from exc
    manifest = db.get(AnalysisInputManifest, manifest_id)
    if manifest is None or manifest.matter_id != program.matter_id:
        raise ReviewProgramError("Negative-population QC manifest is unavailable")
    document_ids = _negative_population_qc_findings_document_ids(db, run=run, manifest=manifest)
    if not document_ids:
        raise ReviewProgramError("Negative-population QC has no positive, disputed, or unresolved findings to review")
    _, task_version, _ = _qa_task_records(db, run)
    try:
        definition_ids = list(
            dict.fromkeys(uuid.UUID(str(value)) for value in task_version.destination_metadata_definition_ids or [])
        )
    except (TypeError, ValueError) as exc:
        raise ReviewProgramError("Q&A destination-field provenance is invalid") from exc
    definitions = list(
        db.scalars(
            select(MetadataDefinition)
            .where(
                MetadataDefinition.matter_id == program.matter_id,
                MetadataDefinition.id.in_(definition_ids),
            )
            .order_by(MetadataDefinition.display_name, MetadataDefinition.id)
        )
    )
    if len(definitions) != len(definition_ids):
        raise ReviewProgramError("A Q&A destination field is unavailable")
    actor_user_id = action.resolved_by_user_id or run.initiated_by_user_id
    batch_id = uuid.uuid4()
    batch = ReviewBatch(
        id=batch_id,
        matter_id=program.matter_id,
        name=f"{program.name} · QC findings review"[:200],
        description="Ad hoc human review of Negative-population QC positives, disputes, and unresolved results.",
        selection_type="EXPLICIT_DOCUMENTS",
        selection_definition={
            "type": "EXPLICIT_DOCUMENTS",
            "review_program_id": str(program.id),
            "review_program_run_id": str(run.id),
            "stage_attempt_id": str(attempt.id),
            "batching_policy": "AD_HOC_BATCH",
            "purpose": "NEGATIVE_POPULATION_QC_FINDINGS",
            "input_manifest_id": str(manifest.id),
            "document_ids": [str(document_id) for document_id in document_ids],
            "candidate_count": len(document_ids),
            "metadata_definition_ids": [str(definition.id) for definition in definitions],
        },
        source_batch_id=None,
        assigned_user_id=program.owner_user_id,
        assigned_by_user_id=actor_user_id,
        assigned_at=utcnow(),
        reviewer_value_visibility="OWN_VALUES",
        status="QUEUED",
        search_status="NOT_CONFIGURED",
        workflow_id=f"review-batch:{batch_id}",
        created_by_user_id=actor_user_id,
    )
    db.add(batch)
    db.flush()
    group = ReviewBatchCodingGroup(
        review_batch_id=batch.id,
        source_metadata_group_id=None,
        display_name="QC findings review",
        description="Assign the final human coding for each QC finding.",
        sort_order=0,
    )
    db.add(group)
    db.flush()
    for ordinal, definition in enumerate(definitions):
        db.add(
            ReviewBatchCodingField(
                review_batch_coding_group_id=group.id,
                metadata_definition_id=definition.id,
                sort_order=ordinal,
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
        purpose="NEGATIVE_POPULATION_QC_FINDINGS_REVIEW",
        child_type="REVIEW_BATCH",
        child_id=batch.id,
        derivation_key=f"negative-population-qc-findings:{attempt.id}:{manifest.id}",
        batching_policy="AD_HOC_BATCH",
        context={
            "input_manifest_id": str(manifest.id),
            "candidate_count": len(document_ids),
            "assigned_user_id": str(program.owner_user_id),
        },
    )
    review = {
        "schema_version": 1,
        "state": "AWAITING_MANUAL_REVIEW",
        "review_batch_id": str(batch.id),
        "document_count": len(document_ids),
        "metadata_definition_ids": [str(definition.id) for definition in definitions],
        "assigned_user_id": str(program.owner_user_id),
        "started_by_user_id": str(actor_user_id),
        "started_at": action.resolved_at.isoformat() if action.resolved_at else None,
    }
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=actor_user_id,
        configuration_stage="NO_CODE_QC",
        configuration_updates={"negative_population_qc_findings_review": review},
        source_hash_updates={"negative_population_qc_findings_review_hash": canonical_hash(review)},
    )
    qc = {**qc, "state": "AWAITING_MANUAL_REVIEW", "findings_review": review}
    run.progress_summary = {
        **run.progress_summary,
        "negative_population_qc_state": "AWAITING_MANUAL_REVIEW",
        "negative_population_qc": qc,
    }
    attempt.status = "RUNNING"
    attempt.result_summary = {
        **attempt.result_summary,
        "findings_review": review,
        "program_version_id": str(version.id),
    }
    _create_action_for_stage(
        db,
        program=program,
        run=run,
        attempt=attempt,
        stage=_stage_for_run(program, run),
        template=_template_for_program(program),
    )
    enqueue_review_batch(db, execution.dbos_workflow_id, str(batch.id))


def _confirm_negative_population_qc_findings_review(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    action: ReviewProgramAction,
) -> None:
    qc = dict((run.progress_summary or {}).get("negative_population_qc") or {})
    review = dict(qc.get("findings_review") or {})
    if qc.get("state") != "AWAITING_MANUAL_REVIEW" or review.get("state") != "AWAITING_MANUAL_REVIEW":
        raise ReviewProgramError("QC findings review is not awaiting confirmation")
    try:
        batch_id = uuid.UUID(str(review["review_batch_id"]))
        definition_ids = [uuid.UUID(str(value)) for value in review.get("metadata_definition_ids", [])]
    except (KeyError, TypeError, ValueError) as exc:
        raise ReviewProgramError("QC findings-review provenance is invalid") from exc
    batch = db.get(ReviewBatch, batch_id)
    if batch is None or batch.matter_id != program.matter_id or batch.status != "READY":
        raise ReviewProgramError("QC findings-review batch is not ready")
    if batch.assigned_user_id is None:
        raise ReviewProgramError("QC findings-review batch has no assigned reviewer")
    coding = db.scalar(
        select(ReviewBatchReviewerCoding).where(
            ReviewBatchReviewerCoding.review_batch_id == batch.id,
            ReviewBatchReviewerCoding.reviewer_user_id == batch.assigned_user_id,
        )
    )
    if coding is None:
        raise ReviewProgramError("QC findings-review batch has not been reviewed")
    document_ids = list(
        db.scalars(
            select(ReviewBatchDocument.matter_document_id)
            .where(ReviewBatchDocument.review_batch_id == batch.id)
            .order_by(ReviewBatchDocument.sequence_number)
        )
    )
    if len(document_ids) != batch.document_count:
        raise ReviewProgramError("QC findings-review batch membership is incomplete")
    resolved = resolve_reviewer_coding_as_of(
        db,
        reviewer_coding_id=coding.id,
        revision=coding.revision,
        document_ids=document_ids,
        metadata_definition_ids=definition_ids,
    )
    completed_ids = {item.matter_document_id for item in resolved.documents if item.status == "COMPLETED"}
    if completed_ids != set(document_ids):
        raise ReviewProgramError(
            f"QC findings-review batch has {len(set(document_ids) - completed_ids)} unresolved documents"
        )
    reviewed_fields = {(item.matter_document_id, item.metadata_definition_id) for item in resolved.fields}
    expected_fields = {(document_id, definition_id) for document_id in document_ids for definition_id in definition_ids}
    if reviewed_fields != expected_fields:
        raise ReviewProgramError("QC findings-review batch has incomplete field coding")
    review = {
        **review,
        "state": "COMPLETED",
        "reviewer_coding_id": str(coding.id),
        "reviewer_coding_revision": coding.revision,
        "reviewer_user_id": str(coding.reviewer_user_id),
        "confirmed_by_user_id": str(action.resolved_by_user_id or run.initiated_by_user_id),
        "confirmed_at": action.resolved_at.isoformat() if action.resolved_at else None,
    }
    report = {**dict(qc.get("report") or {}), "human_findings_review": review}
    qc = {**qc, "state": "REPORT_READY", "report": report, "findings_review": review}
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=action.resolved_by_user_id or run.initiated_by_user_id,
        configuration_stage="NO_CODE_QC",
        configuration_updates={"negative_population_qc": qc},
        source_hash_updates={"negative_population_qc_human_review_hash": canonical_hash(review)},
    )
    run.progress_summary = {
        **run.progress_summary,
        "negative_population_qc_state": "REPORT_READY",
        "negative_population_qc": qc,
    }
    attempt.status = "RUNNING"
    attempt.result_summary = {
        **attempt.result_summary,
        "findings_review": review,
        "program_version_id": str(version.id),
    }
    _create_action_for_stage(
        db,
        program=program,
        run=run,
        attempt=attempt,
        stage=_stage_for_run(program, run),
        template=_template_for_program(program),
    )


def _finalize_negative_population_qc(
    db: Session,
    *,
    program: ReviewProgram,
    run: ReviewProgramRun,
    attempt: ReviewProgramStageAttempt,
    progress: dict[str, Any],
) -> None:
    try:
        manifest_id = uuid.UUID(str(progress["input_manifest_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ReviewProgramError("Negative-population QC manifest is unavailable") from exc
    manifest = db.get(AnalysisInputManifest, manifest_id)
    if manifest is None or manifest.matter_id != program.matter_id:
        raise ReviewProgramError("Negative-population QC manifest is unavailable")
    _, task_version, profiles = _qa_task_records(db, run)
    try:
        rows = AnalysisInputManifestReader(manifest, actor_user_id=run.initiated_by_user_id).rows(
            0, manifest.item_count
        )
    except AnalysisInputManifestError as exc:
        raise ReviewProgramError(str(exc)) from exc
    profile_ids = {profile.id for profile in profiles}
    results_by_document = _program_qa_results_by_profile(
        db,
        run=run,
        task_version_id=task_version.id,
        profile_ids=profile_ids,
        completed_only=False,
    )
    matter = db.get(Matter, program.matter_id)
    if matter is None:
        raise ReviewProgramError("Review Program matter is unavailable")
    _, _, issue_universe = _classifier_alignment_predictions(
        db,
        program=program,
        run=run,
        matter=matter,
        actor_user_id=run.initiated_by_user_id,
    )
    lane_counts: dict[str, Counter] = {}
    issue_counts = Counter()
    overall = Counter()
    representative_positive_document_ids: list[str] = []
    for row in rows:
        try:
            document_id = uuid.UUID(str(row["matter_document_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewProgramError("Negative-population QC manifest is invalid") from exc
        lane = str(row.get("selection_lane") or "UNSPECIFIED")
        counts = lane_counts.setdefault(lane, Counter())
        counts["sampled"] += 1
        expected_source_hash = row.get("source_content_hash")
        document_results = results_by_document.get(document_id, {})
        current = {
            profile_id: result
            for profile_id, result in document_results.items()
            if result.status == "COMPLETED"
            and expected_source_hash is not None
            and result.source_content_hash == expected_source_hash
        }
        if len(current) != len(profiles):
            counts["unresolved"] += 1
            overall["unresolved"] += 1
            continue
        issue_sets = [
            _decision_result_issue_keys(current[profile.id]).intersection(issue_universe)
            for profile in profiles
        ]
        left, right = issue_sets
        union = left | right
        counts["resolved"] += 1
        overall["resolved"] += 1
        if union:
            counts["positive"] += 1
            overall["positive"] += 1
            if len(representative_positive_document_ids) < 25:
                representative_positive_document_ids.append(str(document_id))
        if left == right:
            counts["exact"] += 1
            overall["exact"] += 1
        elif left.intersection(right):
            counts["partial"] += 1
            overall["partial"] += 1
        else:
            counts["disagreement"] += 1
            overall["disagreement"] += 1
        for issue_key in union:
            issue_counts[(issue_key, "union")] += 1
            issue_counts[(issue_key, "intersection")] += issue_key in left and issue_key in right
            issue_counts[(issue_key, "profile_a")] += issue_key in left
            issue_counts[(issue_key, "profile_b")] += issue_key in right
    random_counts = lane_counts.get("RANDOM_AUDIT", Counter())
    random_resolved = random_counts["resolved"]
    random_positive = random_counts["positive"]
    interval = _wilson_interval(random_positive, random_resolved)
    random_frame_count = int(progress.get("eligible_negative_frame_count") or 0)
    random_estimate = {
        "sampling_frame_count": random_frame_count,
        "sampled_count": random_counts["sampled"],
        "resolved_count": random_resolved,
        "positive_count": random_positive,
        "observed_positive_rate": random_positive / random_resolved if random_resolved else None,
        "confidence_level": 0.95 if interval is not None else None,
        "wilson_interval": list(interval) if interval is not None else None,
        "estimated_positive_document_count": (
            random_frame_count * random_positive / random_resolved if random_resolved else None
        ),
        "inclusion_probability": progress.get("random_inclusion_probability"),
        "estimator": "SIMPLE_RANDOM_SAMPLE_WITHOUT_REPLACEMENT",
    }
    report = {
        "schema_version": 1,
        "state": "COMPLETED",
        "policy_version": "review-program-negative-population-qc-report-v1",
        "selected_document_count": manifest.item_count,
        "resolved_document_count": overall["resolved"],
        "unresolved_document_count": overall["unresolved"],
        "positive_document_count": overall["positive"],
        "exact_q_and_a_count": overall["exact"],
        "partial_q_and_a_count": overall["partial"],
        "disagreement_q_and_a_count": overall["disagreement"],
        "mandatory_queue_count": int(progress.get("mandatory_queue_count") or 0),
        "mandatory_unreviewable_count": int(progress.get("mandatory_unreviewable_count") or 0),
        "lanes": [
            {"lane": lane, **{key: counts[key] for key in ("sampled", "resolved", "unresolved", "positive", "exact", "partial", "disagreement")}}
            for lane, counts in sorted(lane_counts.items())
        ],
        "random_audit_estimate": random_estimate,
        "issues": [
            {
                "issue_key": issue_key,
                "union_count": issue_counts[(issue_key, "union")],
                "intersection_count": issue_counts[(issue_key, "intersection")],
                "profile_a_count": issue_counts[(issue_key, "profile_a")],
                "profile_b_count": issue_counts[(issue_key, "profile_b")],
            }
            for issue_key in sorted(issue_universe)
        ],
        "representative_positive_document_ids": representative_positive_document_ids,
        "input_manifest_id": str(manifest.id),
        "input_manifest_content_hash": manifest.artifact_content_hash,
        "batch_set_id": progress.get("batch_set_id"),
        "batch_set_run_ids": progress.get("batch_set_run_ids", []),
        "profile_runs": progress.get("profile_runs", []),
        "limitations": [
            "Only the random-audit lane supports a collection-level miss-rate estimate.",
            "Targeted-lane positive yield must not be interpreted as a population error rate.",
            "Agreement between Q&A profiles is consistency evidence, not ground-truth accuracy.",
        ],
    }
    qc = {**progress, "state": "REPORT_READY", "report": report}
    version = _create_review_program_version(
        db,
        program=program,
        run=run,
        actor_user_id=run.initiated_by_user_id,
        configuration_stage="NO_CODE_QC",
        configuration_updates={"negative_population_qc": qc},
        source_hash_updates={"negative_population_qc_report_hash": canonical_hash(report)},
    )
    qc["program_version_id"] = str(version.id)
    run.progress_summary = {
        **run.progress_summary,
        "negative_population_qc_state": "REPORT_READY",
        "negative_population_qc": qc,
    }
    attempt.result_summary = {"negative_population_qc": qc}
    _create_action_for_stage(
        db,
        program=program,
        run=run,
        attempt=attempt,
        stage=_stage_for_run(program, run),
        template=_template_for_program(program),
    )


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
    profile_stage = {
        "DISCOVERY_QA_PROFILE": (
            "DISCOVERY_QA",
            "DISCOVERY_QA_PROFILE_FAILED",
            "discovery_qa",
        ),
        "CLASSIFIER_IMPROVEMENT_QA_PROFILE": (
            "CLASSIFIER_IMPROVEMENT",
            "CLASSIFIER_IMPROVEMENT_QA_PROFILE_FAILED",
            "classifier_improvement",
        ),
        "CLASSIFIER_POSITIVE_COVERAGE_QA_PROFILE": (
            "CLASSIFIER_POSITIVE_COVERAGE",
            "CLASSIFIER_POSITIVE_COVERAGE_QA_PROFILE_FAILED",
            "classifier_positive_coverage",
        ),
        "NEGATIVE_POPULATION_QC_QA_PROFILE": (
            "NO_CODE_QC",
            "NEGATIVE_POPULATION_QC_QA_PROFILE_FAILED",
            "negative_population_qc",
        ),
    }.get(child.purpose)
    if profile_stage is None:
        return None
    stage_key, failure_code, progress_key = profile_stage
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
        raise ReviewProgramError("Review Program Q&A profile link is incomplete")
    recovering_failed_profile = (
        run.status == "FAILED"
        and run.failure_code == failure_code
        and run.current_stage == stage_key
        and attempt.id == _current_stage_attempt(db, run).id
    )
    if (
        (run.status in TERMINAL_RUN_STATUSES and not recovering_failed_profile)
        or run.current_stage != stage_key
        or attempt.id != _current_stage_attempt(db, run).id
    ):
        return None
    profile_children = list(
        db.scalars(
            select(ReviewProgramChild)
            .where(
                ReviewProgramChild.run_id == run.id,
                ReviewProgramChild.stage_attempt_id == attempt.id,
                ReviewProgramChild.purpose == child.purpose,
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
        item.status in {"FAILED", "CANCELED"} or item.failed_child_count > 0 for item in resolved_runs
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
            error_code=failure_code,
            error_summary=f"One or more {stage_key.replace('_', ' ').title()} profile runs failed",
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

    progress = dict((run.progress_summary or {}).get(progress_key) or {})
    progress["profile_runs"] = [
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
        f"{progress_key}_state": "COMPLETED",
        progress_key: progress,
    }
    if stage_key == "CLASSIFIER_POSITIVE_COVERAGE":
        return _finalize_classifier_positive_coverage(
            db,
            program=program,
            run=run,
            attempt=attempt,
            progress=progress,
        )
    if stage_key == "NO_CODE_QC":
        _finalize_negative_population_qc(
            db,
            program=program,
            run=run,
            attempt=attempt,
            progress=progress,
        )
        return None
    if stage_key == "CLASSIFIER_IMPROVEMENT":
        attempt.status = "COMPLETED"
        attempt.completed_at = utcnow()
        attempt.result_summary = {"classifier_improvement": progress}
        completed = list((run.progress_summary or {}).get("completed_stages", []))
        if stage_key not in completed:
            completed.append(stage_key)
        run.progress_summary = {
            **run.progress_summary,
            "completed_stages": completed,
            "last_completed_stage": stage_key,
        }
        accumulated_profile_runs = _completed_discovery_profile_runs(db, run=run)
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
        _activate_stage_by_key(
            db,
            program=program,
            run=run,
            stage_key="ISSUE_SUPPORT_DECISION",
        )
        return None

    complete_review_program_stage(
        db,
        program=program,
        run=run,
        result_summary={"discovery_qa": progress},
        idempotency_key=f"discovery-qa-terminal:{attempt.id}",
    )
    if run.current_stage != "DISCOVERY_ANALYSIS" or run.status != "RUNNING":
        return None
    all_profile_child_ids = list(
        db.scalars(
            select(ReviewProgramChild.child_id).where(
                ReviewProgramChild.run_id == run.id,
                ReviewProgramChild.purpose.in_({"DISCOVERY_QA_PROFILE", "CLASSIFIER_IMPROVEMENT_QA_PROFILE"}),
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
    if action.action_type == "QA_TASK_SETUP" and choice_key != "STOP" and not input_payload.get("task_id"):
        raise ReviewProgramError("Use the Q&A task setup command for this choice")
    if action.action_type == "DISCOVERY_QA_SETUP" and choice_key != "STOP" and not input_payload.get("batch_set_id"):
        raise ReviewProgramError("Use the Discovery Q&A setup command for this choice")
    if (
        action.action_type == "CLASSIFIER_IMPROVEMENT_SETUP"
        and choice_key != "STOP"
        and not input_payload.get("batch_set_id")
    ):
        raise ReviewProgramError("Use the classifier improvement setup command for this choice")
    if (
        action.action_type == "NEGATIVE_POPULATION_QC_SETUP"
        and choice_key != "STOP"
        and not input_payload.get("batch_set_id")
    ):
        raise ReviewProgramError("Use the Negative-population QC setup command for this choice")
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
                ~MatterDefinitionAssessmentRun.status.in_({"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}),
            )
        )
    )
    return [(assessment, db.get(WorkflowRun, assessment.workflow_run_id)) for assessment in assessments]


def cancel_review_program_discovery_children(
    db: Session,
    *,
    run: ReviewProgramRun,
    actor_user_id: uuid.UUID,
) -> tuple[list[str], list[str], list[str], list[str], list[str], list[str]]:
    batch_set_workflow_ids: list[str] = []
    batch_run_workflow_ids: list[str] = []
    child_workflow_ids: list[str] = []
    review_batch_workflow_ids: list[str] = []
    classification_training_workflow_ids: list[str] = []
    classification_scoring_workflow_ids: list[str] = []
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
        elif child.child_type == "CLASSIFICATION_TRAINING_JOB":
            training_job = db.get(ClassificationTrainingJob, child.child_id)
            if training_job is None or training_job.status not in {
                "QUEUED",
                "SNAPSHOTTING",
                "PREPARING_FEATURES",
                "LAUNCHING",
                "TRAINING",
                "FINALIZING",
            }:
                continue
            execution, canceled = cancel_job_execution(
                db,
                job_type=CLASSIFICATION_TRAINING_JOB_TYPE,
                job_id=training_job.id,
                actor_user_id=actor_user_id,
            )
            if canceled:
                child.status = "CANCELED"
                classification_training_workflow_ids.append(execution.dbos_workflow_id)
        elif child.child_type == "CLASSIFICATION_SCORING_RUN":
            scoring_run = db.get(ClassificationScoringRun, child.child_id)
            if scoring_run is None or scoring_run.status not in {
                "QUEUED",
                "VALIDATING",
                "WAITING_FOR_RUNTIME",
                "SCORING",
                "PUBLISHING",
                "FINALIZING",
                "RECONCILING",
            }:
                continue
            execution, canceled = cancel_job_execution(
                db,
                job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
                job_id=scoring_run.id,
                actor_user_id=actor_user_id,
            )
            if canceled:
                child.status = "CANCELED"
                classification_scoring_workflow_ids.append(execution.dbos_workflow_id)
    return (
        batch_set_workflow_ids,
        batch_run_workflow_ids,
        child_workflow_ids,
        review_batch_workflow_ids,
        classification_training_workflow_ids,
        classification_scoring_workflow_ids,
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
    if (
        run.status == "WAITING_FOR_USER"
        and run.current_stage == "DEFINITION_ACCEPTANCE"
        and (run.progress_summary or {}).get("final_unassessed_refinement")
    ):
        refinement_actions = list(
            db.scalars(
                select(ReviewProgramAction)
                .where(
                    ReviewProgramAction.run_id == run.id,
                    ReviewProgramAction.stage_key == "DEFINITION_REFINEMENT",
                    ReviewProgramAction.status == "RESOLVED",
                )
                .order_by(ReviewProgramAction.resolved_at.desc(), ReviewProgramAction.id.desc())
            )
        )
        source_action = next(
            (
                action
                for action in refinement_actions
                if ((action.resolution_payload or {}).get("input") or {}).get("final_round_decision")
                == "ACCEPT_REFINEMENT"
            ),
            None,
        )
        if source_action is not None:
            _carry_forward_final_refinement_acceptance(
                db,
                program=program,
                run=run,
                source_action=source_action,
            )
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
    if run.status == "RUNNING" and run.current_stage == "WHOLE_COLLECTION_SCORING":
        current_attempt = _current_stage_attempt(db, run, for_update=True)
        scoring_children = list(
            db.scalars(
                select(ReviewProgramChild).where(
                    ReviewProgramChild.stage_attempt_id == current_attempt.id,
                    ReviewProgramChild.child_type == "CLASSIFICATION_SCORING_RUN",
                )
            )
        )
        for child in scoring_children:
            scoring_run = db.get(ClassificationScoringRun, child.child_id)
            workflow = (
                current_job_execution(
                    db,
                    job_type=CLASSIFICATION_SCORING_RUN_JOB_TYPE,
                    job_id=scoring_run.id,
                )
                if scoring_run is not None
                else None
            )
            if (
                scoring_run is not None
                and workflow is not None
                and scoring_run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}
            ):
                project_review_program_classifier_scoring_terminal(
                    db,
                    scoring_run=scoring_run,
                    workflow=workflow,
                    review_program_run_id=run.id,
                )
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
        raise ReviewProgramError(f"Review Program stage requires {expected} batching, not {batching_policy}")
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
    if action.status == "OPEN":
        if action.stage_key in {"MATTER_DEFINITION", "DEFINITION_REFINEMENT", "DEFINITION_ACCEPTANCE"}:
            program = db.get(ReviewProgram, run.program_id)
            if program is not None:
                context.update(_definition_stage_context(db, program=program, run=run))
        elif action.stage_key in {"DISAGREEMENT_REVIEW", "TRAINING_LABEL_FINALIZATION"}:
            context.update(_training_label_stage_context(db, run=run))
    return context
