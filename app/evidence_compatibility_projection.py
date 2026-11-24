"""Deterministic direct-schema projection for evidence-first document analysis."""

from __future__ import annotations

import json
import uuid
from hashlib import sha256
from io import BytesIO
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifact_gateway import (
    WorkflowArtifactReference,
    copy_workflow_artifact_content,
    find_workflow_artifact_reference,
    store_workflow_artifact,
)
from app.document_analysis_schemas import (
    DOCUMENT_ANALYSIS_OUTPUT_SCHEMA,
    DOCUMENT_ANALYSIS_PIPELINE_VERSION,
)
from app.evidence_adjudication_artifacts import CriterionDecisionsResult
from app.evidence_document_decision import DocumentDecisionResult
from app.evidence_first_artifacts import CompactedEvidenceObservations, EvidenceEvaluationPlan
from app.evidence_first_contracts import (
    UNMAPPED_CRITERION_KEY,
    AggregatedEvidenceObservation,
    CriterionDecision,
    DocumentDecision,
    FrozenCriterionCatalog,
    NarrativeProjection,
)
from app.model_execution import content_hash, validate_structured_output
from artifact_service.schemas import WorkflowArtifactLineageInput
from artifact_service.workflow_artifact_types import ArtifactType

COMPATIBILITY_PROJECTION_VERSION = "evidence-compatibility-projection-v1"
COMPATIBILITY_PROJECTION_POLICY_VERSION = "deterministic-evidence-digest-v1"
ANALYSIS_COMPATIBILITY_RESULT_SCHEMA_VERSION = 1

DIRECT_OUTPUT_FIELDS = frozenset(DOCUMENT_ANALYSIS_OUTPUT_SCHEMA["properties"])


