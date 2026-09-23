import hashlib
import random
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import Settings
from app.models import (
    Matter,
    MatterDefinition,
    MatterDefinitionAssessmentCandidate,
    MatterDefinitionAssessmentRun,
    MatterDefinitionRevision,
    MatterDocument,
    ReviewBatch,
    ReviewBatchDocument,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    SearchIndexGeneration,
    WorkflowRun,
    WorkflowStepRun,
)
from app.workflow_specs import MATTER_DEFINITION_ASSESSMENT_SPEC, binding_snapshot, resolve_workflow_skill_bindings
from app.workflows.dispatcher import (
    enqueue_definition_assessment,
    enqueue_definition_assessment_reanalysis,
    enqueue_definition_assessment_synthesis,
)

RRF_CONSTANT = 60
MERGE_ALGORITHM_VERSION = "assessment-rrf-v1"


class AssessmentError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RetrievalHit:
    document_id: uuid.UUID
    query_ordinal: int
    criterion_key: str
    rank: int
    score: float | None
    best_passage: dict[str, Any] | None = None


@dataclass(frozen=True)
class SelectedCandidate:
    document_id: uuid.UUID
    fused_score: float
    provenance: tuple[dict[str, Any], ...]
    reason: str


def merge_retrieval_candidates(
    hits: list[RetrievalHit],
    *,
    maximum_document_count: int,
    query_quotas: dict[int, int],
    control_document_ids: list[uuid.UUID],
    control_sample_size: int,
    seed: str,
) -> tuple[list[SelectedCandidate], list[SelectedCandidate]]:
    """Deduplicate and deterministically select candidates using reciprocal-rank fusion."""

    provenance: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    fused: dict[uuid.UUID, float] = defaultdict(float)
    per_query: dict[int, list[RetrievalHit]] = defaultdict(list)
    for hit in sorted(hits, key=lambda item: (item.query_ordinal, item.rank, str(item.document_id))):
        per_query[hit.query_ordinal].append(hit)
        fused[hit.document_id] += 1.0 / (RRF_CONSTANT + hit.rank)
        provenance[hit.document_id].append(
            {
                "query_ordinal": hit.query_ordinal,
                "criterion_key": hit.criterion_key,
                "rank": hit.rank,
                "score": hit.score,
                "best_passage": hit.best_passage,
            }
        )

    selected_ids: list[uuid.UUID] = []
    selected_set: set[uuid.UUID] = set()
    reasons: dict[uuid.UUID, str] = {}
    for query_ordinal in sorted(per_query):
        quota = max(0, query_quotas.get(query_ordinal, 0))
        for hit in per_query[query_ordinal]:
            if len([value for value in selected_ids if any(
                item["query_ordinal"] == query_ordinal for item in provenance[value]
            )]) >= quota:
                break
            if hit.document_id not in selected_set and len(selected_ids) < maximum_document_count:
                selected_ids.append(hit.document_id)
                selected_set.add(hit.document_id)
                reasons[hit.document_id] = "CRITERION_QUOTA"

    ranked_ids = sorted(fused, key=lambda value: (-fused[value], str(value)))
    for document_id in ranked_ids:
        if len(selected_ids) >= maximum_document_count:
            break
        if document_id not in selected_set:
            selected_ids.append(document_id)
            selected_set.add(document_id)
            reasons[document_id] = "RANK_FUSION"

    available_controls = sorted(set(control_document_ids) - set(provenance), key=str)
    rng = random.Random(seed)
    rng.shuffle(available_controls)
    reserved_controls = min(control_sample_size, maximum_document_count)
    controls = available_controls[:reserved_controls]
    if controls:
        keep_retrieved = max(0, maximum_document_count - len(controls))
        selected_ids = selected_ids[:keep_retrieved]
        selected_set = set(selected_ids)
        for document_id in controls:
            selected_ids.append(document_id)
            selected_set.add(document_id)
            reasons[document_id] = "CONTROL_SAMPLE"
            provenance[document_id] = [{"control_sample": True}]
            fused[document_id] = 0.0

    selected = [
        SelectedCandidate(
            document_id=value,
            fused_score=fused[value],
            provenance=tuple(provenance[value]),
            reason=reasons[value],
        )
        for value in selected_ids
    ]
    all_candidates = [
        SelectedCandidate(
            document_id=value,
            fused_score=fused[value],
            provenance=tuple(provenance[value]),
            reason=reasons.get(value, "NOT_SELECTED"),
        )
        for value in sorted(provenance, key=lambda item: (-fused[item], str(item)))
    ]
    return selected, all_candidates


