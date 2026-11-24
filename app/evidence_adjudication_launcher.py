"""Controlled launcher for non-authoritative criterion adjudication."""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter, defaultdict
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
    find_workflow_artifact_reference,
    get_workflow_artifact_reference,
)
from app.audit import record_audit
from app.config import Settings, get_settings
from app.evidence_adjudication_artifacts import (
    MAX_PACKET_OBSERVATIONS,
    CriterionAdjudicationPlan,
    build_adjudication_plan,
)
from app.evidence_adjudication_managed import execute_managed_adjudication_shadow
from app.evidence_compatibility_projection import (
    build_evidence_compatibility_projection,
    store_compatibility_projection_artifact,
)
from app.evidence_document_decision import (
    build_document_decision_result,
    store_document_decision_artifact,
)
from app.evidence_first_artifacts import (
    AnalysisRoleBinding,
    EvidenceEvaluationPlan,
    load_compacted_observations_bytes,
    load_evaluation_plan_bytes,
    scope_family_observation_results,
)
from app.evidence_first_contracts import (
    UNMAPPED_CRITERION_KEY,
    CriterionAdjudicationOutput,
    criterion_adjudication_schemas,
    criterion_catalog_from_retrieval_plan,
)
from app.evidence_first_launcher import _require_shadow_operator
from app.evidence_first_managed import require_evidence_shadow_enabled
from app.execution_accounting import refresh_workflow_execution_usage
from app.model_execution import content_hash, provider_compatible_model_settings
from app.models import (
    Matter,
    MatterDefinitionAssessmentRun,
    MatterDefinitionRevision,
    SkillDefinitionVersion,
    User,
    WorkflowRun,
    WorkflowStepRun,
)
from app.provider_schemas import provider_output_schema
from app.provider_usage import external_model_identity
from app.workflow_specs import (
    MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW_SPEC,
    binding_snapshot,
    resolve_workflow_skill_bindings,
)
from artifact_service.workflow_artifact_types import ArtifactType

ADJUDICATION_SHADOW_LAUNCHER_VERSION = "matter-definition-adjudication-shadow-launcher-v3"
# Keep the launch identity stable so a completed v2 adjudication workflow can
# resume into the deterministic compatibility-projection step without replay.
ADJUDICATION_SHADOW_IDENTITY_VERSION = "matter-definition-adjudication-shadow-launcher-v2"


@dataclass(frozen=True)
class PreparedAdjudicationShadowLaunch:
    actor_user_id: uuid.UUID
    assessment: MatterDefinitionAssessmentRun
    matter: Matter
    revision: MatterDefinitionRevision
    catalog: Any
    observations_artifact: WorkflowArtifactReference
    observations: Any
    extraction_plan: EvidenceEvaluationPlan
    skill_version: SkillDefinitionVersion
    resolved_model: str
    binding_snapshot: dict[str, dict]
    plan: CriterionAdjudicationPlan
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


def _role_binding(skill_version: SkillDefinitionVersion, *, resolved_model: str) -> AnalysisRoleBinding:
    identity = external_model_identity(resolved_model)
    if identity is None:
        raise ValueError("Adjudication shadow requires an explicitly resolved provider:model identity")
    provider, _provider_model = identity
    application_schema = CriterionAdjudicationOutput.model_json_schema()
    admitted_schema = provider_output_schema(application_schema, provider=provider)
    return AnalysisRoleBinding(
        role_key="criterion_adjudication",
        skill_definition_version_id=skill_version.id,
        provider=provider,
        model_key=resolved_model,
        prompt_hash=content_hash(skill_version.instructions),
        application_schema_hash=content_hash(application_schema),
        provider_schema_hash=content_hash(admitted_schema),
        model_settings_hash=content_hash(
            provider_compatible_model_settings(
                skill_version.model_policy,
                resolved_model=resolved_model,
            )
        ),
        capability_profile_hash=content_hash(
            {
                "provider": provider,
                "model": resolved_model,
                "required_capabilities": sorted(skill_version.required_capabilities),
            }
        ),
    )


