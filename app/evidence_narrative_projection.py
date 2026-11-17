"""Durable optional narrative over admitted evidence-first observations."""

from __future__ import annotations

import json
import uuid
from hashlib import sha256
from io import BytesIO
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifact_gateway import WorkflowArtifactReference, store_workflow_artifact
from app.document_analysis_schemas import DOCUMENT_ANALYSIS_OUTPUT_SCHEMA
from app.evidence_adjudication_artifacts import CriterionDecisionsResult
from app.evidence_compatibility_projection import (
    EvidenceCompatibilityProjection,
    apply_narrative_projection,
)
from app.evidence_first_artifacts import CompactedEvidenceObservations
from app.evidence_first_contracts import NarrativeProjection, validate_narrative_projection
from app.model_execution import content_hash, validate_structured_output
from artifact_service.schemas import WorkflowArtifactLineageInput
from artifact_service.workflow_artifact_types import ArtifactType

NARRATIVE_RESULT_VERSION = "evidence-narrative-result-v1"
NARRATIVE_SELECTION_POLICY_VERSION = "adjudicated-material-observations-v1"
ANALYSIS_NARRATIVE_PROJECTION_SCHEMA_VERSION = 1
MATERIAL_DISPOSITIONS = frozenset({"MATCH", "NEAR_MISS", "UNCLEAR", "INSUFFICIENT"})