def start_assessment(
    db: Session,
    *,
    matter: Matter,
    initiated_by_user_id: uuid.UUID,
    settings: Settings,
    name: str | None = None,
    revision_number: int | None = None,
    maximum_document_count: int = 500,
    control_sample_size: int = 0,
    acknowledge_large_run_warning: bool = False,
) -> MatterDefinitionAssessmentRun:
    if matter.status != "ACTIVE":
        raise AssessmentError("Matter is not active")
    if maximum_document_count <= 0:
        raise AssessmentError("Maximum document count must be positive")
    if control_sample_size < 0 or control_sample_size > maximum_document_count:
        raise AssessmentError("Control sample size must not exceed the maximum document count")
    warning_required = maximum_document_count > settings.definition_assessment_warning_document_count
    if warning_required and not acknowledge_large_run_warning:
        raise AssessmentError(
            f"Assessments over {settings.definition_assessment_warning_document_count} documents require "
            "large-run warning acknowledgment"
        )
    definition = db.scalar(select(MatterDefinition).where(MatterDefinition.matter_id == matter.id))
    if definition is None:
        raise AssessmentError("Matter Definition not found")
    selected_revision = revision_number or definition.current_revision
    revision = db.scalar(
        select(MatterDefinitionRevision).where(
            MatterDefinitionRevision.matter_definition_id == definition.id,
            MatterDefinitionRevision.revision == selected_revision,
        )
    )
    if revision is None:
        raise AssessmentError("Matter Definition revision not found")
    generation = db.scalar(
        select(SearchIndexGeneration).where(
            SearchIndexGeneration.matter_id == matter.id,
            SearchIndexGeneration.status == "ACTIVE",
        )
    )
    if generation is None:
        raise AssessmentError("Matter search index is not ready")
    resolved = resolve_workflow_skill_bindings(
        db,
        workflow_key=MATTER_DEFINITION_ASSESSMENT_SPEC.key,
        tenant_id=matter.client.tenant_id,
    )
    bindings = binding_snapshot(resolved)
    assessment_id = uuid.uuid4()
    workflow_id = f"definition-assessment:{assessment_id}"
    configuration = {
        "maximum_document_count": maximum_document_count,
        "control_sample_size": control_sample_size,
        "large_run_warning_threshold": settings.definition_assessment_warning_document_count,
        "large_run_warning_acknowledged": acknowledge_large_run_warning,
        "merge_algorithm_version": MERGE_ALGORITHM_VERSION,
        "search_index": {
            "id": str(generation.id),
            "generation": generation.generation,
            "index_name": generation.index_name,
            "schema_hash": generation.schema_hash,
        },
    }
    workflow = WorkflowRun(
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        matter_id=matter.id,
        workflow_key=MATTER_DEFINITION_ASSESSMENT_SPEC.key,
        code_version=MATTER_DEFINITION_ASSESSMENT_SPEC.code_version,
        dbos_workflow_id=workflow_id,
        status="QUEUED",
        input_snapshot={
            "assessment_id": str(assessment_id),
            "matter_definition_revision_id": str(revision.id),
            "definition_content_hash": hashlib.sha256(revision.content_markdown.encode()).hexdigest(),
        },
        binding_snapshot=bindings,
        configuration_snapshot=configuration,
        progress={},
        initiated_by_user_id=initiated_by_user_id,
    )
    db.add(workflow)
    db.flush()
    now = utcnow()
    assessment_name = " ".join(name.split()) if name else f"Matter Definition assessment {now:%Y-%m-%d %H:%M}"
    if not assessment_name:
        raise AssessmentError("Assessment name must not be blank")
    assessment = MatterDefinitionAssessmentRun(
        id=assessment_id,
        name=assessment_name,
        matter_id=matter.id,
        matter_definition_revision_id=revision.id,
        definition_content_hash=workflow.input_snapshot["definition_content_hash"],
        workflow_run_id=workflow.id,
        search_index_generation_id=generation.id,
        configuration_snapshot=configuration,
        binding_snapshot=bindings,
        requested_document_count=maximum_document_count,
        control_sample_size=control_sample_size,
        large_run_warning_acknowledged=acknowledge_large_run_warning,
        warning_acknowledged_by_user_id=initiated_by_user_id if warning_required else None,
        warning_acknowledged_at=now if warning_required else None,
        status="QUEUED",
        initiated_by_user_id=initiated_by_user_id,
    )
    db.add(assessment)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=initiated_by_user_id,
        action="matter_definition.assessment.started",
        target_type="matter_definition_assessment_run",
        target_id=assessment.id,
        details={
            "matter_id": str(matter.id),
            "revision": selected_revision,
            "maximum_document_count": maximum_document_count,
            "control_sample_size": control_sample_size,
        },
    )
    db.flush()
    enqueue_definition_assessment(db, workflow_id, str(assessment.id))
    return assessment


