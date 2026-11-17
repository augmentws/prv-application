"""Restart-safe criterion-adjudication shadow orchestration."""

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
from app.evidence_adjudication_artifacts import (
    CriterionAdjudicationAttempt,
    CriterionAdjudicationPacket,
    CriterionAdjudicationPlan,
    CriterionDecisionsResult,
    adjudication_attempt_derivation_key,
    adjudication_plan_bytes,
    compact_adjudication_attempts,
    criterion_decisions_bytes,
    criterion_decisions_derivation_key,
    deterministic_empty_attempt,
    load_adjudication_attempt_bytes,
    load_adjudication_plan_bytes,
    load_criterion_decisions_bytes,
    store_adjudication_attempt_artifact,
    store_adjudication_plan_artifact,
    store_criterion_decisions_artifact,
)
from app.evidence_first_contracts import criterion_decision_from_adjudication
from artifact_service.workflow_artifact_types import ArtifactType

CriterionAdjudicator = Callable[
    [CriterionAdjudicationPlan, CriterionAdjudicationPacket, int],
    Awaitable[CriterionAdjudicationAttempt],
]


class AdjudicationShadowArtifactRepository(Protocol):
    def persist_plan(self, plan: CriterionAdjudicationPlan) -> WorkflowArtifactReference: ...

    def load_attempt(
        self,
        plan: CriterionAdjudicationPlan,
        packet: CriterionAdjudicationPacket,
        attempt: int,
    ) -> tuple[CriterionAdjudicationAttempt, WorkflowArtifactReference] | None: ...

    def persist_attempt(
        self,
        plan: CriterionAdjudicationPlan,
        plan_artifact_id: uuid.UUID,
        result: CriterionAdjudicationAttempt,
    ) -> WorkflowArtifactReference: ...

    def persist_result(
        self,
        plan: CriterionAdjudicationPlan,
        plan_artifact_id: uuid.UUID,
        fragment_artifact_ids: tuple[uuid.UUID, ...],
        result: CriterionDecisionsResult,
    ) -> WorkflowArtifactReference: ...


