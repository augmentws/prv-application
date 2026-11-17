"""Read-only assembly and optional publication of evidence-first outcome manifests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifact_gateway import list_workflow_artifact_references
from app.evidence_first_launcher import _require_shadow_operator
from app.evidence_outcome_manifest import (
    EvidenceAnalysisOutcomeManifest,
    build_evidence_outcome_manifest,
    store_outcome_manifest_artifact,
)
from app.models import (
    Matter,
    MatterDefinitionAssessmentCandidate,
    MatterDefinitionAssessmentRun,
    ReviewBatchRunDocument,
    User,
    WorkflowRun,
)
from artifact_service.workflow_artifact_types import ArtifactType

OUTCOME_LAUNCHER_VERSION = "evidence-analysis-outcome-launcher-v1"
EVIDENCE_WORKFLOW_JOB_TYPES = frozenset(
    {
        "MATTER_DEFINITION_EVIDENCE_SHADOW",
        "MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW",
        "MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW",
    }
)


@dataclass(frozen=True)
class PreparedEvidenceOutcomeManifest:
    actor_user_id: uuid.UUID
    assessment: MatterDefinitionAssessmentRun
    matter: Matter
    manifest: EvidenceAnalysisOutcomeManifest


def prepare_evidence_outcome_manifest(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    assessment_id: uuid.UUID,
    require_operator_role: bool = True,
) -> PreparedEvidenceOutcomeManifest:
    assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
    if assessment is None:
        raise ValueError("Matter Definition assessment not found")
    matter = db.get(Matter, assessment.matter_id)
    if matter is None:
        raise ValueError("Assessment matter is unavailable")
    user = db.get(User, actor_user_id)
    if user is None:
        raise ValueError("Outcome manifest actor not found")
    actor = (
        _require_shadow_operator(user, tenant_id=matter.client.tenant_id)
        if require_operator_role
        else user
    )
    document_ids = tuple(
        db.scalars(
            select(MatterDefinitionAssessmentCandidate.matter_document_id)
            .where(
                MatterDefinitionAssessmentCandidate.assessment_run_id == assessment.id,
                MatterDefinitionAssessmentCandidate.selected.is_(True),
            )
            .order_by(
                MatterDefinitionAssessmentCandidate.selection_order,
                MatterDefinitionAssessmentCandidate.matter_document_id,
            )
        )
    )
    workflows = list(
        db.scalars(
            select(WorkflowRun)
            .where(
                WorkflowRun.matter_id == matter.id,
                WorkflowRun.job_type.in_(EVIDENCE_WORKFLOW_JOB_TYPES),
            )
            .order_by(WorkflowRun.created_at, WorkflowRun.id)
        )
    )
    document_failures = {}
    pending_document_ids: set[uuid.UUID] = set()
    if assessment.review_batch_run_id is not None:
        run_documents = list(
            db.scalars(
                select(ReviewBatchRunDocument).where(
                    ReviewBatchRunDocument.review_batch_run_id
                    == assessment.review_batch_run_id
                )
            )
        )
        document_failures = {
            value.matter_document_id: value
            for value in run_documents
            if value.status == "FAILED"
        }
        pending_document_ids = {
            value.matter_document_id
            for value in run_documents
            if value.status in {"QUEUED", "IN_PROGRESS"}
        }
    workflows = [
        value
        for value in workflows
        if str((value.input_snapshot or {}).get("assessment_id")) == str(assessment.id)
        and not (value.configuration_snapshot or {}).get(
            "superseded_by_evidence_retry"
        )
    ]
    by_type: dict[str, list[WorkflowRun]] = {
        key: [] for key in EVIDENCE_WORKFLOW_JOB_TYPES
    }
    for workflow in workflows:
        by_type[workflow.job_type].append(workflow)
    producer_run_ids = {value.id for value in workflows}
    artifact_references = (
        [
            reference
            for artifact_type in (
                ArtifactType.ANALYSIS_OBSERVATIONS,
                ArtifactType.ANALYSIS_DOCUMENT_DECISION,
                ArtifactType.ANALYSIS_COMPATIBILITY_RESULT,
                ArtifactType.ANALYSIS_NARRATIVE_PROJECTION,
            )
            for reference in list_workflow_artifact_references(
                actor_user_id=actor.id,
                tenant_id=matter.client.tenant_id,
                client_id=matter.client_id,
                core_scope_kind="MATTER",
                core_scope_id=matter.id,
                artifact_type=artifact_type,
                limit=500,
            )
            if reference.producer_run_id in producer_run_ids
        ]
        if producer_run_ids
        else []
    )
    manifest = build_evidence_outcome_manifest(
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        matter_id=matter.id,
        assessment_id=assessment.id,
        document_ids=document_ids,
        extraction_workflows=by_type["MATTER_DEFINITION_EVIDENCE_SHADOW"],
        adjudication_workflows=by_type[
            "MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW"
        ],
        narrative_workflows=by_type["MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW"],
        artifact_references=artifact_references,
        document_failures=document_failures,
        pending_document_ids=pending_document_ids,
    )
    return PreparedEvidenceOutcomeManifest(
        actor_user_id=actor.id,
        assessment=assessment,
        matter=matter,
        manifest=manifest,
    )


def outcome_manifest_report(
    prepared: PreparedEvidenceOutcomeManifest,
    *,
    mode: str = "DRY_RUN",
    artifact_id: uuid.UUID | None = None,
    created: bool | None = None,
) -> dict[str, Any]:
    return {
        "result_version": OUTCOME_LAUNCHER_VERSION,
        "mode": mode,
        "authoritative": False,
        "assessment_id": str(prepared.assessment.id),
        "matter_id": str(prepared.matter.id),
        "document_count": prepared.manifest.document_count,
        "readiness_counts": prepared.manifest.readiness_counts,
        "stage_status_counts": prepared.manifest.stage_status_counts,
        "usage": prepared.manifest.usage.model_dump(mode="json"),
        "source_workflow_count": len(prepared.manifest.source_workflow_ids),
        "source_artifact_count": len(prepared.manifest.source_artifact_ids),
        "manifest_result_hash": prepared.manifest.result_hash,
        "artifact_id": str(artifact_id) if artifact_id else None,
        "artifact_created": created,
    }


def publish_prepared_evidence_outcome_manifest(
    prepared: PreparedEvidenceOutcomeManifest,
) -> dict[str, Any]:
    reference, created = store_outcome_manifest_artifact(
        prepared.manifest,
        producer_run_id=prepared.assessment.workflow_run_id,
        actor_user_id=prepared.actor_user_id,
    )
    return outcome_manifest_report(
        prepared,
        mode="PUBLISH",
        artifact_id=reference.artifact_id,
        created=created,
    )
