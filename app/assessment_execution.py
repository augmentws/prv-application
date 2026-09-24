import asyncio
import json
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.artifact_gateway import get_preferred_text_source, read_artifact_bytes, store_derived_artifact
from app.config import Settings, get_settings
from app.document_evidence import (
    PARAGRAPH_MAP_VERSION,
    EvidenceParagraph,
    ParagraphMap,
    build_document_map_plan,
    build_document_reduce_input,
    render_document_analysis_markdown,
    render_model_document,
    segment_paragraphs,
    validate_document_analysis_citations,
)
from app.embedding_gateway import get_query_embedding_gateway
from app.matter_definition_assessments import (
    AssessmentError,
    RetrievalHit,
    assessment_control_population,
    materialize_assessment_batch,
    merge_retrieval_candidates,
)
from app.model_execution import content_hash
from app.models import (
    BatchTopic,
    BatchTopicAssignment,
    BatchTopicTaxonomy,
    Matter,
    MatterDefinitionAssessmentQuery,
    MatterDefinitionAssessmentQuestion,
    MatterDefinitionAssessmentRun,
    MatterDefinitionRevision,
    MatterDocument,
    MetadataDefinition,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    SearchIndexGeneration,
    SkillDefinitionVersion,
    SkillRun,
    WorkflowRun,
    WorkflowStepRun,
)
from app.schemas import MatterSearchRequest
from app.search.client import OpenSearchClient
from app.search.query import execute_search
from app.skill_execution import (
    complete_skill_run,
    create_skill_run,
    execute_skill_call,
    execute_skill_run,
    fail_skill_run,
)
from app.token_estimation import estimate_analysis_tokens

SYNTHESIS_COVERAGE_POLICY = {
    "version": "assessment_synthesis_coverage_v1",
    "minimum_successful_documents": 3,
    "minimum_success_ratio": 0.5,
}
REFINEMENT_DIMENSIONS = {
    "INCLUSION_EXCLUSION_BOUNDARIES",
    "UNCOVERED_SUBJECTS",
    "CONFLICTING_TREATMENT",
    "TEMPORAL_SCOPE",
    "GEOGRAPHIC_SCOPE",
    "ACTOR_ENTITY_SCOPE",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _records(db: Session, assessment_id: uuid.UUID):
    assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
    if assessment is None:
        raise AssessmentError("Assessment not found")
    workflow = db.get(WorkflowRun, assessment.workflow_run_id)
    revision = db.get(MatterDefinitionRevision, assessment.matter_definition_revision_id)
    matter = db.get(Matter, assessment.matter_id)
    if workflow is None or revision is None or matter is None:
        raise AssessmentError("Assessment references are incomplete")
    return assessment, workflow, revision, matter


def _step(db: Session, workflow: WorkflowRun, *, ordinal: int, role_key: str, total_count: int = 1) -> WorkflowStepRun:
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
            fan_out_group="documents" if role_key == "document_analysis" else None,
            status="QUEUED",
            total_count=total_count,
        )
        db.add(step)
        db.flush()
    elif total_count > step.total_count:
        step.total_count = total_count
    return step


def _skill_version(db: Session, assessment: MatterDefinitionAssessmentRun, role_key: str) -> SkillDefinitionVersion:
    role = assessment.binding_snapshot.get(role_key) or {}
    version_id = role.get("skill_definition_version_id")
    if not version_id:
        raise AssessmentError(f"Assessment has no pinned binding for {role_key}")
    version = db.get(SkillDefinitionVersion, uuid.UUID(version_id))
    if version is None:
        raise AssessmentError(f"Pinned skill version for {role_key} no longer exists")
    return version


def _pinned_model(assessment: MatterDefinitionAssessmentRun, role_key: str) -> str | None:
    models = assessment.configuration_snapshot.get("resolved_models")
    if not isinstance(models, dict):
        return None
    value = models.get(role_key)
    return value if isinstance(value, str) and value else None


def _normalized_query(item: dict[str, Any], *, ordinal: int) -> tuple[dict[str, Any], MatterSearchRequest]:
    search_data = item.get("search") or item.get("search_request") or item.get("request")
    if not isinstance(search_data, dict):
        raise AssessmentError(f"Retrieval query {ordinal} does not contain a controlled search request")
    raw_dsl_keys = {"query_dsl", "dsl", "bool", "must", "should", "knn"}
    if raw_dsl_keys.intersection(search_data):
        raise AssessmentError(f"Retrieval query {ordinal} contains raw search DSL")
    allowed = set(MatterSearchRequest.model_fields)
    extras = set(search_data) - allowed
    if extras:
        raise AssessmentError(f"Retrieval query {ordinal} has unsupported fields: {', '.join(sorted(extras))}")
    request = MatterSearchRequest.model_validate(
        {**search_data, "offset": 0, "size": min(500, max(1, int(item.get("quota", 50)))), "facets": []}
    )
    normalized = {
        "criterion_key": str(item.get("criterion_key") or f"criterion_{ordinal}"),
        "criterion_label": str(item.get("criterion_label") or item.get("label") or f"Criterion {ordinal}"),
        "rationale": str(item.get("rationale") or "Diagnostic retrieval query"),
        "quota": min(500, max(1, int(item.get("quota", request.size)))),
        "search": request.model_dump(mode="json", by_alias=True),
    }
    return normalized, request


def _validate_retrieval_plan(output: dict[str, Any]) -> None:
    raw_queries = output.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise AssessmentError("Retrieval planner must return at least one query")
    for index, item in enumerate(raw_queries, start=1):
        if not isinstance(item, dict):
            raise AssessmentError(f"Retrieval query {index} must be an object")
        _normalized_query(item, ordinal=index)


