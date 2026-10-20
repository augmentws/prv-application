from __future__ import annotations

import asyncio
import copy
import json
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis_input_manifests import AnalysisInputManifestError, AnalysisInputManifestReader
from app.analysis_task_evidence import (
    EVIDENCE_LOCALIZATION_VERSION,
    build_conditional_evidence_questions,
    collect_conditional_evidence,
    primary_decision_answers,
    select_evidence_question_keys,
)
from app.analysis_tasks import (
    MatterAnalysisTaskConflict,
    MatterAnalysisTaskInputTooLarge,
    decision_provider_engine_key,
    json_content_hash,
)
from app.artifact_gateway import TextSourceReference, get_preferred_text_source, read_text_source
from app.config import Settings, get_settings
from app.decision_engine import DecisionRequest
from app.decision_execution import execute_decision_request
from app.decision_policy import evaluate_decision_policy
from app.decision_specifications import DecisionSpecification
from app.document_evidence import segment_paragraphs
from app.document_metadata import event_value, value_columns
from app.execution_accounting import ProviderUsageContext, persist_model_invocations
from app.jev_decision_engine import get_shared_decision_engine_registry
from app.models import (
    AnalysisInputManifest,
    DocumentMetadataCurrent,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    MatterDocument,
    MetadataDefinition,
    ReviewBatch,
    ReviewBatchDocument,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewBatchRunValue,
    ReviewDecisionResult,
    SkillDefinitionVersion,
    WorkflowRun,
    WorkflowStepRun,
    utcnow,
)
from app.review_decision_results import record_review_decision_result
from app.skill_execution import complete_skill_run, create_skill_run, fail_skill_run
from app.workflow_specs import (
    MATTER_ANALYSIS_TASK_BATCH_SPEC,
    MATTER_ANALYSIS_TASK_PLAYGROUND_SPEC,
    binding_snapshot,
    resolve_workflow_skill_bindings,
)
from app.workflows.dispatcher import enqueue_analysis_task_playground


def materialize_decision_recommendations(
    db: Session,
    *,
    matter_id: uuid.UUID,
    review_run_id: uuid.UUID,
    document_id: uuid.UUID,
    decision_result: ReviewDecisionResult,
    recommendations: dict[str, Any],
) -> None:
    """Persist accepted policy outputs as isolated, typed batch-run coding values."""

    candidates: list[tuple[str, dict[str, Any]]] = []
    for recommendation in recommendations.values():
        if not isinstance(recommendation, dict) or recommendation.get("matched") is not True:
            continue
        definition_key = recommendation.get("metadata_definition_key")
        if isinstance(definition_key, str) and "value" in recommendation:
            candidates.append((definition_key, recommendation))
    if not candidates:
        return

    definitions = {
        definition.key: definition
        for definition in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter_id,
                MetadataDefinition.status == "ACTIVE",
                MetadataDefinition.key.in_({key for key, _ in candidates}),
            )
        )
    }
    seen: dict[uuid.UUID, list[Any]] = defaultdict(list)
    for definition_key, recommendation in candidates:
        definition = definitions.get(definition_key)
        if definition is None:
            raise MatterAnalysisTaskConflict(
                f"Decision recommendation targets an unavailable metadata field: {definition_key}"
            )
        raw_value = recommendation.get("value")
        existing_values = seen[definition.id]
        if raw_value in existing_values:
            continue
        if (
            definition.cardinality == "SINGLE"
            and existing_values
            and existing_values[0] != raw_value
        ):
            raise MatterAnalysisTaskConflict(
                f"Decision policy produced conflicting values for metadata field: {definition_key}"
            )
        try:
            columns = value_columns(raw_value, definition)
        except ValueError as exc:
            raise MatterAnalysisTaskConflict(
                f"Decision recommendation is invalid for metadata field '{definition_key}': {exc}"
            ) from exc
        raw_confidence = recommendation.get("confidence", recommendation.get("probability"))
        confidence = (
            float(raw_confidence)
            if isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool)
            else None
        )
        confidence_kind = recommendation.get("confidence_kind")
        if confidence is None:
            confidence_kind = "NONE"
        elif confidence_kind not in {
            "PROVIDER_CONFIDENCE",
            "SELECTED_PROBABILITY",
            "DERIVED_PROBABILITY",
        }:
            confidence_kind = "SELECTED_PROBABILITY"
        db.add(
            ReviewBatchRunValue(
                review_batch_run_id=review_run_id,
                matter_document_id=document_id,
                metadata_definition_id=definition.id,
                value_ordinal=len(existing_values) if definition.cardinality == "MULTIPLE" else 0,
                confidence=confidence,
                confidence_kind=confidence_kind,
                review_decision_result_id=decision_result.id,
                question_key=recommendation.get("question_key"),
                **columns,
            )
        )
        existing_values.append(raw_value)


