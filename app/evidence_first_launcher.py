"""Controlled launcher for non-authoritative Matter Definition evidence shadow runs."""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_models import resolve_agent_model
from app.artifact_gateway import (
    PreferredTextSource,
    get_collection_item_analysis_input_snapshots,
    get_preferred_text_source,
)
from app.audit import record_audit
from app.config import Settings, get_settings
from app.document_evidence import build_document_map_plan, segment_paragraphs
from app.evidence_context import DocumentEvidenceContext, build_document_evidence_context
from app.evidence_first_artifacts import (
    AnalysisRoleBinding,
    AnalysisRoleBudget,
    EvidenceEvaluationPlan,
    build_evidence_evaluation_plan,
)
from app.evidence_first_contracts import (
    EvidenceExtractionOutput,
    FrozenCriterionCatalog,
    criterion_catalog_from_retrieval_plan,
)
from app.evidence_first_managed import (
    ManagedEvidenceUnitExtractor,
    execute_managed_evidence_shadow,
    require_evidence_shadow_enabled,
)
from app.evidence_first_shadow import (
    WorkflowEvidenceShadowArtifactRepository,
    compact_evidence_shadow,
    execute_evidence_unit,
)
from app.execution_accounting import refresh_workflow_execution_usage
from app.model_execution import content_hash, provider_compatible_model_settings
from app.models import (
    Matter,
    MatterDefinitionAssessmentRun,
    MatterDefinitionRevision,
    MatterDocument,
    ReviewBatchDocument,
    SkillDefinitionVersion,
    User,
    WorkflowRun,
    WorkflowStepRun,
)
from app.privilege_evidence_context import (
    PRIVILEGE_FAMILY_EVIDENCE_MODE,
    load_privilege_family_evidence_context,
)
from app.provider_schemas import provider_output_schema
from app.provider_usage import external_model_identity
from app.workflow_specs import (
    MATTER_DEFINITION_EVIDENCE_SHADOW_SPEC,
    binding_snapshot,
    resolve_workflow_skill_bindings,
)

EVIDENCE_SHADOW_LAUNCHER_VERSION = "matter-definition-evidence-shadow-launcher-v2"
EVIDENCE_SHADOW_TASK_VERSION = "matter-definition:evidence-first-v2"
MAX_SHADOW_DOCUMENTS = 25
SourceLoader = Callable[..., PreferredTextSource | None]