class WorkflowAdjudicationShadowArtifactRepository:
    def __init__(self, *, actor_user_id: uuid.UUID, producer_run_id: uuid.UUID) -> None:
        self.actor_user_id = actor_user_id
        self.producer_run_id = producer_run_id

    def persist_plan(self, plan: CriterionAdjudicationPlan) -> WorkflowArtifactReference:
        existing = find_workflow_artifact_reference(
            artifact_type=ArtifactType.ANALYSIS_ADJUDICATION_PLAN,
            derivation_key=plan.plan_hash,
            actor_user_id=self.actor_user_id,
            tenant_id=plan.tenant_id,
            client_id=plan.client_id,
        )
        if existing is not None:
            content = self._verified_content(existing, plan)
            if sha256(adjudication_plan_bytes(plan)).hexdigest() != existing.content_hash:
                raise ValueError("Existing adjudication plan content does not match its identity")
            if load_adjudication_plan_bytes(content) != plan:
                raise ValueError("Existing adjudication plan does not match the requested plan")
            return existing
        reference, _created = store_adjudication_plan_artifact(
            plan,
            producer_run_id=self.producer_run_id,
            actor_user_id=self.actor_user_id,
        )
        return reference

    def _verified_content(
        self,
        reference: WorkflowArtifactReference,
        plan: CriterionAdjudicationPlan,
    ) -> bytes:
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
            raise ValueError("Adjudication artifact content hash does not match its reference")
        return content

    def load_attempt(
        self,
        plan: CriterionAdjudicationPlan,
        packet: CriterionAdjudicationPacket,
        attempt: int,
    ) -> tuple[CriterionAdjudicationAttempt, WorkflowArtifactReference] | None:
        derivation_key = adjudication_attempt_derivation_key(
            plan,
            unit_id=packet.unit_id,
            attempt=attempt,
        )
        reference = find_workflow_artifact_reference(
            artifact_type=ArtifactType.ANALYSIS_CRITERION_ADJUDICATION_FRAGMENT,
            derivation_key=derivation_key,
            actor_user_id=self.actor_user_id,
            tenant_id=plan.tenant_id,
            client_id=plan.client_id,
        )
        if reference is None:
            return None
        content = self._verified_content(reference, plan)
        result = load_adjudication_attempt_bytes(content)
        if result.unit_id != packet.unit_id or result.attempt != attempt:
            raise ValueError("Adjudication fragment does not match its unit attempt")
        return result, reference

    def persist_attempt(
        self,
        plan: CriterionAdjudicationPlan,
        plan_artifact_id: uuid.UUID,
        result: CriterionAdjudicationAttempt,
    ) -> WorkflowArtifactReference:
        reference, _created = store_adjudication_attempt_artifact(
            plan,
            plan_artifact_id=plan_artifact_id,
            result=result,
            producer_run_id=self.producer_run_id,
            actor_user_id=self.actor_user_id,
        )
        return reference

    def persist_result(
        self,
        plan: CriterionAdjudicationPlan,
        plan_artifact_id: uuid.UUID,
        fragment_artifact_ids: tuple[uuid.UUID, ...],
        result: CriterionDecisionsResult,
    ) -> WorkflowArtifactReference:
        derivation_key = criterion_decisions_derivation_key(plan, result)
        existing = find_workflow_artifact_reference(
            artifact_type=ArtifactType.ANALYSIS_CRITERION_DECISIONS,
            derivation_key=derivation_key,
            actor_user_id=self.actor_user_id,
            tenant_id=plan.tenant_id,
            client_id=plan.client_id,
        )
        if existing is not None:
            content = self._verified_content(existing, plan)
            if sha256(criterion_decisions_bytes(result)).hexdigest() != existing.content_hash:
                raise ValueError("Existing criterion decisions content does not match its identity")
            if load_criterion_decisions_bytes(content) != result:
                raise ValueError("Existing criterion decisions do not match the compacted result")
            return existing
        reference, _created = store_criterion_decisions_artifact(
            plan,
            result=result,
            plan_artifact_id=plan_artifact_id,
            fragment_artifact_ids=fragment_artifact_ids,
            producer_run_id=self.producer_run_id,
            actor_user_id=self.actor_user_id,
        )
        return reference


@dataclass(frozen=True)
class AdjudicationShadowExecutionOutcome:
    plan_artifact: WorkflowArtifactReference
    fragment_artifacts: tuple[WorkflowArtifactReference, ...]
    decisions_artifact: WorkflowArtifactReference
    result: CriterionDecisionsResult
    executed_unit_ids: tuple[str, ...]
    reused_unit_ids: tuple[str, ...]


def _validate_attempt(
    plan: CriterionAdjudicationPlan,
    packet: CriterionAdjudicationPacket,
    result: CriterionAdjudicationAttempt,
    *,
    attempt: int,
) -> CriterionAdjudicationAttempt:
    if (
        result.plan_hash != plan.plan_hash
        or result.unit_id != packet.unit_id
        or result.unit_ordinal != packet.ordinal
        or result.criterion_key != packet.criterion_key
        or result.attempt != attempt
    ):
        raise ValueError("Adjudication attempt does not match its immutable planned packet")
    if result.deterministic_without_model != packet.deterministic_without_model:
        raise ValueError("Adjudication attempt has an invalid execution mode")
    admitted_ids = set(packet.observation_ids)
    if result.decision and not set(result.decision.observation_ids()).issubset(admitted_ids):
        raise ValueError("Adjudication decision references evidence outside its packet")
    if result.adjudication:
        if not set(result.adjudication.observation_ids()).issubset(admitted_ids):
            raise ValueError("Adjudication output references evidence outside its packet")
        if result.decision != criterion_decision_from_adjudication(result.adjudication):
            raise ValueError("Adjudication decision does not match its admitted model output")
    return result