def _document_metadata_state(db: Session, document: MatterDocument) -> dict[str, Any]:
    rows = db.execute(
        select(DocumentMetadataCurrent, MetadataDefinition)
        .join(MetadataDefinition, MetadataDefinition.id == DocumentMetadataCurrent.metadata_definition_id)
        .where(
            DocumentMetadataCurrent.matter_document_id == document.id,
            DocumentMetadataCurrent.resolution_state.in_(("VALUE", "CONFLICTED")),
            MetadataDefinition.status == "ACTIVE",
        )
        .order_by(MetadataDefinition.key, DocumentMetadataCurrent.value_ordinal)
    ).all()
    grouped: dict[str, list[Any]] = defaultdict(list)
    cardinality: dict[str, str] = {}
    for current, definition in rows:
        grouped[definition.key].append(event_value(current))
        cardinality[definition.key] = definition.cardinality
    return {
        key: values if cardinality[key] == "MULTIPLE" else values[0]
        for key, values in grouped.items()
    }


def _state_path_exists(state: Any, path: str) -> bool:
    current = state
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def build_document_review_state(
    db: Session,
    *,
    matter: Matter,
    document: MatterDocument,
    decision_context: dict[str, Any],
    source_content_hash: str,
    source_text: str,
) -> tuple[dict[str, Any], Any]:
    paragraph_map = segment_paragraphs(source_text)
    state = {
        "matter": {
            "id": str(matter.id),
            "decision_context": decision_context,
        },
        "runtime": {"today": utcnow().date().isoformat()},
        "document": {
            "id": str(document.id),
            "collection_item_id": str(document.collection_item_id),
            "source_content_hash": source_content_hash,
            "text": source_text,
            "metadata": _document_metadata_state(db, document),
            "paragraphs": [paragraph.as_dict() for paragraph in paragraph_map.paragraphs],
        },
    }
    return state, paragraph_map


_STRUCTURED_DECISION_CONTEXT_KEYS = (
    "controlling_guidance",
    "responsiveness_scope",
    "inclusion_criteria",
    "exclusion_criteria",
    "issue_definitions",
    "key_entities",
    "date_scope",
    "terminology",
    "examples",
)


def _runtime_decision_context(decision_context: dict[str, Any]) -> dict[str, Any]:
    """Remove exact source material once its compiled execution context is available.

    The published specification retains source_material for review and provenance. Sending it
    again on every document evaluation duplicates the structured criteria produced by the
    compiler and consumes a substantial portion of the provider token budget.
    """

    runtime_context = copy.deepcopy(decision_context)
    has_structured_context = any(
        bool(runtime_context.get(key)) for key in _STRUCTURED_DECISION_CONTEXT_KEYS
    )
    if has_structured_context and runtime_context.get("source_material"):
        runtime_context.pop("source_material", None)
    return runtime_context


def _classification_state(
    state: dict[str, Any],
    *,
    required_paths: list[str],
) -> dict[str, Any]:
    """Avoid sending two equivalent representations of the document to classification."""

    projected = copy.deepcopy(state)
    document = projected.get("document")
    if not isinstance(document, dict):
        return projected
    required = set(required_paths)
    requires_text = "document.text" in required
    requires_paragraphs = "document.paragraphs" in required
    if requires_text and not requires_paragraphs:
        document.pop("paragraphs", None)
    elif requires_paragraphs and not requires_text:
        document.pop("text", None)
    return projected