class EvidenceInputUnavailableError(ValueError):
    """A deterministic evidence-input failure that retrying cannot repair."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def uses_privilege_family_evidence_context(mode: object) -> bool:
    if mode == PRIVILEGE_FAMILY_EVIDENCE_MODE:
        return True
    if isinstance(mode, str) and mode.startswith("PRIVILEGE_"):
        raise ValueError(f"Unsupported privilege evidence execution mode: {mode}")
    return False


def frozen_evidence_stage_model(
    configuration_snapshot: dict[str, Any],
    *,
    stage: str,
) -> str | None:
    orchestration = configuration_snapshot.get("evidence_first_orchestration")
    stages = orchestration.get("stages") if isinstance(orchestration, dict) else None
    frozen_stage = stages.get(stage) if isinstance(stages, dict) else None
    model = frozen_stage.get("resolved_model") if isinstance(frozen_stage, dict) else None
    return model if isinstance(model, str) and model.strip() else None


@dataclass(frozen=True)
class PreparedEvidenceShadowDocument:
    document: MatterDocument
    source: PreferredTextSource
    plan: EvidenceEvaluationPlan
    document_context: DocumentEvidenceContext | None = None


@dataclass(frozen=True)
class PreparedEvidenceShadowLaunch:
    actor_user_id: uuid.UUID
    assessment: MatterDefinitionAssessmentRun
    matter: Matter
    revision: MatterDefinitionRevision
    catalog: FrozenCriterionCatalog
    skill_version: SkillDefinitionVersion
    resolved_model: str
    model_settings: dict[str, Any]
    binding_snapshot: dict[str, dict]
    documents: tuple[PreparedEvidenceShadowDocument, ...]
    launch_key: str


def validate_shadow_selection(
    *,
    document_ids: list[uuid.UUID] | tuple[uuid.UUID, ...] | None,
    max_documents: int | None,
) -> tuple[uuid.UUID, ...] | None:
    selected = tuple(dict.fromkeys(document_ids or ()))
    if bool(selected) == (max_documents is not None):
        raise ValueError("Specify exactly one of explicit document IDs or max_documents")
    count = len(selected) if selected else int(max_documents or 0)
    if count < 1 or count > MAX_SHADOW_DOCUMENTS:
        raise ValueError(f"Evidence shadow selection must contain between 1 and {MAX_SHADOW_DOCUMENTS} documents")
    return selected or None


def shadow_launch_key(
    *,
    actor_user_id: uuid.UUID,
    assessment_id: uuid.UUID,
    document_plans: list[tuple[uuid.UUID, str]] | tuple[tuple[uuid.UUID, str], ...],
) -> str:
    return content_hash(
        {
            "launcher_version": EVIDENCE_SHADOW_LAUNCHER_VERSION,
            "actor_user_id": str(actor_user_id),
            "assessment_id": str(assessment_id),
            "document_plans": [
                {"matter_document_id": str(document_id), "plan_hash": plan_hash}
                for document_id, plan_hash in document_plans
            ],
        }
    )


def _require_shadow_operator(
    actor: User | None,
    *,
    tenant_id: uuid.UUID,
    allow_tenant_member: bool = False,
) -> User:
    if actor is None or actor.status != "ACTIVE" or actor.tenant.status != "ACTIVE":
        raise PermissionError("Evidence shadow operator is unavailable or inactive")
    if allow_tenant_member and actor.tenant_id == tenant_id:
        return actor
    if actor.tenant_role == "ADMIN" and (actor.tenant.is_root or actor.tenant_id == tenant_id):
        return actor
    raise PermissionError("Evidence shadow execution requires a root or owning-tenant administrator")


def _selected_document_ids(
    db: Session,
    assessment: MatterDefinitionAssessmentRun,
    *,
    document_ids: tuple[uuid.UUID, ...] | None,
    max_documents: int | None,
) -> tuple[uuid.UUID, ...]:
    if assessment.review_batch_id is None:
        raise ValueError("Assessment does not have a frozen review batch")
    statement = select(ReviewBatchDocument.matter_document_id).where(
        ReviewBatchDocument.review_batch_id == assessment.review_batch_id
    )
    if document_ids is None:
        return tuple(db.scalars(statement.order_by(ReviewBatchDocument.sequence_number).limit(int(max_documents or 0))))
    available = set(db.scalars(statement.where(ReviewBatchDocument.matter_document_id.in_(document_ids))))
    missing = [str(value) for value in document_ids if value not in available]
    if missing:
        raise ValueError("Selected documents are not members of the assessment batch: " + ", ".join(missing))
    return document_ids


def _analysis_role_binding(
    skill_version: SkillDefinitionVersion,
    *,
    resolved_model: str,
    model_settings: dict[str, Any] | None = None,
) -> AnalysisRoleBinding:
    identity = external_model_identity(resolved_model)
    if identity is None:
        raise ValueError("Evidence shadow requires an explicitly resolved provider:model identity")
    provider, _provider_model = identity
    application_schema = EvidenceExtractionOutput.model_json_schema()
    admitted_schema = provider_output_schema(application_schema, provider=provider)
    return AnalysisRoleBinding(
        role_key="evidence_extraction",
        skill_definition_version_id=skill_version.id,
        provider=provider,
        model_key=resolved_model,
        prompt_hash=content_hash(skill_version.instructions),
        application_schema_hash=content_hash(application_schema),
        provider_schema_hash=content_hash(admitted_schema),
        model_settings_hash=content_hash(
            skill_version.model_policy if model_settings is None else model_settings
        ),
        capability_profile_hash=content_hash(
            {
                "provider": provider,
                "model": resolved_model,
                "required_capabilities": sorted(skill_version.required_capabilities),
            }
        ),
    )


def _analysis_role_budget(
    skill_version: SkillDefinitionVersion,
    *,
    unit_count: int,
) -> AnalysisRoleBudget:
    limits = skill_version.limits or {}
    per_unit_requests = max(1, int(limits.get("max_requests", 1)))
    per_unit_output = max(0, int(limits.get("max_output_tokens", 0)))
    per_unit_input = max(0, int(limits.get("max_input_tokens", 0)))
    return AnalysisRoleBudget(
        role_key="evidence_extraction",
        max_requests=unit_count * per_unit_requests,
        repair_request_reserve=unit_count * max(0, int(limits.get("max_output_retries", 0))),
        max_input_tokens=unit_count * per_unit_input,
        max_output_tokens=unit_count * per_unit_output,
        max_elapsed_seconds=unit_count * max(1, int(limits.get("max_elapsed_seconds", 600))),
    )


def _build_document_plan(
    *,
    assessment: MatterDefinitionAssessmentRun,
    matter: Matter,
    revision: MatterDefinitionRevision,
    catalog: FrozenCriterionCatalog,
    document: MatterDocument,
    source: PreferredTextSource,
    role_binding: AnalysisRoleBinding,
    skill_version: SkillDefinitionVersion,
    map_max_characters: int,
    document_context: DocumentEvidenceContext | None = None,
) -> EvidenceEvaluationPlan:
    if hashlib.sha256(revision.content_markdown.encode()).hexdigest() != assessment.definition_content_hash:
        raise ValueError("Assessment Review Guidance hash no longer matches its frozen revision")
    paragraph_map = segment_paragraphs(source.text)
    if not paragraph_map.paragraphs:
        raise ValueError("Document has no extractable text paragraphs")
    map_plan = build_document_map_plan(
        paragraph_map,
        max_characters=map_max_characters,
        overlap_segments=1,
    )
    return build_evidence_evaluation_plan(
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        matter_id=matter.id,
        matter_document_id=document.id,
        collection_item_id=document.collection_item_id,
        source_artifact_id=source.artifact_id,
        source_content_hash=source.content_hash,
        task_version_key=EVIDENCE_SHADOW_TASK_VERSION,
        guidance_revision_id=revision.id,
        guidance_content_hash=assessment.definition_content_hash,
        criterion_catalog_hash=catalog.catalog_hash,
        paragraph_map=paragraph_map,
        document_map_plan=map_plan,
        role_bindings=(role_binding,),
        role_budgets=(_analysis_role_budget(skill_version, unit_count=len(map_plan.windows)),),
        document_context_hash=(document_context.context_hash if document_context is not None else None),
        document_context_citation_ids=(
            tuple(sorted(document_context.citation_ids)) if document_context is not None else ()
        ),
    )


def prepare_evidence_shadow_launch(
    db: Session,
    *,
    actor_user_id: uuid.UUID,
    assessment_id: uuid.UUID,
    document_ids: list[uuid.UUID] | tuple[uuid.UUID, ...] | None = None,
    max_documents: int | None = None,
    map_max_characters: int | None = None,
    extraction_model: str | None = None,
    extraction_model_settings: dict[str, Any] | None = None,
    settings: Settings | None = None,
    source_loader: SourceLoader = get_preferred_text_source,
    require_operator_role: bool = True,
) -> PreparedEvidenceShadowLaunch:
    selected = validate_shadow_selection(document_ids=document_ids, max_documents=max_documents)
    active_settings = settings or get_settings()
    assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
    if assessment is None:
        raise ValueError("Matter Definition assessment not found")
    matter = db.get(Matter, assessment.matter_id)
    revision = db.get(MatterDefinitionRevision, assessment.matter_definition_revision_id)
    if matter is None or revision is None:
        raise ValueError("Assessment matter or Review Guidance revision is unavailable")
    actor = _require_shadow_operator(
        db.get(User, actor_user_id),
        tenant_id=matter.client.tenant_id,
        allow_tenant_member=not require_operator_role,
    )
    retrieval_plan = assessment.configuration_snapshot.get("retrieval_plan")
    if not isinstance(retrieval_plan, dict):
        raise TypeError("Assessment does not contain a frozen retrieval plan")
    catalog = criterion_catalog_from_retrieval_plan(retrieval_plan)
    resolved = resolve_workflow_skill_bindings(
        db,
        workflow_key=MATTER_DEFINITION_EVIDENCE_SHADOW_SPEC.key,
        tenant_id=matter.client.tenant_id,
    )
    _binding, _definition, skill_version = resolved["evidence_extraction"]
    frozen_model = frozen_evidence_stage_model(
        assessment.configuration_snapshot,
        stage="EXTRACTION",
    )
    resolved_model = (
        extraction_model
        or frozen_model
        or resolve_agent_model(skill_version.model_key, active_settings)
    )
    requested_model_settings = (
        dict(skill_version.model_policy)
        if extraction_model_settings is None
        else dict(extraction_model_settings)
    )
    model_settings = provider_compatible_model_settings(
        requested_model_settings,
        resolved_model=resolved_model,
    )
    role_binding = _analysis_role_binding(
        skill_version,
        resolved_model=resolved_model,
        model_settings=model_settings,
    )
    selected_ids = _selected_document_ids(
        db,
        assessment,
        document_ids=selected,
        max_documents=max_documents,
    )
    if not selected_ids:
        raise ValueError("Assessment batch does not contain any selected documents")
    maximum = map_max_characters or active_settings.definition_assessment_map_max_characters
    documents = {
        document.id: document
        for document in db.scalars(select(MatterDocument).where(MatterDocument.id.in_(selected_ids)))
    }
    tenant_id = matter.client.tenant_id
    client_id = matter.client_id
    db.commit()
    input_snapshots = get_collection_item_analysis_input_snapshots(
        item_ids=[
            document.collection_item_id
            for document_id in selected_ids
            if (document := documents.get(document_id)) is not None
        ],
        actor_user_id=actor.id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    prepared: list[PreparedEvidenceShadowDocument] = []
    for document_id in selected_ids:
        document = documents.get(document_id)
        if document is None or document.matter_id != matter.id:
            raise EvidenceInputUnavailableError(
                "DOCUMENT_UNAVAILABLE",
                f"Selected Matter document is unavailable: {document_id}",
            )
        source = source_loader(
            collection_item_id=document.collection_item_id,
            actor_user_id=actor.id,
            tenant_id=tenant_id,
            client_id=client_id,
        )
        if source is None:
            raise EvidenceInputUnavailableError(
                "NO_PREFERRED_TEXT_SOURCE",
                f"Selected document has no preferred text source: {document_id}",
            )
        input_snapshot = input_snapshots.get(document.collection_item_id)
        if input_snapshot is None:
            raise EvidenceInputUnavailableError(
                "ANALYSIS_INPUT_MISSING",
                f"Selected document has no frozen analysis metadata: {document_id}",
            )
        if (
            input_snapshot.text_source is None
            or input_snapshot.text_source.artifact_id != source.artifact_id
            or input_snapshot.text_source.content_hash != source.content_hash
        ):
            raise EvidenceInputUnavailableError(
                "TEXT_SOURCE_CHANGED",
                f"Selected document text source does not match its input snapshot: {document_id}",
            )
        assessment_execution_mode = assessment.configuration_snapshot.get(
            "assessment_execution_mode"
        )
        if uses_privilege_family_evidence_context(assessment_execution_mode):
            packet_reference = assessment.configuration_snapshot.get("privilege_evidence_packets")
            raw_program_id = assessment.configuration_snapshot.get("privilege_review_program_id")
            if not isinstance(packet_reference, dict) or raw_program_id is None:
                raise ValueError("Privilege evidence assessment is missing its frozen family packets")
            document_context = load_privilege_family_evidence_context(
                packet_reference=packet_reference,
                matter_id=matter.id,
                program_id=uuid.UUID(str(raw_program_id)),
                target_document_id=document.id,
                target_collection_item_id=document.collection_item_id,
                target_snapshot=input_snapshot,
                actor_user_id=actor.id,
                tenant_id=tenant_id,
                client_id=client_id,
            )
        else:
            document_context = build_document_evidence_context(input_snapshot)
        prepared.append(
            PreparedEvidenceShadowDocument(
                document=document,
                source=source,
                document_context=document_context,
                plan=_build_document_plan(
                    assessment=assessment,
                    matter=matter,
                    revision=revision,
                    catalog=catalog,
                    document=document,
                    source=source,
                    role_binding=role_binding,
                    skill_version=skill_version,
                    map_max_characters=maximum,
                    document_context=document_context,
                ),
            )
        )
    launch_key = shadow_launch_key(
        actor_user_id=actor.id,
        assessment_id=assessment.id,
        document_plans=[(value.document.id, value.plan.plan_hash) for value in prepared],
    )
    return PreparedEvidenceShadowLaunch(
        actor_user_id=actor.id,
        assessment=assessment,
        matter=matter,
        revision=revision,
        catalog=catalog,
        skill_version=skill_version,
        resolved_model=resolved_model,
        model_settings=model_settings,
        binding_snapshot=binding_snapshot(resolved),
        documents=tuple(prepared),
        launch_key=launch_key,
    )


def shadow_plan_report(prepared: PreparedEvidenceShadowLaunch) -> dict[str, Any]:
    return {
        "result_version": EVIDENCE_SHADOW_LAUNCHER_VERSION,
        "mode": "DRY_RUN",
        "actor_user_id": str(prepared.actor_user_id),
        "assessment_id": str(prepared.assessment.id),
        "matter_id": str(prepared.matter.id),
        "launch_key": prepared.launch_key,
        "resolved_model": prepared.resolved_model,
        "criterion_catalog_hash": prepared.catalog.catalog_hash,
        "document_count": len(prepared.documents),
        "documents": [
            {
                "matter_document_id": str(value.document.id),
                "collection_item_id": str(value.document.collection_item_id),
                "source_artifact_id": str(value.source.artifact_id),
                "source_content_hash": value.source.content_hash,
                "source_character_count": value.plan.source_length,
                "document_context_hash": value.plan.document_context_hash,
                "document_context_item_count": len(value.plan.document_context_citation_ids),
                "plan_hash": value.plan.plan_hash,
                "unit_count": len(value.plan.extraction_units),
            }
            for value in prepared.documents
        ],
    }


def _shadow_workflow(
    db: Session,
    prepared: PreparedEvidenceShadowLaunch,
    *,
    retry_failed: bool,
    workflow_run_id: uuid.UUID | None = None,
) -> tuple[WorkflowRun, WorkflowStepRun]:
    dbos_workflow_id = f"definition-evidence-shadow:{prepared.launch_key}"
    workflow = (
        db.get(WorkflowRun, workflow_run_id)
        if workflow_run_id is not None
        else db.scalar(select(WorkflowRun).where(WorkflowRun.dbos_workflow_id == dbos_workflow_id))
    )
    if workflow_run_id is not None and workflow is None:
        raise ValueError("Selected evidence-extraction workflow is unavailable")
    if workflow is not None and (
        workflow.job_type != "MATTER_DEFINITION_EVIDENCE_SHADOW"
        or workflow.matter_id != prepared.matter.id
        or str(workflow.input_snapshot.get("assessment_id")) != str(prepared.assessment.id)
    ):
        raise ValueError("Selected workflow is not the prepared evidence-extraction run")
    unit_count = sum(len(value.plan.extraction_units) for value in prepared.documents)
    if workflow is None:
        job_id = uuid.uuid5(uuid.NAMESPACE_URL, dbos_workflow_id)
        workflow = WorkflowRun(
            tenant_id=prepared.matter.client.tenant_id,
            client_id=prepared.matter.client_id,
            matter_id=prepared.matter.id,
            job_type="MATTER_DEFINITION_EVIDENCE_SHADOW",
            job_id=job_id,
            attempt_number=1,
            workflow_key=MATTER_DEFINITION_EVIDENCE_SHADOW_SPEC.key,
            code_version=MATTER_DEFINITION_EVIDENCE_SHADOW_SPEC.code_version,
            dbos_workflow_id=dbos_workflow_id,
            idempotency_key=prepared.launch_key,
            status="QUEUED",
            current_stage="EVIDENCE_EXTRACTION",
            progress_total=unit_count,
            progress_unit="UNITS",
            input_snapshot={
                "assessment_id": str(prepared.assessment.id),
                "matter_document_ids": [str(value.document.id) for value in prepared.documents],
                "plan_hashes": [value.plan.plan_hash for value in prepared.documents],
            },
            binding_snapshot=prepared.binding_snapshot,
            configuration_snapshot={
                "launcher_version": EVIDENCE_SHADOW_LAUNCHER_VERSION,
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
            role_key="evidence_extraction",
            ordinal=1,
            fan_out_group="documents",
            status="QUEUED",
            total_count=unit_count,
        )
        db.add(step)
        record_audit(
            db,
            tenant_id=workflow.tenant_id,
            actor_user_id=workflow.initiated_by_user_id,
            action="matter_definition.evidence_shadow.started",
            target_type="workflow_run",
            target_id=workflow.id,
            details={
                "assessment_id": str(prepared.assessment.id),
                "document_count": len(prepared.documents),
                "unit_count": unit_count,
                "authoritative": False,
            },
        )
        db.flush()
        return workflow, step
    if workflow.input_snapshot.get("plan_hashes") != [value.plan.plan_hash for value in prepared.documents]:
        raise ValueError("Existing shadow workflow does not match the prepared plans")
    step = db.scalar(
        select(WorkflowStepRun).where(
            WorkflowStepRun.workflow_run_id == workflow.id,
            WorkflowStepRun.ordinal == 1,
        )
    )
    if step is None or step.role_key != "evidence_extraction":
        raise ValueError("Existing shadow workflow is missing its extraction step")
    step.total_count = unit_count
    return workflow, step


def initialize_prepared_evidence_shadow(
    db: Session,
    prepared: PreparedEvidenceShadowLaunch,
    *,
    retry_failed: bool = False,
    workflow_run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Checkpoint a one-document extraction plan before unit fan-out."""

    require_evidence_shadow_enabled()
    if len(prepared.documents) != 1:
        raise ValueError("Durable extraction fan-out requires exactly one prepared document")
    workflow, step = _shadow_workflow(
        db,
        prepared,
        retry_failed=retry_failed,
        workflow_run_id=workflow_run_id,
    )
    value = prepared.documents[0]
    workflow.status = "RUNNING"
    workflow.started_at = workflow.started_at or datetime.now(timezone.utc)
    workflow.completed_at = None
    workflow.current_stage = "EVIDENCE_EXTRACTION"
    step.status = "RUNNING"
    step.started_at = step.started_at or datetime.now(timezone.utc)
    step.completed_at = None
    step.error_message = None
    repository = WorkflowEvidenceShadowArtifactRepository(
        actor_user_id=workflow.initiated_by_user_id,
        producer_run_id=workflow.id,
    )
    db.commit()
    plan_artifact = repository.persist_plan(value.plan)
    return {
        "workflow_run_id": str(workflow.id),
        "plan_artifact_id": str(plan_artifact.artifact_id),
        "plan_hash": value.plan.plan_hash,
        "unit_ids": [unit.unit_id for unit in value.plan.extraction_units],
    }