def retry_assessment(
    db: Session,
    assessment: MatterDefinitionAssessmentRun,
    *,
    initiated_by_user_id: uuid.UUID,
) -> MatterDefinitionAssessmentRun:
    previous_status = assessment.status
    if previous_status not in {"FAILED", "COMPLETED_WITH_ERRORS"}:
        raise AssessmentError("Only failed assessments or assessments with failed documents can be retried")
    if previous_status == "COMPLETED_WITH_ERRORS" and assessment.failed_count == 0:
        raise AssessmentError("This assessment has no failed documents to retry")
    workflow = db.get(WorkflowRun, assessment.workflow_run_id)
    if workflow is None:
        raise AssessmentError("Assessment workflow record is unavailable")

    attempt_id = str(uuid.uuid4())
    retry_history = assessment.configuration_snapshot.get("retry_history")
    if not isinstance(retry_history, list):
        retry_history = []
    retried_at = utcnow()
    assessment.configuration_snapshot = {
        **assessment.configuration_snapshot,
        "retry_history": [
            *retry_history,
            {
                "attempt_id": attempt_id,
                "retried_at": retried_at.isoformat(),
                "retried_by_user_id": str(initiated_by_user_id),
                "previous_status": previous_status,
                "previous_error": assessment.error_message,
                "previous_failed_document_count": assessment.failed_count,
            },
        ],
    }
    assessment.status = "QUEUED"
    assessment.error_message = None
    assessment.completed_at = None
    assessment.canceled_at = None
    assessment.failed_count = 0
    assessment.partial_coverage_count = 0
    assessment.invalid_result_count = 0
    assessment.coverage_snapshot = None
    assessment.synthesis_result = None

    workflow.dbos_workflow_id = f"definition-assessment:{assessment.id}:retry:{attempt_id}"
    workflow.status = "QUEUED"
    workflow.error_message = None
    workflow.completed_at = None
    workflow.configuration_snapshot = assessment.configuration_snapshot
    retryable_step_ordinals = {3, 4} if previous_status == "COMPLETED_WITH_ERRORS" else set()
    for step in db.scalars(select(WorkflowStepRun).where(WorkflowStepRun.workflow_run_id == workflow.id)):
        if step.status != "FAILED" and step.ordinal not in retryable_step_ordinals:
            continue
        step.status = "QUEUED"
        step.failed_count = 0
        step.error_message = None
        step.completed_at = None
        if step.ordinal == 4:
            step.completed_count = 0

    if assessment.review_batch_run_id is not None:
        review_run = db.get(ReviewBatchRun, assessment.review_batch_run_id)
        if review_run is not None:
            review_run.status = "QUEUED"
            review_run.error_message = None
            review_run.completed_at = None
        db.execute(
            update(ReviewBatchRunDocument)
            .where(
                ReviewBatchRunDocument.review_batch_run_id == assessment.review_batch_run_id,
                ReviewBatchRunDocument.status.in_(("IN_PROGRESS", "FAILED")),
            )
            .values(status="QUEUED", completed_at=None)
        )

    db.flush()
    enqueue_definition_assessment(
        db,
        workflow.dbos_workflow_id,
        str(assessment.id),
        attempt_id=attempt_id,
    )
    return assessment