def queue_analysis_task_playground(
    db: Session,
    *,
    matter: Matter,
    task: MatterAnalysisTask,
    task_version: MatterAnalysisTaskVersion,
    review_batch_id: uuid.UUID,
    matter_document_id: uuid.UUID,
    initiated_by_user_id: uuid.UUID,
) -> tuple[WorkflowRun, ReviewBatchRun]:
    if task.matter_id != matter.id or task_version.matter_analysis_task_id != task.id:
        raise MatterAnalysisTaskConflict("Analysis task version does not belong to this matter")
    if task_version.status != "PUBLISHED" or task_version.compilation_status != "READY":
        raise MatterAnalysisTaskConflict("Playground evaluation requires a ready, published task version")
    batch = db.scalar(
        select(ReviewBatch).where(
            ReviewBatch.id == review_batch_id,
            ReviewBatch.matter_id == matter.id,
            ReviewBatch.status == "READY",
        )
    )
    if batch is None:
        raise MatterAnalysisTaskConflict("A ready review batch is required")
    if db.get(ReviewBatchDocument, (batch.id, matter_document_id)) is None:
        raise MatterAnalysisTaskConflict("Document is not in the selected review batch")
    try:
        resolved = resolve_workflow_skill_bindings(
            db,
            workflow_key=MATTER_ANALYSIS_TASK_PLAYGROUND_SPEC.key,
            tenant_id=matter.client.tenant_id,
        )
    except ValueError as exc:
        raise MatterAnalysisTaskConflict(f"Decision evaluation managed skill is not configured: {exc}") from exc
    bindings = binding_snapshot(resolved)
    role_binding = bindings["decision_evaluation"]
    engine_key = decision_provider_engine_key(task_version.decision_provider)
    model_key = task_version.decision_model or str(role_binding["model_key"])
    workflow_id = f"analysis-task:{task.id}:version:{task_version.version}:playground:{uuid.uuid4()}"
    workflow = WorkflowRun(
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        matter_id=matter.id,
        workflow_key=MATTER_ANALYSIS_TASK_PLAYGROUND_SPEC.key,
        code_version=MATTER_ANALYSIS_TASK_PLAYGROUND_SPEC.code_version,
        dbos_workflow_id=workflow_id,
        status="QUEUED",
        input_snapshot={
            "task_id": str(task.id),
            "task_version_id": str(task_version.id),
            "task_version": task_version.version,
            "definition_content_hash": task_version.definition_content_hash,
            "specification_content_hash": task_version.specification_content_hash,
            "review_batch_id": str(batch.id),
            "matter_document_id": str(matter_document_id),
        },
        binding_snapshot=bindings,
        configuration_snapshot={
            "engine_key": engine_key,
            "model_key": model_key,
            "decision_provider": task_version.decision_provider,
            "result_policy": "ISOLATED",
        },
        progress={"stage": "QUEUED"},
        initiated_by_user_id=initiated_by_user_id,
    )
    db.add(workflow)
    db.flush()
    step = WorkflowStepRun(
        workflow_run_id=workflow.id,
        role_key="decision_evaluation",
        ordinal=1,
        status="QUEUED",
        total_count=1,
    )
    db.add(step)
    review_run = ReviewBatchRun(
        review_batch_id=batch.id,
        run_type="WORKFLOW",
        purpose="REVIEW",
        status="QUEUED",
        result_policy="ISOLATED",
        workflow_run_record_id=workflow.id,
        configuration_snapshot={
            "mode": "PLAYGROUND",
            "task_id": str(task.id),
            "task_version_id": str(task_version.id),
            "task_version": task_version.version,
            "engine_key": engine_key,
            "model_key": model_key,
            "decision_provider": task_version.decision_provider,
        },
        initiated_by_user_id=initiated_by_user_id,
        dbos_workflow_id=workflow_id,
    )
    db.add(review_run)
    db.flush()
    db.add(
        ReviewBatchRunDocument(
            review_batch_run_id=review_run.id,
            matter_document_id=matter_document_id,
            status="QUEUED",
        )
    )
    db.flush()
    enqueue_analysis_task_playground(db, workflow_id, str(workflow.id))
    return workflow, review_run