class EvidenceCompatibilityProjection(BaseModel):
    """A shadow-only, lineage-complete projection into the existing result contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["evidence-compatibility-projection-v1"] = COMPATIBILITY_PROJECTION_VERSION
    policy_version: Literal["deterministic-evidence-digest-v1"] = COMPATIBILITY_PROJECTION_POLICY_VERSION
    direct_output_schema_version: Literal["document-analysis-pipeline-v4"] = DOCUMENT_ANALYSIS_PIPELINE_VERSION
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    matter_id: uuid.UUID
    matter_document_id: uuid.UUID
    observations_artifact_id: uuid.UUID
    observations_result_hash: str = Field(min_length=64, max_length=64)
    criterion_decisions_artifact_id: uuid.UUID
    criterion_decisions_result_hash: str = Field(min_length=64, max_length=64)
    document_decision_artifact_id: uuid.UUID
    document_decision_result_hash: str = Field(min_length=64, max_length=64)
    status: Literal["COMPLETED", "BLOCKED"]
    narrative_status: Literal["NOT_REQUESTED"] = "NOT_REQUESTED"
    output: dict[str, Any] | None = None
    field_provenance: dict[
        str,
        Literal[
            "EXTRACTION_PLAN",
            "EVIDENCE_DIGEST",
            "CRITERION_DECISIONS",
            "DOCUMENT_DECISION",
            "DETERMINISTIC_ASSEMBLY",
        ],
    ]
    blocking_reasons: tuple[str, ...] = ()
    result_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_projection(self) -> EvidenceCompatibilityProjection:
        if set(self.field_provenance) != DIRECT_OUTPUT_FIELDS:
            raise ValueError("Compatibility field provenance must cover the direct output contract")
        if self.status == "COMPLETED":
            if self.output is None or self.blocking_reasons:
                raise ValueError("A completed compatibility projection requires output and no blockers")
            validate_structured_output(self.output, DOCUMENT_ANALYSIS_OUTPUT_SCHEMA)
        elif self.output is not None or not self.blocking_reasons:
            raise ValueError("A blocked compatibility projection requires blockers and no output")
        payload = self.model_dump(mode="json", exclude={"result_hash"})
        if content_hash(payload) != self.result_hash:
            raise ValueError("Compatibility projection hash does not match its immutable content")
        return self


def _artifact(reference: WorkflowArtifactReference, expected: ArtifactType) -> None:
    if reference.artifact_type != expected:
        raise ValueError(f"Compatibility projection requires {expected.value}")


def _ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _coverage(
    plan: EvidenceEvaluationPlan,
    observations: CompactedEvidenceObservations,
) -> dict[str, Any]:
    selected = {
        value.unit_id: value
        for value in observations.selected_attempts
        if value.status in {"COMPLETED", "COMPLETED_WITH_ADJUSTMENTS"}
    }
    successful_units = [unit for unit in plan.extraction_units if unit.unit_id in selected]
    analyzed_paragraph_ids = _ordered_unique(
        [segment.paragraph_id for unit in successful_units for segment in unit.segments]
    )
    omitted_ranges = [value.model_dump(mode="json") for value in observations.omitted_ranges]
    partial_paragraph_ids = _ordered_unique([str(value["paragraph_id"]) for value in omitted_ranges])
    return {
        "status": observations.coverage_status,
        "map_plan_version": plan.version,
        "paragraph_map_version": plan.paragraph_map_version,
        "window_count": len(plan.extraction_units),
        "successful_window_count": len(successful_units),
        "analyzed_paragraph_ids": analyzed_paragraph_ids,
        "partial_paragraph_ids": partial_paragraph_ids,
        "omitted_ranges": omitted_ranges,
    }


def _field_provenance() -> dict[str, str]:
    return {
        "summary": "EVIDENCE_DIGEST",
        "determination": "DOCUMENT_DECISION",
        "confidence": "DOCUMENT_DECISION",
        "responsiveness_summary": "EVIDENCE_DIGEST",
        "criterion_matches": "CRITERION_DECISIONS",
        "scope_analysis": "DETERMINISTIC_ASSEMBLY",
        "countervailing_considerations": "EVIDENCE_DIGEST",
        "clarification_requests": "DETERMINISTIC_ASSEMBLY",
        "limitations": "DETERMINISTIC_ASSEMBLY",
        "coverage": "EXTRACTION_PLAN",
    }


def assemble_direct_compatibility_output(
    *,
    document_decision: DocumentDecision,
    criterion_decisions: list[CriterionDecision] | tuple[CriterionDecision, ...],
    observations: list[AggregatedEvidenceObservation]
    | tuple[AggregatedEvidenceObservation, ...],
    catalog: FrozenCriterionCatalog,
    coverage: dict[str, Any],
    evidence_complete: bool,
    material_adjustment_count: int,
    unmapped_observation_count: int,
    include_evidence_digest: bool = True,
) -> dict[str, Any]:
    """Assemble the shared direct-output contract without invoking a model."""

    observation_by_id = {value.observation_id: value for value in observations}
    criterion_by_key = {
        value.criterion_key: value
        for value in catalog.criteria
        if value.criterion_key != UNMAPPED_CRITERION_KEY
    }
    digest_observation_ids: list[str] = []
    countervailing_ids: list[str] = []
    matches: list[dict[str, Any]] = []
    limitations: list[str] = []
    for decision in criterion_decisions:
        referenced = list(decision.observation_ids())
        missing = [value for value in referenced if value not in observation_by_id]
        if missing:
            raise ValueError(
                "Criterion decision references observations absent from the compacted evidence"
            )
        if decision.disposition in {"MATCH", "NEAR_MISS", "UNCLEAR", "INSUFFICIENT"}:
            digest_observation_ids.extend(referenced)
        countervailing_ids.extend(
            value
            for value in referenced
            if observation_by_id[value].signal_type == "COUNTERVAILING"
        )
        if decision.disposition not in {"MATCH", "NEAR_MISS"}:
            if decision.disposition in {"UNCLEAR", "INSUFFICIENT"}:
                detail = decision.unresolved_boundary or "The criterion could not be resolved."
                limitations.append(f"{decision.criterion_key}: {detail}")
            continue
        criterion = criterion_by_key.get(decision.criterion_key)
        if criterion is None:
            raise ValueError(
                "Criterion decision references a criterion absent from the frozen catalog"
            )
        citations = _ordered_unique(
            [
                citation
                for observation_id in referenced
                for citation in observation_by_id[observation_id].citation_ids
            ]
        )
        if not citations:
            raise ValueError("A projected criterion match requires cited evidence")
        reasoning = decision.unresolved_boundary or (
            f"The cited evidence was adjudicated as satisfying {criterion.display_label}."
        )
        matches.append(
            {
                "criterion_key": decision.criterion_key,
                "label": criterion.display_label,
                "match_type": (
                    "PRIMARY" if decision.disposition == "MATCH" else "NEAR_MISS"
                ),
                "reasoning": reasoning,
                "citation_ids": citations[:30],
            }
        )

    digest_observation_ids = _ordered_unique(digest_observation_ids)
    digest = (
        [
            {
                "text": observation_by_id[value].fact,
                "citation_ids": list(observation_by_id[value].citation_ids),
            }
            for value in digest_observation_ids[:12]
        ]
        if include_evidence_digest
        else []
    )
    if len(digest_observation_ids) > 12:
        limitations.append(
            "The deterministic presentation digest includes 12 of "
            f"{len(digest_observation_ids)} cited observations."
        )
    if not evidence_complete:
        limitations.append("Evidence extraction coverage is incomplete.")
    if material_adjustment_count:
        limitations.append(
            f"Evidence extraction made {material_adjustment_count} material adjustment(s)."
        )
    if unmapped_observation_count:
        limitations.append(
            f"{unmapped_observation_count} material observation(s) could not be mapped to a frozen criterion."
        )
    limitations = _ordered_unique(limitations)[:12]
    countervailing = (
        [
            {
                "text": observation_by_id[value].fact,
                "citation_ids": list(observation_by_id[value].citation_ids),
            }
            for value in _ordered_unique(countervailing_ids)[:12]
        ]
        if include_evidence_digest
        else []
    )
    output = {
        "summary": digest,
        "determination": document_decision.determination,
        "confidence": document_decision.confidence,
        "responsiveness_summary": digest,
        "criterion_matches": matches,
        "scope_analysis": [],
        "countervailing_considerations": countervailing,
        "clarification_requests": [],
        "limitations": limitations,
        "coverage": coverage,
    }
    validate_structured_output(output, DOCUMENT_ANALYSIS_OUTPUT_SCHEMA)
    return output


def apply_narrative_projection(
    *,
    base_output: dict[str, Any],
    narrative: NarrativeProjection,
    observations: list[AggregatedEvidenceObservation]
    | tuple[AggregatedEvidenceObservation, ...],
) -> dict[str, Any]:
    """Overlay optional prose without permitting the model to alter any decision field.

    Empty narrative sections intentionally retain the deterministic evidence digest.
    The application resolves observation references into source paragraph citations;
    the narrative model never supplies paragraph IDs directly.
    """

    validate_structured_output(base_output, DOCUMENT_ANALYSIS_OUTPUT_SCHEMA)
    observation_by_id = {value.observation_id: value for value in observations}

    def render(statements: list[Any]) -> list[dict[str, Any]]:
        rendered: list[dict[str, Any]] = []
        for statement in statements:
            unknown = [
                value for value in statement.observation_ids if value not in observation_by_id
            ]
            if unknown:
                raise ValueError("Narrative references observations absent from admitted evidence")
            citation_ids = _ordered_unique(
                [
                    citation
                    for observation_id in statement.observation_ids
                    for citation in observation_by_id[observation_id].citation_ids
                ]
            )
            if not citation_ids:
                raise ValueError("Narrative statement does not resolve to source citations")
            rendered.append({"text": statement.text, "citation_ids": citation_ids[:30]})
        return rendered

    summary = render(narrative.summary)
    explanation = render(narrative.explanation)
    output = {
        **base_output,
        "summary": summary or base_output["summary"],
        "responsiveness_summary": explanation or base_output["responsiveness_summary"],
    }
    validate_structured_output(output, DOCUMENT_ANALYSIS_OUTPUT_SCHEMA)
    return output


def build_evidence_compatibility_projection(
    *,
    plan: EvidenceEvaluationPlan,
    observations: CompactedEvidenceObservations,
    observations_artifact: WorkflowArtifactReference,
    criterion_decisions: CriterionDecisionsResult,
    criterion_decisions_artifact: WorkflowArtifactReference,
    document_decision: DocumentDecisionResult,
    document_decision_artifact: WorkflowArtifactReference,
    catalog: FrozenCriterionCatalog,
) -> EvidenceCompatibilityProjection:
    _artifact(observations_artifact, ArtifactType.ANALYSIS_OBSERVATIONS)
    _artifact(criterion_decisions_artifact, ArtifactType.ANALYSIS_CRITERION_DECISIONS)
    _artifact(document_decision_artifact, ArtifactType.ANALYSIS_DOCUMENT_DECISION)
    if observations.plan_hash != plan.plan_hash:
        raise ValueError("Observations do not belong to the evaluation plan")
    if criterion_decisions.extraction_plan_hash != plan.plan_hash:
        raise ValueError("Criterion decisions do not belong to the evaluation plan")
    if criterion_decisions.observations_result_hash != observations.result_hash:
        raise ValueError("Criterion decisions do not belong to the supplied observations")
    if document_decision.criterion_decisions_result_hash != criterion_decisions.result_hash:
        raise ValueError("Document decision does not belong to the supplied criterion decisions")
    if document_decision.criterion_catalog_hash != catalog.catalog_hash:
        raise ValueError("Document decision does not use the supplied criterion catalog")

    base = {
        "version": COMPATIBILITY_PROJECTION_VERSION,
        "policy_version": COMPATIBILITY_PROJECTION_POLICY_VERSION,
        "direct_output_schema_version": DOCUMENT_ANALYSIS_PIPELINE_VERSION,
        "tenant_id": plan.tenant_id,
        "client_id": plan.client_id,
        "matter_id": plan.matter_id,
        "matter_document_id": plan.matter_document_id,
        "observations_artifact_id": observations_artifact.artifact_id,
        "observations_result_hash": observations.result_hash,
        "criterion_decisions_artifact_id": criterion_decisions_artifact.artifact_id,
        "criterion_decisions_result_hash": criterion_decisions.result_hash,
        "document_decision_artifact_id": document_decision_artifact.artifact_id,
        "document_decision_result_hash": document_decision.result_hash,
        "narrative_status": "NOT_REQUESTED",
        "field_provenance": _field_provenance(),
    }
    if document_decision.decision is None:
        blockers = tuple(
            f"Criterion {key} requires the bounded document-decider fallback"
            for key in document_decision.fallback_criterion_keys
        ) or ("A code-owned document decision is unavailable",)
        payload = {**base, "status": "BLOCKED", "output": None, "blocking_reasons": blockers}
        return EvidenceCompatibilityProjection(**payload, result_hash=content_hash(payload))

    output = assemble_direct_compatibility_output(
        document_decision=document_decision.decision,
        criterion_decisions=criterion_decisions.criterion_decisions,
        observations=observations.observations,
        catalog=catalog,
        coverage=_coverage(plan, observations),
        evidence_complete=observations.evidence_complete,
        material_adjustment_count=observations.material_adjustment_count,
        unmapped_observation_count=criterion_decisions.unmapped_observation_count,
    )
    payload = {**base, "status": "COMPLETED", "output": output, "blocking_reasons": ()}
    return EvidenceCompatibilityProjection(**payload, result_hash=content_hash(payload))


def compatibility_projection_bytes(result: EvidenceCompatibilityProjection) -> bytes:
    return json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def load_compatibility_projection_bytes(content: bytes) -> EvidenceCompatibilityProjection:
    return EvidenceCompatibilityProjection.model_validate_json(content)


def compatibility_projection_derivation_key(result: EvidenceCompatibilityProjection) -> str:
    return content_hash(
        {
            "artifact_type": "ANALYSIS_COMPATIBILITY_RESULT",
            "policy_version": result.policy_version,
            "document_decision_result_hash": result.document_decision_result_hash,
            "result_hash": result.result_hash,
        }
    )


def store_compatibility_projection_artifact(
    result: EvidenceCompatibilityProjection,
    *,
    producer_run_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowArtifactReference, bool]:
    content = compatibility_projection_bytes(result)
    derivation_key = compatibility_projection_derivation_key(result)
    existing = find_workflow_artifact_reference(
        artifact_type=ArtifactType.ANALYSIS_COMPATIBILITY_RESULT,
        derivation_key=derivation_key,
        actor_user_id=actor_user_id,
        tenant_id=result.tenant_id,
        client_id=result.client_id,
    )
    if existing is not None:
        destination = BytesIO()
        copy_workflow_artifact_content(
            artifact_id=existing.artifact_id,
            destination=destination,
            actor_user_id=actor_user_id,
            tenant_id=result.tenant_id,
            client_id=result.client_id,
        )
        if destination.getvalue() != content or existing.content_hash != sha256(content).hexdigest():
            raise ValueError("Existing compatibility projection does not match its deterministic identity")
        return existing, False
    return store_workflow_artifact(
        content=BytesIO(content),
        artifact_type=ArtifactType.ANALYSIS_COMPATIBILITY_RESULT,
        core_scope_kind="MATTER",
        core_scope_id=result.matter_id,
        schema_version=ANALYSIS_COMPATIBILITY_RESULT_SCHEMA_VERSION,
        derivation_key=derivation_key,
        content_hash=sha256(content).hexdigest(),
        byte_length=len(content),
        media_type="application/json",
        producer_kind="CORE",
        producer_run_id=producer_run_id,
        artifact_metadata={
            "matter_document_id": str(result.matter_document_id),
            "result_hash": result.result_hash,
            "status": result.status,
            "determination": result.output.get("determination") if result.output else None,
            "policy_version": result.policy_version,
            "authoritative": False,
        },
        lineage=[
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=artifact_id,
            )
            for artifact_id in (
                result.observations_artifact_id,
                result.criterion_decisions_artifact_id,
                result.document_decision_artifact_id,
            )
        ],
        actor_user_id=actor_user_id,
        tenant_id=result.tenant_id,
        client_id=result.client_id,
        filename=f"{result.matter_document_id}-compatibility-result.json",
    )
