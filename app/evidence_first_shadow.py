"""Restart-safe shadow orchestration for evidence-first extraction."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from typing import Protocol

from app.artifact_gateway import (
    WorkflowArtifactReference,
    copy_workflow_artifact_content,
    find_workflow_artifact_reference,
)
from app.evidence_first_artifacts import (
    ANALYSIS_OBSERVATION_FRAGMENT_SCHEMA_VERSION,
    CompactedEvidenceObservations,
    EvidenceEvaluationPlan,
    EvidenceExtractionUnitPlan,
    EvidenceExtractionUnitResult,
    compact_observation_results,
    load_observation_fragment_bytes,
    observation_fragment_attempt_derivation_key,
    store_compacted_observations_artifact,
    store_evaluation_plan_artifact,
    store_observation_fragment_artifact,
    validate_observation_fragment,
)
from artifact_service.workflow_artifact_types import ArtifactType

EvidenceUnitExtractor = Callable[
    [EvidenceEvaluationPlan, EvidenceExtractionUnitPlan, int],
    Awaitable[EvidenceExtractionUnitResult],
]


class EvidenceShadowArtifactRepository(Protocol):
    def persist_plan(self, plan: EvidenceEvaluationPlan) -> WorkflowArtifactReference: ...

    def load_attempt(
        self,
        plan: EvidenceEvaluationPlan,
        unit: EvidenceExtractionUnitPlan,
        attempt: int,
    ) -> tuple[EvidenceExtractionUnitResult, WorkflowArtifactReference] | None: ...

    def persist_attempt(
        self,
        plan: EvidenceEvaluationPlan,
        plan_artifact_id: uuid.UUID,
        result: EvidenceExtractionUnitResult,
    ) -> WorkflowArtifactReference: ...

    def persist_compacted(
        self,
        plan: EvidenceEvaluationPlan,
        plan_artifact_id: uuid.UUID,
        fragment_artifact_ids: tuple[uuid.UUID, ...],
        result: CompactedEvidenceObservations,
    ) -> WorkflowArtifactReference: ...


class WorkflowEvidenceShadowArtifactRepository:
    """Workflow-artifact implementation used by the production shadow adapter."""

    def __init__(self, *, actor_user_id: uuid.UUID, producer_run_id: uuid.UUID) -> None:
        self.actor_user_id = actor_user_id
        self.producer_run_id = producer_run_id

    def persist_plan(self, plan: EvidenceEvaluationPlan) -> WorkflowArtifactReference:
        reference, _created = store_evaluation_plan_artifact(
            plan,
            producer_run_id=self.producer_run_id,
            actor_user_id=self.actor_user_id,
        )
        return reference

    def load_attempt(
        self,
        plan: EvidenceEvaluationPlan,
        unit: EvidenceExtractionUnitPlan,
        attempt: int,
    ) -> tuple[EvidenceExtractionUnitResult, WorkflowArtifactReference] | None:
        reference = None
        for schema_version in dict.fromkeys(
            (ANALYSIS_OBSERVATION_FRAGMENT_SCHEMA_VERSION, 1)
        ):
            derivation_key = observation_fragment_attempt_derivation_key(
                plan,
                unit_id=unit.unit_id,
                attempt=attempt,
                schema_version=schema_version,
            )
            reference = find_workflow_artifact_reference(
                artifact_type=ArtifactType.ANALYSIS_OBSERVATION_FRAGMENT,
                derivation_key=derivation_key,
                actor_user_id=self.actor_user_id,
                tenant_id=plan.tenant_id,
                client_id=plan.client_id,
            )
            if reference is not None:
                break
        if reference is None:
            return None
        destination = BytesIO()
        copy_workflow_artifact_content(
            artifact_id=reference.artifact_id,
            destination=destination,
            actor_user_id=self.actor_user_id,
            tenant_id=plan.tenant_id,
            client_id=plan.client_id,
        )
        content = destination.getvalue()
        if sha256(content).hexdigest() != reference.content_hash:
            raise ValueError("Observation fragment content hash does not match its artifact reference")
        results = load_observation_fragment_bytes(content)
        if len(results) != 1:
            raise ValueError("Shadow extraction requires one unit attempt per fragment artifact")
        result = validate_observation_fragment(plan, results)[0]
        if result.unit_id != unit.unit_id or result.attempt != attempt:
            raise ValueError("Observation fragment derivation does not match its unit attempt")
        return result, reference

    def persist_attempt(
        self,
        plan: EvidenceEvaluationPlan,
        plan_artifact_id: uuid.UUID,
        result: EvidenceExtractionUnitResult,
    ) -> WorkflowArtifactReference:
        reference, _created = store_observation_fragment_artifact(
            plan,
            plan_artifact_id=plan_artifact_id,
            results=[result],
            producer_run_id=self.producer_run_id,
            actor_user_id=self.actor_user_id,
        )
        return reference

    def persist_compacted(
        self,
        plan: EvidenceEvaluationPlan,
        plan_artifact_id: uuid.UUID,
        fragment_artifact_ids: tuple[uuid.UUID, ...],
        result: CompactedEvidenceObservations,
    ) -> WorkflowArtifactReference:
        reference, _created = store_compacted_observations_artifact(
            plan,
            result=result,
            plan_artifact_id=plan_artifact_id,
            fragment_artifact_ids=fragment_artifact_ids,
            producer_run_id=self.producer_run_id,
            actor_user_id=self.actor_user_id,
        )
        return reference


@dataclass(frozen=True)
class EvidenceShadowExecutionOutcome:
    plan_artifact: WorkflowArtifactReference
    fragment_artifacts: tuple[WorkflowArtifactReference, ...]
    observations_artifact: WorkflowArtifactReference
    compacted: CompactedEvidenceObservations
    executed_unit_ids: tuple[str, ...]
    reused_unit_ids: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceUnitExecutionOutcome:
    """Durable result of executing or reusing one extraction unit."""

    plan_artifact: WorkflowArtifactReference
    results: tuple[EvidenceExtractionUnitResult, ...]
    fragment_artifacts: tuple[WorkflowArtifactReference, ...]
    executed: bool
    reused: bool


def _failed_result(
    plan: EvidenceEvaluationPlan,
    unit: EvidenceExtractionUnitPlan,
    attempt: int,
    *,
    code: str,
    message: str,
    retryable: bool,
    started_at: datetime,
) -> EvidenceExtractionUnitResult:
    return EvidenceExtractionUnitResult(
        plan_hash=plan.plan_hash,
        source_content_hash=plan.source_content_hash,
        unit_id=unit.unit_id,
        unit_ordinal=unit.ordinal,
        attempt=attempt,
        status="FAILED",
        error_code=code[:100],
        error_message=message[:4_000],
        retryable=retryable,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc),
    )


async def execute_evidence_unit(
    plan: EvidenceEvaluationPlan,
    unit: EvidenceExtractionUnitPlan,
    *,
    repository: EvidenceShadowArtifactRepository,
    extractor: EvidenceUnitExtractor,
    retry_failed: bool = False,
    max_attempts_per_unit: int = 3,
) -> EvidenceUnitExecutionOutcome:
    """Execute one independently addressable unit and persist every attempt."""

    if max_attempts_per_unit < 1:
        raise ValueError("max_attempts_per_unit must be positive")
    if unit not in plan.extraction_units:
        raise ValueError("Extraction unit is not present in the immutable evaluation plan")
    plan_artifact = repository.persist_plan(plan)
    results: list[EvidenceExtractionUnitResult] = []
    fragment_artifacts: list[WorkflowArtifactReference] = []
    existing: list[EvidenceExtractionUnitResult] = []
    for attempt in range(1, max_attempts_per_unit + 1):
        loaded = repository.load_attempt(plan, unit, attempt)
        if loaded is None:
            break
        result, reference = loaded
        existing.append(result)
        results.append(result)
        fragment_artifacts.append(reference)
    latest = existing[-1] if existing else None
    should_execute = latest is None or (
        latest.status in {"FAILED", "CANCELED"}
        and latest.attempt < max_attempts_per_unit
        and (retry_failed or latest.retryable)
    )
    if should_execute:
        attempt = 1 if latest is None else latest.attempt + 1
        while attempt <= max_attempts_per_unit:
            started_at = datetime.now(timezone.utc)
            try:
                result = await extractor(plan, unit, attempt)
            except Exception as exc:  # noqa: BLE001 - persist unexpected failures at the durable unit boundary.
                result = _failed_result(
                    plan,
                    unit,
                    attempt,
                    code=str(getattr(exc, "code", None) or "SHADOW_EXTRACTION_FAILED"),
                    message=str(exc),
                    retryable=bool(getattr(exc, "retryable", False)),
                    started_at=started_at,
                )
            else:
                try:
                    if not isinstance(result, EvidenceExtractionUnitResult):
                        raise TypeError("Extractor did not return an extraction-unit result")
                    result = validate_observation_fragment(plan, [result])[0]
                    if result.unit_id != unit.unit_id or result.attempt != attempt:
                        raise ValueError("Extractor returned a result for a different unit attempt")
                except (TypeError, ValueError) as exc:
                    result = _failed_result(
                        plan,
                        unit,
                        attempt,
                        code="SHADOW_EXTRACTION_INVALID_RESULT",
                        message=str(exc),
                        retryable=False,
                        started_at=started_at,
                    )
            reference = repository.persist_attempt(plan, plan_artifact.artifact_id, result)
            results.append(result)
            fragment_artifacts.append(reference)
            if result.status != "FAILED" or not result.retryable:
                break
            attempt += 1
    return EvidenceUnitExecutionOutcome(
        plan_artifact=plan_artifact,
        results=tuple(results),
        fragment_artifacts=tuple(fragment_artifacts),
        executed=should_execute,
        reused=(
            not should_execute
            and latest is not None
            and latest.status in {"COMPLETED", "COMPLETED_WITH_ADJUSTMENTS"}
        ),
    )


def compact_evidence_shadow(
    plan: EvidenceEvaluationPlan,
    *,
    repository: EvidenceShadowArtifactRepository,
    max_attempts_per_unit: int = 3,
) -> EvidenceShadowExecutionOutcome:
    """Compact independently persisted unit attempts into one document artifact."""

    if max_attempts_per_unit < 1:
        raise ValueError("max_attempts_per_unit must be positive")
    plan_artifact = repository.persist_plan(plan)
    results: list[EvidenceExtractionUnitResult] = []
    fragment_artifacts: list[WorkflowArtifactReference] = []
    reused_unit_ids: list[str] = []
    for unit in plan.extraction_units:
        latest: EvidenceExtractionUnitResult | None = None
        for attempt in range(1, max_attempts_per_unit + 1):
            loaded = repository.load_attempt(plan, unit, attempt)
            if loaded is None:
                break
            result, reference = loaded
            latest = result
            results.append(result)
            fragment_artifacts.append(reference)
        if latest is not None and latest.status in {"COMPLETED", "COMPLETED_WITH_ADJUSTMENTS"}:
            reused_unit_ids.append(unit.unit_id)
    compacted = compact_observation_results(plan, results)
    observations_artifact = repository.persist_compacted(
        plan,
        plan_artifact.artifact_id,
        tuple(reference.artifact_id for reference in fragment_artifacts),
        compacted,
    )
    return EvidenceShadowExecutionOutcome(
        plan_artifact=plan_artifact,
        fragment_artifacts=tuple(fragment_artifacts),
        observations_artifact=observations_artifact,
        compacted=compacted,
        executed_unit_ids=(),
        reused_unit_ids=tuple(reused_unit_ids),
    )


async def execute_evidence_shadow(
    plan: EvidenceEvaluationPlan,
    *,
    repository: EvidenceShadowArtifactRepository,
    extractor: EvidenceUnitExtractor,
    retry_failed: bool = False,
    max_attempts_per_unit: int = 3,
) -> EvidenceShadowExecutionOutcome:
    """Execute or reuse unit attempts without changing the authoritative document result."""

    executed_unit_ids: list[str] = []
    reused_unit_ids: list[str] = []
    for unit in plan.extraction_units:
        outcome = await execute_evidence_unit(
            plan,
            unit,
            repository=repository,
            extractor=extractor,
            retry_failed=retry_failed,
            max_attempts_per_unit=max_attempts_per_unit,
        )
        if outcome.executed:
            executed_unit_ids.append(unit.unit_id)
        if outcome.reused:
            reused_unit_ids.append(unit.unit_id)
    compacted = compact_evidence_shadow(
        plan,
        repository=repository,
        max_attempts_per_unit=max_attempts_per_unit,
    )
    return EvidenceShadowExecutionOutcome(
        plan_artifact=compacted.plan_artifact,
        fragment_artifacts=compacted.fragment_artifacts,
        observations_artifact=compacted.observations_artifact,
        compacted=compacted.compacted,
        executed_unit_ids=tuple(executed_unit_ids),
        reused_unit_ids=tuple(reused_unit_ids),
    )
