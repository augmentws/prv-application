import hashlib
import random
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
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
)
from app.workflow_specs import MATTER_DEFINITION_ASSESSMENT_SPEC, binding_snapshot, resolve_workflow_skill_bindings
from app.workflows.dispatcher import enqueue_definition_assessment

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
    assessment = MatterDefinitionAssessmentRun(
        id=assessment_id,
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
        name=f"Matter Definition assessment {assessment.created_at:%Y-%m-%d %H:%M}",
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