def _failed_attempt(
    plan: CriterionAdjudicationPlan,
    packet: CriterionAdjudicationPacket,
    attempt: int,
    exc: Exception,
    *,
    started_at: datetime,
) -> CriterionAdjudicationAttempt:
    return CriterionAdjudicationAttempt(
        plan_hash=plan.plan_hash,
        unit_id=packet.unit_id,
        unit_ordinal=packet.ordinal,
        criterion_key=packet.criterion_key,
        attempt=attempt,
        status="FAILED",
        deterministic_without_model=packet.deterministic_without_model,
        error_code=str(getattr(exc, "code", None) or "CRITERION_ADJUDICATION_FAILED")[:100],
        error_message=type(exc).__name__,
        retryable=bool(getattr(exc, "retryable", False)),
        started_at=started_at,
        completed_at=datetime.now(timezone.utc),
    )


async def execute_adjudication_shadow(
    plan: CriterionAdjudicationPlan,
    *,
    repository: AdjudicationShadowArtifactRepository,
    adjudicator: CriterionAdjudicator,
    retry_failed: bool = False,
    max_attempts_per_unit: int = 3,
) -> AdjudicationShadowExecutionOutcome:
    if max_attempts_per_unit < 1:
        raise ValueError("max_attempts_per_unit must be positive")
    plan_artifact = repository.persist_plan(plan)
    results: list[CriterionAdjudicationAttempt] = []
    fragment_artifacts: list[WorkflowArtifactReference] = []
    executed: list[str] = []
    reused: list[str] = []
    for packet in plan.packets:
        existing: list[CriterionAdjudicationAttempt] = []
        for attempt_number in range(1, max_attempts_per_unit + 1):
            loaded = repository.load_attempt(plan, packet, attempt_number)
            if loaded is None:
                break
            result, reference = loaded
            _validate_attempt(plan, packet, result, attempt=attempt_number)
            existing.append(result)
            results.append(result)
            fragment_artifacts.append(reference)
        latest = existing[-1] if existing else None
        should_execute = latest is None or (
            latest.status in {"FAILED", "CANCELED"}
            and latest.attempt < max_attempts_per_unit
            and (retry_failed or latest.retryable)
        )
        if not should_execute:
            if latest is not None and latest.status in {"COMPLETED", "COMPLETED_WITH_ADJUSTMENTS"}:
                reused.append(packet.unit_id)
            continue
        attempt_number = 1 if latest is None else latest.attempt + 1
        while attempt_number <= max_attempts_per_unit:
            started_at = datetime.now(timezone.utc)
            try:
                if packet.deterministic_without_model:
                    result = deterministic_empty_attempt(
                        plan,
                        packet,
                        attempt=attempt_number,
                        now=started_at,
                    )
                else:
                    result = await adjudicator(plan, packet, attempt_number)
                result = _validate_attempt(plan, packet, result, attempt=attempt_number)
            except Exception as exc:  # noqa: BLE001 - persist one bounded failed attempt.
                result = _failed_attempt(
                    plan,
                    packet,
                    attempt_number,
                    exc,
                    started_at=started_at,
                )
            reference = repository.persist_attempt(plan, plan_artifact.artifact_id, result)
            results.append(result)
            fragment_artifacts.append(reference)
            if result.status != "FAILED" or not result.retryable:
                break
            attempt_number += 1
        executed.append(packet.unit_id)
    compacted = compact_adjudication_attempts(plan, results)
    decisions_artifact = repository.persist_result(
        plan,
        plan_artifact.artifact_id,
        tuple(reference.artifact_id for reference in fragment_artifacts),
        compacted,
    )
    return AdjudicationShadowExecutionOutcome(
        plan_artifact=plan_artifact,
        fragment_artifacts=tuple(fragment_artifacts),
        decisions_artifact=decisions_artifact,
        result=compacted,
        executed_unit_ids=tuple(executed),
        reused_unit_ids=tuple(reused),
    )