def plan_retrieval(db: Session, assessment_id: uuid.UUID, *, model: Any | None = None) -> dict[str, Any]:
    assessment, workflow, revision, matter = _records(db, assessment_id)
    existing = assessment.configuration_snapshot.get("retrieval_plan")
    if isinstance(existing, dict):
        return existing
    assessment.status = "PLANNING"
    assessment.started_at = assessment.started_at or utcnow()
    workflow.status = "RUNNING"
    workflow.started_at = workflow.started_at or utcnow()
    step = _step(db, workflow, ordinal=1, role_key="retrieval_planner")
    step.status = "RUNNING"
    step.started_at = step.started_at or utcnow()
    version = _skill_version(db, assessment, "retrieval_planner")
    model = model if model is not None else _pinned_model(assessment, "retrieval_planner")
    output, _ = asyncio.run(
        execute_skill_run(
            db,
            workflow=workflow,
            step=step,
            skill_version=version,
            scope_type="MATTER_DEFINITION_REVISION",
            scope_id=revision.id,
            stable_context={"matter_definition": revision.content_markdown},
            dynamic_input={
                "matter_id": str(matter.id),
                "requested_document_count": assessment.requested_document_count,
            },
            cache_identity={
                "tenant_id": str(workflow.tenant_id),
                "matter_id": str(matter.id),
                "revision_hash": assessment.definition_content_hash,
                "skill_version_id": str(version.id),
            },
            output_validators=(_validate_retrieval_plan,),
            model=model,
        )
    )
    raw_queries = output.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise AssessmentError("Retrieval planner returned no queries")
    queries = [_normalized_query(item, ordinal=index)[0] for index, item in enumerate(raw_queries, start=1)]
    plan = {
        "criteria": output.get("criteria") or [],
        "queries": queries,
        "sampling_guidance": output.get("sampling_guidance") or {},
    }
    assessment.configuration_snapshot = {**assessment.configuration_snapshot, "retrieval_plan": plan}
    workflow.configuration_snapshot = {**workflow.configuration_snapshot, "retrieval_plan": plan}
    step.status = "COMPLETED"
    step.completed_count = 1
    step.completed_at = utcnow()
    db.execute(
        delete(MatterDefinitionAssessmentQuery).where(
            MatterDefinitionAssessmentQuery.assessment_run_id == assessment.id
        )
    )
    db.add_all(
        MatterDefinitionAssessmentQuery(
            assessment_run_id=assessment.id,
            ordinal=index,
            criterion_key=item["criterion_key"],
            criterion_label=item["criterion_label"],
            rationale=item["rationale"],
            search_request=item["search"],
            quota=item["quota"],
        )
        for index, item in enumerate(queries, start=1)
    )
    db.commit()
    return plan


def retrieve_and_materialize(db: Session, assessment_id: uuid.UUID, settings: Settings) -> list[str]:
    assessment, workflow, revision, matter = _records(db, assessment_id)
    if assessment.review_batch_id is not None:
        rows = db.scalars(
            select(ReviewBatchRunDocument.matter_document_id).where(
                ReviewBatchRunDocument.review_batch_run_id == assessment.review_batch_run_id
            )
        )
        return [str(value) for value in rows]
    plan = assessment.configuration_snapshot.get("retrieval_plan")
    if not isinstance(plan, dict):
        raise AssessmentError("Retrieval plan has not been created")
    assessment.status = "RETRIEVING"
    step = _step(db, workflow, ordinal=2, role_key="retrieval_execution", total_count=len(plan["queries"]))
    step.status = "RUNNING"
    step.started_at = step.started_at or utcnow()
    generation = db.get(SearchIndexGeneration, assessment.search_index_generation_id)
    if generation is None:
        raise AssessmentError("Pinned search generation no longer exists")
    definitions = list(
        db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter.id,
                MetadataDefinition.status == "ACTIVE",
            )
        )
    )
    query_rows = list(
        db.scalars(
            select(MatterDefinitionAssessmentQuery)
            .where(MatterDefinitionAssessmentQuery.assessment_run_id == assessment.id)
            .order_by(MatterDefinitionAssessmentQuery.ordinal)
        )
    )
    hits: list[RetrievalHit] = []
    client = OpenSearchClient(settings)
    try:
        for row in query_rows:
            request = MatterSearchRequest.model_validate(row.search_request)
            query_vector = None
            if request.search_mode != "KEYWORD":
                query_vector = get_query_embedding_gateway().embed([request.query or ""], "query").embeddings[0]
            response = execute_search(
                client,
                generation.index_name,
                request,
                definitions,
                tenant_id=str(matter.client.tenant_id),
                matter_id=str(matter.id),
                query_vector=query_vector,
            )
            row.result_count = response.total
            hits.extend(
                RetrievalHit(
                    document_id=hit.document_id,
                    query_ordinal=row.ordinal,
                    criterion_key=row.criterion_key,
                    rank=rank,
                    score=hit.score,
                    best_passage=hit.best_passage.model_dump(mode="json") if hit.best_passage else None,
                )
                for rank, hit in enumerate(response.hits, start=1)
            )
            step.completed_count += 1
    finally:
        client.close()
    assessment.status = "BUILDING_BATCH"
    selected, candidates = merge_retrieval_candidates(
        hits,
        maximum_document_count=assessment.requested_document_count,
        query_quotas={row.ordinal: row.quota for row in query_rows},
        control_document_ids=assessment_control_population(db, assessment),
        control_sample_size=assessment.control_sample_size,
        seed=str(assessment.id),
    )
    materialize_assessment_batch(
        db,
        assessment,
        selected=selected,
        all_candidates=candidates,
        query_plan=plan,
    )
    step.status = "COMPLETED"
    step.completed_at = utcnow()
    analysis_step = _step(
        db,
        workflow,
        ordinal=3,
        role_key="document_analysis",
        total_count=len(selected),
    )
    analysis_step.status = "QUEUED"

    analysis_inputs: list[str] = []
    source_hashes: list[str] = []
    request_count = 0
    reduce_input_overhead_characters = 0
    for item in selected:
        document = db.get(MatterDocument, item.document_id)
        if document is None:
            continue
        source = get_preferred_text_source(
            collection_item_id=document.collection_item_id,
            actor_user_id=assessment.initiated_by_user_id,
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
        )
        if source is not None:
            source_hashes.append(source.content_hash)
            paragraph_map = segment_paragraphs(source.text)
            map_plan = build_document_map_plan(
                paragraph_map,
                max_characters=settings.definition_assessment_map_max_characters,
            )
            analysis_inputs.extend(window.text for window in map_plan.windows)
            request_count += len(map_plan.windows)
            if len(map_plan.windows) > 1:
                request_count += 1
                reduce_input_overhead_characters += len(map_plan.windows) * 4800
    version = _skill_version(db, assessment, "document_analysis")
    estimate = estimate_analysis_tokens(
        analysis_inputs,
        stable_prefix=revision.content_markdown
        + version.instructions
        + json.dumps(version.output_schema, sort_keys=True),
        model=version.model_key,
        request_count=request_count,
        reduce_input_overhead_characters=reduce_input_overhead_characters,
    )
    assessment.estimated_input_tokens = estimate.input_tokens
    assessment.estimated_output_tokens = estimate.output_tokens
    assessment.token_estimator = estimate.method
    assessment.token_estimator_version = estimate.version
    assessment.estimation_model = version.model_key
    assessment.estimate_source_hashes = source_hashes
    assessment.status = "SUMMARIZING"
    review_run = db.get(ReviewBatchRun, assessment.review_batch_run_id)
    if review_run is not None:
        review_run.status = "RUNNING"
    db.commit()
    return [str(item.document_id) for item in selected]