def _packet_schema_hashes(catalog, observations, *, provider: str):
    by_criterion: dict[str, list[str]] = defaultdict(list)
    for observation in observations.observations:
        by_criterion[observation.criterion_key].append(observation.observation_id)
    values: dict[tuple[str, tuple[str, ...]], tuple[str, str]] = {}
    for criterion in catalog.criteria:
        if criterion.criterion_key == UNMAPPED_CRITERION_KEY:
            continue
        observation_ids = tuple(by_criterion.get(criterion.criterion_key, ()))
        shards = [
            observation_ids[index : index + MAX_PACKET_OBSERVATIONS]
            for index in range(0, len(observation_ids), MAX_PACKET_OBSERVATIONS)
        ] or [()]
        for shard in shards:
            application_schema, admitted_schema = criterion_adjudication_schemas(
                criterion_key=criterion.criterion_key,
                observation_ids=list(shard),
                provider=provider,
            )
            values[(criterion.criterion_key, shard)] = (
                content_hash(application_schema),
                content_hash(admitted_schema),
            )
    return values


def prepare_adjudication_shadow_launch(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    assessment_id: uuid.UUID,
    observations_artifact_id: uuid.UUID,
    target_matter_document_id: uuid.UUID | None = None,
    adjudication_model: str | None = None,
    settings: Settings | None = None,
) -> PreparedAdjudicationShadowLaunch:
    active_settings = settings or get_settings()
    assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
    if assessment is None:
        raise ValueError("Matter Definition assessment not found")
    matter = db.get(Matter, assessment.matter_id)
    revision = db.get(MatterDefinitionRevision, assessment.matter_definition_revision_id)
    if matter is None or revision is None:
        raise ValueError("Assessment matter or Review Guidance revision is unavailable")
    actor = _require_shadow_operator(db.get(User, actor_user_id), tenant_id=matter.client.tenant_id)
    retrieval_plan = assessment.configuration_snapshot.get("retrieval_plan")
    if not isinstance(retrieval_plan, dict):
        raise TypeError("Assessment does not contain a frozen retrieval plan")
    catalog = criterion_catalog_from_retrieval_plan(retrieval_plan)
    if hashlib.sha256(revision.content_markdown.encode()).hexdigest() != assessment.definition_content_hash:
        raise ValueError("Assessment Review Guidance hash no longer matches its frozen revision")

    observations_reference = get_workflow_artifact_reference(
        artifact_id=observations_artifact_id,
        actor_user_id=actor.id,
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
    )
    if observations_reference.artifact_type != ArtifactType.ANALYSIS_OBSERVATIONS:
        raise ValueError("Selected artifact is not a compacted evidence-observations artifact")
    observations = load_compacted_observations_bytes(
        _copy_verified_artifact(observations_reference, actor_user_id=actor.id)
    )
    extraction_plan_reference = find_workflow_artifact_reference(
        artifact_type=ArtifactType.ANALYSIS_EVALUATION_PLAN,
        derivation_key=observations.plan_hash,
        actor_user_id=actor.id,
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
    )
    if extraction_plan_reference is None:
        raise ValueError("Evidence observations do not have a matching evaluation-plan artifact")
    extraction_plan = load_evaluation_plan_bytes(
        _copy_verified_artifact(extraction_plan_reference, actor_user_id=actor.id)
    )
    if (
        extraction_plan.matter_id != matter.id
        or extraction_plan.guidance_revision_id != revision.id
        or extraction_plan.guidance_content_hash != assessment.definition_content_hash
        or extraction_plan.criterion_catalog_hash != catalog.catalog_hash
    ):
        raise ValueError("Evidence observations do not belong to the selected frozen assessment")
    matter_document_id = extraction_plan.matter_document_id
    if target_matter_document_id is not None:
        observations = scope_family_observation_results(
            extraction_plan,
            observations,
            target_document_id=target_matter_document_id,
        )
        matter_document_id = target_matter_document_id

    resolved = resolve_workflow_skill_bindings(
        db,
        workflow_key=MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW_SPEC.key,
        tenant_id=matter.client.tenant_id,
    )
    _binding, _definition, skill_version = resolved["criterion_adjudication"]
    resolved_model = (
        adjudication_model.strip()
        if adjudication_model and adjudication_model.strip()
        else resolve_agent_model(skill_version.model_key, active_settings)
    )
    role_binding = _role_binding(skill_version, resolved_model=resolved_model)
    plan = build_adjudication_plan(
        extraction_plan_hash=extraction_plan.plan_hash,
        observations_artifact=observations_reference,
        observations=observations,
        catalog=catalog,
        guidance_revision_id=revision.id,
        guidance_content_hash=assessment.definition_content_hash,
        role_binding=role_binding,
        packet_schema_hashes=_packet_schema_hashes(
            catalog,
            observations,
            provider=role_binding.provider,
        ),
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        matter_id=matter.id,
        matter_document_id=matter_document_id,
    )
    launch_key = content_hash(
        {
            "launcher_version": ADJUDICATION_SHADOW_IDENTITY_VERSION,
            "actor_user_id": str(actor.id),
            "assessment_id": str(assessment.id),
            "observations_artifact_id": str(observations_reference.artifact_id),
            "plan_hash": plan.plan_hash,
        }
    )
    return PreparedAdjudicationShadowLaunch(
        actor_user_id=actor.id,
        assessment=assessment,
        matter=matter,
        revision=revision,
        catalog=catalog,
        observations_artifact=observations_reference,
        observations=observations,
        extraction_plan=extraction_plan,
        skill_version=skill_version,
        resolved_model=resolved_model,
        binding_snapshot=binding_snapshot(resolved),
        plan=plan,
        launch_key=launch_key,
    )