def execute_analysis_task_document(
    db: Session,
    workflow_run_id: uuid.UUID,
    matter_document_id: uuid.UUID,
    *,
    complete_run: bool,
    settings: Settings | None = None,
    registry=None,
) -> ReviewDecisionResult:
    settings = settings or get_settings()
    workflow = db.get(WorkflowRun, workflow_run_id)
    if workflow is None or workflow.workflow_key not in {
        MATTER_ANALYSIS_TASK_PLAYGROUND_SPEC.key,
        MATTER_ANALYSIS_TASK_BATCH_SPEC.key,
    }:
        raise MatterAnalysisTaskConflict("Analysis task workflow not found")
    review_run = db.scalar(
        select(ReviewBatchRun).where(ReviewBatchRun.workflow_run_record_id == workflow.id)
    )
    if review_run is None:
        raise MatterAnalysisTaskConflict("Analysis task run provenance is incomplete")
    run_document = db.get(ReviewBatchRunDocument, (review_run.id, matter_document_id))
    if run_document is None:
        raise MatterAnalysisTaskConflict("Analysis task document snapshot is unavailable")
    existing = db.scalar(
        select(ReviewDecisionResult).where(
            ReviewDecisionResult.review_batch_run_id == review_run.id,
            ReviewDecisionResult.matter_document_id == matter_document_id,
        )
    )
    if existing is not None:
        if run_document.status != "COMPLETED":
            run_document.status = "COMPLETED"
            run_document.failure_code = None
            run_document.failure_message = None
            run_document.failure_retriable = False
            run_document.completed_at = run_document.completed_at or utcnow()
            db.commit()
        return existing
    if workflow.status == "CANCELED" or run_document.status == "SKIPPED":
        raise MatterAnalysisTaskConflict("Analysis task batch run was canceled")
    snapshot = workflow.input_snapshot
    task_version = db.get(MatterAnalysisTaskVersion, uuid.UUID(snapshot["task_version_id"]))
    document = db.get(MatterDocument, matter_document_id)
    matter = db.get(Matter, workflow.matter_id)
    step = db.scalar(
        select(WorkflowStepRun).where(
            WorkflowStepRun.workflow_run_id == workflow.id,
            WorkflowStepRun.ordinal == 1,
        )
    )
    role = workflow.binding_snapshot.get("decision_evaluation") or {}
    skill_version_id = role.get("skill_definition_version_id")
    skill_version = db.get(SkillDefinitionVersion, uuid.UUID(skill_version_id)) if skill_version_id else None
    if any(
        record is None
        for record in (task_version, document, matter, review_run, step, skill_version)
    ):
        raise MatterAnalysisTaskConflict("Analysis task workflow provenance is incomplete")
    assert task_version is not None
    assert document is not None
    assert matter is not None
    assert review_run is not None
    assert step is not None
    assert skill_version is not None
    if workflow.client_id is None:
        raise MatterAnalysisTaskConflict("Analysis task workflow has no client scope")
    if task_version.status != "PUBLISHED" or task_version.compilation_status != "READY":
        raise MatterAnalysisTaskConflict("Pinned task version is no longer executable")
    if task_version.decision_specification is None:
        raise MatterAnalysisTaskConflict("Pinned task version has no Decision Specification")
    specification = DecisionSpecification.model_validate(task_version.decision_specification)
    if review_run.analysis_input_manifest_id is not None:
        manifest = db.get(AnalysisInputManifest, review_run.analysis_input_manifest_id)
        ordinal = run_document.input_manifest_ordinal
        if (
            manifest is None
            or manifest.owner_type != "REVIEW_BATCH_RUN"
            or manifest.owner_id != review_run.id
            or manifest.scope_type != "MATTER"
            or manifest.scope_id != matter.id
            or manifest.input_contract.get("key") != "question-answering.population"
            or manifest.input_contract.get("version") != 1
            or manifest.input_contract.get("resolver_version")
            != "question-answering-population-resolver-v1"
            or ordinal is None
        ):
            raise MatterAnalysisTaskConflict("Whole-matter input manifest provenance is invalid")
        try:
            row = AnalysisInputManifestReader(
                manifest,
                actor_user_id=workflow.initiated_by_user_id,
            ).rows(ordinal - 1, 1)[0]
        except (AnalysisInputManifestError, IndexError) as exc:
            raise MatterAnalysisTaskConflict("Whole-matter input manifest is unreadable") from exc
        if row["matter_document_id"] != str(document.id):
            raise MatterAnalysisTaskConflict("Whole-matter input manifest document order changed")
        if row["input_status"] != "READY":
            raise MatterAnalysisTaskConflict(
                f"Document input was unavailable when the population was frozen: {row['failure_code']}"
            )
        frozen_source = row["resolved_inputs"]["text_source"]
        source_artifact_id = uuid.UUID(row["source_artifact_id"])
        source_content_hash = row["source_content_hash"]
        source_text = read_text_source(
            TextSourceReference(
                artifact_id=source_artifact_id,
                role=frozen_source["role"],
                media_type=frozen_source["media_type"],
                original_filename=frozen_source["original_filename"],
                content_hash=source_content_hash,
                byte_length=int(frozen_source["byte_length"]),
            ),
            record_type=frozen_source["record_type"],
            actor_user_id=workflow.initiated_by_user_id,
            tenant_id=workflow.tenant_id,
            client_id=workflow.client_id,
        )
        if source_text is None:
            raise MatterAnalysisTaskConflict("The pinned text source has no faithful text")
    else:
        source = get_preferred_text_source(
            collection_item_id=document.collection_item_id,
            actor_user_id=workflow.initiated_by_user_id,
            tenant_id=workflow.tenant_id,
            client_id=workflow.client_id,
        )
        if source is None:
            raise MatterAnalysisTaskConflict("Document has no faithful text source")
        source_artifact_id = source.artifact_id
        source_content_hash = source.content_hash
        source_text = source.text
    decision_context = specification.decision_context.model_dump(mode="json")
    if not decision_context.get("source_material"):
        # Specifications published before exact source material was embedded still receive the
        # complete reviewed definition. This avoids silently reducing a substantive matter
        # definition to a sparse generated summary at the provider boundary.
        decision_context["reviewed_task_definition"] = {
            "content_hash": task_version.definition_content_hash,
            "markdown": task_version.definition_markdown,
        }
    state, paragraph_map = build_document_review_state(
        db,
        matter=matter,
        document=document,
        decision_context=_runtime_decision_context(decision_context),
        source_content_hash=source_content_hash,
        source_text=source_text,
    )
    missing_paths = [
        path for path in specification.state_contract.required_paths if not _state_path_exists(state, path)
    ]
    if missing_paths:
        raise MatterAnalysisTaskConflict(
            f"State builder cannot provide required paths: {', '.join(missing_paths)}"
        )
    state = _classification_state(
        state,
        required_paths=specification.state_contract.required_paths,
    )
    state_characters = len(json.dumps(state, ensure_ascii=False, separators=(",", ":")))
    if state_characters > settings.analysis_task_decision_max_characters:
        raise MatterAnalysisTaskInputTooLarge(
            actual_characters=state_characters,
            maximum_characters=settings.analysis_task_decision_max_characters,
        )
    now = utcnow()
    if complete_run:
        workflow.status = "RUNNING"
        workflow.started_at = workflow.started_at or now
        workflow.progress = {"stage": "EVALUATING"}
        review_run.status = "RUNNING"
        review_run.started_at = review_run.started_at or now
        step.status = "RUNNING"
        step.started_at = step.started_at or now
    run_document.status = "IN_PROGRESS"
    run_document.started_at = now
    request_input = {
        "phase": "CLASSIFICATION",
        "state": state,
        "questions": {
            key: question.model_dump(mode="json") for key, question in specification.questions.items()
        },
        "task_version": {
            "id": str(task_version.id),
            "definition_content_hash": task_version.definition_content_hash,
            "specification_content_hash": task_version.specification_content_hash,
            "decision_context_hash": json_content_hash(decision_context),
        },
    }
    skill_run = create_skill_run(
        db,
        workflow=workflow,
        step=step,
        skill_version=skill_version,
        scope_type="MATTER_DOCUMENT",
        scope_id=document.id,
        request_input=request_input,
    )
    engine_key = str(workflow.configuration_snapshot.get("engine_key") or "jev")
    model_key = str(workflow.configuration_snapshot.get("model_key") or skill_version.model_key)
    model_settings = {**skill_version.model_policy, "engine_key": engine_key}
    request = DecisionRequest(
        state=state,
        questions=specification.questions,
        model_key=model_key,
        model_settings=model_settings,
        limits=skill_version.limits,
        run_id=str(skill_run.id),
        request_type="analysis-task-classification",
        idempotency_key=json_content_hash(
            {
                "phase": "CLASSIFICATION",
                "task_version_id": str(task_version.id),
                "source_content_hash": source_content_hash,
                "state_hash": json_content_hash(state),
                "model_key": model_key,
            }
        ),
    )
    decision_registry = registry or get_shared_decision_engine_registry(settings)
    usage_job_type = (
        "MATTER_ANALYSIS_TASK_PLAYGROUND"
        if workflow.workflow_key == MATTER_ANALYSIS_TASK_PLAYGROUND_SPEC.key
        else "MATTER_ANALYSIS_TASK_BATCH"
    )
    try:
        execution = asyncio.run(
            execute_decision_request(
                request,
                engine_key=engine_key,
                registry=decision_registry,
                settings=settings,
            )
        )
        invocations = persist_model_invocations(
            db,
            execution.invocations,
            skill_run_id=skill_run.id,
            usage_context=ProviderUsageContext(
                tenant_id=workflow.tenant_id,
                client_id=workflow.client_id,
                matter_id=workflow.matter_id,
                started_by_user_id=workflow.initiated_by_user_id,
                job_type=usage_job_type,
                job_id=workflow.id,
                job_created_at=workflow.created_at,
                details={
                    "phase": "CLASSIFICATION",
                    "workflow_key": workflow.workflow_key,
                    "role_key": step.role_key,
                    "task_version_id": str(task_version.id),
                    "matter_document_id": str(document.id),
                },
            ),
        )
        complete_skill_run(db, skill_run, refresh_workflow_usage=complete_run)
    except Exception as exc:
        fail_skill_run(skill_run, exc)
        raise
    primary_answers = primary_decision_answers(specification, execution.decision.answers)
    primary_envelope = execution.decision.model_copy(update={"answers": primary_answers})
    recommendations, routes = evaluate_decision_policy(
        specification.decision_policy,
        primary_answers,
        specification.questions,
    )
    evidence_question_keys = select_evidence_question_keys(
        specification,
        recommendations,
        routes,
    )
    evidence_questions, evidence_plans = build_conditional_evidence_questions(
        specification,
        paragraph_map,
        evidence_question_keys,
    )
    evidence_skill_run = None
    evidence_answers = {}
    if evidence_questions:
        evidence_state = {
            "document": {
                "id": str(document.id),
                "source_content_hash": source_content_hash,
                "paragraphs": [paragraph.as_dict() for paragraph in paragraph_map.paragraphs],
            },
            "classification": {
                "answers": {
                    key: primary_answers[key].model_dump(mode="json") for key in evidence_question_keys
                }
            },
        }
        evidence_request_input = {
            "phase": "EVIDENCE_LOCALIZATION",
            "state": evidence_state,
            "questions": {
                key: question.model_dump(mode="json") for key, question in evidence_questions.items()
            },
            "evidence_localization_version": EVIDENCE_LOCALIZATION_VERSION,
            "task_version": {
                "id": str(task_version.id),
                "definition_content_hash": task_version.definition_content_hash,
                "specification_content_hash": task_version.specification_content_hash,
            },
        }
        evidence_skill_run = create_skill_run(
            db,
            workflow=workflow,
            step=step,
            skill_version=skill_version,
            scope_type="MATTER_DOCUMENT_EVIDENCE",
            scope_id=document.id,
            request_input=evidence_request_input,
        )
        evidence_request = DecisionRequest(
            state=evidence_state,
            questions=evidence_questions,
            model_key=model_key,
            model_settings=model_settings,
            limits=skill_version.limits,
            run_id=str(evidence_skill_run.id),
            request_type="analysis-task-evidence-localization",
            idempotency_key=json_content_hash(
                {
                    "phase": "EVIDENCE_LOCALIZATION",
                    "task_version_id": str(task_version.id),
                    "source_content_hash": source_content_hash,
                    "classification_answers": evidence_state["classification"]["answers"],
                    "question_keys": evidence_question_keys,
                    "model_key": model_key,
                    "evidence_localization_version": EVIDENCE_LOCALIZATION_VERSION,
                }
            ),
        )
        try:
            evidence_execution = asyncio.run(
                execute_decision_request(
                    evidence_request,
                    engine_key=engine_key,
                    registry=decision_registry,
                    settings=settings,
                )
            )
            persist_model_invocations(
                db,
                evidence_execution.invocations,
                skill_run_id=evidence_skill_run.id,
                usage_context=ProviderUsageContext(
                    tenant_id=workflow.tenant_id,
                    client_id=workflow.client_id,
                    matter_id=workflow.matter_id,
                    started_by_user_id=workflow.initiated_by_user_id,
                    job_type=usage_job_type,
                    job_id=workflow.id,
                    job_created_at=workflow.created_at,
                    details={
                        "phase": "EVIDENCE_LOCALIZATION",
                        "workflow_key": workflow.workflow_key,
                        "role_key": step.role_key,
                        "task_version_id": str(task_version.id),
                        "matter_document_id": str(document.id),
                    },
                ),
            )
            complete_skill_run(db, evidence_skill_run, refresh_workflow_usage=complete_run)
            evidence_answers = evidence_execution.decision.answers
        except Exception as exc:
            fail_skill_run(evidence_skill_run, exc)
            raise
    evidence, evidence_complete = collect_conditional_evidence(
        evidence_plans,
        evidence_answers,
    )
    result = record_review_decision_result(
        db,
        workflow_run_id=workflow.id,
        review_batch_run_id=review_run.id,
        matter_document_id=document.id,
        task_version_id=task_version.id,
        evaluation_skill_run_id=skill_run.id,
        model_invocation_id=invocations[0].id,
        engine_key=engine_key,
        source_artifact_id=source_artifact_id,
        source_content_hash=source_content_hash,
        state=state,
        paragraph_map_version=paragraph_map.version,
        envelope=primary_envelope,
        status="COMPLETED" if evidence_complete else "PARTIAL",
        recommendations=recommendations,
        routes=routes,
        evidence=evidence,
        coverage_details={
            "input_complete": True,
            "evidence_complete": evidence_complete,
            "evidence_target_question_count": len(evidence_question_keys),
            "evidence_localization_request_count": 1 if evidence_questions else 0,
            "paragraph_count": len(paragraph_map.paragraphs),
        },
        evidence_skill_run_id=evidence_skill_run.id if evidence_skill_run is not None else None,
    )
    materialize_decision_recommendations(
        db,
        matter_id=matter.id,
        review_run_id=review_run.id,
        document_id=document.id,
        decision_result=result,
        recommendations=recommendations,
    )
    completed_at = utcnow()
    run_document.status = "COMPLETED"
    run_document.failure_code = None
    run_document.failure_message = None
    run_document.failure_retriable = False
    run_document.completed_at = completed_at
    if complete_run:
        review_run.status = "COMPLETED"
        review_run.processed_document_count = 1
        review_run.completed_at = completed_at
        step.status = "COMPLETED"
        step.completed_count = 1
        step.completed_at = completed_at
        workflow.status = "COMPLETED"
        workflow.progress = {
            "stage": "COMPLETED",
            "result_id": str(result.id),
            "result_status": result.status,
        }
        workflow.completed_at = completed_at
    db.commit()
    return result


