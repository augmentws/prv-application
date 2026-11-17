"""Controlled launcher for optional, non-authoritative evidence narrative."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_models import resolve_agent_model
from app.artifact_gateway import (
    WorkflowArtifactReference,
    copy_workflow_artifact_content,
    get_workflow_artifact_reference,
)
from app.audit import record_audit
from app.config import Settings, get_settings
from app.evidence_adjudication_artifacts import (
    CriterionDecisionsResult,
    load_criterion_decisions_bytes,
)
from app.evidence_compatibility_projection import (
    EvidenceCompatibilityProjection,
    load_compatibility_projection_bytes,
)
from app.evidence_document_decision import DocumentDecisionResult, load_document_decision_bytes
from app.evidence_first_artifacts import (
    CompactedEvidenceObservations,
    load_compacted_observations_bytes,
)
from app.evidence_first_launcher import _require_shadow_operator
from app.evidence_first_managed import require_evidence_shadow_enabled
from app.evidence_narrative_managed import execute_managed_evidence_narrative
from app.evidence_narrative_projection import (
    EvidenceNarrativeResult,
    build_evidence_narrative_result,
    selected_narrative_observation_ids,
    store_narrative_result_artifact,
)
from app.execution_accounting import refresh_workflow_execution_usage
from app.model_execution import content_hash
from app.models import Matter, MatterDefinitionAssessmentRun, SkillDefinitionVersion, User, WorkflowRun, WorkflowStepRun
from app.provider_usage import external_model_identity
from app.workflow_specs import (
    MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW_SPEC,
    binding_snapshot,
    resolve_workflow_skill_bindings,
)
from artifact_service.workflow_artifact_types import ArtifactType

NARRATIVE_SHADOW_LAUNCHER_VERSION = "matter-definition-evidence-narrative-shadow-launcher-v1"


@dataclass(frozen=True)
class PreparedNarrativeShadowLaunch:
    actor_user_id: uuid.UUID
    assessment: MatterDefinitionAssessmentRun
    matter: Matter
    compatibility_artifact: WorkflowArtifactReference
    compatibility_result: EvidenceCompatibilityProjection
    observations_artifact: WorkflowArtifactReference
    observations: CompactedEvidenceObservations
    criterion_decisions: CriterionDecisionsResult
    document_decision: DocumentDecisionResult
    skill_version: SkillDefinitionVersion
    resolved_model: str
    binding_snapshot: dict[str, dict]
    selected_observation_ids: tuple[str, ...]
    launch_key: str


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


def _artifact(
    artifact_id: uuid.UUID,
    expected: ArtifactType,
    *,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> WorkflowArtifactReference:
    reference = get_workflow_artifact_reference(
        artifact_id=artifact_id,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    if reference.artifact_type != expected:
        raise ValueError(f"Selected artifact is not {expected.value}")
    return reference


def prepare_narrative_shadow_launch(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    assessment_id: uuid.UUID,
    compatibility_artifact_id: uuid.UUID,
    narrative_model: str | None = None,
    settings: Settings | None = None,
) -> PreparedNarrativeShadowLaunch:
    active_settings = settings or get_settings()
    assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
    if assessment is None:
        raise ValueError("Matter Definition assessment not found")
    matter = db.get(Matter, assessment.matter_id)
    if matter is None:
        raise ValueError("Assessment matter is unavailable")
    actor = _require_shadow_operator(db.get(User, actor_user_id), tenant_id=matter.client.tenant_id)
    compatibility_artifact = _artifact(
        compatibility_artifact_id,
        ArtifactType.ANALYSIS_COMPATIBILITY_RESULT,
        actor_user_id=actor.id,
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
    )
    compatibility_result = load_compatibility_projection_bytes(
        _copy_verified_artifact(compatibility_artifact, actor_user_id=actor.id)
    )
    if (
        compatibility_result.matter_id != matter.id
        or compatibility_result.tenant_id != matter.client.tenant_id
        or compatibility_result.client_id != matter.client_id
        or compatibility_result.status != "COMPLETED"
    ):
        raise ValueError("Compatibility result is not a completed result for the selected assessment")

    observations_artifact = _artifact(
        compatibility_result.observations_artifact_id,
        ArtifactType.ANALYSIS_OBSERVATIONS,
        actor_user_id=actor.id,
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
    )
    observations = load_compacted_observations_bytes(
        _copy_verified_artifact(observations_artifact, actor_user_id=actor.id)
    )
    criterion_artifact = _artifact(
        compatibility_result.criterion_decisions_artifact_id,
        ArtifactType.ANALYSIS_CRITERION_DECISIONS,
        actor_user_id=actor.id,
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
    )
    criterion_decisions = load_criterion_decisions_bytes(
        _copy_verified_artifact(criterion_artifact, actor_user_id=actor.id)
    )
    document_artifact = _artifact(
        compatibility_result.document_decision_artifact_id,
        ArtifactType.ANALYSIS_DOCUMENT_DECISION,
        actor_user_id=actor.id,
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
    )
    document_decision = load_document_decision_bytes(
        _copy_verified_artifact(document_artifact, actor_user_id=actor.id)
    )
    if compatibility_result.observations_result_hash != observations.result_hash:
        raise ValueError("Compatibility result does not match its observations artifact")
    if compatibility_result.criterion_decisions_result_hash != criterion_decisions.result_hash:
        raise ValueError("Compatibility result does not match its criterion-decisions artifact")
    if compatibility_result.document_decision_result_hash != document_decision.result_hash:
        raise ValueError("Compatibility result does not match its document-decision artifact")

    resolved = resolve_workflow_skill_bindings(
        db,
        workflow_key=MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW_SPEC.key,
        tenant_id=matter.client.tenant_id,
    )
    _binding, _definition, skill_version = resolved["narrative"]
    resolved_model = (
        narrative_model.strip()
        if narrative_model and narrative_model.strip()
        else resolve_agent_model(skill_version.model_key, active_settings)
    )
    if external_model_identity(resolved_model) is None:
        raise ValueError("Narrative shadow requires an explicitly resolved provider:model identity")
    selected_ids = selected_narrative_observation_ids(criterion_decisions)
    launch_key = content_hash(
        {
            "launcher_version": NARRATIVE_SHADOW_LAUNCHER_VERSION,
            "actor_user_id": str(actor.id),
            "assessment_id": str(assessment.id),
            "compatibility_result_hash": compatibility_result.result_hash,
            "selected_observation_ids": selected_ids,
            "skill_definition_version_id": str(skill_version.id),
            "resolved_model": resolved_model,
        }
    )
    return PreparedNarrativeShadowLaunch(
        actor_user_id=actor.id,
        assessment=assessment,
        matter=matter,
        compatibility_artifact=compatibility_artifact,
        compatibility_result=compatibility_result,
        observations_artifact=observations_artifact,
        observations=observations,
        criterion_decisions=criterion_decisions,
        document_decision=document_decision,
        skill_version=skill_version,
        resolved_model=resolved_model,
        binding_snapshot=binding_snapshot(resolved),
        selected_observation_ids=selected_ids,
        launch_key=launch_key,
    )


def narrative_plan_report(prepared: PreparedNarrativeShadowLaunch) -> dict[str, Any]:
    return {
        "result_version": NARRATIVE_SHADOW_LAUNCHER_VERSION,
        "mode": "DRY_RUN",
        "authoritative": False,
        "actor_user_id": str(prepared.actor_user_id),
        "assessment_id": str(prepared.assessment.id),
        "matter_id": str(prepared.matter.id),
        "matter_document_id": str(prepared.compatibility_result.matter_document_id),
        "compatibility_result_artifact_id": str(prepared.compatibility_artifact.artifact_id),
        "fallback_compatibility_result_artifact_id": str(
            prepared.compatibility_artifact.artifact_id
        ),
        "resolved_model": prepared.resolved_model,
        "selected_observation_count": len(prepared.selected_observation_ids),
        "model_request_count": 1 if prepared.selected_observation_ids else 0,
    }


def _workflow(
    db: Session,
    prepared: PreparedNarrativeShadowLaunch,
    *,
    workflow_run_id: uuid.UUID | None = None,
) -> tuple[WorkflowRun, WorkflowStepRun]:
    dbos_workflow_id = f"definition-evidence-narrative-shadow:{prepared.launch_key}"
    workflow = (
        db.get(WorkflowRun, workflow_run_id)
        if workflow_run_id is not None
        else db.scalar(select(WorkflowRun).where(WorkflowRun.dbos_workflow_id == dbos_workflow_id))
    )
    if workflow_run_id is not None and workflow is None:
        raise ValueError("Selected narrative workflow is unavailable")
    if workflow is not None and (
        workflow.job_type != "MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW"
        or workflow.matter_id != prepared.matter.id
        or str(workflow.input_snapshot.get("assessment_id")) != str(prepared.assessment.id)
        or str(workflow.input_snapshot.get("matter_document_id"))
        != str(prepared.compatibility_result.matter_document_id)
    ):
        raise ValueError("Selected workflow is not the prepared narrative run")
    if workflow is None:
        workflow = WorkflowRun(
            tenant_id=prepared.matter.client.tenant_id,
            client_id=prepared.matter.client_id,
            matter_id=prepared.matter.id,
            job_type="MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW",
            job_id=uuid.uuid5(uuid.NAMESPACE_URL, dbos_workflow_id),
            attempt_number=1,
            workflow_key=MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW_SPEC.key,
            code_version=MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW_SPEC.code_version,
            dbos_workflow_id=dbos_workflow_id,
            idempotency_key=prepared.launch_key,
            status="QUEUED",
            current_stage="NARRATIVE",
            progress_total=1,
            progress_unit="STAGES",
            input_snapshot={
                "assessment_id": str(prepared.assessment.id),
                "matter_document_id": str(prepared.compatibility_result.matter_document_id),
                "compatibility_result_artifact_id": str(
                    prepared.compatibility_artifact.artifact_id
                ),
                "compatibility_result_hash": prepared.compatibility_result.result_hash,
            },
            binding_snapshot=prepared.binding_snapshot,
            configuration_snapshot={
                "launcher_version": NARRATIVE_SHADOW_LAUNCHER_VERSION,
                "resolved_model": prepared.resolved_model,
                "authoritative": False,
            },
            progress={},
            initiated_by_user_id=prepared.actor_user_id,
        )
        db.add(workflow)
        db.flush()
        step = WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key="narrative",
            ordinal=1,
            status="QUEUED",
            total_count=1,
        )
        db.add(step)
        db.flush()
        return workflow, step
    if workflow.input_snapshot.get("compatibility_result_hash") != prepared.compatibility_result.result_hash:
        raise ValueError("Existing narrative workflow does not match the prepared result")
    step = db.scalar(
        select(WorkflowStepRun).where(
            WorkflowStepRun.workflow_run_id == workflow.id,
            WorkflowStepRun.ordinal == 1,
        )
    )
    if step is None or step.role_key != "narrative":
        raise ValueError("Existing narrative workflow is missing its narrative step")
    return workflow, step


def _result_report(
    prepared: PreparedNarrativeShadowLaunch,
    *,
    workflow: WorkflowRun,
    status: str,
    narrative_status: str,
    narrative_artifact: WorkflowArtifactReference | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "result_version": NARRATIVE_SHADOW_LAUNCHER_VERSION,
        "mode": "EXECUTE",
        "authoritative": False,
        "actor_user_id": str(prepared.actor_user_id),
        "assessment_id": str(prepared.assessment.id),
        "matter_id": str(prepared.matter.id),
        "matter_document_id": str(prepared.compatibility_result.matter_document_id),
        "workflow_run_id": str(workflow.id),
        "status": status,
        "narrative_status": narrative_status,
        "narrative_artifact_id": (
            str(narrative_artifact.artifact_id) if narrative_artifact else None
        ),
        "fallback_compatibility_result_artifact_id": str(
            prepared.compatibility_artifact.artifact_id
        ),
        "selected_observation_count": len(prepared.selected_observation_ids),
        "resolved_model": prepared.resolved_model,
        "error": error,
    }


async def execute_prepared_narrative_shadow(
    db: Session,
    prepared: PreparedNarrativeShadowLaunch,
    *,
    retry_failed: bool = False,
    workflow_run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    require_evidence_shadow_enabled()
    workflow, step = (
        _workflow(db, prepared)
        if workflow_run_id is None
        else _workflow(db, prepared, workflow_run_id=workflow_run_id)
    )
    existing_artifact_id = workflow.progress.get("narrative_artifact_id")
    if workflow.status == "COMPLETED" and existing_artifact_id and not retry_failed:
        reference = _artifact(
            uuid.UUID(str(existing_artifact_id)),
            ArtifactType.ANALYSIS_NARRATIVE_PROJECTION,
            actor_user_id=prepared.actor_user_id,
            tenant_id=prepared.matter.client.tenant_id,
            client_id=prepared.matter.client_id,
        )
        return _result_report(
            prepared,
            workflow=workflow,
            status="COMPLETED",
            narrative_status="COMPLETED",
            narrative_artifact=reference,
        )
    if workflow.status == "COMPLETED_WITH_ERRORS" and not retry_failed:
        return _result_report(
            prepared,
            workflow=workflow,
            status="COMPLETED_WITH_ERRORS",
            narrative_status="FAILED",
            error=workflow.failure_summary,
        )

    workflow.status = "RUNNING"
    workflow.current_stage = "NARRATIVE"
    workflow.started_at = workflow.started_at or datetime.now(timezone.utc)
    workflow.completed_at = None
    workflow.failure_code = None
    workflow.failure_summary = None
    workflow.failure_retriable = False
    workflow.error_message = None
    step.status = "RUNNING"
    step.started_at = datetime.now(timezone.utc)
    step.completed_at = None
    step.completed_count = 0
    step.failed_count = 0
    step.error_message = None
    db.commit()
    try:
        projection, skill_run = await execute_managed_evidence_narrative(
            db=db,
            workflow=workflow,
            step=step,
            skill_version=prepared.skill_version,
            observations=prepared.observations,
            criterion_decisions=prepared.criterion_decisions,
            document_decision=prepared.document_decision,
            resolved_model=prepared.resolved_model,
        )
        result: EvidenceNarrativeResult = build_evidence_narrative_result(
            compatibility_result=prepared.compatibility_result,
            compatibility_artifact=prepared.compatibility_artifact,
            observations=prepared.observations,
            observations_artifact=prepared.observations_artifact,
            criterion_decisions=prepared.criterion_decisions,
            projection=projection,
        )
        artifact, _created = store_narrative_result_artifact(
            result,
            producer_run_id=workflow.id,
            actor_user_id=prepared.actor_user_id,
        )
    except Exception as exc:  # noqa: BLE001 - optional stage must preserve deterministic fallback
        completed_at = datetime.now(timezone.utc)
        message = f"{type(exc).__name__}: optional narrative did not complete"
        step.status = "COMPLETED_WITH_ERRORS"
        step.failed_count = 1
        step.error_message = message
        step.completed_at = completed_at
        workflow.status = "COMPLETED_WITH_ERRORS"
        workflow.current_stage = "COMPLETED"
        workflow.progress_completed = 1
        workflow.progress = {
            "narrative_status": "FAILED",
            "fallback_compatibility_result_artifact_id": str(
                prepared.compatibility_artifact.artifact_id
            ),
        }
        workflow.failure_code = "OPTIONAL_NARRATIVE_FAILED"
        workflow.failure_summary = message
        workflow.failure_retriable = True
        workflow.error_message = message
        workflow.completed_at = completed_at
        refresh_workflow_execution_usage(db, workflow.id)
        db.commit()
        return _result_report(
            prepared,
            workflow=workflow,
            status="COMPLETED_WITH_ERRORS",
            narrative_status="FAILED",
            error=message,
        )

    completed_at = datetime.now(timezone.utc)
    step.status = "COMPLETED"
    step.completed_count = 1
    step.completed_at = completed_at
    workflow.status = "COMPLETED"
    workflow.current_stage = "COMPLETED"
    workflow.progress_completed = 1
    workflow.progress = {
        "narrative_status": "COMPLETED",
        "narrative_artifact_id": str(artifact.artifact_id),
        "selected_observation_count": len(prepared.selected_observation_ids),
        "model_request_count": 1 if skill_run is not None else 0,
        "fallback_compatibility_result_artifact_id": str(
            prepared.compatibility_artifact.artifact_id
        ),
    }
    workflow.completed_at = completed_at
    refresh_workflow_execution_usage(db, workflow.id)
    record_audit(
        db,
        tenant_id=workflow.tenant_id,
        actor_user_id=prepared.actor_user_id,
        action="matter_definition.evidence_narrative_shadow.completed",
        target_type="workflow_run",
        target_id=workflow.id,
        details={
            "assessment_id": str(prepared.assessment.id),
            "matter_document_id": str(prepared.compatibility_result.matter_document_id),
            "narrative_artifact_id": str(artifact.artifact_id),
            "selected_observation_count": len(prepared.selected_observation_ids),
            "authoritative": False,
        },
    )
    db.commit()
    return _result_report(
        prepared,
        workflow=workflow,
        status="COMPLETED",
        narrative_status="COMPLETED",
        narrative_artifact=artifact,
    )