def adjudication_plan_report(prepared: PreparedAdjudicationShadowLaunch) -> dict[str, Any]:
    return {
        "result_version": ADJUDICATION_SHADOW_LAUNCHER_VERSION,
        "mode": "DRY_RUN",
        "actor_user_id": str(prepared.actor_user_id),
        "assessment_id": str(prepared.assessment.id),
        "matter_id": str(prepared.matter.id),
        "matter_document_id": str(prepared.plan.matter_document_id),
        "observations_artifact_id": str(prepared.observations_artifact.artifact_id),
        "adjudication_plan_hash": prepared.plan.plan_hash,
        "resolved_model": prepared.resolved_model,
        "evidence_complete": prepared.plan.evidence_complete,
        "criterion_count": len({value.criterion_key for value in prepared.plan.packets}),
        "packet_count": len(prepared.plan.packets),
        "model_packet_count": sum(not value.deterministic_without_model for value in prepared.plan.packets),
        "deterministic_packet_count": sum(value.deterministic_without_model for value in prepared.plan.packets),
        "observation_count": len(prepared.observations.observations),
        "unmapped_observation_count": prepared.plan.unmapped_observation_count,
    }


def _workflow(
    db: Session,
    prepared: PreparedAdjudicationShadowLaunch,
    *,
    retry_failed: bool,
    workflow_run_id: uuid.UUID | None = None,
) -> tuple[WorkflowRun, WorkflowStepRun, WorkflowStepRun, WorkflowStepRun]:
    dbos_workflow_id = f"definition-evidence-adjudication-shadow:{prepared.launch_key}"
    workflow = (
        db.get(WorkflowRun, workflow_run_id)
        if workflow_run_id is not None
        else db.scalar(select(WorkflowRun).where(WorkflowRun.dbos_workflow_id == dbos_workflow_id))
    )
    if workflow_run_id is not None and workflow is None:
        raise ValueError("Selected criterion-adjudication workflow is unavailable")
    if workflow is not None and (
        workflow.job_type != "MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW"
        or workflow.matter_id != prepared.matter.id
        or str(workflow.input_snapshot.get("assessment_id")) != str(prepared.assessment.id)
        or str(workflow.input_snapshot.get("matter_document_id"))
        != str(prepared.plan.matter_document_id)
    ):
        raise ValueError("Selected workflow is not the prepared criterion-adjudication run")
    packet_count = len(prepared.plan.packets)
    if workflow is None:
        workflow = WorkflowRun(
            tenant_id=prepared.matter.client.tenant_id,
            client_id=prepared.matter.client_id,
            matter_id=prepared.matter.id,
            job_type="MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW",
            job_id=uuid.uuid5(uuid.NAMESPACE_URL, dbos_workflow_id),
            attempt_number=1,
            workflow_key=MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW_SPEC.key,
            code_version=MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW_SPEC.code_version,
            dbos_workflow_id=dbos_workflow_id,
            idempotency_key=prepared.launch_key,
            status="QUEUED",
            current_stage="CRITERION_ADJUDICATION",
            progress_total=packet_count + 2,
            progress_unit="UNITS",
            input_snapshot={
                "assessment_id": str(prepared.assessment.id),
                "matter_document_id": str(prepared.plan.matter_document_id),
                "observations_artifact_id": str(prepared.observations_artifact.artifact_id),
                "adjudication_plan_hash": prepared.plan.plan_hash,
            },
            binding_snapshot=prepared.binding_snapshot,
            configuration_snapshot={
                "launcher_version": ADJUDICATION_SHADOW_LAUNCHER_VERSION,
                "resolved_model": prepared.resolved_model,
                "retry_failed": retry_failed,
                "authoritative": False,
            },
            progress={},
            initiated_by_user_id=prepared.actor_user_id,
        )
        db.add(workflow)
        db.flush()
        step = WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key="criterion_adjudication",
            ordinal=1,
            fan_out_group="criteria",
            status="QUEUED",
            total_count=packet_count,
        )
        db.add(step)
        decision_step = WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key="document_decision",
            ordinal=2,
            fan_out_group=None,
            status="QUEUED",
            total_count=1,
        )
        db.add(decision_step)
        projection_step = WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key="compatibility_projection",
            ordinal=3,
            fan_out_group=None,
            status="QUEUED",
            total_count=1,
        )
        db.add(projection_step)
        record_audit(
            db,
            tenant_id=workflow.tenant_id,
            actor_user_id=prepared.actor_user_id,
            action="matter_definition.evidence_adjudication_shadow.started",
            target_type="workflow_run",
            target_id=workflow.id,
            details={
                "assessment_id": str(prepared.assessment.id),
                "matter_document_id": str(prepared.plan.matter_document_id),
                "packet_count": packet_count,
                "authoritative": False,
            },
        )
        db.flush()
        return workflow, step, decision_step, projection_step
    if workflow.input_snapshot.get("adjudication_plan_hash") != prepared.plan.plan_hash:
        raise ValueError("Existing adjudication workflow does not match the prepared plan")
    workflow.code_version = MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW_SPEC.code_version
    step = db.scalar(
        select(WorkflowStepRun).where(
            WorkflowStepRun.workflow_run_id == workflow.id,
            WorkflowStepRun.ordinal == 1,
        )
    )
    if step is None or step.role_key != "criterion_adjudication":
        raise ValueError("Existing adjudication workflow is missing its criterion step")
    decision_step = db.scalar(
        select(WorkflowStepRun).where(
            WorkflowStepRun.workflow_run_id == workflow.id,
            WorkflowStepRun.ordinal == 2,
        )
    )
    if decision_step is None:
        decision_step = WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key="document_decision",
            ordinal=2,
            status="QUEUED",
            total_count=1,
        )
        db.add(decision_step)
        db.flush()
    elif decision_step.role_key != "document_decision":
        raise ValueError("Existing adjudication workflow has an incompatible decision step")
    projection_step = db.scalar(
        select(WorkflowStepRun).where(
            WorkflowStepRun.workflow_run_id == workflow.id,
            WorkflowStepRun.ordinal == 3,
        )
    )
    if projection_step is None:
        projection_step = WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key="compatibility_projection",
            ordinal=3,
            status="QUEUED",
            total_count=1,
        )
        db.add(projection_step)
        db.flush()
    elif projection_step.role_key != "compatibility_projection":
        raise ValueError("Existing adjudication workflow has an incompatible projection step")
    workflow.progress_total = packet_count + 2
    workflow.progress_unit = "UNITS"
    return workflow, step, decision_step, projection_step


