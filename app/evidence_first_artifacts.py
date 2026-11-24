"""Immutable artifact contracts for evidence-first document execution."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from typing import Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifact_gateway import WorkflowArtifactReference, store_workflow_artifact
from app.document_evidence import DocumentMapPlan, ParagraphMap, document_map_coverage
from app.evidence_first_contracts import (
    AGGREGATED_EVIDENCE_OBSERVATION_VERSION,
    AggregatedEvidenceObservation,
    EvidenceObservation,
    aggregate_observations,
    content_hash,
    observation_id,
)
from artifact_service.schemas import WorkflowArtifactLineageInput
from artifact_service.workflow_artifact_types import ArtifactType

ANALYSIS_EVALUATION_PLAN_VERSION = "analysis-evaluation-plan-v1"
ANALYSIS_EVALUATION_PLAN_SCHEMA_VERSION = 1
ANALYSIS_OBSERVATION_FRAGMENT_SCHEMA_VERSION = 2
ANALYSIS_OBSERVATIONS_SCHEMA_VERSION = 1
ANALYSIS_OBSERVATION_FRAGMENT_VERSION = "analysis-observation-fragment-v1"
ANALYSIS_OBSERVATIONS_VERSION = "analysis-observations-v1"

Sha256 = str


def evidence_observation_source_hash(
    *,
    source_content_hash: str,
    document_context_hash: str | None,
) -> str:
    """Bind observation identity to every citable input while preserving legacy plans."""

    if document_context_hash is None:
        return source_content_hash
    return content_hash(
        {
            "source_content_hash": source_content_hash,
            "document_context_hash": document_context_hash,
        }
    )


class AnalysisRoleBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role_key: Literal["evidence_extraction", "criterion_adjudication", "document_decision", "narrative"]
    skill_definition_version_id: uuid.UUID
    provider: str = Field(min_length=1, max_length=100)
    model_key: str = Field(min_length=1, max_length=200)
    prompt_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    application_schema_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    provider_schema_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    model_settings_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    capability_profile_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class AnalysisRoleBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role_key: Literal["evidence_extraction", "criterion_adjudication", "document_decision", "narrative"]
    max_requests: int = Field(ge=0)
    repair_request_reserve: int = Field(ge=0)
    max_input_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(ge=0)
    max_elapsed_seconds: int = Field(ge=0)


class EvidenceUnitSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    paragraph_id: str = Field(min_length=1, max_length=100)
    part: int = Field(ge=1)
    part_count: int = Field(ge=1)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> EvidenceUnitSegment:
        if self.part > self.part_count:
            raise ValueError("Segment part cannot exceed part_count")
        if self.char_end <= self.char_start:
            raise ValueError("Segment char_end must be greater than char_start")
        return self


class EvidenceExtractionUnitPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    unit_id: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    ordinal: int = Field(ge=1)
    parent_unit_id: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    refinement_depth: int = Field(default=0, ge=0)
    input_character_count: int = Field(ge=1)
    source_char_start: int = Field(ge=0)
    source_char_end: int = Field(gt=0)
    segments: tuple[EvidenceUnitSegment, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ranges(self) -> EvidenceExtractionUnitPlan:
        if self.source_char_end <= self.source_char_start:
            raise ValueError("Unit source_char_end must be greater than source_char_start")
        if self.source_char_start != min(segment.char_start for segment in self.segments):
            raise ValueError("Unit source_char_start does not match its segments")
        if self.source_char_end != max(segment.char_end for segment in self.segments):
            raise ValueError("Unit source_char_end does not match its segments")
        return self


class EvidenceEvaluationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["analysis-evaluation-plan-v1"] = ANALYSIS_EVALUATION_PLAN_VERSION
    execution_mode: Literal["EVIDENCE_FIRST"] = "EVIDENCE_FIRST"
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    matter_id: uuid.UUID
    matter_document_id: uuid.UUID
    collection_item_id: uuid.UUID
    source_artifact_id: uuid.UUID
    source_content_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    source_length: int = Field(ge=1)
    document_context_hash: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    document_context_citation_ids: tuple[str, ...] = ()
    task_version_key: str = Field(min_length=1, max_length=200)
    guidance_revision_id: uuid.UUID
    guidance_content_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    criterion_catalog_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    paragraph_map_version: str = Field(min_length=1, max_length=100)
    splitter_version: str = Field(min_length=1, max_length=100)
    max_unit_characters: int = Field(ge=100)
    overlap_segments: int = Field(ge=0)
    adjudication_strategy: Literal["PER_CRITERION"] = "PER_CRITERION"
    role_bindings: tuple[AnalysisRoleBinding, ...] = Field(min_length=1)
    role_budgets: tuple[AnalysisRoleBudget, ...] = Field(min_length=1)
    extraction_units: tuple[EvidenceExtractionUnitPlan, ...] = Field(min_length=1)
    plan_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_plan(self) -> EvidenceEvaluationPlan:
        if bool(self.document_context_hash) != bool(self.document_context_citation_ids):
            raise ValueError("Document context hash and citation IDs must be supplied together")
        if len(self.document_context_citation_ids) != len(set(self.document_context_citation_ids)):
            raise ValueError("Document context citation IDs must be unique")
        role_keys = [binding.role_key for binding in self.role_bindings]
        if len(role_keys) != len(set(role_keys)):
            raise ValueError("Plan role bindings must be unique")
        if "evidence_extraction" not in role_keys:
            raise ValueError("Plan requires an evidence_extraction role binding")
        budget_keys = [budget.role_key for budget in self.role_budgets]
        if len(budget_keys) != len(set(budget_keys)):
            raise ValueError("Plan role budgets must be unique")
        if set(budget_keys) != set(role_keys):
            raise ValueError("Plan role budgets must match role bindings")
        ordinals = [unit.ordinal for unit in self.extraction_units]
        if ordinals != list(range(1, len(self.extraction_units) + 1)):
            raise ValueError("Extraction unit ordinals must be contiguous and ordered")
        unit_ids = [unit.unit_id for unit in self.extraction_units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("Extraction unit IDs must be unique")
        if any(unit.source_char_end > self.source_length for unit in self.extraction_units):
            raise ValueError("Extraction unit range exceeds the source length")
        payload = self.model_dump(mode="json", exclude={"plan_hash"})
        expected_hash = content_hash(payload)
        legacy_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"document_context_hash", "document_context_citation_ids"}
        }
        legacy_hash = content_hash(legacy_payload)
        if self.plan_hash != expected_hash and not (
            self.document_context_hash is None
            and not self.document_context_citation_ids
            and self.plan_hash == legacy_hash
        ):
            raise ValueError("Evaluation plan hash does not match its immutable content")
        return self


class EvidenceUnitAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_index: int | None = Field(default=None, ge=0)
    code: str = Field(min_length=1, max_length=100)
    policy: Literal["NORMALIZE", "DROP_ITEM", "FAIL_UNIT", "FOCUSED_REPAIR", "OPERATOR_REVIEW"]
    path: str = Field(min_length=1, max_length=500)
    detail: str = Field(min_length=1, max_length=2_000)


class EvidenceExtractionUnitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["analysis-observation-fragment-v1"] = ANALYSIS_OBSERVATION_FRAGMENT_VERSION
    plan_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    source_content_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    unit_id: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    unit_ordinal: int = Field(ge=1)
    attempt: int = Field(ge=1)
    status: Literal["COMPLETED", "COMPLETED_WITH_ADJUSTMENTS", "FAILED", "CANCELED"]
    observations: tuple[EvidenceObservation, ...] = ()
    adjustments: tuple[EvidenceUnitAdjustment, ...] = ()
    invocation_ids: tuple[uuid.UUID, ...] = ()
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=4_000)
    retryable: bool = False
    started_at: datetime
    completed_at: datetime

    @model_validator(mode="after")
    def validate_outcome(self) -> EvidenceExtractionUnitResult:
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("Extraction unit timestamps must include a timezone")
        if self.completed_at < self.started_at:
            raise ValueError("Extraction unit completion cannot precede its start")
        if self.status in {"FAILED", "CANCELED"} and self.observations:
            raise ValueError("Failed or canceled extraction units cannot publish observations")
        if self.status == "FAILED" and not self.error_code:
            raise ValueError("Failed extraction units require an error_code")
        if self.retryable and self.status != "FAILED":
            raise ValueError("Only failed extraction units can be retryable")
        if self.status in {"COMPLETED", "COMPLETED_WITH_ADJUSTMENTS"} and self.error_code:
            raise ValueError("Completed extraction units cannot carry an error_code")
        if self.status == "COMPLETED_WITH_ADJUSTMENTS" and not self.adjustments:
            raise ValueError("COMPLETED_WITH_ADJUSTMENTS requires at least one adjustment")
        return self


class SelectedExtractionAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    unit_id: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    unit_ordinal: int = Field(ge=1)
    attempt: int = Field(ge=1)
    status: Literal["COMPLETED", "COMPLETED_WITH_ADJUSTMENTS", "FAILED", "CANCELED"]


class EvidenceCoverageRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    paragraph_id: str = Field(min_length=1, max_length=100)
    char_start: int = Field(ge=0)
    char_end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> EvidenceCoverageRange:
        if self.char_end <= self.char_start:
            raise ValueError("Coverage range char_end must be greater than char_start")
        return self


class CompactedEvidenceObservations(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["analysis-observations-v1"] = ANALYSIS_OBSERVATIONS_VERSION
    plan_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    source_content_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_status: Literal["COMPLETE", "PARTIAL"]
    evidence_complete: bool
    selected_attempts: tuple[SelectedExtractionAttempt, ...]
    missing_unit_ids: tuple[Sha256, ...]
    omitted_ranges: tuple[EvidenceCoverageRange, ...]
    material_adjustment_count: int = Field(ge=0)
    observations: tuple[AggregatedEvidenceObservation, ...]
    result_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_result(self) -> CompactedEvidenceObservations:
        selected_ids = [value.unit_id for value in self.selected_attempts]
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError("Compacted evidence contains duplicate selected units")
        if set(selected_ids) & set(self.missing_unit_ids):
            raise ValueError("Selected and missing extraction units must be disjoint")
        if (self.coverage_status == "COMPLETE") != (not self.omitted_ranges):
            raise ValueError("Coverage status does not match omitted source ranges")
        if self.evidence_complete and (self.coverage_status != "COMPLETE" or self.material_adjustment_count):
            raise ValueError("Complete evidence cannot have coverage gaps or material adjustments")
        expected_hash = content_hash(self.model_dump(mode="json", exclude={"result_hash"}))
        if self.result_hash != expected_hash:
            raise ValueError("Compacted evidence hash does not match its immutable content")
        return self


def _unit_payload(
    *,
    source_content_hash: str,
    document_context_hash: str | None,
    task_version_key: str,
    guidance_content_hash: str,
    criterion_catalog_hash: str,
    extraction_binding_hash: str,
    paragraph_map_version: str,
    splitter_version: str,
    window,
) -> dict[str, object]:
    segments = [
        {
            "paragraph_id": segment.paragraph_id,
            "part": segment.part,
            "part_count": segment.part_count,
            "char_start": segment.char_start,
            "char_end": segment.char_end,
        }
        for segment in window.segments
    ]
    return {
        "source_content_hash": source_content_hash,
        "document_context_hash": document_context_hash,
        "task_version_key": task_version_key,
        "guidance_content_hash": guidance_content_hash,
        "criterion_catalog_hash": criterion_catalog_hash,
        "extraction_binding_hash": extraction_binding_hash,
        "paragraph_map_version": paragraph_map_version,
        "splitter_version": splitter_version,
        "ordinal": window.ordinal,
        "segments": segments,
    }


def build_evidence_evaluation_plan(
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    matter_id: uuid.UUID,
    matter_document_id: uuid.UUID,
    collection_item_id: uuid.UUID,
    source_artifact_id: uuid.UUID,
    source_content_hash: str,
    task_version_key: str,
    guidance_revision_id: uuid.UUID,
    guidance_content_hash: str,
    criterion_catalog_hash: str,
    paragraph_map: ParagraphMap,
    document_map_plan: DocumentMapPlan,
    role_bindings: tuple[AnalysisRoleBinding, ...],
    role_budgets: tuple[AnalysisRoleBudget, ...],
    document_context_hash: str | None = None,
    document_context_citation_ids: tuple[str, ...] = (),
) -> EvidenceEvaluationPlan:
    if bool(document_context_hash) != bool(document_context_citation_ids):
        raise ValueError("Document context hash and citation IDs must be supplied together")
    if document_map_plan.paragraph_map_version != paragraph_map.version:
        raise ValueError("Document map plan does not match the paragraph map version")
    if not document_map_plan.windows:
        raise ValueError("Evidence-first planning requires at least one extraction unit")
    coverage = document_map_coverage(
        paragraph_map,
        document_map_plan,
        successful_window_ids={window.window_id for window in document_map_plan.windows},
    )
    if coverage["omitted_ranges"]:
        raise ValueError("Evidence-first plan does not cover the complete source")
    extraction_bindings = [binding for binding in role_bindings if binding.role_key == "evidence_extraction"]
    if len(extraction_bindings) != 1:
        raise ValueError("Evidence-first planning requires exactly one evidence_extraction role binding")
    extraction_binding_hash = content_hash(extraction_bindings[0].model_dump(mode="json"))
    units: list[dict[str, object]] = []
    for window in document_map_plan.windows:
        identity = _unit_payload(
            source_content_hash=source_content_hash,
            document_context_hash=document_context_hash,
            task_version_key=task_version_key,
            guidance_content_hash=guidance_content_hash,
            criterion_catalog_hash=criterion_catalog_hash,
            extraction_binding_hash=extraction_binding_hash,
            paragraph_map_version=paragraph_map.version,
            splitter_version=document_map_plan.version,
            window=window,
        )
        segments = identity["segments"]
        assert isinstance(segments, list)
        units.append(
            {
                "unit_id": content_hash(identity),
                "ordinal": window.ordinal,
                "parent_unit_id": None,
                "refinement_depth": 0,
                "input_character_count": len(window.text),
                "source_char_start": min(int(segment["char_start"]) for segment in segments),
                "source_char_end": max(int(segment["char_end"]) for segment in segments),
                "segments": segments,
            }
        )
    payload = {
        "version": ANALYSIS_EVALUATION_PLAN_VERSION,
        "execution_mode": "EVIDENCE_FIRST",
        "tenant_id": tenant_id,
        "client_id": client_id,
        "matter_id": matter_id,
        "matter_document_id": matter_document_id,
        "collection_item_id": collection_item_id,
        "source_artifact_id": source_artifact_id,
        "source_content_hash": source_content_hash,
        "source_length": paragraph_map.source_length,
        "document_context_hash": document_context_hash,
        "document_context_citation_ids": list(document_context_citation_ids),
        "task_version_key": task_version_key,
        "guidance_revision_id": guidance_revision_id,
        "guidance_content_hash": guidance_content_hash,
        "criterion_catalog_hash": criterion_catalog_hash,
        "paragraph_map_version": paragraph_map.version,
        "splitter_version": document_map_plan.version,
        "max_unit_characters": document_map_plan.max_characters,
        "overlap_segments": document_map_plan.overlap_segments,
        "adjudication_strategy": "PER_CRITERION",
        "role_bindings": [
            binding.model_dump(mode="json") for binding in sorted(role_bindings, key=lambda value: value.role_key)
        ],
        "role_budgets": [
            budget.model_dump(mode="json") for budget in sorted(role_budgets, key=lambda value: value.role_key)
        ],
        "extraction_units": units,
    }
    return EvidenceEvaluationPlan(**payload, plan_hash=content_hash(payload))


def evaluation_plan_bytes(plan: EvidenceEvaluationPlan) -> bytes:
    return json.dumps(
        plan.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def load_evaluation_plan_bytes(content: bytes) -> EvidenceEvaluationPlan:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Evaluation plan artifact must contain valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TypeError("Evaluation plan artifact must contain a top-level object")
    return EvidenceEvaluationPlan.model_validate(value)


def store_evaluation_plan_artifact(
    plan: EvidenceEvaluationPlan,
    *,
    producer_run_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowArtifactReference, bool]:
    content = evaluation_plan_bytes(plan)
    return store_workflow_artifact(
        content=BytesIO(content),
        artifact_type=ArtifactType.ANALYSIS_EVALUATION_PLAN,
        core_scope_kind="MATTER",
        core_scope_id=plan.matter_id,
        schema_version=ANALYSIS_EVALUATION_PLAN_SCHEMA_VERSION,
        derivation_key=plan.plan_hash,
        content_hash=sha256(content).hexdigest(),
        byte_length=len(content),
        media_type="application/json",
        producer_kind="CORE",
        producer_run_id=producer_run_id,
        artifact_metadata={
            "matter_document_id": str(plan.matter_document_id),
            "collection_item_id": str(plan.collection_item_id),
            "source_content_hash": plan.source_content_hash,
            "plan_hash": plan.plan_hash,
            "task_version_key": plan.task_version_key,
            "extraction_unit_count": len(plan.extraction_units),
        },
        lineage=[
            WorkflowArtifactLineageInput(
                source_kind="ARTIFACT",
                source_artifact_id=plan.source_artifact_id,
            )
        ],
        actor_user_id=actor_user_id,
        tenant_id=plan.tenant_id,
        client_id=plan.client_id,
        filename=f"{plan.matter_document_id}-evidence-evaluation-plan.json",
    )


_OBSERVATION_FRAGMENT_FIELDS_V1 = [
    pa.field("plan_hash", pa.string(), nullable=False),
    pa.field("source_content_hash", pa.string(), nullable=False),
    pa.field("unit_id", pa.string(), nullable=False),
    pa.field("unit_ordinal", pa.int32(), nullable=False),
    pa.field("attempt", pa.int32(), nullable=False),
    pa.field("status", pa.string(), nullable=False),
    pa.field("observations_json", pa.large_string(), nullable=False),
    pa.field("adjustments_json", pa.large_string(), nullable=False),
    pa.field("invocation_ids_json", pa.large_string(), nullable=False),
    pa.field("error_code", pa.string()),
    pa.field("error_message", pa.large_string()),
    pa.field("started_at", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("completed_at", pa.timestamp("us", tz="UTC"), nullable=False),
]
_OBSERVATION_FRAGMENT_SCHEMA_V1 = pa.schema(
    _OBSERVATION_FRAGMENT_FIELDS_V1,
    metadata={
        b"artifact_type": b"ANALYSIS_OBSERVATION_FRAGMENT",
        b"schema_version": b"1",
    },
)
_OBSERVATION_FRAGMENT_SCHEMA = pa.schema(
    [
        *_OBSERVATION_FRAGMENT_FIELDS_V1[:-2],
        pa.field("retryable", pa.bool_(), nullable=False),
        *_OBSERVATION_FRAGMENT_FIELDS_V1[-2:],
    ],
    metadata={
        b"artifact_type": b"ANALYSIS_OBSERVATION_FRAGMENT",
        b"schema_version": str(ANALYSIS_OBSERVATION_FRAGMENT_SCHEMA_VERSION).encode(),
    },
)


def validate_observation_fragment(
    plan: EvidenceEvaluationPlan,
    results: tuple[EvidenceExtractionUnitResult, ...] | list[EvidenceExtractionUnitResult],
) -> tuple[EvidenceExtractionUnitResult, ...]:
    units = {unit.unit_id: unit for unit in plan.extraction_units}
    observation_source_hash = evidence_observation_source_hash(
        source_content_hash=plan.source_content_hash,
        document_context_hash=plan.document_context_hash,
    )
    seen: set[tuple[str, int]] = set()
    validated: list[EvidenceExtractionUnitResult] = []
    for result in results:
        if result.plan_hash != plan.plan_hash or result.source_content_hash != plan.source_content_hash:
            raise ValueError("Observation fragment does not belong to the evaluation plan")
        unit = units.get(result.unit_id)
        if unit is None or result.unit_ordinal != unit.ordinal:
            raise ValueError("Observation fragment references an unknown extraction unit")
        attempt_key = (result.unit_id, result.attempt)
        if attempt_key in seen:
            raise ValueError("Observation fragment repeats an extraction unit attempt")
        seen.add(attempt_key)
        allowed_citation_ids = {
            *(segment.paragraph_id for segment in unit.segments),
            *plan.document_context_citation_ids,
        }
        for observation in result.observations:
            if observation.source_unit_id != result.unit_id:
                raise ValueError("Observation source unit does not match its fragment")
            if not set(observation.citation_ids).issubset(allowed_citation_ids):
                raise ValueError("Observation cites evidence outside its extraction unit")
            expected_id = observation_id(
                source_hash=observation_source_hash,
                source_unit_id=result.unit_id,
                criterion_key=observation.criterion_key,
                signal_type=observation.signal_type,
                fact=observation.fact,
                citation_ids=observation.citation_ids,
            )
            if observation.observation_id != expected_id:
                raise ValueError("Observation identifier does not match its immutable content")
        validated.append(result)
    return tuple(sorted(validated, key=lambda value: (value.unit_ordinal, value.attempt)))


def observation_fragment_bytes(
    plan: EvidenceEvaluationPlan,
    results: tuple[EvidenceExtractionUnitResult, ...] | list[EvidenceExtractionUnitResult],
) -> bytes:
    validated = validate_observation_fragment(plan, results)
    if not validated:
        raise ValueError("Observation fragments require at least one extraction unit result")
    rows = [
        {
            "plan_hash": result.plan_hash,
            "source_content_hash": result.source_content_hash,
            "unit_id": result.unit_id,
            "unit_ordinal": result.unit_ordinal,
            "attempt": result.attempt,
            "status": result.status,
            "observations_json": json.dumps(
                [value.model_dump(mode="json") for value in result.observations],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            "adjustments_json": json.dumps(
                [value.model_dump(mode="json") for value in result.adjustments],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            "invocation_ids_json": json.dumps(
                [str(value) for value in result.invocation_ids],
                separators=(",", ":"),
            ),
            "error_code": result.error_code,
            "error_message": result.error_message,
            "retryable": result.retryable,
            "started_at": result.started_at,
            "completed_at": result.completed_at,
        }
        for result in validated
    ]
    destination = BytesIO()
    pq.write_table(
        pa.Table.from_pylist(rows, schema=_OBSERVATION_FRAGMENT_SCHEMA),
        destination,
        compression="zstd",
    )
    return destination.getvalue()


def load_observation_fragment_bytes(content: bytes) -> tuple[EvidenceExtractionUnitResult, ...]:
    try:
        table = pq.read_table(BytesIO(content))
    except (pa.ArrowInvalid, OSError) as exc:
        raise ValueError("Observation fragment must contain valid Parquet") from exc
    metadata = table.schema.metadata or {}
    if metadata.get(b"artifact_type") != b"ANALYSIS_OBSERVATION_FRAGMENT":
        raise ValueError("Parquet artifact is not an observation fragment")
    schema_version = metadata.get(b"schema_version")
    expected_schema = (
        _OBSERVATION_FRAGMENT_SCHEMA_V1
        if schema_version == b"1"
        else _OBSERVATION_FRAGMENT_SCHEMA
        if schema_version == str(ANALYSIS_OBSERVATION_FRAGMENT_SCHEMA_VERSION).encode()
        else None
    )
    if expected_schema is None:
        raise ValueError("Observation fragment schema version is unsupported")
    if table.schema.remove_metadata() != expected_schema.remove_metadata():
        raise ValueError("Observation fragment Parquet schema is invalid")
    results: list[EvidenceExtractionUnitResult] = []
    for row in table.to_pylist():
        results.append(
            EvidenceExtractionUnitResult(
                plan_hash=row["plan_hash"],
                source_content_hash=row["source_content_hash"],
                unit_id=row["unit_id"],
                unit_ordinal=row["unit_ordinal"],
                attempt=row["attempt"],
                status=row["status"],
                observations=json.loads(row["observations_json"]),
                adjustments=json.loads(row["adjustments_json"]),
                invocation_ids=json.loads(row["invocation_ids_json"]),
                error_code=row["error_code"],
                error_message=row["error_message"],
                retryable=bool(row.get("retryable", False)),
                started_at=row["started_at"],
                completed_at=row["completed_at"],
            )
        )
    return tuple(results)


def observation_fragment_derivation_key(
    plan: EvidenceEvaluationPlan,
    results: tuple[EvidenceExtractionUnitResult, ...] | list[EvidenceExtractionUnitResult],
) -> str:
    validated = validate_observation_fragment(plan, results)
    if not validated:
        raise ValueError("Observation fragments require at least one extraction unit result")
    return content_hash(
        {
            "artifact_type": "ANALYSIS_OBSERVATION_FRAGMENT",
            "schema_version": ANALYSIS_OBSERVATION_FRAGMENT_SCHEMA_VERSION,
            "plan_hash": plan.plan_hash,
            "unit_attempts": [{"unit_id": result.unit_id, "attempt": result.attempt} for result in validated],
        }
    )


def observation_fragment_attempt_derivation_key(
    plan: EvidenceEvaluationPlan,
    *,
    unit_id: str,
    attempt: int,
    schema_version: int = ANALYSIS_OBSERVATION_FRAGMENT_SCHEMA_VERSION,
) -> str:
    if attempt < 1:
        raise ValueError("Extraction attempt must be positive")
    if unit_id not in {unit.unit_id for unit in plan.extraction_units}:
        raise ValueError("Extraction attempt references an unknown unit")
    if schema_version not in {1, ANALYSIS_OBSERVATION_FRAGMENT_SCHEMA_VERSION}:
        raise ValueError("Extraction attempt schema version is unsupported")
    return content_hash(
        {
            "artifact_type": "ANALYSIS_OBSERVATION_FRAGMENT",
            "schema_version": schema_version,
            "plan_hash": plan.plan_hash,
            "unit_attempts": [{"unit_id": unit_id, "attempt": attempt}],
        }
    )


def _merged_ranges(values: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(values):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _omitted_ranges(
    plan: EvidenceEvaluationPlan,
    *,
    successful_unit_ids: set[str],
) -> tuple[EvidenceCoverageRange, ...]:
    planned: dict[str, list[tuple[int, int]]] = {}
    completed: dict[str, list[tuple[int, int]]] = {}
    for unit in plan.extraction_units:
        for segment in unit.segments:
            planned.setdefault(segment.paragraph_id, []).append((segment.char_start, segment.char_end))
            if unit.unit_id in successful_unit_ids:
                completed.setdefault(segment.paragraph_id, []).append((segment.char_start, segment.char_end))
    omitted: list[EvidenceCoverageRange] = []
    for paragraph_id, planned_values in planned.items():
        completed_values = _merged_ranges(completed.get(paragraph_id, []))
        for planned_start, planned_end in _merged_ranges(planned_values):
            cursor = planned_start
            for completed_start, completed_end in completed_values:
                if completed_end <= cursor:
                    continue
                if completed_start >= planned_end:
                    break
                if completed_start > cursor:
                    omitted.append(
                        EvidenceCoverageRange(
                            paragraph_id=paragraph_id,
                            char_start=cursor,
                            char_end=min(completed_start, planned_end),
                        )
                    )
                cursor = max(cursor, min(completed_end, planned_end))
                if cursor >= planned_end:
                    break
            if cursor < planned_end:
                omitted.append(
                    EvidenceCoverageRange(
                        paragraph_id=paragraph_id,
                        char_start=cursor,
                        char_end=planned_end,
                    )
                )
    return tuple(omitted)


def compact_observation_results(
    plan: EvidenceEvaluationPlan,
    results: tuple[EvidenceExtractionUnitResult, ...] | list[EvidenceExtractionUnitResult],
) -> CompactedEvidenceObservations:
    by_attempt: dict[tuple[str, int], EvidenceExtractionUnitResult] = {}
    for result in results:
        validated = validate_observation_fragment(plan, [result])[0]
        key = (validated.unit_id, validated.attempt)
        existing = by_attempt.get(key)
        if existing is not None and existing != validated:
            raise ValueError("Conflicting observation fragments exist for one unit attempt")
        by_attempt[key] = validated
    latest: dict[str, EvidenceExtractionUnitResult] = {}
    for result in by_attempt.values():
        existing = latest.get(result.unit_id)
        if existing is None or result.attempt > existing.attempt:
            latest[result.unit_id] = result
    selected = [latest[unit.unit_id] for unit in plan.extraction_units if unit.unit_id in latest]
    missing_unit_ids = tuple(unit.unit_id for unit in plan.extraction_units if unit.unit_id not in latest)
    successful = {result.unit_id for result in selected if result.status in {"COMPLETED", "COMPLETED_WITH_ADJUSTMENTS"}}
    omitted_ranges = _omitted_ranges(plan, successful_unit_ids=successful)
    material_adjustment_count = sum(
        adjustment.policy != "NORMALIZE" for result in selected for adjustment in result.adjustments
    )
    observations = aggregate_observations(
        [observation for result in selected if result.unit_id in successful for observation in result.observations],
        source_hash=evidence_observation_source_hash(
            source_content_hash=plan.source_content_hash,
            document_context_hash=plan.document_context_hash,
        ),
    )
    payload = {
        "version": ANALYSIS_OBSERVATIONS_VERSION,
        "plan_hash": plan.plan_hash,
        "source_content_hash": plan.source_content_hash,
        "coverage_status": "COMPLETE" if not omitted_ranges else "PARTIAL",
        "evidence_complete": not omitted_ranges and material_adjustment_count == 0,
        "selected_attempts": [
            {
                "unit_id": result.unit_id,
                "unit_ordinal": result.unit_ordinal,
                "attempt": result.attempt,
                "status": result.status,
            }
            for result in selected
        ],
        "missing_unit_ids": missing_unit_ids,
        "omitted_ranges": [value.model_dump(mode="json") for value in omitted_ranges],
        "material_adjustment_count": material_adjustment_count,
        "observations": [value.model_dump(mode="json") for value in observations],
    }
    return CompactedEvidenceObservations(**payload, result_hash=content_hash(payload))


def merge_family_observation_results(
    members: tuple[
        tuple[EvidenceEvaluationPlan, CompactedEvidenceObservations], ...
    ]
    | list[tuple[EvidenceEvaluationPlan, CompactedEvidenceObservations]],
) -> tuple[EvidenceEvaluationPlan, CompactedEvidenceObservations]:
    """Merge complete member evidence into one citation-safe family artifact.

    Member extraction remains independently restartable.  This deterministic
    projection namespaces source citations by matter document and gives
    adjudication one immutable family evidence set.
    """

    if not members:
        raise ValueError("Family evidence requires at least one member")
    ordered = sorted(members, key=lambda value: str(value[0].matter_document_id))
    anchor = ordered[0][0]

    def contract_identity(plan: EvidenceEvaluationPlan) -> tuple[object, ...]:
        return (
            plan.tenant_id,
            plan.client_id,
            plan.matter_id,
            plan.task_version_key,
            plan.guidance_revision_id,
            plan.guidance_content_hash,
            plan.criterion_catalog_hash,
            plan.paragraph_map_version,
            plan.splitter_version,
            plan.max_unit_characters,
            plan.overlap_segments,
            plan.adjudication_strategy,
            plan.role_bindings,
        )

    for plan, observations in ordered:
        if observations.plan_hash != plan.plan_hash:
            raise ValueError("Family member observations do not match their evaluation plan")
        if not observations.evidence_complete:
            raise ValueError("Family evidence cannot merge an incomplete member")
        if contract_identity(plan) != contract_identity(anchor):
            raise ValueError("Family member evidence plans do not share one frozen assessment contract")

    role_budgets: list[dict[str, object]] = []
    for role_key in sorted(budget.role_key for budget in anchor.role_budgets):
        member_budgets = [
            next(budget for budget in plan.role_budgets if budget.role_key == role_key)
            for plan, _observations in ordered
        ]
        role_budgets.append(
            {
                "role_key": role_key,
                "max_requests": sum(budget.max_requests for budget in member_budgets),
                "repair_request_reserve": sum(
                    budget.repair_request_reserve for budget in member_budgets
                ),
                "max_input_tokens": sum(budget.max_input_tokens for budget in member_budgets),
                "max_output_tokens": sum(budget.max_output_tokens for budget in member_budgets),
                "max_elapsed_seconds": sum(
                    budget.max_elapsed_seconds for budget in member_budgets
                ),
            }
        )

    source_content_hash = content_hash(
        [
            {
                "matter_document_id": str(plan.matter_document_id),
                "source_content_hash": plan.source_content_hash,
            }
            for plan, _observations in ordered
        ]
    )
    context_citation_ids = tuple(
        f"document:{plan.matter_document_id}:{citation_id}"
        for plan, _observations in ordered
        for citation_id in plan.document_context_citation_ids
    )
    document_context_hash = (
        content_hash(
            [
                {
                    "matter_document_id": str(plan.matter_document_id),
                    "document_context_hash": plan.document_context_hash,
                }
                for plan, _observations in ordered
            ]
        )
        if context_citation_ids
        else None
    )

    units: list[dict[str, object]] = []
    selected_attempts: list[dict[str, object]] = []
    merged_observations: list[dict[str, object]] = []
    source_offset = 0
    unit_ordinal = 0
    for plan, observations in ordered:
        document_prefix = f"document:{plan.matter_document_id}:"
        selected_by_unit = {value.unit_id: value for value in observations.selected_attempts}
        family_unit_ids = {
            unit.unit_id: content_hash(
                {
                    "family_member_document_id": str(plan.matter_document_id),
                    "member_unit_id": unit.unit_id,
                }
            )
            for unit in plan.extraction_units
        }
        for unit in plan.extraction_units:
            unit_ordinal += 1
            units.append(
                {
                    "unit_id": family_unit_ids[unit.unit_id],
                    "ordinal": unit_ordinal,
                    "parent_unit_id": (
                        family_unit_ids.get(unit.parent_unit_id) if unit.parent_unit_id else None
                    ),
                    "refinement_depth": unit.refinement_depth,
                    "input_character_count": unit.input_character_count,
                    "source_char_start": source_offset + unit.source_char_start,
                    "source_char_end": source_offset + unit.source_char_end,
                    "segments": [
                        {
                            **segment.model_dump(mode="json"),
                            "paragraph_id": f"{document_prefix}{segment.paragraph_id}",
                            "char_start": source_offset + segment.char_start,
                            "char_end": source_offset + segment.char_end,
                        }
                        for segment in unit.segments
                    ],
                }
            )
            selected = selected_by_unit.get(unit.unit_id)
            if selected is None:
                raise ValueError("Complete family member evidence is missing a selected unit")
            selected_attempts.append(
                {
                    "unit_id": family_unit_ids[selected.unit_id],
                    "unit_ordinal": unit_ordinal,
                    "attempt": selected.attempt,
                    "status": selected.status,
                }
            )
        for observation in observations.observations:
            citation_ids = tuple(
                f"{document_prefix}{citation_id}"
                for citation_id in observation.citation_ids
            )
            merged_observations.append(
                {
                    "version": AGGREGATED_EVIDENCE_OBSERVATION_VERSION,
                    "observation_id": content_hash(
                        {
                            "version": AGGREGATED_EVIDENCE_OBSERVATION_VERSION,
                            "source_hash": source_content_hash,
                            "matter_document_id": str(plan.matter_document_id),
                            "criterion_key": observation.criterion_key,
                            "signal_type": observation.signal_type,
                            "fact": " ".join(observation.fact.casefold().split()),
                            "citation_ids": citation_ids,
                        }
                    ),
                    "criterion_key": observation.criterion_key,
                    "signal_type": observation.signal_type,
                    "fact": observation.fact,
                    "citation_ids": citation_ids,
                    "source_unit_ids": tuple(
                        family_unit_ids[source_unit_id]
                        for source_unit_id in observation.source_unit_ids
                    ),
                }
            )
        source_offset += plan.source_length

    plan_payload = {
        "version": ANALYSIS_EVALUATION_PLAN_VERSION,
        "execution_mode": "EVIDENCE_FIRST",
        "tenant_id": anchor.tenant_id,
        "client_id": anchor.client_id,
        "matter_id": anchor.matter_id,
        "matter_document_id": anchor.matter_document_id,
        "collection_item_id": anchor.collection_item_id,
        "source_artifact_id": anchor.source_artifact_id,
        "source_content_hash": source_content_hash,
        "source_length": source_offset,
        "document_context_hash": document_context_hash,
        "document_context_citation_ids": list(context_citation_ids),
        "task_version_key": f"{anchor.task_version_key}:family-v1",
        "guidance_revision_id": anchor.guidance_revision_id,
        "guidance_content_hash": anchor.guidance_content_hash,
        "criterion_catalog_hash": anchor.criterion_catalog_hash,
        "paragraph_map_version": "family-evidence-map-v1",
        "splitter_version": "family-evidence-merge-v1",
        "max_unit_characters": max(plan.max_unit_characters for plan, _ in ordered),
        "overlap_segments": max(plan.overlap_segments for plan, _ in ordered),
        "adjudication_strategy": "PER_CRITERION",
        "role_bindings": [value.model_dump(mode="json") for value in anchor.role_bindings],
        "role_budgets": role_budgets,
        "extraction_units": units,
    }
    family_plan = EvidenceEvaluationPlan(**plan_payload, plan_hash=content_hash(plan_payload))
    result_payload = {
        "version": ANALYSIS_OBSERVATIONS_VERSION,
        "plan_hash": family_plan.plan_hash,
        "source_content_hash": family_plan.source_content_hash,
        "coverage_status": "COMPLETE",
        "evidence_complete": True,
        "selected_attempts": selected_attempts,
        "missing_unit_ids": [],
        "omitted_ranges": [],
        "material_adjustment_count": sum(
            observations.material_adjustment_count for _plan, observations in ordered
        ),
        "observations": merged_observations,
    }
    family_observations = CompactedEvidenceObservations(
        **result_payload,
        result_hash=content_hash(result_payload),
    )
    return family_plan, family_observations


def scope_family_observation_results(
    plan: EvidenceEvaluationPlan,
    observations: CompactedEvidenceObservations,
    *,
    target_document_id: uuid.UUID,
) -> CompactedEvidenceObservations:
    """Project one member's evidence from an immutable family artifact.

    Family extraction namespaces every source paragraph and context citation with
    the matter document that was being evaluated.  Adjudication must retain the
    family artifact as provenance while admitting only observations produced for
    the target document.  Otherwise one family decision is copied to a privileged
    parent and its non-privileged attachments alike.
    """

    if observations.plan_hash != plan.plan_hash:
        raise ValueError("Family observations do not match their evaluation plan")
    prefix = f"document:{target_document_id}:"
    target_units = {
        unit.unit_id
        for unit in plan.extraction_units
        if any(segment.paragraph_id.startswith(prefix) for segment in unit.segments)
    }
    if not target_units:
        raise ValueError("Target document is not represented in the family evidence plan")

    selected_attempts = tuple(
        value for value in observations.selected_attempts if value.unit_id in target_units
    )
    missing_unit_ids = tuple(
        value for value in observations.missing_unit_ids if value in target_units
    )
    omitted_ranges = tuple(
        value for value in observations.omitted_ranges if value.paragraph_id.startswith(prefix)
    )
    scoped_observations = tuple(
        value
        for value in observations.observations
        if any(citation_id.startswith(prefix) for citation_id in value.citation_ids)
    )
    if len(selected_attempts) + len(missing_unit_ids) != len(target_units):
        raise ValueError("Target family evidence coverage cannot be reconstructed")

    coverage_status = "COMPLETE" if not omitted_ranges else "PARTIAL"
    evidence_complete = (
        observations.evidence_complete
        and coverage_status == "COMPLETE"
        and not missing_unit_ids
    )
    payload = {
        "version": ANALYSIS_OBSERVATIONS_VERSION,
        # The persisted family plan remains the immutable provenance source.
        "plan_hash": plan.plan_hash,
        "source_content_hash": content_hash(
            {
                "family_source_content_hash": observations.source_content_hash,
                "target_document_id": str(target_document_id),
            }
        ),
        "coverage_status": coverage_status,
        "evidence_complete": evidence_complete,
        "selected_attempts": [value.model_dump(mode="json") for value in selected_attempts],
        "missing_unit_ids": list(missing_unit_ids),
        "omitted_ranges": [value.model_dump(mode="json") for value in omitted_ranges],
        "material_adjustment_count": 0 if evidence_complete else observations.material_adjustment_count,
        "observations": [value.model_dump(mode="json") for value in scoped_observations],
    }
    return CompactedEvidenceObservations(
        **payload,
        result_hash=content_hash(payload),
    )


_OBSERVATIONS_SCHEMA = pa.schema(
    [
        pa.field("plan_hash", pa.string(), nullable=False),
        pa.field("source_content_hash", pa.string(), nullable=False),
        pa.field("observation_id", pa.string(), nullable=False),
        pa.field("criterion_key", pa.string(), nullable=False),
        pa.field("signal_type", pa.string(), nullable=False),
        pa.field("fact", pa.large_string(), nullable=False),
        pa.field("citation_ids_json", pa.large_string(), nullable=False),
        pa.field("source_unit_ids_json", pa.large_string(), nullable=False),
    ]
)


def compacted_observations_bytes(result: CompactedEvidenceObservations) -> bytes:
    manifest = result.model_dump(mode="json", exclude={"observations"})
    schema = _OBSERVATIONS_SCHEMA.with_metadata(
        {
            b"artifact_type": b"ANALYSIS_OBSERVATIONS",
            b"schema_version": str(ANALYSIS_OBSERVATIONS_SCHEMA_VERSION).encode(),
            b"manifest_json": json.dumps(
                manifest,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        }
    )
    rows = [
        {
            "plan_hash": result.plan_hash,
            "source_content_hash": result.source_content_hash,
            "observation_id": observation.observation_id,
            "criterion_key": observation.criterion_key,
            "signal_type": observation.signal_type,
            "fact": observation.fact,
            "citation_ids_json": json.dumps(list(observation.citation_ids), separators=(",", ":")),
            "source_unit_ids_json": json.dumps(list(observation.source_unit_ids), separators=(",", ":")),
        }
        for observation in result.observations
    ]
    destination = BytesIO()
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), destination, compression="zstd")
    return destination.getvalue()


def load_compacted_observations_bytes(content: bytes) -> CompactedEvidenceObservations:
    try:
        table = pq.read_table(BytesIO(content))
    except (pa.ArrowInvalid, OSError) as exc:
        raise ValueError("Compacted observations must contain valid Parquet") from exc
    metadata = table.schema.metadata or {}
    if metadata.get(b"artifact_type") != b"ANALYSIS_OBSERVATIONS":
        raise ValueError("Parquet artifact is not a compacted observation set")
    expected_version = str(ANALYSIS_OBSERVATIONS_SCHEMA_VERSION).encode()
    if metadata.get(b"schema_version") != expected_version:
        raise ValueError("Compacted observation schema version is unsupported")
    if table.schema.remove_metadata() != _OBSERVATIONS_SCHEMA:
        raise ValueError("Compacted observation Parquet schema is invalid")
    raw_manifest = metadata.get(b"manifest_json")
    if raw_manifest is None:
        raise ValueError("Compacted observation artifact is missing its manifest")
    try:
        manifest = json.loads(raw_manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Compacted observation manifest is invalid") from exc
    observations = [
        {
            "observation_id": row["observation_id"],
            "criterion_key": row["criterion_key"],
            "signal_type": row["signal_type"],
            "fact": row["fact"],
            "citation_ids": json.loads(row["citation_ids_json"]),
            "source_unit_ids": json.loads(row["source_unit_ids_json"]),
        }
        for row in table.to_pylist()
    ]
    return CompactedEvidenceObservations.model_validate({**manifest, "observations": observations})


def compacted_observations_derivation_key(result: CompactedEvidenceObservations) -> str:
    return content_hash(
        {
            "artifact_type": "ANALYSIS_OBSERVATIONS",
            "schema_version": ANALYSIS_OBSERVATIONS_SCHEMA_VERSION,
            "plan_hash": result.plan_hash,
            "selected_attempts": [value.model_dump(mode="json") for value in result.selected_attempts],
        }
    )


def store_observation_fragment_artifact(
    plan: EvidenceEvaluationPlan,
    *,
    plan_artifact_id: uuid.UUID,
    results: tuple[EvidenceExtractionUnitResult, ...] | list[EvidenceExtractionUnitResult],
    producer_run_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowArtifactReference, bool]:
    validated = validate_observation_fragment(plan, results)
    content = observation_fragment_bytes(plan, validated)
    return store_workflow_artifact(
        content=BytesIO(content),
        artifact_type=ArtifactType.ANALYSIS_OBSERVATION_FRAGMENT,
        core_scope_kind="MATTER",
        core_scope_id=plan.matter_id,
        schema_version=ANALYSIS_OBSERVATION_FRAGMENT_SCHEMA_VERSION,
        derivation_key=observation_fragment_derivation_key(plan, validated),
        content_hash=sha256(content).hexdigest(),
        byte_length=len(content),
        media_type="application/vnd.apache.parquet",
        producer_kind="CORE",
        producer_run_id=producer_run_id,
        artifact_metadata={
            "matter_document_id": str(plan.matter_document_id),
            "plan_hash": plan.plan_hash,
            "unit_result_count": len(validated),
            "minimum_unit_ordinal": min(value.unit_ordinal for value in validated),
            "maximum_unit_ordinal": max(value.unit_ordinal for value in validated),
            "completed_count": sum(value.status in {"COMPLETED", "COMPLETED_WITH_ADJUSTMENTS"} for value in validated),
            "failed_count": sum(value.status == "FAILED" for value in validated),
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
        filename=f"{plan.matter_document_id}-observation-fragment.parquet",
    )


def store_compacted_observations_artifact(
    plan: EvidenceEvaluationPlan,
    *,
    result: CompactedEvidenceObservations,
    plan_artifact_id: uuid.UUID,
    fragment_artifact_ids: tuple[uuid.UUID, ...] | list[uuid.UUID],
    producer_run_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowArtifactReference, bool]:
    if result.plan_hash != plan.plan_hash or result.source_content_hash != plan.source_content_hash:
        raise ValueError("Compacted observations do not belong to the evaluation plan")
    lineage_ids = [plan_artifact_id, *fragment_artifact_ids]
    if len(lineage_ids) != len(set(lineage_ids)):
        raise ValueError("Compacted observation lineage contains duplicate artifacts")
    content = compacted_observations_bytes(result)
    return store_workflow_artifact(
        content=BytesIO(content),
        artifact_type=ArtifactType.ANALYSIS_OBSERVATIONS,
        core_scope_kind="MATTER",
        core_scope_id=plan.matter_id,
        schema_version=ANALYSIS_OBSERVATIONS_SCHEMA_VERSION,
        derivation_key=compacted_observations_derivation_key(result),
        content_hash=sha256(content).hexdigest(),
        byte_length=len(content),
        media_type="application/vnd.apache.parquet",
        producer_kind="CORE",
        producer_run_id=producer_run_id,
        artifact_metadata={
            "matter_document_id": str(plan.matter_document_id),
            "plan_hash": plan.plan_hash,
            "result_hash": result.result_hash,
            "coverage_status": result.coverage_status,
            "evidence_complete": result.evidence_complete,
            "selected_unit_count": len(result.selected_attempts),
            "missing_unit_count": len(result.missing_unit_ids),
            "observation_count": len(result.observations),
            "material_adjustment_count": result.material_adjustment_count,
        },
        lineage=[
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=artifact_id,
            )
            for artifact_id in lineage_ids
        ],
        actor_user_id=actor_user_id,
        tenant_id=plan.tenant_id,
        client_id=plan.client_id,
        filename=f"{plan.matter_document_id}-observations.parquet",
    )