def regenerate_assessment_synthesis(
    db: Session,
    assessment: MatterDefinitionAssessmentRun,
    *,
    initiated_by_user_id: uuid.UUID,
) -> MatterDefinitionAssessmentRun:
    if assessment.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise AssessmentError("Only completed assessments can regenerate refinement questions")
    if assessment.summarized_count <= 0:
        raise AssessmentError("This assessment has no completed document analyses")
    workflow = db.get(WorkflowRun, assessment.workflow_run_id)
    matter = db.get(Matter, assessment.matter_id)
    if workflow is None or matter is None:
        raise AssessmentError("Assessment workflow record is unavailable")

    resolved = resolve_workflow_skill_bindings(
        db,
        workflow_key=MATTER_DEFINITION_ASSESSMENT_SPEC.key,
        tenant_id=matter.client.tenant_id,
    )
    current_bindings = binding_snapshot(resolved)
    assessment.binding_snapshot = {
        **assessment.binding_snapshot,
        "assessment_synthesis": current_bindings["assessment_synthesis"],
    }
    attempt_id = str(uuid.uuid4())
    regenerated_at = utcnow()
    history = assessment.configuration_snapshot.get("synthesis_regeneration_history")
    if not isinstance(history, list):
        history = []
    assessment.configuration_snapshot = {
        **assessment.configuration_snapshot,
        "synthesis_regeneration_history": [
            *history,
            {
                "attempt_id": attempt_id,
                "regenerated_at": regenerated_at.isoformat(),
                "regenerated_by_user_id": str(initiated_by_user_id),
                "previous_synthesis_status": (assessment.synthesis_result or {}).get("status"),
            },
        ],
    }
    assessment.status = "QUEUED"
    assessment.error_message = None
    assessment.completed_at = None
    assessment.canceled_at = None
    assessment.synthesis_result = None

    workflow.dbos_workflow_id = f"definition-assessment:{assessment.id}:synthesis:{attempt_id}"
    workflow.code_version = MATTER_DEFINITION_ASSESSMENT_SPEC.code_version
    workflow.status = "QUEUED"
    workflow.error_message = None
    workflow.completed_at = None
    workflow.binding_snapshot = assessment.binding_snapshot
    workflow.configuration_snapshot = assessment.configuration_snapshot
    synthesis_step = db.scalar(
        select(WorkflowStepRun).where(
            WorkflowStepRun.workflow_run_id == workflow.id,
            WorkflowStepRun.ordinal == 4,
        )
    )
    if synthesis_step is None:
        synthesis_step = WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key="assessment_synthesis",
            ordinal=4,
            status="QUEUED",
            total_count=1,
        )
        db.add(synthesis_step)
    else:
        synthesis_step.status = "QUEUED"
        synthesis_step.completed_count = 0
        synthesis_step.failed_count = 0
        synthesis_step.error_message = None
        synthesis_step.started_at = None
        synthesis_step.completed_at = None

    db.flush()
    enqueue_definition_assessment_synthesis(db, workflow.dbos_workflow_id, str(assessment.id))
    return assessment