def analyze_document(
    db: Session,
    assessment_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    model: Any | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    assessment, workflow, revision, matter = _records(db, assessment_id)
    if assessment.status == "CANCELED":
        return {"status": "CANCELED"}
    if assessment.review_batch_run_id is None:
        raise AssessmentError("Assessment batch run has not been created")
    run_document = db.get(ReviewBatchRunDocument, (assessment.review_batch_run_id, document_id))
    if run_document is None:
        raise AssessmentError("Document is not in the assessment batch")
    if run_document.status in {"COMPLETED", "SKIPPED"}:
        return {"status": run_document.status}
    document = db.get(MatterDocument, document_id)
    if document is None:
        raise AssessmentError("Matter document not found")
    source = get_preferred_text_source(
        collection_item_id=document.collection_item_id,
        actor_user_id=assessment.initiated_by_user_id,
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
    )
    run_document.status = "IN_PROGRESS"
    if source is None:
        run_document.status = "SKIPPED"
        run_document.completed_at = utcnow()
        db.commit()
        return {"status": "SKIPPED", "reason": "NO_TEXT_SOURCE"}
    paragraph_map = segment_paragraphs(source.text)
    if not paragraph_map.paragraphs:
        run_document.status = "SKIPPED"
        run_document.completed_at = utcnow()
        db.commit()
        return {"status": "SKIPPED", "reason": "EMPTY_TEXT_SOURCE"}
    step = _step(db, workflow, ordinal=3, role_key="document_analysis", total_count=assessment.selected_count)
    step.status = "RUNNING"
    step.started_at = step.started_at or utcnow()
    version = _skill_version(db, assessment, "document_analysis")
    settings = settings or get_settings()
    plan = build_document_map_plan(
        paragraph_map,
        max_characters=settings.definition_assessment_map_max_characters,
    )
    model = model if model is not None else _pinned_model(assessment, "document_analysis")
    output, skill_run = asyncio.run(
        _execute_document_analysis(
            db,
            assessment=assessment,
            workflow=workflow,
            step=step,
            version=version,
            revision=revision,
            document=document,
            source=source,
            paragraph_map=paragraph_map,
            plan=plan,
            model=model,
        )
    )
    artifact_id = persist_document_analysis(
        db,
        assessment=assessment,
        matter=matter,
        revision=revision,
        document=document,
        run_document=run_document,
        version=version,
        source=source,
        paragraph_map=paragraph_map,
        output=output,
        skill_run=skill_run,
    )
    db.commit()
    return {"status": "COMPLETED", "artifact_id": str(artifact_id)}


def persist_document_analysis(
    db: Session,
    *,
    assessment: MatterDefinitionAssessmentRun,
    matter: Matter,
    revision: MatterDefinitionRevision,
    document: MatterDocument,
    run_document: ReviewBatchRunDocument,
    version: SkillDefinitionVersion,
    source,
    paragraph_map: ParagraphMap,
    output: dict[str, Any],
    skill_run: SkillRun,
) -> uuid.UUID:
    markdown = render_document_analysis_markdown(output)
    payload = {
        "schema_version": version.output_schema_key,
        "result": output,
        "rendered_markdown": markdown,
        "paragraph_map": paragraph_map.as_dict(),
        "source": {
            "artifact_id": str(source.artifact_id),
            "content_hash": source.content_hash,
            "artifact_role": source.artifact_role,
            "media_type": source.media_type,
        },
    }
    derivation_key = content_hash(
        {
            "source_content_hash": source.content_hash,
            "review_batch_run_id": str(assessment.review_batch_run_id),
            "definition_content_hash": assessment.definition_content_hash,
            "skill_version_id": str(version.id),
            "model_key": version.model_key,
            "model_policy": version.model_policy,
            "output_schema_version": version.output_schema_key,
            "paragraph_map_version": PARAGRAPH_MAP_VERSION,
        }
    )
    artifact, _ = store_derived_artifact(
        collection_item_id=document.collection_item_id,
        content=json.dumps(payload, sort_keys=True, ensure_ascii=False).encode(),
        artifact_type="SUMMARY",
        source_artifact_id=source.artifact_id,
        relationship="DERIVED_FROM",
        processing_run_id=assessment.review_batch_run_id,
        derivation_key=derivation_key,
        artifact_metadata={
            "matter_id": str(matter.id),
            "matter_document_id": str(document.id),
            "review_batch_id": str(assessment.review_batch_id),
            "review_batch_run_id": str(assessment.review_batch_run_id),
            "matter_definition_revision_id": str(revision.id),
            "skill_definition_version_id": str(version.id),
            "schema_version": version.output_schema_key,
            "source_role": source.artifact_role,
            "source_hash": source.content_hash,
            "paragraph_map_version": PARAGRAPH_MAP_VERSION,
            "cache_fingerprint": skill_run.cache_fingerprint,
        },
        actor_user_id=assessment.initiated_by_user_id,
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        media_type="application/json",
        filename=f"{document.id}-document-analysis.json",
    )
    skill_run.output_artifact_id = artifact.artifact_id
    run_document.status = "COMPLETED"
    run_document.completed_at = utcnow()
    return artifact.artifact_id


async def _execute_document_analysis(
    db: Session,
    *,
    assessment: MatterDefinitionAssessmentRun,
    workflow: WorkflowRun,
    step: WorkflowStepRun,
    version: SkillDefinitionVersion,
    revision: MatterDefinitionRevision,
    document: MatterDocument,
    source,
    paragraph_map: ParagraphMap,
    plan,
    model: Any | None,
):
    stable_context = {"matter_definition": revision.content_markdown}
    cache_identity = {
        "tenant_id": str(workflow.tenant_id),
        "matter_id": str(assessment.matter_id),
        "revision_hash": assessment.definition_content_hash,
        "skill_version_id": str(version.id),
        "schema_version": version.output_schema_key,
        "paragraph_map_version": PARAGRAPH_MAP_VERSION,
    }
    document_metadata = {
        "matter_document_id": str(document.id),
        "collection_item_id": str(document.collection_item_id),
        "source_artifact_id": str(source.artifact_id),
        "source_hash": source.content_hash,
    }
    if len(plan.windows) == 1:
        return await execute_skill_run(
            db,
            workflow=workflow,
            step=step,
            skill_version=version,
            scope_type="MATTER_DOCUMENT",
            scope_id=document.id,
            stable_context=stable_context,
            dynamic_input={
                "analysis_stage": "DOCUMENT",
                "document": {**document_metadata, "text": render_model_document(paragraph_map)},
                "paragraph_map": paragraph_map.as_dict()["paragraphs"],
            },
            cache_identity=cache_identity,
            output_validators=(lambda result: validate_document_analysis_citations(result, paragraph_map),),
            model=model,
        )

    skill_run = create_skill_run(
        db,
        workflow=workflow,
        step=step,
        skill_version=version,
        scope_type="MATTER_DOCUMENT",
        scope_id=document.id,
        request_input={
            "definition_hash": assessment.definition_content_hash,
            "source_hash": source.content_hash,
            "map_plan_version": plan.version,
            "window_count": len(plan.windows),
        },
    )
    try:
        map_results: dict[str, dict[str, Any]] = {}
        paragraphs_by_id = {item.paragraph_id: item for item in paragraph_map.paragraphs}
        for window in plan.windows:
            window_ids = list(dict.fromkeys(segment.paragraph_id for segment in window.segments))
            window_paragraphs = []
            for paragraph_id in window_ids:
                segments = [item for item in window.segments if item.paragraph_id == paragraph_id]
                original = paragraphs_by_id[paragraph_id]
                window_paragraphs.append(
                    EvidenceParagraph(
                        paragraph_id=paragraph_id,
                        text="".join(item.text for item in segments),
                        char_start=min(item.char_start for item in segments),
                        char_end=max(item.char_end for item in segments),
                        line_start=original.line_start,
                        line_end=original.line_end,
                    )
                )
            window_map = ParagraphMap(
                version=paragraph_map.version,
                source_length=paragraph_map.source_length,
                paragraphs=tuple(window_paragraphs),
            )
            map_results[window.window_id] = await execute_skill_call(
                db,
                workflow=workflow,
                step=step,
                skill_version=version,
                skill_run=skill_run,
                stable_context=stable_context,
                dynamic_input={
                    "analysis_stage": "MAP",
                    "window_id": window.window_id,
                    "document": {**document_metadata, "text": window.text},
                    "paragraph_map": window_map.as_dict()["paragraphs"],
                },
                cache_identity=cache_identity,
                output_validators=(
                    lambda result, allowed=window_map: validate_document_analysis_citations(result, allowed),
                ),
                attempt=window.ordinal,
                model=model,
            )
        reduce_input = build_document_reduce_input(paragraph_map, plan, map_results=map_results)
        output = await execute_skill_call(
            db,
            workflow=workflow,
            step=step,
            skill_version=version,
            skill_run=skill_run,
            stable_context=stable_context,
            dynamic_input={
                "analysis_stage": "REDUCE",
                "document": document_metadata,
                "paragraph_ids": [item.paragraph_id for item in paragraph_map.paragraphs],
                **reduce_input,
            },
            cache_identity=cache_identity,
            output_validators=(lambda result: validate_document_analysis_citations(result, paragraph_map),),
            attempt=len(plan.windows) + 1,
            model=model,
        )
        output["coverage"] = reduce_input["coverage"]
        complete_skill_run(db, skill_run)
        return output, skill_run
    except Exception as exc:
        fail_skill_run(skill_run, exc)
        raise


def refresh_progress(db: Session, assessment_id: uuid.UUID) -> dict[str, int]:
    assessment, workflow, _, _ = _records(db, assessment_id)
    if assessment.review_batch_run_id is None:
        raise AssessmentError("Assessment batch run has not been created")
    counts = dict(
        db.execute(
            select(ReviewBatchRunDocument.status, func.count())
            .where(ReviewBatchRunDocument.review_batch_run_id == assessment.review_batch_run_id)
            .group_by(ReviewBatchRunDocument.status)
        ).all()
    )
    assessment.summarized_count = int(counts.get("COMPLETED", 0))
    assessment.skipped_count = int(counts.get("SKIPPED", 0))
    assessment.failed_count = int(counts.get("FAILED", 0))
    review_run = db.get(ReviewBatchRun, assessment.review_batch_run_id)
    if review_run is not None:
        review_run.processed_document_count = assessment.summarized_count + assessment.skipped_count
    step = _step(db, workflow, ordinal=3, role_key="document_analysis", total_count=assessment.selected_count)
    step.completed_count = assessment.summarized_count + assessment.skipped_count
    step.failed_count = assessment.failed_count
    db.commit()
    return {key.lower(): int(value) for key, value in counts.items()}


def _load_document_analyses(
    db: Session,
    assessment: MatterDefinitionAssessmentRun,
    matter: Matter,
) -> tuple[list[dict[str, Any]], int, int]:
    completed_document_ids: set[uuid.UUID] | None = None
    if assessment.review_batch_run_id is not None:
        completed_document_ids = set(
            db.scalars(
                select(ReviewBatchRunDocument.matter_document_id).where(
                    ReviewBatchRunDocument.review_batch_run_id == assessment.review_batch_run_id,
                    ReviewBatchRunDocument.status == "COMPLETED",
                )
            )
        )
        if not completed_document_ids:
            return [], 0, 0
    statement = select(SkillRun.scope_id, SkillRun.output_artifact_id).where(
        SkillRun.workflow_run_id == assessment.workflow_run_id,
        SkillRun.scope_type == "MATTER_DOCUMENT",
        SkillRun.status == "COMPLETED",
        SkillRun.output_artifact_id.is_not(None),
    )
    if completed_document_ids is not None:
        statement = statement.where(SkillRun.scope_id.in_(completed_document_ids))
    rows = db.execute(statement.order_by(SkillRun.created_at.desc(), SkillRun.id.desc())).all()
    analyses: list[dict[str, Any]] = []
    loaded_document_ids: set[uuid.UUID] = set()
    invalid = 0
    partial = 0
    for document_id, artifact_id in rows:
        if document_id is None or document_id in loaded_document_ids:
            continue
        loaded_document_ids.add(document_id)
        try:
            payload = json.loads(
                read_artifact_bytes(
                    artifact_id=artifact_id,
                    actor_user_id=assessment.initiated_by_user_id,
                    tenant_id=matter.client.tenant_id,
                    client_id=matter.client_id,
                )
            )
            result = payload["result"]
            if not isinstance(result, dict):
                raise TypeError("Analysis result is not an object")
            coverage = result.get("coverage") or {}
            if coverage.get("status") == "PARTIAL":
                partial += 1
            analyses.append(
                {
                    "matter_document_id": str(document_id),
                    "output_artifact_id": str(artifact_id),
                    "analysis": result,
                }
            )
        except (KeyError, TypeError, ValueError, PermissionError, json.JSONDecodeError):
            invalid += 1
    return analyses, partial, invalid


def _coverage_envelope(
    assessment: MatterDefinitionAssessmentRun,
    *,
    successful: int,
    partial: int,
    invalid: int,
) -> dict[str, Any]:
    selected = assessment.selected_count
    ratio = successful / selected if selected else 0.0
    sufficient = (
        successful >= SYNTHESIS_COVERAGE_POLICY["minimum_successful_documents"]
        and ratio >= SYNTHESIS_COVERAGE_POLICY["minimum_success_ratio"]
    )
    return {
        "policy": SYNTHESIS_COVERAGE_POLICY,
        "selected_document_count": selected,
        "successful_document_count": successful,
        "skipped_document_count": assessment.skipped_count,
        "failed_document_count": assessment.failed_count,
        "partial_coverage_document_count": partial,
        "invalid_result_count": invalid,
        "successful_document_ratio": ratio,
        "status": "SUFFICIENT" if sufficient else "INSUFFICIENT",
    }


def _synthesis_context(
    analyses: list[dict[str, Any]],
    coverage: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    determination_counts: Counter[str] = Counter()
    match_type_counts: Counter[str] = Counter()
    criterion_counts: dict[str, Counter[str]] = defaultdict(Counter)
    near_misses: list[dict[str, Any]] = []
    document_candidates: list[dict[str, Any]] = []
    limitation_examples: list[dict[str, Any]] = []
    limitation_count = 0

    for row in analyses:
        document_id = str(row["matter_document_id"])
        analysis = row["analysis"]
        determination_counts[str(analysis.get("determination") or "UNKNOWN")] += 1
        for match in analysis.get("criterion_matches") or []:
            if not isinstance(match, dict):
                continue
            match_type = str(match.get("match_type") or "UNKNOWN")
            criterion_key = str(match.get("criterion_key") or "UNSPECIFIED")
            match_type_counts[match_type] += 1
            criterion_counts[criterion_key][match_type] += 1
            if match_type == "NEAR_MISS":
                issue_match = re.search(r"\bissue[\s_-]*(\d+)(?!\d)", criterion_key, flags=re.IGNORECASE)
                normalized_key = (
                    f"ISSUE_{int(issue_match.group(1))}"
                    if issue_match
                    else re.sub(r"[^A-Z0-9]+", "_", criterion_key.upper()).strip("_") or "UNSPECIFIED"
                )
                near_misses.append(
                    {
                        "matter_document_id": document_id,
                        "criterion_key": criterion_key,
                        "normalized_criterion_key": normalized_key,
                        "label": str(match.get("label") or criterion_key),
                        "reasoning": str(match.get("reasoning") or ""),
                        "paragraph_ids": list(match.get("citation_ids") or []),
                    }
                )
        for candidate in analysis.get("clarification_requests") or []:
            if isinstance(candidate, dict):
                document_candidates.append({"matter_document_id": document_id, **candidate})
        limitations = [str(value) for value in analysis.get("limitations") or [] if str(value).strip()]
        limitation_count += len(limitations)
        if limitations and len(limitation_examples) < 50:
            limitation_examples.append({"matter_document_id": document_id, "limitations": limitations})

    statistics = {
        "selected_document_count": int(coverage.get("selected_document_count", 0)),
        "analyzed_document_count": len(analyses),
        "skipped_document_count": int(coverage.get("skipped_document_count", 0)),
        "failed_document_count": int(coverage.get("failed_document_count", 0)),
        "partial_coverage_document_count": int(coverage.get("partial_coverage_document_count", 0)),
        "invalid_result_count": int(coverage.get("invalid_result_count", 0)),
        "determination_counts": dict(sorted(determination_counts.items())),
        "criterion_match_type_counts": dict(sorted(match_type_counts.items())),
        "criterion_match_counts": {
            key: dict(sorted(counts.items())) for key, counts in sorted(criterion_counts.items())
        },
    }
    grouped_near_misses: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for near_miss in near_misses:
        grouped_near_misses[near_miss["normalized_criterion_key"]].append(near_miss)
    recurring_patterns = []
    for normalized_key, occurrences in sorted(grouped_near_misses.items()):
        document_ids = {item["matter_document_id"] for item in occurrences}
        if len(document_ids) < 2:
            continue
        recurring_patterns.append(
            {
                "signal_id": f"NEAR_MISS_{normalized_key}",
                "criterion_key": normalized_key,
                "labels": sorted({item["label"] for item in occurrences}),
                "occurrence_count": len(occurrences),
                "document_count": len(document_ids),
                "representative_evidence": occurrences[:8],
            }
        )
    signals = {
        "near_miss_count": len(near_misses),
        "near_misses": near_misses,
        "recurring_near_miss_pattern_count": len(recurring_patterns),
        "recurring_near_miss_patterns": recurring_patterns,
        "document_clarification_candidate_count": len(document_candidates),
        "document_clarification_candidates": document_candidates,
        "document_limitation_count": limitation_count,
        "document_limitation_examples": limitation_examples,
        "document_limitation_examples_truncated": limitation_count > len(limitation_examples),
    }
    return statistics, signals


def _analysis_citation_ids(value: Any) -> set[str]:
    if isinstance(value, dict):
        result: set[str] = set()
        for key, child in value.items():
            if key in {"citation_ids", "paragraph_ids", "analyzed_paragraph_ids"} and isinstance(child, list):
                result.update(str(item) for item in child)
            else:
                result.update(_analysis_citation_ids(child))
        return result
    if isinstance(value, list):
        result: set[str] = set()
        for child in value:
            result.update(_analysis_citation_ids(child))
        return result
    return set()


def _validate_synthesis_refinement(
    result: dict[str, Any],
    analyses: list[dict[str, Any]],
    coverage: dict[str, Any],
    refinement_signals: dict[str, Any] | None = None,
) -> None:
    refinement = result.get("refinement_assessment")
    if not isinstance(refinement, dict):
        raise AssessmentError("refinement_assessment is required")
    outcome = refinement.get("outcome")
    questions = result.get("clarification_questions")
    if not isinstance(questions, list):
        raise AssessmentError("clarification_questions must be an array")
    dimensions = refinement.get("evaluated_dimensions")
    if not isinstance(dimensions, list):
        raise AssessmentError("refinement_assessment.evaluated_dimensions must be an array")
    dimension_names = [item.get("dimension") for item in dimensions if isinstance(item, dict)]
    if set(dimension_names) != REFINEMENT_DIMENSIONS or len(dimension_names) != len(REFINEMENT_DIMENSIONS):
        raise ValueError("refinement assessment must evaluate each required dimension exactly once")
    question_needed = any(
        isinstance(item, dict) and item.get("conclusion") == "QUESTION_NEEDED" for item in dimensions
    )
    sufficient = coverage.get("status") == "SUFFICIENT"
    if not sufficient and outcome != "INSUFFICIENT_EVIDENCE":
        raise ValueError("insufficient coverage requires INSUFFICIENT_EVIDENCE")
    if sufficient and outcome == "INSUFFICIENT_EVIDENCE":
        raise ValueError("sufficient coverage cannot return INSUFFICIENT_EVIDENCE")
    if outcome == "QUESTIONS_PROPOSED" and not questions:
        raise ValueError("QUESTIONS_PROPOSED requires at least one clarification question")
    if outcome == "NO_REFINEMENT_WARRANTED" and (questions or question_needed):
        raise ValueError("NO_REFINEMENT_WARRANTED requires no questions and no QUESTION_NEEDED dimensions")
    if questions and outcome != "QUESTIONS_PROPOSED":
        raise ValueError("clarification questions require QUESTIONS_PROPOSED")
    for question in questions:
        suggestions = question.get("suggested_answers") if isinstance(question, dict) else None
        if not isinstance(suggestions, list) or not 1 <= len(suggestions) <= 3:
            raise ValueError("clarification questions require one to three suggested answers")
        if any(not isinstance(value, str) or not value.strip() for value in suggestions):
            raise ValueError("clarification question suggested answers must be non-empty strings")
        if len({value.strip().casefold() for value in suggestions}) != len(suggestions):
            raise ValueError("clarification question suggested answers must be distinct")

    signals = refinement_signals or {}
    recurring_patterns = signals.get("recurring_near_miss_patterns") or []
    document_candidates = signals.get("document_clarification_candidates") or []
    if (recurring_patterns or document_candidates) and outcome != "QUESTIONS_PROPOSED":
        raise ValueError(
            "recurring near-miss patterns and document clarification candidates require clarification questions"
        )
    if len(questions) < len(recurring_patterns):
        raise ValueError(
            "clarification questions must include at least one question for each recurring near-miss pattern"
        )

    allowed = {
        str(row["matter_document_id"]): _analysis_citation_ids(row["analysis"])
        for row in analyses
    }
    evidence_groups = [item.get("evidence") for item in questions if isinstance(item, dict)]
    evidence_groups.extend(
        item.get("evidence")
        for item in dimensions
        if isinstance(item, dict) and item.get("conclusion") == "QUESTION_NEEDED"
    )
    for evidence in evidence_groups:
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("questions and QUESTION_NEEDED dimensions require evidence")
        for reference in evidence:
            if not isinstance(reference, dict):
                raise AssessmentError("refinement evidence must be an object")
            document_id = str(reference.get("matter_document_id") or "")
            paragraph_ids = reference.get("paragraph_ids")
            if document_id not in allowed:
                raise ValueError(f"refinement evidence references unknown document {document_id}")
            if not isinstance(paragraph_ids, list) or not paragraph_ids:
                raise ValueError("refinement evidence requires paragraph_ids")
            unknown = sorted({str(item) for item in paragraph_ids} - allowed[document_id])
            if unknown:
                raise ValueError(
                    f"refinement evidence for document {document_id} contains unknown paragraph IDs: "
                    + ", ".join(unknown)
                )

    for pattern in recurring_patterns:
        if not isinstance(pattern, dict):
            continue
        signal_id = str(pattern.get("signal_id") or "recurring near-miss pattern")
        pattern_evidence = pattern.get("representative_evidence") or []
        allowed_pattern_citations = {
            (str(item.get("matter_document_id") or ""), str(paragraph_id))
            for item in pattern_evidence
            if isinstance(item, dict)
            for paragraph_id in item.get("paragraph_ids") or []
        }
        covered = any(
            isinstance(question, dict)
            and any(
                isinstance(reference, dict)
                and any(
                    (str(reference.get("matter_document_id") or ""), str(paragraph_id))
                    in allowed_pattern_citations
                    for paragraph_id in reference.get("paragraph_ids") or []
                )
                for reference in question.get("evidence") or []
            )
            for question in questions
        )
        if not covered:
            raise ValueError(f"clarification questions must address recurring signal {signal_id}")


def _topic_key(value: Any, ordinal: int) -> str:
    candidate = "".join(character if character.isalnum() else "_" for character in str(value or "").lower())
    candidate = "_".join(part for part in candidate.split("_") if part)[:100]
    return candidate or f"topic_{ordinal}"


def _persist_synthesis(
    db: Session,
    assessment: MatterDefinitionAssessmentRun,
    result: dict[str, Any],
) -> None:
    assessment.guidance_refinement_status = "NOT_READY"
    assessment.guidance_refinement_workflow_run_id = None
    assessment.refined_matter_definition_revision_id = None
    assessment.guidance_refinement_error_message = None
    db.execute(
        delete(MatterDefinitionAssessmentQuestion).where(
            MatterDefinitionAssessmentQuestion.assessment_run_id == assessment.id
        )
    )
    seen_questions: set[str] = set()
    for item in result.get("clarification_questions") or []:
        if not isinstance(item, dict) or not str(item.get("question") or "").strip():
            continue
        normalized_question = " ".join(str(item["question"]).casefold().split())
        if normalized_question in seen_questions:
            continue
        seen_questions.add(normalized_question)
        priority = str(item.get("priority") or "MEDIUM").upper()
        if priority not in {"HIGH", "MEDIUM", "LOW"}:
            priority = "MEDIUM"
        db.add(
            MatterDefinitionAssessmentQuestion(
                assessment_run_id=assessment.id,
                question=str(item["question"]).strip(),
                rationale=str(item.get("rationale") or "Clarification would improve the review guidance").strip(),
                priority=priority,
                blocking=bool(item.get("blocking", False)),
                evidence=item.get("evidence") if isinstance(item.get("evidence"), list) else [],
                suggested_answers=[
                    str(value).strip()
                    for value in item.get("suggested_answers") or []
                    if str(value).strip()
                ][:3],
            )
        )

    if assessment.review_batch_id is None:
        return
    prior_for_assessment = db.scalar(
        select(BatchTopicTaxonomy).where(BatchTopicTaxonomy.source_assessment_run_id == assessment.id)
    )
    if prior_for_assessment is not None:
        db.delete(prior_for_assessment)
        db.flush()
    existing = list(
        db.scalars(
            select(BatchTopicTaxonomy)
            .where(BatchTopicTaxonomy.review_batch_id == assessment.review_batch_id)
            .order_by(BatchTopicTaxonomy.version.desc())
        )
    )
    for taxonomy in existing:
        if taxonomy.status == "ACTIVE":
            taxonomy.status = "RETIRED"
    db.flush()
    taxonomy = BatchTopicTaxonomy(
        review_batch_id=assessment.review_batch_id,
        source_assessment_run_id=assessment.id,
        version=(existing[0].version + 1) if existing else 1,
        status="ACTIVE",
    )
    db.add(taxonomy)
    db.flush()
    allowed_documents = set(
        db.scalars(
            select(ReviewBatchRunDocument.matter_document_id).where(
                ReviewBatchRunDocument.review_batch_run_id == assessment.review_batch_run_id
            )
        )
    )
    used_keys: set[str] = set()
    for ordinal, item in enumerate(result.get("topics") or [], start=1):
        if not isinstance(item, dict):
            continue
        key = _topic_key(item.get("topic_key") or item.get("key") or item.get("label"), ordinal)
        if key in used_keys:
            key = f"{key[:90]}_{ordinal}"
        used_keys.add(key)
        topic = BatchTopic(
            taxonomy_id=taxonomy.id,
            topic_key=key,
            label=str(item.get("label") or item.get("name") or key.replace("_", " ").title())[:200],
            description=str(item.get("description") or "").strip() or None,
            ordinal=ordinal,
        )
        db.add(topic)
        db.flush()
        assigned_documents: set[uuid.UUID] = set()
        for assignment in item.get("assignments") or []:
            if not isinstance(assignment, dict):
                continue
            try:
                document_id = uuid.UUID(str(assignment.get("matter_document_id") or assignment.get("document_id")))
            except (TypeError, ValueError):
                continue
            if document_id not in allowed_documents or document_id in assigned_documents:
                continue
            assigned_documents.add(document_id)
            confidence = min(1.0, max(0.0, float(assignment.get("confidence", 0.5))))
            db.add(
                BatchTopicAssignment(
                    review_batch_id=assessment.review_batch_id,
                    taxonomy_id=taxonomy.id,
                    topic_id=topic.id,
                    matter_document_id=document_id,
                    confidence=confidence,
                    evidence=assignment.get("evidence") if isinstance(assignment.get("evidence"), list) else [],
                )
            )


def synthesize_assessment(
    db: Session,
    assessment_id: uuid.UUID,
    *,
    model: Any | None = None,
) -> dict[str, Any]:
    assessment, workflow, revision, matter = _records(db, assessment_id)
    if assessment.synthesis_result is not None:
        return assessment.synthesis_result
    refresh_progress(db, assessment_id)
    db.refresh(assessment)
    analyses, partial, invalid = _load_document_analyses(db, assessment, matter)
    assessment.partial_coverage_count = partial
    assessment.invalid_result_count = invalid
    coverage = _coverage_envelope(
        assessment,
        successful=len(analyses),
        partial=partial,
        invalid=invalid,
    )
    corpus_statistics, refinement_signals = _synthesis_context(analyses, coverage)
    assessment.coverage_snapshot = coverage
    assessment.status = "SYNTHESIZING"
    step = _step(db, workflow, ordinal=4, role_key="assessment_synthesis")
    step.status = "RUNNING"
    step.started_at = step.started_at or utcnow()
    if coverage["status"] == "INSUFFICIENT":
        result = {
            "coverage": coverage,
            "corpus_statistics": corpus_statistics,
            "status": "INSUFFICIENT_COVERAGE",
            "narrative": "The assessment did not meet the minimum coverage required for substantive fit conclusions.",
            "findings": [],
            "topics": [],
            "refinement_assessment": {
                "outcome": "INSUFFICIENT_EVIDENCE",
                "rationale": "Coverage did not meet the minimum required to evaluate Matter Definition refinements.",
                "evaluated_dimensions": [
                    {
                        "dimension": dimension,
                        "conclusion": "NOT_EVALUATED",
                        "rationale": "This dimension was not evaluated because corpus coverage was insufficient.",
                        "evidence": [],
                    }
                    for dimension in sorted(REFINEMENT_DIMENSIONS)
                ],
            },
            "clarification_questions": [],
        }
    else:
        version = _skill_version(db, assessment, "assessment_synthesis")
        model = model if model is not None else _pinned_model(assessment, "assessment_synthesis")
        result, _ = asyncio.run(
            execute_skill_run(
                db,
                workflow=workflow,
                step=step,
                skill_version=version,
                scope_type="MATTER_DEFINITION_ASSESSMENT",
                scope_id=assessment.id,
                stable_context={"matter_definition": revision.content_markdown},
                dynamic_input={
                    "coverage": coverage,
                    "corpus_statistics": corpus_statistics,
                    "refinement_signals": refinement_signals,
                    "document_analyses": analyses,
                },
                cache_identity={
                    "tenant_id": str(workflow.tenant_id),
                    "matter_id": str(matter.id),
                    "revision_hash": assessment.definition_content_hash,
                    "skill_version_id": str(version.id),
                    "assessment_id": str(assessment.id),
                },
                output_validators=(
                    lambda output: _validate_synthesis_refinement(
                        output,
                        analyses,
                        coverage,
                        refinement_signals,
                    ),
                ),
                model=model,
            )
        )
        result = {
            **result,
            "coverage": coverage,
            "corpus_statistics": corpus_statistics,
            "status": "COMPLETED",
        }
    _persist_synthesis(db, assessment, result)
    assessment.synthesis_result = result
    step.status = "COMPLETED"
    step.completed_count = 1
    step.completed_at = utcnow()
    db.commit()
    return result


def fail_document(db: Session, assessment_id: uuid.UUID, document_id: uuid.UUID, message: str) -> None:
    assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
    if assessment is None or assessment.review_batch_run_id is None:
        return
    run_document = db.get(ReviewBatchRunDocument, (assessment.review_batch_run_id, document_id))
    if run_document is not None:
        run_document.status = "FAILED"
        run_document.completed_at = utcnow()
    db.commit()


def complete_assessment(db: Session, assessment_id: uuid.UUID) -> None:
    assessment, workflow, _, _ = _records(db, assessment_id)
    refresh_progress(db, assessment_id)
    db.refresh(assessment)
    now = utcnow()
    final_status = (
        "COMPLETED_WITH_ERRORS"
        if assessment.failed_count
        or assessment.skipped_count
        or assessment.partial_coverage_count
        or assessment.invalid_result_count
        else "COMPLETED"
    )
    assessment.status = final_status
    assessment.completed_at = now
    workflow.status = final_status
    workflow.completed_at = now
    step = _step(db, workflow, ordinal=3, role_key="document_analysis", total_count=assessment.selected_count)
    step.status = final_status
    step.completed_at = now
    synthesis_step = _step(db, workflow, ordinal=4, role_key="assessment_synthesis")
    if synthesis_step.status not in {"COMPLETED", "FAILED"}:
        synthesis_step.status = final_status
        synthesis_step.completed_at = now
    review_run = db.get(ReviewBatchRun, assessment.review_batch_run_id)
    if review_run is not None:
        review_run.status = final_status
        review_run.completed_at = now
    db.commit()


def fail_assessment(db: Session, assessment_id: uuid.UUID, message: str) -> None:
    assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
    if assessment is None or assessment.status == "CANCELED":
        return
    assessment.status = "FAILED"
    assessment.error_message = message[:4000]
    assessment.completed_at = utcnow()
    workflow = db.get(WorkflowRun, assessment.workflow_run_id)
    if workflow is not None:
        workflow.status = "FAILED"
        workflow.error_message = message[:4000]
        workflow.completed_at = assessment.completed_at
        steps = db.scalars(
            select(WorkflowStepRun).where(
                WorkflowStepRun.workflow_run_id == workflow.id,
                WorkflowStepRun.status.in_(("QUEUED", "RUNNING")),
            )
        )
        for step in steps:
            latest_failure = db.scalar(
                select(SkillRun)
                .where(
                    SkillRun.workflow_step_run_id == step.id,
                    SkillRun.status == "FAILED",
                )
                .order_by(SkillRun.created_at.desc())
                .limit(1)
            )
            step.status = "FAILED"
            step.failed_count = max(1, step.failed_count)
            step.error_message = (
                latest_failure.error_message if latest_failure and latest_failure.error_message else message
            )[:4000]
            step.completed_at = assessment.completed_at
    db.commit()
