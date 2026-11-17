"""Lineage-checked evidence-first inputs for Matter Definition synthesis."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifact_gateway import (
    WorkflowArtifactReference,
    copy_workflow_artifact_content,
    get_workflow_artifact_reference,
)
from app.evidence_compatibility_projection import load_compatibility_projection_bytes
from app.evidence_narrative_projection import load_narrative_result_bytes
from app.evidence_outcome_launcher import prepare_evidence_outcome_manifest
from app.evidence_routing import (
    EVIDENCE_FIRST_SYNTHESIS_SOURCE,
    SYNTHESIS_DOCUMENT_SOURCE_KEY,
)
from app.matter_definition_assessments import AssessmentError
from app.models import Matter, MatterDefinitionAssessmentRun, ReviewBatchRunDocument
from artifact_service.workflow_artifact_types import ArtifactType


@dataclass(frozen=True)
class EvidenceSynthesisInputs:
    analyses: list[dict[str, Any]]
    partial_count: int
    invalid_count: int
    diagnostics: dict[str, Any]


def assessment_uses_evidence_first_synthesis(
    assessment: MatterDefinitionAssessmentRun,
) -> bool:
    """Return true only for an assessment explicitly frozen onto evidence-first inputs."""

    return (
        assessment.configuration_snapshot.get(SYNTHESIS_DOCUMENT_SOURCE_KEY)
        == EVIDENCE_FIRST_SYNTHESIS_SOURCE
    )


def _copy_verified_artifact(
    reference: WorkflowArtifactReference,
    *,
    actor_user_id: uuid.UUID,
) -> bytes:
    destination = BytesIO()
    copy_workflow_artifact_content(
        artifact_id=reference.artifact_id,
        destination=destination,
        actor_user_id=actor_user_id,
        tenant_id=reference.tenant_id,
        client_id=reference.client_id,
    )
    content = destination.getvalue()
    if sha256(content).hexdigest() != reference.content_hash:
        raise ValueError("Workflow artifact content hash does not match its reference")
    return content


def _selected_output(
    *,
    reference: WorkflowArtifactReference,
    selected_kind: str,
    actor_user_id: uuid.UUID,
    assessment: MatterDefinitionAssessmentRun,
    matter: Matter,
    document_id: uuid.UUID,
) -> dict[str, Any]:
    if reference.core_scope_kind != "MATTER" or reference.core_scope_id != matter.id:
        raise ValueError("Selected analysis artifact is outside the assessment matter")
    metadata_document_id = reference.metadata.get("matter_document_id")
    if str(metadata_document_id) != str(document_id):
        raise ValueError("Selected analysis artifact belongs to a different document")
    content = _copy_verified_artifact(reference, actor_user_id=actor_user_id)
    if selected_kind == "NARRATIVE_PRESENTATION":
        if reference.artifact_type != ArtifactType.ANALYSIS_NARRATIVE_PROJECTION:
            raise ValueError("Narrative selection does not reference a narrative artifact")
        result = load_narrative_result_bytes(content)
        output = result.presented_output
    elif selected_kind == "DETERMINISTIC_COMPATIBILITY":
        if reference.artifact_type != ArtifactType.ANALYSIS_COMPATIBILITY_RESULT:
            raise ValueError("Compatibility selection does not reference a compatibility artifact")
        result = load_compatibility_projection_bytes(content)
        if result.status != "COMPLETED" or result.output is None:
            raise ValueError("Selected compatibility artifact is not complete")
        output = result.output
    else:
        raise ValueError("Outcome manifest selected an unsupported synthesis artifact")
    if (
        result.tenant_id != matter.client.tenant_id
        or result.client_id != matter.client_id
        or result.matter_id != matter.id
        or result.matter_document_id != document_id
    ):
        raise ValueError("Selected analysis artifact lineage does not match the assessment")
    # The outcome manifest restricts source artifacts to evidence workflows whose
    # frozen input names this assessment. Do not accept arbitrary same-matter data.
    if assessment.matter_id != result.matter_id:
        raise ValueError("Selected analysis artifact does not belong to the assessment matter")
    return output


def load_evidence_first_synthesis_inputs(
    db: Session,
    *,
    assessment: MatterDefinitionAssessmentRun,
    matter: Matter,
) -> EvidenceSynthesisInputs:
    """Load the manifest-selected output for every settled, usable document."""

    orchestration = assessment.configuration_snapshot.get("evidence_first_orchestration")
    frozen_actor_id = (
        orchestration.get("actor_user_id") if isinstance(orchestration, dict) else None
    )
    actor_user_id = (
        uuid.UUID(str(frozen_actor_id))
        if frozen_actor_id is not None
        else assessment.initiated_by_user_id
    )
    prepared = prepare_evidence_outcome_manifest(
        db,
        actor_user_id=actor_user_id,
        assessment_id=assessment.id,
        require_operator_role=frozen_actor_id is not None,
    )
    manifest = prepared.manifest
    pending_count = manifest.readiness_counts.get("PENDING", 0)
    terminal_unavailable_ids: set[uuid.UUID] = set()
    if assessment.review_batch_run_id is not None:
        terminal_unavailable_ids = set(
            db.scalars(
                select(ReviewBatchRunDocument.matter_document_id).where(
                    ReviewBatchRunDocument.review_batch_run_id
                    == assessment.review_batch_run_id,
                    ReviewBatchRunDocument.status.in_(("FAILED", "SKIPPED")),
                )
            )
        )
    not_started_count = sum(
        outcome.extraction.status == "NOT_STARTED"
        and outcome.matter_document_id not in terminal_unavailable_ids
        for outcome in manifest.documents
    )
    unsettled_count = pending_count + not_started_count
    if unsettled_count:
        raise AssessmentError(
            "Evidence-first document analysis is not ready for "
            f"{unsettled_count} document(s)"
        )

    analyses: list[dict[str, Any]] = []
    partial_count = 0
    artifact_error_count = 0
    narrative_count = 0
    deterministic_count = 0
    reconciled_counts = (
        orchestration.get("document_outcome_counts", {})
        if isinstance(orchestration, dict)
        else {}
    )
    reconciled_fallback_count = (
        int(reconciled_counts.get("fallback", 0) or 0)
        if isinstance(reconciled_counts, dict)
        else 0
    )
    fallback_count = max(
        manifest.readiness_counts.get("READY_WITH_FALLBACK", 0),
        reconciled_fallback_count,
    )
    action_required_ids = {
        outcome.matter_document_id
        for outcome in manifest.documents
        if outcome.synthesis_readiness == "ACTION_REQUIRED"
    } | terminal_unavailable_ids
    action_required_count = len(action_required_ids)
    for outcome in manifest.documents:
        if outcome.matter_document_id in terminal_unavailable_ids:
            continue
        if outcome.synthesis_readiness not in {"READY", "READY_WITH_FALLBACK"}:
            continue
        artifact_id = outcome.selected_output_artifact_id
        selected_kind = outcome.selected_output_kind
        if artifact_id is None or selected_kind is None:
            artifact_error_count += 1
            continue
        try:
            reference = get_workflow_artifact_reference(
                artifact_id=artifact_id,
                actor_user_id=actor_user_id,
                tenant_id=matter.client.tenant_id,
                client_id=matter.client_id,
            )
            output = _selected_output(
                reference=reference,
                selected_kind=selected_kind,
                actor_user_id=actor_user_id,
                assessment=assessment,
                matter=matter,
                document_id=outcome.matter_document_id,
            )
            if (output.get("coverage") or {}).get("status") == "PARTIAL":
                partial_count += 1
            if selected_kind == "NARRATIVE_PRESENTATION":
                narrative_count += 1
            else:
                deterministic_count += 1
            analyses.append(
                {
                    "matter_document_id": str(outcome.matter_document_id),
                    "output_artifact_id": str(artifact_id),
                    "analysis": output,
                }
            )
        except (PermissionError, TypeError, ValueError):
            artifact_error_count += 1

    invalid_count = action_required_count + artifact_error_count
    diagnostics = {
        "source": EVIDENCE_FIRST_SYNTHESIS_SOURCE,
        "manifest_result_hash": manifest.result_hash,
        "population_document_count": manifest.document_count,
        "loaded_document_count": len(analyses),
        "narrative_document_count": narrative_count,
        "deterministic_document_count": deterministic_count,
        "fallback_document_count": fallback_count,
        "action_required_document_count": action_required_count,
        "artifact_error_document_count": artifact_error_count,
        "pending_document_count": pending_count,
        "source_workflow_count": len(manifest.source_workflow_ids),
    }
    return EvidenceSynthesisInputs(
        analyses=analyses,
        partial_count=partial_count,
        invalid_count=invalid_count,
        diagnostics=diagnostics,
    )