def regenerate_assessment_document_analyses(
    db: Session,
    assessment: MatterDefinitionAssessmentRun,
    *,
    initiated_by_user_id: uuid.UUID,
) -> MatterDefinitionAssessmentRun:
    if assessment.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise AssessmentError("Only completed assessments can regenerate document analyses")
    if assessment.review_batch_id is None or assessment.review_batch_run_id is None:
        raise AssessmentError("This assessment has no frozen review batch")
    workflow = db.get(WorkflowRun, assessment.workflow_run_id)
    matter = db.get(Matter, assessment.matter_id)
    previous_review_run = db.get(ReviewBatchRun, assessment.review_batch_run_id)
    if workflow is None or matter is None or previous_review_run is None:
        raise AssessmentError("Assessment workflow records are unavailable")
    document_ids = list(
        db.scalars(
            select(ReviewBatchDocument.matter_document_id)
            .where(ReviewBatchDocument.review_batch_id == assessment.review_batch_id)
            .order_by(ReviewBatchDocument.sequence_number)
        )
    )
    if not document_ids:
        raise AssessmentError("This assessment batch has no documents to analyze")

    resolved = resolve_workflow_skill_bindings(
        db,
        workflow_key=MATTER_DEFINITION_ASSESSMENT_SPEC.key,
        tenant_id=matter.client.tenant_id,
    )
    current_bindings = binding_snapshot(resolved)
    assessment.binding_snapshot = {
        **assessment.binding_snapshot,
        "document_analysis": current_bindings["document_analysis"],
        "assessment_synthesis": current_bindings["assessment_synthesis"],
    }
    attempt_id = str(uuid.uuid4())
    regenerated_at = utcnow()
    history = assessment.configuration_snapshot.get("document_analysis_regeneration_history")
    if not isinstance(history, list):
        history = []
    assessment.configuration_snapshot = {
        **assessment.configuration_snapshot,
        "document_analysis_regeneration_history": [
            *history,
            {
                "attempt_id": attempt_id,
                "regenerated_at": regenerated_at.isoformat(),
                "regenerated_by_user_id": str(initiated_by_user_id),
                "previous_review_batch_run_id": str(previous_review_run.id),
                "previous_summarized_document_count": assessment.summarized_count,
                "previous_skipped_document_count": assessment.skipped_count,
                "previous_failed_document_count": assessment.failed_count,
            },
        ],
    }

    workflow_id = f"definition-assessment:{assessment.id}:analysis:{attempt_id}"
    review_run = ReviewBatchRun(
        review_batch_id=assessment.review_batch_id,
        run_type="WORKFLOW",
        purpose="ASSESSMENT",
        status="QUEUED",
        result_policy="ISOLATED",
        parent_run_id=previous_review_run.id,
        workflow_run_record_id=workflow.id,
        configuration_snapshot={
            **assessment.configuration_snapshot,
            "binding_snapshot": assessment.binding_snapshot,
        },
        initiated_by_user_id=initiated_by_user_id,
        dbos_workflow_id=f"{workflow_id}:documents",
    )
    db.add(review_run)
    db.flush()
    db.add_all(
        ReviewBatchRunDocument(
            review_batch_run_id=review_run.id,
            matter_document_id=document_id,
            status="QUEUED",
        )
        for document_id in document_ids
    )

    assessment.review_batch_run_id = review_run.id
    assessment.status = "QUEUED"
    assessment.error_message = None
    assessment.completed_at = None
    assessment.canceled_at = None
    assessment.summarized_count = 0
    assessment.skipped_count = 0
    assessment.failed_count = 0
    assessment.partial_coverage_count = 0
    assessment.invalid_result_count = 0
    assessment.coverage_snapshot = None
    assessment.synthesis_result = None

    workflow.dbos_workflow_id = workflow_id
    workflow.code_version = MATTER_DEFINITION_ASSESSMENT_SPEC.code_version
    workflow.status = "QUEUED"
    workflow.error_message = None
    workflow.completed_at = None
    workflow.binding_snapshot = assessment.binding_snapshot
    workflow.configuration_snapshot = assessment.configuration_snapshot
    for ordinal, role_key, total_count in (
        (3, "document_analysis", len(document_ids)),
        (4, "assessment_synthesis", 1),
    ):
        step = db.scalar(
            select(WorkflowStepRun).where(
                WorkflowStepRun.workflow_run_id == workflow.id,
                WorkflowStepRun.ordinal == ordinal,
            )
        )
        if step is None:
            step = WorkflowStepRun(
                workflow_run_id=workflow.id,
                role_key=role_key,
                ordinal=ordinal,
                fan_out_group="documents" if ordinal == 3 else None,
                status="QUEUED",
                total_count=total_count,
            )
            db.add(step)
        else:
            step.status = "QUEUED"
            step.total_count = total_count
            step.completed_count = 0
            step.failed_count = 0
            step.error_message = None
            step.started_at = None
            step.completed_at = None

    db.flush()
    enqueue_definition_assessment_reanalysis(
        db,
        workflow.dbos_workflow_id,
        str(assessment.id),
        attempt_id,
    )
    return assessment


