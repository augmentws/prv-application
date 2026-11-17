"""Immutable plans and artifacts for criterion-scoped evidence adjudication."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifact_gateway import WorkflowArtifactReference, store_workflow_artifact
from app.evidence_first_artifacts import AnalysisRoleBinding, CompactedEvidenceObservations
from app.evidence_first_contracts import (
    UNMAPPED_CRITERION_KEY,
    CriterionAdjudicationOutput,
    CriterionDecision,
    FrozenCriterionCatalog,
)
from app.model_execution import content_hash
from artifact_service.schemas import WorkflowArtifactLineageInput
from artifact_service.workflow_artifact_types import ArtifactType

ADJUDICATION_PLAN_VERSION = "criterion-adjudication-plan-v1"
ADJUDICATION_ATTEMPT_VERSION = "criterion-adjudication-attempt-v1"
CRITERION_DECISIONS_RESULT_VERSION = "criterion-decisions-result-v1"
ANALYSIS_ADJUDICATION_PLAN_SCHEMA_VERSION = 1
ANALYSIS_CRITERION_ADJUDICATION_FRAGMENT_SCHEMA_VERSION = 1
ANALYSIS_CRITERION_DECISIONS_SCHEMA_VERSION = 1
MAX_PACKET_OBSERVATIONS = 32


class CriterionAdjudicationPacket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    unit_id: str = Field(min_length=64, max_length=64)
    ordinal: int = Field(ge=1)
    criterion_key: str = Field(min_length=1, max_length=200)
    shard_ordinal: int = Field(ge=1)
    observation_ids: tuple[str, ...] = Field(max_length=MAX_PACKET_OBSERVATIONS)
    deterministic_without_model: bool = False
    application_schema_hash: str = Field(min_length=64, max_length=64)
    provider_schema_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_packet(self) -> CriterionAdjudicationPacket:
        if len(self.observation_ids) != len(set(self.observation_ids)):
            raise ValueError("Adjudication packet contains duplicate observation IDs")
        if self.deterministic_without_model != (not self.observation_ids):
            raise ValueError("Only an empty criterion packet may bypass the model")
        return self


class CriterionAdjudicationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["criterion-adjudication-plan-v1"] = ADJUDICATION_PLAN_VERSION
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    matter_id: uuid.UUID
    matter_document_id: uuid.UUID
    extraction_plan_hash: str = Field(min_length=64, max_length=64)
    observations_artifact_id: uuid.UUID
    observations_content_hash: str = Field(min_length=64, max_length=64)
    observations_result_hash: str = Field(min_length=64, max_length=64)
    guidance_revision_id: uuid.UUID
    guidance_content_hash: str = Field(min_length=64, max_length=64)
    criterion_catalog_hash: str = Field(min_length=64, max_length=64)
    evidence_complete: bool
    unmapped_observation_count: int = Field(ge=0)
    role_binding: AnalysisRoleBinding
    packets: tuple[CriterionAdjudicationPacket, ...] = Field(min_length=1)
    plan_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_plan(self) -> CriterionAdjudicationPlan:
        if self.role_binding.role_key != "criterion_adjudication":
            raise ValueError("Adjudication plan requires the criterion_adjudication role")
        if [packet.ordinal for packet in self.packets] != list(range(1, len(self.packets) + 1)):
            raise ValueError("Adjudication packet ordinals must be contiguous")
        if len({packet.unit_id for packet in self.packets}) != len(self.packets):
            raise ValueError("Adjudication plan contains duplicate unit IDs")
        payload = self.model_dump(mode="json", exclude={"plan_hash"})
        if content_hash(payload) != self.plan_hash:
            raise ValueError("Adjudication plan hash does not match its immutable content")
        return self


class AdjudicationAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=100)
    policy: Literal["NORMALIZE", "DROP_ITEM", "FAIL_UNIT", "OPERATOR_REVIEW"]
    path: str = Field(min_length=1, max_length=500)
    detail: str = Field(min_length=1, max_length=2_000)


class CriterionAdjudicationAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["criterion-adjudication-attempt-v1"] = ADJUDICATION_ATTEMPT_VERSION
    plan_hash: str = Field(min_length=64, max_length=64)
    unit_id: str = Field(min_length=64, max_length=64)
    unit_ordinal: int = Field(ge=1)
    criterion_key: str = Field(min_length=1, max_length=200)
    attempt: int = Field(ge=1)
    status: Literal["COMPLETED", "COMPLETED_WITH_ADJUSTMENTS", "FAILED", "CANCELED"]
    deterministic_without_model: bool = False
    adjudication: CriterionAdjudicationOutput | None = None
    decision: CriterionDecision | None = None
    adjustments: tuple[AdjudicationAdjustment, ...] = ()
    invocation_ids: tuple[uuid.UUID, ...] = ()
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=4_000)
    retryable: bool = False
    started_at: datetime
    completed_at: datetime

    @model_validator(mode="after")
    def validate_attempt(self) -> CriterionAdjudicationAttempt:
        completed = self.status in {"COMPLETED", "COMPLETED_WITH_ADJUSTMENTS"}
        if completed != (self.decision is not None):
            raise ValueError("Completed adjudication attempts require one criterion decision")
        if self.status == "FAILED" and not self.error_code:
            raise ValueError("Failed adjudication attempts require an error code")
        if self.retryable and self.status != "FAILED":
            raise ValueError("Only failed adjudication attempts can be retryable")
        if self.deterministic_without_model:
            if self.invocation_ids or self.adjudication is not None:
                raise ValueError("Deterministic adjudication cannot contain model output or invocations")
            if completed and (
                self.decision is None
                or self.decision.disposition not in {"NO_MATCH", "INSUFFICIENT"}
            ):
                raise ValueError("Deterministic adjudication requires NO_MATCH or INSUFFICIENT")
        return self


class SelectedAdjudicationAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    unit_id: str
    unit_ordinal: int
    criterion_key: str
    attempt: int
    status: str


class CriterionDecisionsResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["criterion-decisions-result-v1"] = CRITERION_DECISIONS_RESULT_VERSION
    plan_hash: str = Field(min_length=64, max_length=64)
    extraction_plan_hash: str = Field(min_length=64, max_length=64)
    observations_result_hash: str = Field(min_length=64, max_length=64)
    evidence_complete: bool
    adjudication_complete: bool
    selected_attempts: tuple[SelectedAdjudicationAttempt, ...]
    missing_unit_ids: tuple[str, ...]
    criterion_decisions: tuple[CriterionDecision, ...]
    unmapped_observation_count: int = Field(ge=0)
    material_adjustment_count: int = Field(ge=0)
    result_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_result(self) -> CriterionDecisionsResult:
        payload = self.model_dump(mode="json", exclude={"result_hash"})
        if content_hash(payload) != self.result_hash:
            raise ValueError("Criterion decisions hash does not match its immutable content")
        return self


def build_adjudication_plan(
    *,
    extraction_plan_hash: str,
    observations_artifact: WorkflowArtifactReference,
    observations: CompactedEvidenceObservations,
    catalog: FrozenCriterionCatalog,
    guidance_revision_id: uuid.UUID,
    guidance_content_hash: str,
    role_binding: AnalysisRoleBinding,
    packet_schema_hashes: dict[tuple[str, tuple[str, ...]], tuple[str, str]],
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    matter_id: uuid.UUID,
    matter_document_id: uuid.UUID,
) -> CriterionAdjudicationPlan:
    by_criterion: dict[str, list[str]] = defaultdict(list)
    for observation in observations.observations:
        by_criterion[observation.criterion_key].append(observation.observation_id)
    packets: list[dict] = []
    ordinal = 1
    for criterion in catalog.criteria:
        if criterion.criterion_key == UNMAPPED_CRITERION_KEY:
            continue
        observation_ids = tuple(by_criterion.get(criterion.criterion_key, ()))
        shards = [
            observation_ids[index : index + MAX_PACKET_OBSERVATIONS]
            for index in range(0, len(observation_ids), MAX_PACKET_OBSERVATIONS)
        ] or [()]
        for shard_ordinal, shard in enumerate(shards, start=1):
            application_hash, provider_hash = packet_schema_hashes[(criterion.criterion_key, shard)]
            identity = {
                "version": ADJUDICATION_PLAN_VERSION,
                "extraction_plan_hash": extraction_plan_hash,
                "observations_result_hash": observations.result_hash,
                "criterion_catalog_hash": catalog.catalog_hash,
                "criterion_key": criterion.criterion_key,
                "shard_ordinal": shard_ordinal,
                "observation_ids": shard,
                "role_binding": role_binding.model_dump(mode="json"),
                "application_schema_hash": application_hash,
                "provider_schema_hash": provider_hash,
            }
            packets.append(
                {
                    "unit_id": content_hash(identity),
                    "ordinal": ordinal,
                    "criterion_key": criterion.criterion_key,
                    "shard_ordinal": shard_ordinal,
                    "observation_ids": shard,
                    "deterministic_without_model": not shard,
                    "application_schema_hash": application_hash,
                    "provider_schema_hash": provider_hash,
                }
            )
            ordinal += 1
    payload = {
        "version": ADJUDICATION_PLAN_VERSION,
        "tenant_id": tenant_id,
        "client_id": client_id,
        "matter_id": matter_id,
        "matter_document_id": matter_document_id,
        "extraction_plan_hash": extraction_plan_hash,
        "observations_artifact_id": observations_artifact.artifact_id,
        "observations_content_hash": observations_artifact.content_hash,
        "observations_result_hash": observations.result_hash,
        "guidance_revision_id": guidance_revision_id,
        "guidance_content_hash": guidance_content_hash,
        "criterion_catalog_hash": catalog.catalog_hash,
        "evidence_complete": observations.evidence_complete,
        "unmapped_observation_count": len(by_criterion.get(UNMAPPED_CRITERION_KEY, ())),
        "role_binding": role_binding.model_dump(mode="json"),
        "packets": packets,
    }
    return CriterionAdjudicationPlan(**payload, plan_hash=content_hash(payload))


def deterministic_empty_attempt(
    plan: CriterionAdjudicationPlan,
    packet: CriterionAdjudicationPacket,
    *,
    attempt: int,
    now: datetime,
) -> CriterionAdjudicationAttempt:
    disposition = "NO_MATCH" if plan.evidence_complete else "INSUFFICIENT"
    decision = CriterionDecision(
        criterion_key=packet.criterion_key,
        disposition=disposition,
        confidence=1 if plan.evidence_complete else 0,
        primary_observation_id=None,
        additional_observation_id_1=None,
        additional_observation_id_2=None,
        additional_observation_id_3=None,
        additional_observation_id_4=None,
        unresolved_boundary=(None if plan.evidence_complete else "Source evidence is incomplete."),
    )
    return CriterionAdjudicationAttempt(
        plan_hash=plan.plan_hash,
        unit_id=packet.unit_id,
        unit_ordinal=packet.ordinal,
        criterion_key=packet.criterion_key,
        attempt=attempt,
        status="COMPLETED",
        deterministic_without_model=True,
        decision=decision,
        started_at=now,
        completed_at=now,
    )


def _combine_shard_decisions(decisions: list[CriterionDecision]) -> CriterionDecision:
    if len(decisions) == 1:
        return decisions[0]
    precedence = {"MATCH": 5, "UNCLEAR": 4, "INSUFFICIENT": 3, "NEAR_MISS": 2, "NO_MATCH": 1}
    selected = max(decisions, key=lambda value: (precedence[value.disposition], value.confidence))
    references: list[str] = []
    for decision in sorted(decisions, key=lambda value: precedence[value.disposition], reverse=True):
        for observation_id in decision.observation_ids():
            if observation_id not in references:
                references.append(observation_id)
            if len(references) == 5:
                break
        if len(references) == 5:
            break
    slots = references + [None] * (5 - len(references))
    boundaries = [value.unresolved_boundary for value in decisions if value.unresolved_boundary]
    return CriterionDecision(
        criterion_key=selected.criterion_key,
        disposition=selected.disposition,
        confidence=selected.confidence,
        primary_observation_id=slots[0],
        additional_observation_id_1=slots[1],
        additional_observation_id_2=slots[2],
        additional_observation_id_3=slots[3],
        additional_observation_id_4=slots[4],
        unresolved_boundary=(" ".join(boundaries)[:2_000] if boundaries else None),
    )


def compact_adjudication_attempts(
    plan: CriterionAdjudicationPlan,
    attempts: list[CriterionAdjudicationAttempt] | tuple[CriterionAdjudicationAttempt, ...],
) -> CriterionDecisionsResult:
    packet_by_id = {packet.unit_id: packet for packet in plan.packets}
    latest: dict[str, CriterionAdjudicationAttempt] = {}
    for attempt in attempts:
        packet = packet_by_id.get(attempt.unit_id)
        if packet is None or attempt.plan_hash != plan.plan_hash:
            raise ValueError("Adjudication attempt does not belong to the plan")
        if attempt.criterion_key != packet.criterion_key or attempt.unit_ordinal != packet.ordinal:
            raise ValueError("Adjudication attempt does not match its planned packet")
        referenced = set(attempt.decision.observation_ids()) if attempt.decision else set()
        if not referenced.issubset(set(packet.observation_ids)):
            raise ValueError("Adjudication attempt references an observation outside its packet")
        current = latest.get(attempt.unit_id)
        if current is None or attempt.attempt > current.attempt:
            latest[attempt.unit_id] = attempt
    selected = [latest[packet.unit_id] for packet in plan.packets if packet.unit_id in latest]
    missing = tuple(packet.unit_id for packet in plan.packets if packet.unit_id not in latest)
    successful = [
        attempt
        for attempt in selected
        if attempt.status in {"COMPLETED", "COMPLETED_WITH_ADJUSTMENTS"}
    ]
    by_criterion: dict[str, list[CriterionDecision]] = defaultdict(list)
    for attempt in successful:
        if attempt.decision is not None:
            by_criterion[attempt.criterion_key].append(attempt.decision)
    expected_counts: dict[str, int] = defaultdict(int)
    for packet in plan.packets:
        expected_counts[packet.criterion_key] += 1
    decisions = tuple(
        _combine_shard_decisions(by_criterion[key])
        for key in dict.fromkeys(packet.criterion_key for packet in plan.packets)
        if len(by_criterion[key]) == expected_counts[key]
    )
    material_adjustments = sum(
        adjustment.policy != "NORMALIZE"
        for attempt in successful
        for adjustment in attempt.adjustments
    )
    payload = {
        "version": CRITERION_DECISIONS_RESULT_VERSION,
        "plan_hash": plan.plan_hash,
        "extraction_plan_hash": plan.extraction_plan_hash,
        "observations_result_hash": plan.observations_result_hash,
        "evidence_complete": plan.evidence_complete,
        "adjudication_complete": (
            not missing and len(successful) == len(plan.packets) and material_adjustments == 0
        ),
        "selected_attempts": [
            {
                "unit_id": value.unit_id,
                "unit_ordinal": value.unit_ordinal,
                "criterion_key": value.criterion_key,
                "attempt": value.attempt,
                "status": value.status,
            }
            for value in selected
        ],
        "missing_unit_ids": missing,
        "criterion_decisions": [value.model_dump(mode="json") for value in decisions],
        "unmapped_observation_count": plan.unmapped_observation_count,
        "material_adjustment_count": material_adjustments,
    }
    return CriterionDecisionsResult(**payload, result_hash=content_hash(payload))


def _json_bytes(value: BaseModel) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def adjudication_plan_bytes(plan: CriterionAdjudicationPlan) -> bytes:
    return _json_bytes(plan)


def criterion_decisions_bytes(result: CriterionDecisionsResult) -> bytes:
    return _json_bytes(result)


def load_adjudication_plan_bytes(content: bytes) -> CriterionAdjudicationPlan:
    return CriterionAdjudicationPlan.model_validate_json(content)


def load_adjudication_attempt_bytes(content: bytes) -> CriterionAdjudicationAttempt:
    return CriterionAdjudicationAttempt.model_validate_json(content)


def load_criterion_decisions_bytes(content: bytes) -> CriterionDecisionsResult:
    return CriterionDecisionsResult.model_validate_json(content)


def store_adjudication_plan_artifact(
    plan: CriterionAdjudicationPlan,
    *,
    producer_run_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowArtifactReference, bool]:
    content = adjudication_plan_bytes(plan)
    return store_workflow_artifact(
        content=BytesIO(content),
        artifact_type=ArtifactType.ANALYSIS_ADJUDICATION_PLAN,
        core_scope_kind="MATTER",
        core_scope_id=plan.matter_id,
        schema_version=ANALYSIS_ADJUDICATION_PLAN_SCHEMA_VERSION,
        derivation_key=plan.plan_hash,
        content_hash=sha256(content).hexdigest(),
        byte_length=len(content),
        media_type="application/json",
        producer_kind="CORE",
        producer_run_id=producer_run_id,
        artifact_metadata={
            "matter_document_id": str(plan.matter_document_id),
            "plan_hash": plan.plan_hash,
            "packet_count": len(plan.packets),
        },
        lineage=[
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=plan.observations_artifact_id,
            )
        ],
        actor_user_id=actor_user_id,
        tenant_id=plan.tenant_id,
        client_id=plan.client_id,
        filename=f"{plan.matter_document_id}-adjudication-plan.json",
    )


def store_adjudication_attempt_artifact(
    plan: CriterionAdjudicationPlan,
    *,
    plan_artifact_id: uuid.UUID,
    result: CriterionAdjudicationAttempt,
    producer_run_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowArtifactReference, bool]:
    content = _json_bytes(result)
    derivation_key = adjudication_attempt_derivation_key(
        plan,
        unit_id=result.unit_id,
        attempt=result.attempt,
    )
    return store_workflow_artifact(
        content=BytesIO(content),
        artifact_type=ArtifactType.ANALYSIS_CRITERION_ADJUDICATION_FRAGMENT,
        core_scope_kind="MATTER",
        core_scope_id=plan.matter_id,
        schema_version=ANALYSIS_CRITERION_ADJUDICATION_FRAGMENT_SCHEMA_VERSION,
        derivation_key=derivation_key,
        content_hash=sha256(content).hexdigest(),
        byte_length=len(content),
        media_type="application/json",
        producer_kind="CORE",
        producer_run_id=producer_run_id,
        artifact_metadata={
            "matter_document_id": str(plan.matter_document_id),
            "criterion_key": result.criterion_key,
            "unit_id": result.unit_id,
            "attempt": result.attempt,
            "status": result.status,
        },
        lineage=[
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=plan_artifact_id,
            )
        ],
        actor_user_id=actor_user_id,
        tenant_id=plan.tenant_id,
        client_id=plan.client_id,
        filename=f"{plan.matter_document_id}-{result.criterion_key}-adjudication.json",
    )


def adjudication_attempt_derivation_key(
    plan: CriterionAdjudicationPlan,
    *,
    unit_id: str,
    attempt: int,
) -> str:
    if unit_id not in {packet.unit_id for packet in plan.packets}:
        raise ValueError("Adjudication attempt references an unknown unit")
    if attempt < 1:
        raise ValueError("Adjudication attempt must be positive")
    return content_hash(
        {
            "artifact_type": "ANALYSIS_CRITERION_ADJUDICATION_FRAGMENT",
            "plan_hash": plan.plan_hash,
            "unit_id": unit_id,
            "attempt": attempt,
        }
    )


def store_criterion_decisions_artifact(
    plan: CriterionAdjudicationPlan,
    *,
    result: CriterionDecisionsResult,
    plan_artifact_id: uuid.UUID,
    fragment_artifact_ids: tuple[uuid.UUID, ...],
    producer_run_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowArtifactReference, bool]:
    content = criterion_decisions_bytes(result)
    derivation_key = criterion_decisions_derivation_key(plan, result)
    lineage_ids = (plan_artifact_id, *fragment_artifact_ids)
    return store_workflow_artifact(
        content=BytesIO(content),
        artifact_type=ArtifactType.ANALYSIS_CRITERION_DECISIONS,
        core_scope_kind="MATTER",
        core_scope_id=plan.matter_id,
        schema_version=ANALYSIS_CRITERION_DECISIONS_SCHEMA_VERSION,
        derivation_key=derivation_key,
        content_hash=sha256(content).hexdigest(),
        byte_length=len(content),
        media_type="application/json",
        producer_kind="CORE",
        producer_run_id=producer_run_id,
        artifact_metadata={
            "matter_document_id": str(plan.matter_document_id),
            "result_hash": result.result_hash,
            "criterion_decision_count": len(result.criterion_decisions),
            "adjudication_complete": result.adjudication_complete,
        },
        lineage=[
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=value,
            )
            for value in lineage_ids
        ],
        actor_user_id=actor_user_id,
        tenant_id=plan.tenant_id,
        client_id=plan.client_id,
        filename=f"{plan.matter_document_id}-criterion-decisions.json",
    )


def criterion_decisions_derivation_key(
    plan: CriterionAdjudicationPlan,
    result: CriterionDecisionsResult,
) -> str:
    if result.plan_hash != plan.plan_hash:
        raise ValueError("Criterion decisions do not belong to the adjudication plan")
    return content_hash(
        {
            "artifact_type": "ANALYSIS_CRITERION_DECISIONS",
            "plan_hash": plan.plan_hash,
            "result_hash": result.result_hash,
        }
    )