async def execute_prepared_adjudication_shadow(
    db: Session,
    prepared: PreparedAdjudicationShadowLaunch,
    *,
    retry_failed: bool = False,
    workflow_run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    require_evidence_shadow_enabled()
    workflow, step, decision_step, projection_step = (
        _workflow(db, prepared, retry_failed=retry_failed)
        if workflow_run_id is None
        else _workflow(
            db,
            prepared,
            retry_failed=retry_failed,
            workflow_run_id=workflow_run_id,
        )
    )
    workflow.status = "RUNNING"
    workflow.current_stage = "CRITERION_ADJUDICATION"
    workflow.started_at = workflow.started_at or datetime.now(timezone.utc)
    workflow.completed_at = None
    workflow.failure_code = None
    workflow.failure_summary = None
    workflow.failure_retriable = False
    workflow.error_message = None
    workflow.configuration_snapshot = {
        **workflow.configuration_snapshot,
        "launcher_version": ADJUDICATION_SHADOW_LAUNCHER_VERSION,
        "retry_failed": retry_failed,
    }
    step.status = "RUNNING"
    step.started_at = step.started_at or datetime.now(timezone.utc)
    step.completed_at = None
    step.error_message = None
    decision_step.status = "QUEUED"
    decision_step.started_at = None
    decision_step.completed_at = None
    decision_step.completed_count = 0
    decision_step.failed_count = 0
    decision_step.error_message = None
    projection_step.status = "QUEUED"
    projection_step.started_at = None
    projection_step.completed_at = None
    projection_step.completed_count = 0
    projection_step.failed_count = 0
    projection_step.error_message = None
    db.commit()
    try:
        outcome = await execute_managed_adjudication_shadow(
            prepared.plan,
            db=db,
            workflow=workflow,
            step=step,
            skill_version=prepared.skill_version,
            review_guidance=prepared.revision.content_markdown,
            catalog=prepared.catalog,
            observations=prepared.observations,
            resolved_model=prepared.resolved_model,
            retry_failed=retry_failed,
        )
    except Exception as exc:
        completed_at = datetime.now(timezone.utc)
        message = f"{type(exc).__name__}: criterion adjudication did not complete"
        step.status = "FAILED"
        step.failed_count = max(1, step.failed_count)
        step.error_message = message
        step.completed_at = completed_at
        decision_step.status = "CANCELED"
        decision_step.error_message = message
        decision_step.completed_at = completed_at
        projection_step.status = "CANCELED"
        projection_step.error_message = message
        projection_step.completed_at = completed_at
        workflow.status = "FAILED"
        workflow.current_stage = "COMPLETED"
        workflow.failure_code = "CRITERION_ADJUDICATION_FAILED"
        workflow.failure_summary = message
        workflow.failure_retriable = True
        workflow.error_message = message
        workflow.completed_at = completed_at
        refresh_workflow_execution_usage(db, workflow.id)
        db.commit()
        raise
    statuses = Counter(value.status for value in outcome.result.selected_attempts)
    successful = statuses["COMPLETED"] + statuses["COMPLETED_WITH_ADJUSTMENTS"]
    failed = len(prepared.plan.packets) - successful
    step.completed_count = successful
    step.failed_count = failed
    adjudication_has_errors = failed > 0 or not outcome.result.adjudication_complete
    step.status = "COMPLETED_WITH_ERRORS" if adjudication_has_errors else "COMPLETED"
    step.completed_at = datetime.now(timezone.utc)
    workflow.current_stage = "DOCUMENT_DECISION"
    decision_step.status = "RUNNING"
    decision_step.started_at = datetime.now(timezone.utc)
    db.commit()
    try:
        document_result = build_document_decision_result(
            prepared.plan,
            outcome.result,
            criterion_decisions_artifact=outcome.decisions_artifact,
            catalog=prepared.catalog,
        )
        document_artifact, _created = store_document_decision_artifact(
            document_result,
            producer_run_id=workflow.id,
            actor_user_id=prepared.actor_user_id,
        )
    except Exception as exc:
        completed_at = datetime.now(timezone.utc)
        message = f"{type(exc).__name__}: document decision did not complete"
        decision_step.status = "FAILED"
        decision_step.failed_count = 1
        decision_step.error_message = message
        decision_step.completed_at = completed_at
        projection_step.status = "CANCELED"
        projection_step.error_message = message
        projection_step.completed_at = completed_at
        workflow.status = "FAILED"
        workflow.current_stage = "COMPLETED"
        workflow.failure_code = "DOCUMENT_DECISION_FAILED"
        workflow.failure_summary = message
        workflow.failure_retriable = True
        workflow.error_message = message
        workflow.completed_at = completed_at
        refresh_workflow_execution_usage(db, workflow.id)
        db.commit()
        raise
    decision_has_errors = document_result.status != "COMPLETED"
    decision_step.status = "COMPLETED_WITH_ERRORS" if decision_has_errors else "COMPLETED"
    decision_step.completed_count = 1
    decision_step.completed_at = datetime.now(timezone.utc)
    workflow.current_stage = "COMPATIBILITY_PROJECTION"
    projection_step.status = "RUNNING"
    projection_step.started_at = datetime.now(timezone.utc)
    db.commit()
    try:
        compatibility_result = build_evidence_compatibility_projection(
            plan=prepared.extraction_plan,
            observations=prepared.observations,
            observations_artifact=prepared.observations_artifact,
            criterion_decisions=outcome.result,
            criterion_decisions_artifact=outcome.decisions_artifact,
            document_decision=document_result,
            document_decision_artifact=document_artifact,
            catalog=prepared.catalog,
        )
        compatibility_artifact, _created = store_compatibility_projection_artifact(
            compatibility_result,
            producer_run_id=workflow.id,
            actor_user_id=prepared.actor_user_id,
        )
    except Exception as exc:
        completed_at = datetime.now(timezone.utc)
        message = f"{type(exc).__name__}: compatibility projection did not complete"
        projection_step.status = "FAILED"
        projection_step.failed_count = 1
        projection_step.error_message = message
        projection_step.completed_at = completed_at
        workflow.status = "FAILED"
        workflow.current_stage = "COMPLETED"
        workflow.failure_code = "COMPATIBILITY_PROJECTION_FAILED"
        workflow.failure_summary = message
        workflow.failure_retriable = True
        workflow.error_message = message
        workflow.completed_at = completed_at
        refresh_workflow_execution_usage(db, workflow.id)
        db.commit()
        raise
    projection_has_errors = compatibility_result.status != "COMPLETED"
    projection_step.status = "COMPLETED_WITH_ERRORS" if projection_has_errors else "COMPLETED"
    projection_step.completed_count = 1
    projection_step.completed_at = datetime.now(timezone.utc)
    workflow.progress_completed = successful + failed + 2
    workflow.progress = {
        "packet_count": len(prepared.plan.packets),
        "completed_packet_count": successful,
        "failed_packet_count": failed,
        "criterion_decision_count": len(outcome.result.criterion_decisions),
        "document_decision_status": document_result.status,
        "adjudication_plan_artifact_id": str(outcome.plan_artifact.artifact_id),
        "criterion_decisions_artifact_id": str(outcome.decisions_artifact.artifact_id),
        "document_decision_artifact_id": str(document_artifact.artifact_id),
        "compatibility_projection_status": compatibility_result.status,
        "compatibility_result_artifact_id": str(compatibility_artifact.artifact_id),
    }
    has_errors = adjudication_has_errors or decision_has_errors or projection_has_errors
    final_status = "COMPLETED_WITH_ERRORS" if has_errors else "COMPLETED"
    completed_at = datetime.now(timezone.utc)
    workflow.status = final_status
    workflow.current_stage = "COMPLETED"
    workflow.completed_at = completed_at
    refresh_workflow_execution_usage(db, workflow.id)
    record_audit(
        db,
        tenant_id=workflow.tenant_id,
        actor_user_id=prepared.actor_user_id,
        action="matter_definition.evidence_adjudication_shadow.completed",
        target_type="workflow_run",
        target_id=workflow.id,
        details={
            "assessment_id": str(prepared.assessment.id),
            "matter_document_id": str(prepared.plan.matter_document_id),
            "status": final_status,
            "completed_packet_count": successful,
            "failed_packet_count": failed,
            "document_decision_status": document_result.status,
            "compatibility_projection_status": compatibility_result.status,
            "determination": (document_result.decision.determination if document_result.decision else None),
            "authoritative": False,
        },
    )
    db.commit()
    dispositions = Counter(value.disposition for value in outcome.result.criterion_decisions)
    criterion_dispositions = {
        value.criterion_key: value.disposition
        for value in outcome.result.criterion_decisions
    }
    criterion_signal_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for observation in prepared.observations.observations:
        criterion_signal_counts[observation.criterion_key][observation.signal_type] += 1
    return {
        "result_version": ADJUDICATION_SHADOW_LAUNCHER_VERSION,
        "mode": "EXECUTE",
        "authoritative": False,
        "actor_user_id": str(prepared.actor_user_id),
        "assessment_id": str(prepared.assessment.id),
        "matter_id": str(prepared.matter.id),
        "matter_document_id": str(prepared.plan.matter_document_id),
        "observations_artifact_id": str(prepared.observations_artifact.artifact_id),
        "workflow_run_id": str(workflow.id),
        "status": final_status,
        "resolved_model": prepared.resolved_model,
        "packet_count": len(prepared.plan.packets),
        "model_packet_count": sum(not value.deterministic_without_model for value in prepared.plan.packets),
        "deterministic_packet_count": sum(value.deterministic_without_model for value in prepared.plan.packets),
        "executed_packet_count": len(outcome.executed_unit_ids),
        "reused_packet_count": len(outcome.reused_unit_ids),
        "failed_packet_count": failed,
        "evidence_complete": outcome.result.evidence_complete,
        "adjudication_complete": outcome.result.adjudication_complete,
        "material_adjustment_count": outcome.result.material_adjustment_count,
        "criterion_decision_count": len(outcome.result.criterion_decisions),
        "disposition_counts": dict(sorted(dispositions.items())),
        "criterion_dispositions": dict(sorted(criterion_dispositions.items())),
        "criterion_signal_counts": {
            criterion_key: dict(sorted(counts.items()))
            for criterion_key, counts in sorted(criterion_signal_counts.items())
        },
        "document_decision_status": document_result.status,
        "determination": (document_result.decision.determination if document_result.decision else None),
        "decision_confidence": (document_result.decision.confidence if document_result.decision else None),
        "contributing_criterion_keys": (
            document_result.decision.contributing_criterion_keys if document_result.decision else []
        ),
        "unresolved_criterion_keys": (
            document_result.decision.unresolved_criterion_keys if document_result.decision else []
        ),
        "fallback_criterion_keys": list(document_result.fallback_criterion_keys),
        "unmapped_observation_count": outcome.result.unmapped_observation_count,
        "adjudication_plan_artifact_id": str(outcome.plan_artifact.artifact_id),
        "criterion_decisions_artifact_id": str(outcome.decisions_artifact.artifact_id),
        "document_decision_artifact_id": str(document_artifact.artifact_id),
        "compatibility_projection_status": compatibility_result.status,
        "compatibility_result_artifact_id": str(compatibility_artifact.artifact_id),
        "compatibility_output_schema_version": compatibility_result.direct_output_schema_version,
        "narrative_status": compatibility_result.narrative_status,
        "compatibility_blocking_reasons": list(compatibility_result.blocking_reasons),
        "compatibility_output_counts": (
            {
                "summary_statements": len(compatibility_result.output["summary"]),
                "criterion_matches": len(compatibility_result.output["criterion_matches"]),
                "countervailing_considerations": len(
                    compatibility_result.output["countervailing_considerations"]
                ),
                "clarification_requests": len(
                    compatibility_result.output["clarification_requests"]
                ),
                "limitations": len(compatibility_result.output["limitations"]),
            }
            if compatibility_result.output is not None
            else None
        ),
        "compatibility_coverage_status": (
            compatibility_result.output["coverage"]["status"]
            if compatibility_result.output is not None
            else None
        ),
        "usage": {
            "request_count": workflow.request_count,
            "input_tokens": workflow.input_tokens,
            "cached_input_tokens": workflow.cached_input_tokens,
            "cache_write_tokens": workflow.cache_write_tokens,
            "output_tokens": workflow.output_tokens,
        },
    }