async def execute_prepared_evidence_unit(
    db: Session,
    prepared: PreparedEvidenceShadowLaunch,
    *,
    workflow_run_id: uuid.UUID,
    unit_id: str,
    retry_failed: bool = False,
) -> dict[str, Any]:
    """Execute one durable unit with isolated session/accounting state."""

    require_evidence_shadow_enabled()
    if len(prepared.documents) != 1:
        raise ValueError("Durable extraction fan-out requires exactly one prepared document")
    workflow, step = _shadow_workflow(
        db,
        prepared,
        retry_failed=retry_failed,
        workflow_run_id=workflow_run_id,
    )
    value = prepared.documents[0]
    unit = next((candidate for candidate in value.plan.extraction_units if candidate.unit_id == unit_id), None)
    if unit is None:
        raise ValueError("Selected extraction unit is not present in the immutable plan")
    extractor = ManagedEvidenceUnitExtractor(
        db=db,
        workflow=workflow,
        step=step,
        skill_version=prepared.skill_version,
        review_guidance=prepared.revision.content_markdown,
        catalog=prepared.catalog,
        source_text=value.source.text,
        source_artifact_id=value.source.artifact_id,
        source_content_hash=value.source.content_hash,
        document_context=value.document_context,
        resolved_model=prepared.resolved_model,
        model_settings=prepared.model_settings,
        # Parallel unit workers must not contend on aggregate workflow totals.
        # The document compaction checkpoint refreshes those totals once.
        refresh_workflow_usage=False,
        checkpoint_before_model_call=True,
    )
    repository = WorkflowEvidenceShadowArtifactRepository(
        actor_user_id=workflow.initiated_by_user_id,
        producer_run_id=workflow.id,
    )
    outcome = await execute_evidence_unit(
        value.plan,
        unit,
        repository=repository,
        extractor=extractor,
        retry_failed=retry_failed,
    )
    db.commit()
    latest = outcome.results[-1] if outcome.results else None
    return {
        "matter_document_id": str(value.document.id),
        "unit_id": unit.unit_id,
        "status": latest.status if latest is not None else "MISSING",
        "attempt": latest.attempt if latest is not None else None,
        "executed": outcome.executed,
        "reused": outcome.reused,
    }


