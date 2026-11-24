"""Managed-skill binding for the disabled evidence-first shadow path."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.evidence_context import DocumentEvidenceContext
from app.evidence_first_artifacts import (
    EvidenceEvaluationPlan,
    EvidenceExtractionUnitPlan,
    EvidenceExtractionUnitResult,
    EvidenceUnitAdjustment,
    evidence_observation_source_hash,
)
from app.evidence_first_contracts import (
    ExtractionNormalizationCapture,
    FrozenCriterionCatalog,
    extraction_output_schemas,
)
from app.evidence_first_shadow import (
    EvidenceShadowArtifactRepository,
    EvidenceShadowExecutionOutcome,
    WorkflowEvidenceShadowArtifactRepository,
    execute_evidence_shadow,
)
from app.model_rate_limits import ModelRateLimitPolicy
from app.models import (
    ModelInvocation,
    SkillDefinitionVersion,
    SkillRun,
    WorkflowRun,
    WorkflowStepRun,
)
from app.provider_usage import external_model_identity
from app.skill_execution import execute_skill_run
from app.workflow_specs import MATTER_DEFINITION_EVIDENCE_SHADOW_SPEC

SkillRunner = Callable[..., Awaitable[tuple[dict[str, Any], SkillRun]]]
InvocationIdLoader = Callable[[Session, uuid.UUID], tuple[uuid.UUID, ...]]


class EvidenceShadowDisabledError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedEvidenceUnitModelCall:
    """Provider-independent inputs for one immutable extraction unit."""

    stable_context: dict[str, Any]
    dynamic_input: dict[str, Any]
    cache_identity: dict[str, Any]
    application_schema: dict[str, Any]
    provider_schema: dict[str, Any]
    allowed_citation_ids: tuple[str, ...]
    evidence_input_hash: str


def require_evidence_shadow_enabled() -> None:
    if not get_settings().definition_assessment_evidence_shadow_enabled:
        raise EvidenceShadowDisabledError("Matter Definition evidence shadow execution is disabled by configuration")


def evidence_extraction_rate_limit_policy() -> ModelRateLimitPolicy:
    settings = get_settings()
    return ModelRateLimitPolicy(
        requests_per_minute=settings.definition_assessment_evidence_extraction_requests_per_minute,
        input_tokens_per_minute=(settings.definition_assessment_evidence_extraction_input_tokens_per_minute),
        max_retries=settings.model_rate_limit_max_retries,
        retry_base_seconds=settings.model_rate_limit_retry_base_seconds,
        retry_max_seconds=settings.model_rate_limit_retry_max_seconds,
        characters_per_token=settings.model_rate_limit_characters_per_token,
    )


def _model_invocation_ids(db: Session, skill_run_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
    return tuple(
        db.scalars(
            select(ModelInvocation.id)
            .where(ModelInvocation.skill_run_id == skill_run_id)
            .order_by(ModelInvocation.attempt, ModelInvocation.request_sequence)
        )
    )


def _role_binding(plan: EvidenceEvaluationPlan):
    bindings = [binding for binding in plan.role_bindings if binding.role_key == "evidence_extraction"]
    if len(bindings) != 1:
        raise ValueError("Evidence shadow requires exactly one evidence_extraction binding")
    return bindings[0]


def _render_source_unit(source_text: str, unit: EvidenceExtractionUnitPlan) -> str:
    rendered: list[str] = []
    for segment in unit.segments:
        text = source_text[segment.char_start : segment.char_end]
        if not text:
            raise ValueError("Evidence extraction unit contains an empty source range")
        part_label = f" [part {segment.part}/{segment.part_count}]" if segment.part_count > 1 else ""
        rendered.append(f"{segment.paragraph_id}{part_label} {text}")
    return "\n\n".join(rendered)


@dataclass
class ManagedEvidenceUnitExtractor:
    db: Session
    workflow: WorkflowRun
    step: WorkflowStepRun
    skill_version: SkillDefinitionVersion
    review_guidance: str
    catalog: FrozenCriterionCatalog
    source_text: str
    source_artifact_id: uuid.UUID
    source_content_hash: str
    resolved_model: str
    document_context: DocumentEvidenceContext | None = None
    model: Any | None = None
    model_settings: dict[str, Any] | None = None
    skill_runner: SkillRunner = execute_skill_run
    invocation_id_loader: InvocationIdLoader = _model_invocation_ids
    refresh_workflow_usage: bool = True
    checkpoint_before_model_call: bool = True
    rate_limit_policy: ModelRateLimitPolicy | None = None

    def prepare_model_call(
        self,
        plan: EvidenceEvaluationPlan,
        unit: EvidenceExtractionUnitPlan,
    ) -> PreparedEvidenceUnitModelCall:
        """Build the same extraction request used by real-time and provider-batch execution."""

        self._validate_context(plan, unit)
        paragraph_ids = list(dict.fromkeys(segment.paragraph_id for segment in unit.segments))
        context_citation_ids = sorted(
            self.document_context.citation_ids if self.document_context is not None else ()
        )
        allowed_citation_ids = tuple(dict.fromkeys(paragraph_ids + context_citation_ids))
        application_schema, provider_schema = extraction_output_schemas(
            catalog=self.catalog,
            paragraph_ids=list(allowed_citation_ids),
            provider=_role_binding(plan).provider,
        )
        return PreparedEvidenceUnitModelCall(
            stable_context={
                "review_guidance": self.review_guidance,
                "criterion_catalog": self.catalog.model_dump(mode="json"),
            },
            dynamic_input={
                "metadata_evidence": (
                    self.document_context.as_model_input() if self.document_context is not None else []
                ),
                "source_unit": {
                    "matter_document_id": str(plan.matter_document_id),
                    "unit_id": unit.unit_id,
                    "paragraph_ids": paragraph_ids,
                    "text": _render_source_unit(self.source_text, unit),
                },
            },
            cache_identity={
                "tenant_id": str(plan.tenant_id),
                "matter_id": str(plan.matter_id),
                "skill_version_id": str(self.skill_version.id),
            },
            application_schema=application_schema,
            provider_schema=provider_schema,
            allowed_citation_ids=allowed_citation_ids,
            evidence_input_hash=evidence_observation_source_hash(
                source_content_hash=plan.source_content_hash,
                document_context_hash=plan.document_context_hash,
            ),
        )

    def normalize_model_output(
        self,
        plan: EvidenceEvaluationPlan,
        unit: EvidenceExtractionUnitPlan,
        raw_output: dict[str, Any],
    ) -> ExtractionNormalizationCapture:
        """Apply the extraction contract to an output returned outside the real-time runner."""

        prepared = self.prepare_model_call(plan, unit)
        capture = ExtractionNormalizationCapture(
            catalog=self.catalog,
            allowed_paragraph_ids=frozenset(prepared.allowed_citation_ids),
            source_hash=prepared.evidence_input_hash,
            source_unit_id=unit.unit_id,
        )
        capture(raw_output)
        if capture.result is None:
            raise RuntimeError("Evidence extraction normalizer did not receive model output")
        return capture

    def _validate_context(
        self,
        plan: EvidenceEvaluationPlan,
        unit: EvidenceExtractionUnitPlan,
    ) -> None:
        if self.workflow.workflow_key != MATTER_DEFINITION_EVIDENCE_SHADOW_SPEC.key:
            raise ValueError("Evidence extraction must run in the isolated shadow workflow")
        if self.step.workflow_run_id != self.workflow.id or self.step.role_key != "evidence_extraction":
            raise ValueError("Evidence extraction step does not belong to the shadow workflow")
        if (
            self.workflow.tenant_id != plan.tenant_id
            or self.workflow.client_id != plan.client_id
            or self.workflow.matter_id != plan.matter_id
        ):
            raise ValueError("Evidence shadow workflow scope does not match the evaluation plan")
        binding = _role_binding(plan)
        if binding.skill_definition_version_id != self.skill_version.id:
            raise ValueError("Evaluation plan does not pin the supplied extraction skill version")
        if binding.model_key != self.resolved_model:
            raise ValueError("Evaluation plan does not pin the supplied resolved extraction model")
        identity = external_model_identity(self.resolved_model)
        if identity is None or identity[0] != binding.provider:
            raise ValueError("Resolved extraction model does not match the plan provider")
        if self.skill_version.input_schema_key != "matter_definition_evidence_extraction_input_v2":
            raise ValueError("Managed extraction skill uses an incompatible input contract")
        if self.skill_version.output_schema_key != "matter_definition_evidence_extraction_output_v1":
            raise ValueError("Managed extraction skill uses an incompatible output contract")
        if self.catalog.catalog_hash != plan.criterion_catalog_hash:
            raise ValueError("Criterion catalog does not match the immutable evaluation plan")
        if hashlib.sha256(self.review_guidance.encode()).hexdigest() != plan.guidance_content_hash:
            raise ValueError("Review guidance does not match the immutable evaluation plan")
        if len(self.source_text) != plan.source_length:
            raise ValueError("Source length does not match the immutable evaluation plan")
        if self.source_artifact_id != plan.source_artifact_id:
            raise ValueError("Source artifact does not match the immutable evaluation plan")
        if self.source_content_hash != plan.source_content_hash:
            raise ValueError("Source content hash does not match the immutable evaluation plan")
        context_hash = self.document_context.context_hash if self.document_context is not None else None
        context_citation_ids = (
            tuple(sorted(self.document_context.citation_ids)) if self.document_context is not None else ()
        )
        if context_hash != plan.document_context_hash:
            raise ValueError("Document evidence context does not match the immutable evaluation plan")
        if context_citation_ids != tuple(sorted(plan.document_context_citation_ids)):
            raise ValueError("Document evidence citations do not match the immutable evaluation plan")
        if unit not in plan.extraction_units:
            raise ValueError("Extraction unit is not present in the immutable evaluation plan")

    async def __call__(
        self,
        plan: EvidenceEvaluationPlan,
        unit: EvidenceExtractionUnitPlan,
        attempt: int,
    ) -> EvidenceExtractionUnitResult:
        prepared = self.prepare_model_call(plan, unit)
        capture = ExtractionNormalizationCapture(
            catalog=self.catalog,
            allowed_paragraph_ids=frozenset(prepared.allowed_citation_ids),
            source_hash=prepared.evidence_input_hash,
            source_unit_id=unit.unit_id,
        )
        _output, skill_run = await self.skill_runner(
            self.db,
            workflow=self.workflow,
            step=self.step,
            skill_version=self.skill_version,
            scope_type="MATTER_DOCUMENT",
            scope_id=plan.matter_document_id,
            stable_context=prepared.stable_context,
            dynamic_input=prepared.dynamic_input,
            cache_identity=prepared.cache_identity,
            output_normalizers=(capture,),
            output_schema=prepared.application_schema,
            provider_output_schema=prepared.provider_schema,
            limits=self.skill_version.limits,
            refresh_workflow_usage=self.refresh_workflow_usage,
            checkpoint_before_model_call=self.checkpoint_before_model_call,
            rate_limit_policy=self.rate_limit_policy or evidence_extraction_rate_limit_policy(),
            usage_job_type="MATTER_DEFINITION_EVIDENCE_SHADOW",
            model=self.model if self.model is not None else self.resolved_model,
            model_settings=self.model_settings,
        )
        if capture.result is None:
            raise RuntimeError("Evidence extraction normalizer did not receive model output")
        now = datetime.now(timezone.utc)
        adjustments = tuple(
            EvidenceUnitAdjustment(
                item_index=adjustment.item_index,
                code=adjustment.code,
                policy=adjustment.policy,
                path=adjustment.path,
                detail=adjustment.detail,
            )
            for adjustment in capture.result.adjustments
        )
        return EvidenceExtractionUnitResult(
            plan_hash=plan.plan_hash,
            source_content_hash=plan.source_content_hash,
            unit_id=unit.unit_id,
            unit_ordinal=unit.ordinal,
            attempt=attempt,
            status="COMPLETED_WITH_ADJUSTMENTS" if adjustments else "COMPLETED",
            observations=capture.result.observations,
            adjustments=adjustments,
            invocation_ids=self.invocation_id_loader(self.db, skill_run.id),
            started_at=skill_run.started_at or now,
            completed_at=skill_run.completed_at or now,
        )


async def execute_managed_evidence_shadow(
    plan: EvidenceEvaluationPlan,
    *,
    db: Session,
    workflow: WorkflowRun,
    step: WorkflowStepRun,
    skill_version: SkillDefinitionVersion,
    review_guidance: str,
    catalog: FrozenCriterionCatalog,
    source_text: str,
    source_artifact_id: uuid.UUID,
    source_content_hash: str,
    document_context: DocumentEvidenceContext | None = None,
    resolved_model: str,
    retry_failed: bool = False,
    max_attempts_per_unit: int = 3,
    model: Any | None = None,
    model_settings: dict[str, Any] | None = None,
    repository: EvidenceShadowArtifactRepository | None = None,
) -> EvidenceShadowExecutionOutcome:
    """Run the artifact-backed shadow only when its deployment guard is explicitly enabled."""

    require_evidence_shadow_enabled()
    extractor = ManagedEvidenceUnitExtractor(
        db=db,
        workflow=workflow,
        step=step,
        skill_version=skill_version,
        review_guidance=review_guidance,
        catalog=catalog,
        source_text=source_text,
        source_artifact_id=source_artifact_id,
        source_content_hash=source_content_hash,
        resolved_model=resolved_model,
        document_context=document_context,
        model=model,
        model_settings=model_settings,
    )
    artifact_repository = repository or WorkflowEvidenceShadowArtifactRepository(
        actor_user_id=workflow.initiated_by_user_id,
        producer_run_id=workflow.id,
    )
    return await execute_evidence_shadow(
        plan,
        repository=artifact_repository,
        extractor=extractor,
        retry_failed=retry_failed,
        max_attempts_per_unit=max_attempts_per_unit,
    )