class EvidenceNarrativeResult(BaseModel):
    """An optional presentation artifact that has no authority over decisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["evidence-narrative-result-v1"] = NARRATIVE_RESULT_VERSION
    selection_policy_version: Literal["adjudicated-material-observations-v1"] = (
        NARRATIVE_SELECTION_POLICY_VERSION
    )
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    matter_id: uuid.UUID
    matter_document_id: uuid.UUID
    compatibility_result_artifact_id: uuid.UUID
    compatibility_result_hash: str = Field(min_length=64, max_length=64)
    observations_artifact_id: uuid.UUID
    observations_result_hash: str = Field(min_length=64, max_length=64)
    criterion_decisions_result_hash: str = Field(min_length=64, max_length=64)
    selected_observation_ids: tuple[str, ...]
    projection: NarrativeProjection
    presented_output: dict[str, Any]
    result_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_result(self) -> EvidenceNarrativeResult:
        validate_narrative_projection(
            self.projection.model_dump(mode="json"),
            admitted_observation_ids=set(self.selected_observation_ids),
        )
        validate_structured_output(self.presented_output, DOCUMENT_ANALYSIS_OUTPUT_SCHEMA)
        payload = self.model_dump(mode="json", exclude={"result_hash"})
        if content_hash(payload) != self.result_hash:
            raise ValueError("Narrative result hash does not match its immutable content")
        return self


def selected_narrative_observation_ids(
    criterion_decisions: CriterionDecisionsResult,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            observation_id
            for decision in criterion_decisions.criterion_decisions
            if decision.disposition in MATERIAL_DISPOSITIONS
            for observation_id in decision.observation_ids()
        )
    )


def build_evidence_narrative_result(
    *,
    compatibility_result: EvidenceCompatibilityProjection,
    compatibility_artifact: WorkflowArtifactReference,
    observations: CompactedEvidenceObservations,
    observations_artifact: WorkflowArtifactReference,
    criterion_decisions: CriterionDecisionsResult,
    projection: NarrativeProjection,
) -> EvidenceNarrativeResult:
    if compatibility_artifact.artifact_type != ArtifactType.ANALYSIS_COMPATIBILITY_RESULT:
        raise ValueError("Narrative requires a compatibility-result artifact")
    if observations_artifact.artifact_type != ArtifactType.ANALYSIS_OBSERVATIONS:
        raise ValueError("Narrative requires an observations artifact")
    if compatibility_result.status != "COMPLETED":
        raise ValueError("Narrative requires a completed deterministic compatibility result")
    if compatibility_result.observations_artifact_id != observations_artifact.artifact_id:
        raise ValueError("Compatibility result does not belong to the supplied observations artifact")
    if compatibility_result.observations_result_hash != observations.result_hash:
        raise ValueError("Compatibility result does not belong to the supplied observations")
    if compatibility_result.criterion_decisions_result_hash != criterion_decisions.result_hash:
        raise ValueError("Compatibility result does not belong to the supplied criterion decisions")
    selected_ids = selected_narrative_observation_ids(criterion_decisions)
    admitted_ids = {value.observation_id for value in observations.observations}
    if not set(selected_ids) <= admitted_ids:
        raise ValueError("Narrative selection references observations absent from admitted evidence")
    projection = validate_narrative_projection(
        projection.model_dump(mode="json"),
        admitted_observation_ids=set(selected_ids),
    )
    if compatibility_result.output is None:
        raise ValueError("Completed compatibility result is missing its direct output")
    presented_output = apply_narrative_projection(
        base_output=compatibility_result.output,
        narrative=projection,
        observations=observations.observations,
    )
    protected_fields = set(compatibility_result.output) - {"summary", "responsiveness_summary"}
    if any(
        presented_output[field] != compatibility_result.output[field]
        for field in protected_fields
    ):
        raise ValueError("Narrative changed a protected deterministic decision field")
    payload = {
        "version": NARRATIVE_RESULT_VERSION,
        "selection_policy_version": NARRATIVE_SELECTION_POLICY_VERSION,
        "tenant_id": compatibility_result.tenant_id,
        "client_id": compatibility_result.client_id,
        "matter_id": compatibility_result.matter_id,
        "matter_document_id": compatibility_result.matter_document_id,
        "compatibility_result_artifact_id": compatibility_artifact.artifact_id,
        "compatibility_result_hash": compatibility_result.result_hash,
        "observations_artifact_id": observations_artifact.artifact_id,
        "observations_result_hash": observations.result_hash,
        "criterion_decisions_result_hash": criterion_decisions.result_hash,
        "selected_observation_ids": selected_ids,
        "projection": projection.model_dump(mode="json"),
        "presented_output": presented_output,
    }
    return EvidenceNarrativeResult(**payload, result_hash=content_hash(payload))


def narrative_result_bytes(result: EvidenceNarrativeResult) -> bytes:
    return json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def load_narrative_result_bytes(content: bytes) -> EvidenceNarrativeResult:
    return EvidenceNarrativeResult.model_validate_json(content)


def narrative_result_derivation_key(result: EvidenceNarrativeResult) -> str:
    return content_hash(
        {
            "artifact_type": "ANALYSIS_NARRATIVE_PROJECTION",
            "selection_policy_version": result.selection_policy_version,
            "compatibility_result_hash": result.compatibility_result_hash,
            "result_hash": result.result_hash,
        }
    )


def store_narrative_result_artifact(
    result: EvidenceNarrativeResult,
    *,
    producer_run_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowArtifactReference, bool]:
    content = narrative_result_bytes(result)
    return store_workflow_artifact(
        content=BytesIO(content),
        artifact_type=ArtifactType.ANALYSIS_NARRATIVE_PROJECTION,
        core_scope_kind="MATTER",
        core_scope_id=result.matter_id,
        schema_version=ANALYSIS_NARRATIVE_PROJECTION_SCHEMA_VERSION,
        derivation_key=narrative_result_derivation_key(result),
        content_hash=sha256(content).hexdigest(),
        byte_length=len(content),
        media_type="application/json",
        producer_kind="CORE",
        producer_run_id=producer_run_id,
        artifact_metadata={
            "matter_document_id": str(result.matter_document_id),
            "result_hash": result.result_hash,
            "selected_observation_count": len(result.selected_observation_ids),
            "authoritative": False,
        },
        lineage=[
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=artifact_id,
            )
            for artifact_id in (
                result.compatibility_result_artifact_id,
                result.observations_artifact_id,
            )
        ],
        actor_user_id=actor_user_id,
        tenant_id=result.tenant_id,
        client_id=result.client_id,
        filename=f"{result.matter_document_id}-evidence-narrative.json",
    )
