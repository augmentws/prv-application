import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.artifact_gateway import get_preferred_text_source, store_derived_artifact
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
    Matter,
    MatterDefinitionAssessmentQuery,
    MatterDefinitionAssessmentRun,
    MatterDefinitionRevision,
    MatterDocument,
    MetadataDefinition,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    SearchIndexGeneration,
    SkillDefinitionVersion,
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
    output, _ = asyncio.run(
        execute_skill_run(
            db,
            workflow=workflow,
            step=step,
            skill_version=version,
            scope_type="MATTER_DEFINITION_REVISION",
            scope_id=revision.id,
            stable_context={"matter_definition": revision.content_markdown},
            dynamic_input={"matter_id": str(matter.id), "requested_document_count": assessment.requested_document_count},
            cache_identity={
                "tenant_id": str(workflow.tenant_id),
                "matter_id": str(matter.id),
                "revision_hash": assessment.definition_content_hash,
                "skill_version_id": str(version.id),
            },
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
        stable_prefix=revision.content_markdown + version.instructions + json.dumps(version.output_schema, sort_keys=True),
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
    db.commit()
    return {"status": "COMPLETED", "artifact_id": str(artifact.artifact_id)}


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
    final_status = "COMPLETED_WITH_ERRORS" if assessment.failed_count or assessment.skipped_count else "COMPLETED"
    assessment.status = final_status
    assessment.completed_at = now
    workflow.status = final_status
    workflow.completed_at = now
    step = _step(db, workflow, ordinal=3, role_key="document_analysis", total_count=assessment.selected_count)
    step.status = final_status
    step.completed_at = now
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
    db.commit()