def finalize_prepared_evidence_shadow(
    db: Session,
    prepared: PreparedEvidenceShadowLaunch,
    *,
    workflow_run_id: uuid.UUID,
    unit_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compact terminal unit artifacts and checkpoint document extraction."""

    require_evidence_shadow_enabled()
    if len(prepared.documents) != 1:
        raise ValueError("Durable extraction fan-out requires exactly one prepared document")
    workflow, step = _shadow_workflow(
        db,
        prepared,
        retry_failed=False,
        workflow_run_id=workflow_run_id,
    )
    value = prepared.documents[0]
    repository = WorkflowEvidenceShadowArtifactRepository(
        actor_user_id=workflow.initiated_by_user_id,
        producer_run_id=workflow.id,
    )
    outcome = compact_evidence_shadow(value.plan, repository=repository)
    statuses = Counter(item.status for item in outcome.compacted.selected_attempts)
    successful = statuses["COMPLETED"] + statuses["COMPLETED_WITH_ADJUSTMENTS"]
    failed = len(value.plan.extraction_units) - successful
    observation_counts = Counter((item.criterion_key, item.signal_type) for item in outcome.compacted.observations)
    executed_unit_count = sum(bool(item.get("executed")) for item in unit_results or [])
    reused_unit_count = sum(bool(item.get("reused")) for item in unit_results or [])
    report = {
        "matter_document_id": str(value.document.id),
        "status": ("COMPLETED" if outcome.compacted.evidence_complete else "COMPLETED_WITH_ERRORS"),
        "plan_hash": value.plan.plan_hash,
        "coverage_status": outcome.compacted.coverage_status,
        "evidence_complete": outcome.compacted.evidence_complete,
        "unit_count": len(value.plan.extraction_units),
        "executed_unit_count": executed_unit_count,
        "reused_unit_count": reused_unit_count,
        "selected_attempt_statuses": dict(sorted(statuses.items())),
        "missing_unit_count": len(outcome.compacted.missing_unit_ids),
        "omitted_range_count": len(outcome.compacted.omitted_ranges),
        "material_adjustment_count": outcome.compacted.material_adjustment_count,
        "observation_count": len(outcome.compacted.observations),
        "observation_counts": {
            f"{criterion}:{signal}": count for (criterion, signal), count in sorted(observation_counts.items())
        },
        "plan_artifact_id": str(outcome.plan_artifact.artifact_id),
        "observations_artifact_id": str(outcome.observations_artifact.artifact_id),
    }
    final_status = "COMPLETED" if failed == 0 else "COMPLETED_WITH_ERRORS"
    completed_at = datetime.now(timezone.utc)
    step.completed_count = successful
    step.failed_count = failed
    step.status = final_status
    step.completed_at = completed_at
    workflow.progress_completed = successful + failed
    workflow.progress = {
        "document_count": 1,
        "reported_document_count": 1,
        "completed_unit_count": successful,
        "failed_unit_count": failed,
        "documents": [report],
    }
    workflow.status = final_status
    workflow.current_stage = "COMPLETED"
    workflow.completed_at = completed_at
    refresh_workflow_execution_usage(db, workflow.id)
    record_audit(
        db,
        tenant_id=workflow.tenant_id,
        actor_user_id=workflow.initiated_by_user_id,
        action="matter_definition.evidence_shadow.completed",
        target_type="workflow_run",
        target_id=workflow.id,
        details={
            "assessment_id": str(prepared.assessment.id),
            "status": final_status,
            "completed_unit_count": successful,
            "failed_unit_count": failed,
            "authoritative": False,
            "execution_mode": "DURABLE_UNIT_FAN_OUT",
        },
    )
    db.commit()
    return {
        "result_version": EVIDENCE_SHADOW_LAUNCHER_VERSION,
        "mode": "EXECUTE",
        "authoritative": False,
        "actor_user_id": str(prepared.actor_user_id),
        "assessment_id": str(prepared.assessment.id),
        "matter_id": str(prepared.matter.id),
        "launch_key": prepared.launch_key,
        "workflow_run_id": str(workflow.id),
        "status": final_status,
        "resolved_model": prepared.resolved_model,
        "document_count": 1,
        "completed_unit_count": successful,
        "failed_unit_count": failed,
        "usage": {
            "request_count": workflow.request_count,
            "input_tokens": workflow.input_tokens,
            "cached_input_tokens": workflow.cached_input_tokens,
            "cache_write_tokens": workflow.cache_write_tokens,
            "output_tokens": workflow.output_tokens,
        },
        "documents": [report],
    }


async def execute_prepared_evidence_shadow(
    db: Session,
    prepared: PreparedEvidenceShadowLaunch,
    *,
    retry_failed: bool = False,
    workflow_run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    require_evidence_shadow_enabled()
    workflow, step = (
        _shadow_workflow(db, prepared, retry_failed=retry_failed)
        if workflow_run_id is None
        else _shadow_workflow(
            db,
            prepared,
            retry_failed=retry_failed,
            workflow_run_id=workflow_run_id,
        )
    )
    workflow.status = "RUNNING"
    workflow.started_at = workflow.started_at or datetime.now(timezone.utc)
    step.status = "RUNNING"
    step.started_at = step.started_at or datetime.now(timezone.utc)
    step.error_message = None
    db.commit()

    document_reports: list[dict[str, Any]] = []
    completed_units = 0
    failed_units = 0
    for value in prepared.documents:
        try:
            outcome = await execute_managed_evidence_shadow(
                value.plan,
                db=db,
                workflow=workflow,
                step=step,
                skill_version=prepared.skill_version,
                review_guidance=prepared.revision.content_markdown,
                catalog=prepared.catalog,
                source_text=value.source.text,
                source_artifact_id=value.source.artifact_id,
                source_content_hash=value.source.content_hash,
                document_context=value.document_context,
                resolved_model=prepared.resolved_model,
                model_settings=prepared.model_settings,
                retry_failed=retry_failed,
            )
            statuses = Counter(item.status for item in outcome.compacted.selected_attempts)
            successful = statuses["COMPLETED"] + statuses["COMPLETED_WITH_ADJUSTMENTS"]
            failed = len(value.plan.extraction_units) - successful
            completed_units += successful
            failed_units += failed
            observation_counts = Counter(
                (item.criterion_key, item.signal_type) for item in outcome.compacted.observations
            )
            document_reports.append(
                {
                    "matter_document_id": str(value.document.id),
                    "status": ("COMPLETED" if outcome.compacted.evidence_complete else "COMPLETED_WITH_ERRORS"),
                    "plan_hash": value.plan.plan_hash,
                    "coverage_status": outcome.compacted.coverage_status,
                    "evidence_complete": outcome.compacted.evidence_complete,
                    "unit_count": len(value.plan.extraction_units),
                    "executed_unit_count": len(outcome.executed_unit_ids),
                    "reused_unit_count": len(outcome.reused_unit_ids),
                    "selected_attempt_statuses": dict(sorted(statuses.items())),
                    "missing_unit_count": len(outcome.compacted.missing_unit_ids),
                    "omitted_range_count": len(outcome.compacted.omitted_ranges),
                    "material_adjustment_count": outcome.compacted.material_adjustment_count,
                    "observation_count": len(outcome.compacted.observations),
                    "observation_counts": {
                        f"{criterion}:{signal}": count
                        for (criterion, signal), count in sorted(observation_counts.items())
                    },
                    "plan_artifact_id": str(outcome.plan_artifact.artifact_id),
                    "observations_artifact_id": str(outcome.observations_artifact.artifact_id),
                }
            )
        except Exception as exc:  # noqa: BLE001 - retain one document failure and continue the cohort.
            failed_units += len(value.plan.extraction_units)
            document_reports.append(
                {
                    "matter_document_id": str(value.document.id),
                    "status": "FAILED",
                    "plan_hash": value.plan.plan_hash,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:4_000],
                }
            )
        step.completed_count = completed_units
        step.failed_count = failed_units
        workflow.progress_completed = completed_units + failed_units
        workflow.progress = {
            "document_count": len(prepared.documents),
            "reported_document_count": len(document_reports),
            "completed_unit_count": completed_units,
            "failed_unit_count": failed_units,
            "documents": document_reports,
        }
        db.commit()

    has_errors = failed_units > 0 or any(value["status"] != "COMPLETED" for value in document_reports)
    final_status = "COMPLETED_WITH_ERRORS" if has_errors else "COMPLETED"
    completed_at = datetime.now(timezone.utc)
    step.status = final_status
    step.completed_at = completed_at
    workflow.status = final_status
    workflow.current_stage = "COMPLETED"
    workflow.completed_at = completed_at
    refresh_workflow_execution_usage(db, workflow.id)
    record_audit(
        db,
        tenant_id=workflow.tenant_id,
        actor_user_id=workflow.initiated_by_user_id,
        action="matter_definition.evidence_shadow.completed",
        target_type="workflow_run",
        target_id=workflow.id,
        details={
            "assessment_id": str(prepared.assessment.id),
            "status": final_status,
            "completed_unit_count": completed_units,
            "failed_unit_count": failed_units,
            "authoritative": False,
        },
    )
    db.commit()
    return {
        "result_version": EVIDENCE_SHADOW_LAUNCHER_VERSION,
        "mode": "EXECUTE",
        "authoritative": False,
        "actor_user_id": str(prepared.actor_user_id),
        "assessment_id": str(prepared.assessment.id),
        "matter_id": str(prepared.matter.id),
        "launch_key": prepared.launch_key,
        "workflow_run_id": str(workflow.id),
        "status": final_status,
        "resolved_model": prepared.resolved_model,
        "document_count": len(prepared.documents),
        "completed_unit_count": completed_units,
        "failed_unit_count": failed_units,
        "usage": {
            "request_count": workflow.request_count,
            "input_tokens": workflow.input_tokens,
            "cached_input_tokens": workflow.cached_input_tokens,
            "cache_write_tokens": workflow.cache_write_tokens,
            "output_tokens": workflow.output_tokens,
        },
        "documents": document_reports,
    }