def execute_analysis_task_playground(
    db: Session,
    workflow_run_id: uuid.UUID,
    *,
    settings: Settings | None = None,
    registry=None,
) -> ReviewDecisionResult:
    workflow = db.get(WorkflowRun, workflow_run_id)
    if workflow is None or workflow.workflow_key != MATTER_ANALYSIS_TASK_PLAYGROUND_SPEC.key:
        raise MatterAnalysisTaskConflict("Analysis task playground workflow not found")
    document_id = workflow.input_snapshot.get("matter_document_id")
    if document_id is None:
        raise MatterAnalysisTaskConflict("Playground document snapshot is unavailable")
    return execute_analysis_task_document(
        db,
        workflow_run_id,
        uuid.UUID(str(document_id)),
        complete_run=True,
        settings=settings,
        registry=registry,
    )


def fail_analysis_task_playground(db: Session, workflow_run_id: uuid.UUID, message: str) -> None:
    workflow = db.get(WorkflowRun, workflow_run_id)
    if workflow is None or workflow.status == "COMPLETED":
        return
    now = utcnow()
    workflow.status = "FAILED"
    workflow.error_message = message[:4000]
    workflow.progress = {"stage": "FAILED"}
    workflow.completed_at = now
    step = db.scalar(select(WorkflowStepRun).where(WorkflowStepRun.workflow_run_id == workflow.id))
    if step is not None:
        step.status = "FAILED"
        step.failed_count = 1
        step.error_message = message[:4000]
        step.completed_at = now
    review_run = db.scalar(
        select(ReviewBatchRun).where(ReviewBatchRun.workflow_run_record_id == workflow.id)
    )
    if review_run is not None:
        review_run.status = "FAILED"
        review_run.error_message = message[:4000]
        review_run.completed_at = now
        run_document = db.scalar(
            select(ReviewBatchRunDocument).where(
                ReviewBatchRunDocument.review_batch_run_id == review_run.id
            )
        )
        if run_document is not None:
            run_document.status = "FAILED"
            run_document.completed_at = now
    db.commit()