def materialize_assessment_batch(
    db: Session,
    assessment: MatterDefinitionAssessmentRun,
    *,
    selected: list[SelectedCandidate],
    all_candidates: list[SelectedCandidate],
    query_plan: dict[str, Any],
) -> ReviewBatch:
    if assessment.review_batch_id is not None:
        batch = db.get(ReviewBatch, assessment.review_batch_id)
        if batch is None:
            raise AssessmentError("Assessment batch reference is invalid")
        return batch
    db.execute(
        delete(MatterDefinitionAssessmentCandidate).where(
            MatterDefinitionAssessmentCandidate.assessment_run_id == assessment.id
        )
    )
    selection_order = {item.document_id: index for index, item in enumerate(selected, start=1)}
    db.add_all(
        MatterDefinitionAssessmentCandidate(
            assessment_run_id=assessment.id,
            matter_document_id=item.document_id,
            retrieval_provenance=list(item.provenance),
            fused_score=item.fused_score,
            selected=item.document_id in selection_order,
            selection_order=selection_order.get(item.document_id),
            selection_reason=item.reason,
        )
        for item in all_candidates
    )
    batch_id = uuid.uuid4()
    generation = db.get(SearchIndexGeneration, assessment.search_index_generation_id)
    batch = ReviewBatch(
        id=batch_id,
        matter_id=assessment.matter_id,
        name=assessment.name,
        description="Frozen diagnostic sample created from a Matter Definition assessment.",
        selection_type="DEFINITION_ASSESSMENT",
        selection_definition={
            "type": "DEFINITION_ASSESSMENT",
            "assessment_run_id": str(assessment.id),
            "matter_definition_revision_id": str(assessment.matter_definition_revision_id),
            "definition_content_hash": assessment.definition_content_hash,
            "search_index_generation": {
                "id": str(generation.id),
                "generation": generation.generation,
                "index_name": generation.index_name,
                "schema_hash": generation.schema_hash,
            },
            "query_plan": query_plan,
            "merge_algorithm_version": MERGE_ALGORITHM_VERSION,
            "selected_candidates": [
                {
                    "document_id": str(item.document_id),
                    "fused_score": item.fused_score,
                    "reason": item.reason,
                    "provenance": list(item.provenance),
                }
                for item in selected
            ],
        },
        search_index_generation_id=assessment.search_index_generation_id,
        status="READY",
        search_status="QUEUED",
        workflow_id=f"definition-assessment-batch:{assessment.id}",
        document_count=len(selected),
        created_by_user_id=assessment.initiated_by_user_id,
        completed_at=utcnow(),
    )
    db.add(batch)
    db.flush()
    db.add_all(
        ReviewBatchDocument(
            review_batch_id=batch.id,
            matter_document_id=item.document_id,
            sequence_number=index,
        )
        for index, item in enumerate(selected, start=1)
    )
    workflow_run = db.get(WorkflowRun, assessment.workflow_run_id)
    if workflow_run is None:
        raise AssessmentError("Assessment workflow record is missing")
    review_run = ReviewBatchRun(
        review_batch_id=batch.id,
        run_type="WORKFLOW",
        purpose="ASSESSMENT",
        status="QUEUED",
        result_policy="ISOLATED",
        workflow_run_record_id=workflow_run.id,
        configuration_snapshot=assessment.configuration_snapshot,
        initiated_by_user_id=assessment.initiated_by_user_id,
        dbos_workflow_id=f"{workflow_run.dbos_workflow_id}:documents",
    )
    db.add(review_run)
    db.flush()
    db.add_all(
        ReviewBatchRunDocument(
            review_batch_run_id=review_run.id,
            matter_document_id=item.document_id,
            status="QUEUED",
        )
        for item in selected
    )
    assessment.review_batch_id = batch.id
    assessment.review_batch_run_id = review_run.id
    assessment.candidate_count = len(all_candidates)
    assessment.selected_count = len(selected)
    return batch


def assessment_control_population(db: Session, assessment: MatterDefinitionAssessmentRun) -> list[uuid.UUID]:
    return list(
        db.scalars(
            select(MatterDocument.id)
            .where(MatterDocument.matter_id == assessment.matter_id)
            .order_by(MatterDocument.id)
        )
    )
