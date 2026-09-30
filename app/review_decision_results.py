from __future__ import annotations

import uuid
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis_tasks import json_content_hash
from app.decision_engine import DecisionEnvelope
from app.decision_specifications import DecisionSpecification
from app.models import (
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    MatterDocument,
    ModelInvocation,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewDecisionResult,
    SkillRun,
    WorkflowRun,
)


class ReviewDecisionResultError(ValueError):
    pass


class ReviewDecisionResultConflict(ReviewDecisionResultError):
    pass


def _require_sha256(value: str, field: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ReviewDecisionResultError(f"{field} must be a lowercase SHA-256 hash")


def _validate_provenance(
    db: Session,
    *,
    workflow_run_id: uuid.UUID,
    review_batch_run_id: uuid.UUID,
    matter_document_id: uuid.UUID,
    task_version_id: uuid.UUID,
    evaluation_skill_run_id: uuid.UUID,
    model_invocation_id: uuid.UUID,
) -> tuple[WorkflowRun, ReviewBatchRun, MatterAnalysisTaskVersion, ModelInvocation]:
    workflow = db.get(WorkflowRun, workflow_run_id)
    batch_run = db.get(ReviewBatchRun, review_batch_run_id)
    document = db.get(MatterDocument, matter_document_id)
    task_version = db.get(MatterAnalysisTaskVersion, task_version_id)
    skill_run = db.get(SkillRun, evaluation_skill_run_id)
    invocation = db.get(ModelInvocation, model_invocation_id)
    if any(
        record is None
        for record in (workflow, batch_run, document, task_version, skill_run, invocation)
    ):
        raise ReviewDecisionResultError("Decision Result provenance references a missing record")
    assert workflow is not None
    assert batch_run is not None
    assert document is not None
    assert task_version is not None
    assert skill_run is not None
    assert invocation is not None

    if batch_run.run_type != "WORKFLOW" or batch_run.workflow_run_record_id != workflow.id:
        raise ReviewDecisionResultError("Review batch run is not owned by the supplied workflow")
    if db.get(ReviewBatchRunDocument, (batch_run.id, document.id)) is None:
        raise ReviewDecisionResultError("Document is not frozen into the review batch run")
    task = db.get(MatterAnalysisTask, task_version.matter_analysis_task_id)
    if task is None or task.matter_id != document.matter_id or workflow.matter_id != document.matter_id:
        raise ReviewDecisionResultError("Task version, workflow, and document must belong to the same matter")
    if task_version.status != "PUBLISHED" or task_version.compilation_status != "READY":
        raise ReviewDecisionResultError("Decision Results require a ready, published task version")
    if skill_run.workflow_run_id != workflow.id:
        raise ReviewDecisionResultError("Evaluation SkillRun does not belong to the workflow")
    if invocation.skill_run_id != skill_run.id or invocation.agent_run_id is not None:
        raise ReviewDecisionResultError("ModelInvocation is not owned by the evaluation SkillRun")
    return workflow, batch_run, task_version, invocation


def record_review_decision_result(
    db: Session,
    *,
    workflow_run_id: uuid.UUID,
    review_batch_run_id: uuid.UUID,
    matter_document_id: uuid.UUID,
    task_version_id: uuid.UUID,
    evaluation_skill_run_id: uuid.UUID,
    model_invocation_id: uuid.UUID,
    engine_key: str,
    source_artifact_id: uuid.UUID,
    source_content_hash: str,
    state: Any,
    paragraph_map_version: str,
    envelope: DecisionEnvelope,
    status: Literal["COMPLETED", "PARTIAL"],
    recommendations: dict[str, Any],
    routes: dict[str, Any],
    evidence: dict[str, Any],
    coverage_details: dict[str, Any] | None = None,
    evidence_skill_run_id: uuid.UUID | None = None,
    reused_from_result_id: uuid.UUID | None = None,
) -> ReviewDecisionResult:
    """Append one successful immutable result, returning an identical retry unchanged."""

    _, _, task_version, invocation = _validate_provenance(
        db,
        workflow_run_id=workflow_run_id,
        review_batch_run_id=review_batch_run_id,
        matter_document_id=matter_document_id,
        task_version_id=task_version_id,
        evaluation_skill_run_id=evaluation_skill_run_id,
        model_invocation_id=model_invocation_id,
    )
    _require_sha256(source_content_hash, "source_content_hash")
    if not paragraph_map_version.strip():
        raise ReviewDecisionResultError("paragraph_map_version must not be blank")
    if task_version.decision_specification is None or task_version.specification_content_hash is None:
        raise ReviewDecisionResultError("Task version has no compiled Decision Specification")
    specification = DecisionSpecification.model_validate(task_version.decision_specification)
    expected_keys = set(specification.questions)
    answered_keys = set(envelope.answers)
    if not answered_keys.issubset(expected_keys):
        raise ReviewDecisionResultError("Decision answers contain unknown question keys")
    missing_keys = sorted(expected_keys - answered_keys)
    extra_coverage = coverage_details or {}
    declared_incomplete = any(
        key.endswith("_complete") and value is False for key, value in extra_coverage.items()
    )
    if status == "COMPLETED" and (missing_keys or declared_incomplete):
        raise ReviewDecisionResultError(
            "A completed Decision Result must have complete question, input, and evidence coverage"
        )
    if status == "PARTIAL" and not missing_keys and not declared_incomplete:
        raise ReviewDecisionResultError("A partial Decision Result must identify incomplete coverage")
    if invocation.status != "COMPLETED":
        raise ReviewDecisionResultError("Decision Result requires a completed ModelInvocation")
    if invocation.provider != envelope.provider or invocation.model != envelope.model:
        raise ReviewDecisionResultError("Decision envelope does not match ModelInvocation provider identity")
    if (
        invocation.provider_request_id != envelope.provider_request_id
        or invocation.request_count != envelope.usage.request_count
        or invocation.input_tokens != envelope.usage.input_tokens
        or invocation.output_tokens != envelope.usage.output_tokens
        or invocation.latency_ms != envelope.latency_ms
    ):
        raise ReviewDecisionResultError("Decision envelope usage does not match ModelInvocation telemetry")

    answers = {key: answer.model_dump(mode="json") for key, answer in envelope.answers.items()}
    raw_answer_hash = json_content_hash(answers)
    policy_evaluation_hash = json_content_hash({"recommendations": recommendations, "routes": routes})
    state_content_hash = json_content_hash(state)
    question_set_hash = json_content_hash(specification.model_dump(mode="json")["questions"])
    decision_policy_hash = json_content_hash(specification.decision_policy.model_dump(mode="json"))
    reserved_coverage = {
        "question_count": len(expected_keys),
        "answered_question_count": len(answered_keys),
        "missing_question_keys": missing_keys,
        "question_complete": not missing_keys,
        "complete": not missing_keys and not declared_incomplete,
    }
    if set(reserved_coverage).intersection(extra_coverage):
        raise ReviewDecisionResultError("coverage_details cannot replace reserved coverage fields")
    coverage = {**extra_coverage, **reserved_coverage}

    existing = db.scalar(
        select(ReviewDecisionResult).where(
            ReviewDecisionResult.review_batch_run_id == review_batch_run_id,
            ReviewDecisionResult.matter_document_id == matter_document_id,
        )
    )
    identity = (
        status,
        task_version_id,
        task_version.definition_content_hash,
        task_version.specification_content_hash,
        source_artifact_id,
        source_content_hash,
        state_content_hash,
        question_set_hash,
        decision_policy_hash,
        paragraph_map_version,
        json_content_hash(coverage),
        raw_answer_hash,
        policy_evaluation_hash,
        json_content_hash(evidence),
        engine_key,
        envelope.provider,
        envelope.model,
        envelope.provider_request_id,
        json_content_hash(envelope.provider_metadata),
        evaluation_skill_run_id,
        evidence_skill_run_id,
        model_invocation_id,
        reused_from_result_id,
        envelope.attempts,
        envelope.usage.request_count,
        envelope.usage.input_tokens,
        envelope.usage.output_tokens,
        envelope.latency_ms,
    )
    if existing is not None:
        existing_identity = (
            existing.status,
            existing.matter_analysis_task_version_id,
            existing.definition_content_hash,
            existing.specification_content_hash,
            existing.source_artifact_id,
            existing.source_content_hash,
            existing.state_content_hash,
            existing.question_set_hash,
            existing.decision_policy_hash,
            existing.paragraph_map_version,
            json_content_hash(existing.coverage),
            existing.raw_answer_hash,
            existing.policy_evaluation_hash,
            json_content_hash(existing.evidence),
            existing.engine_key,
            existing.provider,
            existing.model,
            existing.provider_request_id,
            json_content_hash(existing.provider_metadata),
            existing.evaluation_skill_run_id,
            existing.evidence_skill_run_id,
            existing.model_invocation_id,
            existing.reused_from_result_id,
            existing.attempts,
            existing.request_count,
            existing.input_tokens,
            existing.output_tokens,
            existing.latency_ms,
        )
        if existing_identity == identity:
            return existing
        raise ReviewDecisionResultConflict("A different Decision Result already exists for this run document")

    if evidence_skill_run_id is not None:
        evidence_skill_run = db.get(SkillRun, evidence_skill_run_id)
        if evidence_skill_run is None or evidence_skill_run.workflow_run_id != workflow_run_id:
            raise ReviewDecisionResultError("Evidence SkillRun does not belong to the workflow")
    if reused_from_result_id is not None and db.get(ReviewDecisionResult, reused_from_result_id) is None:
        raise ReviewDecisionResultError("Reused Decision Result does not exist")

    result = ReviewDecisionResult(
        review_batch_run_id=review_batch_run_id,
        workflow_run_id=workflow_run_id,
        matter_document_id=matter_document_id,
        matter_analysis_task_version_id=task_version_id,
        definition_content_hash=task_version.definition_content_hash,
        specification_content_hash=task_version.specification_content_hash,
        source_artifact_id=source_artifact_id,
        source_content_hash=source_content_hash,
        state_content_hash=state_content_hash,
        question_set_hash=question_set_hash,
        decision_policy_hash=decision_policy_hash,
        paragraph_map_version=paragraph_map_version,
        status=status,
        coverage=coverage,
        answers=answers,
        recommendations=recommendations,
        routes=routes,
        evidence=evidence,
        raw_answer_hash=raw_answer_hash,
        policy_evaluation_hash=policy_evaluation_hash,
        engine_key=engine_key,
        provider=envelope.provider,
        model=envelope.model,
        provider_request_id=envelope.provider_request_id,
        provider_metadata=envelope.provider_metadata,
        evaluation_skill_run_id=evaluation_skill_run_id,
        evidence_skill_run_id=evidence_skill_run_id,
        model_invocation_id=model_invocation_id,
        reused_from_result_id=reused_from_result_id,
        attempts=envelope.attempts,
        request_count=envelope.usage.request_count,
        input_tokens=envelope.usage.input_tokens,
        output_tokens=envelope.usage.output_tokens,
        latency_ms=envelope.latency_ms,
        started_at=envelope.started_at,
        completed_at=envelope.completed_at,
    )
    db.add(result)
    db.flush()
    return result
