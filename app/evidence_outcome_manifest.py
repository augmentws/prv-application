"""Collection-level operator manifest for the evidence-first shadow pipeline."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from hashlib import sha256
from io import BytesIO
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifact_gateway import WorkflowArtifactReference, store_workflow_artifact
from app.model_execution import content_hash
from artifact_service.schemas import WorkflowArtifactLineageInput
from artifact_service.workflow_artifact_types import ArtifactType

OUTCOME_MANIFEST_VERSION = "evidence-analysis-outcome-manifest-v1"
ANALYSIS_OUTCOME_MANIFEST_SCHEMA_VERSION = 1

StageStatus = Literal[
    "NOT_STARTED",
    "QUEUED",
    "RUNNING",
    "COMPLETED",
    "COMPLETED_WITH_ERRORS",
    "FAILED",
    "CANCELED",
    "BLOCKED",
    "NOT_REQUESTED",
]


class EvidenceStageOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: StageStatus
    workflow_run_id: uuid.UUID | None = None
    artifact_ids: dict[str, uuid.UUID] = Field(default_factory=dict)
    completed_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    retryable: bool = False
    error_code: str | None = Field(default=None, max_length=100)
    error_summary: str | None = Field(default=None, max_length=1_000)


class EvidenceDocumentOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    matter_document_id: uuid.UUID
    extraction: EvidenceStageOutcome
    adjudication: EvidenceStageOutcome
    document_decision: EvidenceStageOutcome
    compatibility_projection: EvidenceStageOutcome
    narrative: EvidenceStageOutcome
    synthesis_readiness: Literal["PENDING", "READY", "READY_WITH_FALLBACK", "ACTION_REQUIRED"]
    selected_output_artifact_id: uuid.UUID | None = None
    selected_output_kind: Literal["DETERMINISTIC_COMPATIBILITY", "NARRATIVE_PRESENTATION"] | None = None
    retryable_stages: tuple[Literal["EXTRACTION", "ADJUDICATION", "NARRATIVE"], ...] = ()

    @model_validator(mode="after")
    def validate_readiness(self) -> EvidenceDocumentOutcome:
        ready = self.synthesis_readiness in {"READY", "READY_WITH_FALLBACK"}
        if ready != (self.selected_output_artifact_id is not None):
            raise ValueError("Synthesis readiness and selected output artifact must agree")
        if (self.selected_output_artifact_id is None) != (self.selected_output_kind is None):
            raise ValueError("Selected output artifact and kind must be supplied together")
        return self


class EvidenceOutcomeUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_count: int = Field(ge=0)
    request_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    cache_write_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class EvidenceAnalysisOutcomeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["evidence-analysis-outcome-manifest-v1"] = OUTCOME_MANIFEST_VERSION
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    matter_id: uuid.UUID
    assessment_id: uuid.UUID
    population_hash: str = Field(min_length=64, max_length=64)
    document_count: int = Field(ge=0)
    documents: tuple[EvidenceDocumentOutcome, ...]
    readiness_counts: dict[str, int]
    stage_status_counts: dict[str, dict[str, int]]
    usage: EvidenceOutcomeUsage
    source_workflow_ids: tuple[uuid.UUID, ...]
    source_artifact_ids: tuple[uuid.UUID, ...]
    result_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_manifest(self) -> EvidenceAnalysisOutcomeManifest:
        if len(self.documents) != self.document_count:
            raise ValueError("Outcome manifest document count does not match its population")
        if len({value.matter_document_id for value in self.documents}) != self.document_count:
            raise ValueError("Outcome manifest contains duplicate documents")
        if sum(self.readiness_counts.values()) != self.document_count:
            raise ValueError("Outcome manifest readiness counts do not reconcile")
        if content_hash([str(value.matter_document_id) for value in self.documents]) != self.population_hash:
            raise ValueError("Outcome manifest population hash does not match its documents")
        payload = self.model_dump(mode="json", exclude={"result_hash"})
        if content_hash(payload) != self.result_hash:
            raise ValueError("Outcome manifest hash does not match its immutable content")
        return self


def _workflow_status(value: Any) -> StageStatus:
    status = str(getattr(value, "status", "NOT_STARTED"))
    return status if status in StageStatus.__args__ else "FAILED"  # type: ignore[attr-defined]


def _safe_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _stage(
    workflow: Any | None,
    *,
    status: StageStatus | None = None,
    artifacts: dict[str, Any] | None = None,
    completed_count: int = 0,
    failed_count: int = 0,
    retryable: bool | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
) -> EvidenceStageOutcome:
    artifact_ids = {
        key: parsed
        for key, value in (artifacts or {}).items()
        if (parsed := _safe_uuid(value)) is not None
    }
    if workflow is None:
        return EvidenceStageOutcome(
            status=status or "NOT_STARTED",
            artifact_ids=artifact_ids,
            completed_count=completed_count,
            failed_count=failed_count,
            retryable=bool(retryable),
            error_code=error_code,
            error_summary=error_summary,
        )
    resolved_status = status or _workflow_status(workflow)
    has_error = resolved_status in {"FAILED", "COMPLETED_WITH_ERRORS", "CANCELED", "BLOCKED"}
    resolved_retryable = (
        retryable
        if retryable is not None
        else bool(getattr(workflow, "failure_retriable", False))
        or resolved_status in {"FAILED", "COMPLETED_WITH_ERRORS", "CANCELED"}
    )
    return EvidenceStageOutcome(
        status=resolved_status,
        workflow_run_id=workflow.id,
        artifact_ids=artifact_ids,
        completed_count=completed_count,
        failed_count=failed_count,
        retryable=resolved_retryable,
        error_code=(error_code or getattr(workflow, "failure_code", None)) if has_error else None,
        error_summary=(
            error_summary
            or getattr(workflow, "failure_summary", None)
            or getattr(workflow, "error_message", None)
            if has_error
            else None
        ),
    )


def _latest_by_document(workflows: Iterable[Any]) -> dict[uuid.UUID, Any]:
    values: dict[uuid.UUID, Any] = {}
    for workflow in workflows:
        document_id = _safe_uuid((workflow.input_snapshot or {}).get("matter_document_id"))
        if document_id is None:
            continue
        previous = values.get(document_id)
        previous_time = getattr(previous, "updated_at", None) or getattr(previous, "created_at", None)
        current_time = getattr(workflow, "updated_at", None) or getattr(workflow, "created_at", None)
        if previous is None or (current_time is not None and current_time >= previous_time):
            values[document_id] = workflow
    return values


def build_evidence_outcome_manifest(
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    document_ids: Sequence[uuid.UUID],
    extraction_workflows: Sequence[Any],
    adjudication_workflows: Sequence[Any],
    narrative_workflows: Sequence[Any],
    artifact_references: Sequence[WorkflowArtifactReference] = (),
    document_failures: dict[uuid.UUID, Any] | None = None,
    pending_document_ids: set[uuid.UUID] | frozenset[uuid.UUID] = frozenset(),
) -> EvidenceAnalysisOutcomeManifest:
    ordered_ids = tuple(dict.fromkeys(document_ids))
    if len(ordered_ids) != len(document_ids):
        raise ValueError("Outcome manifest population contains duplicate documents")
    extraction_by_document: dict[uuid.UUID, tuple[Any, dict[str, Any]]] = {}
    for workflow in extraction_workflows:
        for report in (workflow.progress or {}).get("documents", []):
            document_id = _safe_uuid(report.get("matter_document_id"))
            if document_id is not None:
                extraction_by_document[document_id] = (workflow, report)
    workflow_by_id = {
        value.id: value
        for value in (*extraction_workflows, *adjudication_workflows, *narrative_workflows)
    }
    artifacts_by_stage: dict[tuple[uuid.UUID, ArtifactType], WorkflowArtifactReference] = {}
    for reference in artifact_references:
        document_id = _safe_uuid(reference.metadata.get("matter_document_id"))
        if document_id is None or reference.producer_run_id not in workflow_by_id:
            continue
        key = (document_id, reference.artifact_type)
        previous = artifacts_by_stage.get(key)
        if previous is None or reference.created_at >= previous.created_at:
            artifacts_by_stage[key] = reference
    for document_id in ordered_ids:
        if document_id in extraction_by_document:
            continue
        reference = artifacts_by_stage.get((document_id, ArtifactType.ANALYSIS_OBSERVATIONS))
        if reference is None:
            continue
        workflow = workflow_by_id[reference.producer_run_id]
        missing = int(reference.metadata.get("missing_unit_count", 0) or 0)
        selected = int(reference.metadata.get("selected_unit_count", 0) or 0)
        extraction_by_document[document_id] = (
            workflow,
            {
                "matter_document_id": str(document_id),
                "status": (
                    "COMPLETED"
                    if bool(reference.metadata.get("evidence_complete"))
                    else "COMPLETED_WITH_ERRORS"
                ),
                "unit_count": selected + missing,
                "missing_unit_count": missing,
                "observations_artifact_id": str(reference.artifact_id),
            },
        )
    adjudication_by_document = _latest_by_document(adjudication_workflows)
    narrative_by_document = _latest_by_document(narrative_workflows)

    documents: list[EvidenceDocumentOutcome] = []
    source_artifact_ids: list[uuid.UUID] = []
    failures = document_failures or {}
    for document_id in ordered_ids:
        extraction_value = extraction_by_document.get(document_id)
        if extraction_value is None:
            failure = failures.get(document_id)
            extraction = (
                _stage(
                    None,
                    status="FAILED",
                    failed_count=1,
                    retryable=bool(getattr(failure, "failure_retriable", False)),
                    error_code=getattr(failure, "failure_code", None),
                    error_summary=getattr(failure, "failure_message", None),
                )
                if failure is not None and getattr(failure, "status", None) == "FAILED"
                else _stage(None, status="QUEUED")
                if document_id in pending_document_ids
                else _stage(None)
            )
        else:
            extraction_workflow, report = extraction_value
            extraction = _stage(
                extraction_workflow,
                status=report.get("status", _workflow_status(extraction_workflow)),
                artifacts={
                    "evaluation_plan": report.get("plan_artifact_id"),
                    "observations": report.get("observations_artifact_id"),
                },
                completed_count=int(report.get("unit_count", 0))
                - int(report.get("missing_unit_count", 0)),
                failed_count=int(report.get("missing_unit_count", 0)),
                retryable=report.get("status") in {"FAILED", "COMPLETED_WITH_ERRORS"},
            )
        adjudication_workflow = adjudication_by_document.get(document_id)
        progress = adjudication_workflow.progress if adjudication_workflow is not None else {}
        adjudication_active = (
            adjudication_workflow is not None
            and _workflow_status(adjudication_workflow) in {"QUEUED", "RUNNING"}
        )
        document_reference = (
            None
            if adjudication_active
            else artifacts_by_stage.get((document_id, ArtifactType.ANALYSIS_DOCUMENT_DECISION))
        )
        compatibility_reference = (
            None
            if adjudication_active
            else artifacts_by_stage.get((document_id, ArtifactType.ANALYSIS_COMPATIBILITY_RESULT))
        )
        adjudication = _stage(
            adjudication_workflow,
            artifacts={
                "adjudication_plan": progress.get("adjudication_plan_artifact_id"),
                "criterion_decisions": progress.get("criterion_decisions_artifact_id"),
            },
            completed_count=int(progress.get("completed_packet_count", 0)),
            failed_count=int(progress.get("failed_packet_count", 0)),
        )
        decision_status = progress.get("document_decision_status") or (
            document_reference.metadata.get("status") if document_reference else None
        )
        document_decision = _stage(
            adjudication_workflow,
            status=(
                "COMPLETED"
                if decision_status == "COMPLETED"
                else "BLOCKED"
                if decision_status == "MODEL_FALLBACK_REQUIRED"
                else "COMPLETED_WITH_ERRORS"
                if decision_status == "INSUFFICIENT"
                else None
            ),
            artifacts={
                "document_decision": progress.get("document_decision_artifact_id")
                or (document_reference.artifact_id if document_reference else None)
            },
            completed_count=1 if decision_status else 0,
        )
        compatibility_status = progress.get("compatibility_projection_status") or (
            compatibility_reference.metadata.get("status")
            if compatibility_reference
            else None
        )
        compatibility = _stage(
            adjudication_workflow,
            status=(
                "COMPLETED"
                if compatibility_status == "COMPLETED"
                else "BLOCKED"
                if compatibility_status == "BLOCKED"
                else None
            ),
            artifacts={
                "compatibility_result": progress.get("compatibility_result_artifact_id")
                or (compatibility_reference.artifact_id if compatibility_reference else None)
            },
            completed_count=1 if compatibility_status else 0,
        )
        narrative_workflow = narrative_by_document.get(document_id)
        narrative_progress = narrative_workflow.progress if narrative_workflow is not None else {}
        narrative_reference = artifacts_by_stage.get(
            (document_id, ArtifactType.ANALYSIS_NARRATIVE_PROJECTION)
        )
        narrative_active = (
            narrative_workflow is not None
            and _workflow_status(narrative_workflow) in {"QUEUED", "RUNNING"}
        )
        narrative_status = None if narrative_active else narrative_progress.get(
            "narrative_status"
        ) or ("COMPLETED" if narrative_reference else None)
        narrative = _stage(
            narrative_workflow,
            status=(
                "NOT_REQUESTED"
                if narrative_workflow is None
                else "COMPLETED"
                if narrative_status == "COMPLETED"
                else "COMPLETED_WITH_ERRORS"
                if narrative_status == "FAILED"
                else None
            ),
            artifacts={
                "narrative_projection": narrative_progress.get("narrative_artifact_id")
                or (narrative_reference.artifact_id if narrative_reference else None)
            },
            completed_count=1 if narrative_status == "COMPLETED" else 0,
            failed_count=1 if narrative_status == "FAILED" else 0,
        )

        compatibility_artifact_id = compatibility.artifact_ids.get("compatibility_result")
        narrative_artifact_id = narrative.artifact_ids.get("narrative_projection")
        if narrative.status == "COMPLETED" and narrative_artifact_id is not None:
            readiness = "READY"
            selected_artifact = narrative_artifact_id
            selected_kind = "NARRATIVE_PRESENTATION"
        elif compatibility.status == "COMPLETED" and compatibility_artifact_id is not None:
            readiness = (
                "READY_WITH_FALLBACK"
                if narrative.status == "COMPLETED_WITH_ERRORS"
                else "READY"
            )
            selected_artifact = compatibility_artifact_id
            selected_kind = "DETERMINISTIC_COMPATIBILITY"
        elif any(
            stage.status in {"RUNNING", "QUEUED"}
            for stage in (extraction, adjudication, document_decision, compatibility, narrative)
        ):
            readiness = "PENDING"
            selected_artifact = None
            selected_kind = None
        else:
            readiness = "ACTION_REQUIRED"
            selected_artifact = None
            selected_kind = None
        retryable_stages: list[str] = []
        if extraction.retryable or (
            extraction.workflow_run_id is not None
            and extraction.status in {"FAILED", "COMPLETED_WITH_ERRORS"}
        ):
            retryable_stages.append("EXTRACTION")
        if adjudication.retryable or (
            adjudication.workflow_run_id is not None
            and adjudication.status in {"FAILED", "COMPLETED_WITH_ERRORS"}
        ):
            retryable_stages.append("ADJUDICATION")
        if narrative.retryable or (
            narrative.workflow_run_id is not None
            and narrative.status == "COMPLETED_WITH_ERRORS"
        ):
            retryable_stages.append("NARRATIVE")
        for stage in (extraction, adjudication, document_decision, compatibility, narrative):
            source_artifact_ids.extend(stage.artifact_ids.values())
        documents.append(
            EvidenceDocumentOutcome(
                matter_document_id=document_id,
                extraction=extraction,
                adjudication=adjudication,
                document_decision=document_decision,
                compatibility_projection=compatibility,
                narrative=narrative,
                synthesis_readiness=readiness,
                selected_output_artifact_id=selected_artifact,
                selected_output_kind=selected_kind,
                retryable_stages=tuple(retryable_stages),
            )
        )

    workflows = workflow_by_id
    readiness_counts = Counter(value.synthesis_readiness for value in documents)
    stage_status_counts = {
        name: dict(sorted(Counter(getattr(value, name).status for value in documents).items()))
        for name in (
            "extraction",
            "adjudication",
            "document_decision",
            "compatibility_projection",
            "narrative",
        )
    }
    usage = EvidenceOutcomeUsage(
        workflow_count=len(workflows),
        request_count=sum(int(getattr(value, "request_count", 0) or 0) for value in workflows.values()),
        input_tokens=sum(int(getattr(value, "input_tokens", 0) or 0) for value in workflows.values()),
        cached_input_tokens=sum(
            int(getattr(value, "cached_input_tokens", 0) or 0) for value in workflows.values()
        ),
        cache_write_tokens=sum(
            int(getattr(value, "cache_write_tokens", 0) or 0) for value in workflows.values()
        ),
        output_tokens=sum(int(getattr(value, "output_tokens", 0) or 0) for value in workflows.values()),
    )
    payload = {
        "version": OUTCOME_MANIFEST_VERSION,
        "tenant_id": tenant_id,
        "client_id": client_id,
        "matter_id": matter_id,
        "assessment_id": assessment_id,
        "population_hash": content_hash([str(value) for value in ordered_ids]),
        "document_count": len(documents),
        "documents": [value.model_dump(mode="json") for value in documents],
        "readiness_counts": dict(sorted(readiness_counts.items())),
        "stage_status_counts": stage_status_counts,
        "usage": usage.model_dump(mode="json"),
        "source_workflow_ids": tuple(sorted(workflows)),
        "source_artifact_ids": tuple(sorted(set(source_artifact_ids))),
    }
    return EvidenceAnalysisOutcomeManifest(**payload, result_hash=content_hash(payload))


def outcome_manifest_bytes(result: EvidenceAnalysisOutcomeManifest) -> bytes:
    return json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def load_outcome_manifest_bytes(content: bytes) -> EvidenceAnalysisOutcomeManifest:
    return EvidenceAnalysisOutcomeManifest.model_validate_json(content)


def outcome_manifest_derivation_key(result: EvidenceAnalysisOutcomeManifest) -> str:
    return content_hash(
        {
            "artifact_type": "ANALYSIS_OUTCOME_MANIFEST",
            "assessment_id": str(result.assessment_id),
            "population_hash": result.population_hash,
            "source_workflow_ids": [str(value) for value in result.source_workflow_ids],
            "result_hash": result.result_hash,
        }
    )


def store_outcome_manifest_artifact(
    result: EvidenceAnalysisOutcomeManifest,
    *,
    producer_run_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowArtifactReference, bool]:
    content = outcome_manifest_bytes(result)
    return store_workflow_artifact(
        content=BytesIO(content),
        artifact_type=ArtifactType.ANALYSIS_OUTCOME_MANIFEST,
        core_scope_kind="MATTER",
        core_scope_id=result.matter_id,
        schema_version=ANALYSIS_OUTCOME_MANIFEST_SCHEMA_VERSION,
        derivation_key=outcome_manifest_derivation_key(result),
        content_hash=sha256(content).hexdigest(),
        byte_length=len(content),
        media_type="application/json",
        producer_kind="CORE",
        producer_run_id=producer_run_id,
        artifact_metadata={
            "assessment_id": str(result.assessment_id),
            "result_hash": result.result_hash,
            "document_count": result.document_count,
            "readiness_counts": result.readiness_counts,
            "authoritative": False,
        },
        lineage=[
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=artifact_id,
            )
            for artifact_id in result.source_artifact_ids
        ],
        actor_user_id=actor_user_id,
        tenant_id=result.tenant_id,
        client_id=result.client_id,
        filename=f"{result.assessment_id}-evidence-outcomes.json",
    )
