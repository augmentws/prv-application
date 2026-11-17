"""Managed criterion adjudication for the isolated evidence-first shadow path."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence_adjudication_artifacts import (
    AdjudicationAdjustment,
    CriterionAdjudicationAttempt,
    CriterionAdjudicationPacket,
    CriterionAdjudicationPlan,
)
from app.evidence_adjudication_shadow import (
    AdjudicationShadowArtifactRepository,
    AdjudicationShadowExecutionOutcome,
    WorkflowAdjudicationShadowArtifactRepository,
    execute_adjudication_shadow,
)
from app.evidence_first_artifacts import CompactedEvidenceObservations
from app.evidence_first_contracts import (
    CriterionDecisionNormalizationCapture,
    FrozenCriterionCatalog,
    criterion_adjudication_schemas,
    criterion_decision_from_adjudication,
    validate_criterion_adjudication,
)
from app.evidence_first_managed import require_evidence_shadow_enabled
from app.models import ModelInvocation, SkillDefinitionVersion, SkillRun, WorkflowRun, WorkflowStepRun
from app.provider_usage import external_model_identity
from app.skill_execution import execute_skill_run
from app.workflow_specs import MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW_SPEC

SkillRunner = Callable[..., Awaitable[tuple[dict[str, Any], SkillRun]]]


def _invocation_ids(db: Session, skill_run_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
    return tuple(
        db.scalars(
            select(ModelInvocation.id)
            .where(ModelInvocation.skill_run_id == skill_run_id)
            .order_by(ModelInvocation.attempt, ModelInvocation.request_sequence)
        )
    )


@dataclass
class ManagedCriterionAdjudicator:
    db: Session
    workflow: WorkflowRun
    step: WorkflowStepRun
    skill_version: SkillDefinitionVersion
    review_guidance: str
    catalog: FrozenCriterionCatalog
    observations: CompactedEvidenceObservations
    resolved_model: str
    model: Any | None = None
    skill_runner: SkillRunner = execute_skill_run

    def _validate_context(
        self,
        plan: CriterionAdjudicationPlan,
        packet: CriterionAdjudicationPacket,
    ) -> None:
        if self.workflow.workflow_key != MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW_SPEC.key:
            raise ValueError("Criterion adjudication must run in the isolated adjudication workflow")
        if self.step.workflow_run_id != self.workflow.id or self.step.role_key != "criterion_adjudication":
            raise ValueError("Criterion adjudication step does not belong to the shadow workflow")
        if (
            self.workflow.tenant_id != plan.tenant_id
            or self.workflow.client_id != plan.client_id
            or self.workflow.matter_id != plan.matter_id
        ):
            raise ValueError("Adjudication workflow scope does not match its immutable plan")
        if plan.role_binding.skill_definition_version_id != self.skill_version.id:
            raise ValueError("Adjudication plan does not pin the supplied skill version")
        if plan.role_binding.model_key != self.resolved_model:
            raise ValueError("Adjudication plan does not pin the supplied model")
        identity = external_model_identity(self.resolved_model)
        if identity is None or identity[0] != plan.role_binding.provider:
            raise ValueError("Adjudication model does not match the pinned provider")
        if self.skill_version.input_schema_key != "matter_definition_criterion_adjudication_input_v1":
            raise ValueError("Managed adjudication skill uses an incompatible input contract")
        if self.skill_version.output_schema_key != "matter_definition_criterion_adjudication_output_v1":
            raise ValueError("Managed adjudication skill uses an incompatible output contract")
        if hashlib.sha256(self.review_guidance.encode()).hexdigest() != plan.guidance_content_hash:
            raise ValueError("Review guidance does not match the adjudication plan")
        if self.catalog.catalog_hash != plan.criterion_catalog_hash:
            raise ValueError("Criterion catalog does not match the adjudication plan")
        if self.observations.result_hash != plan.observations_result_hash:
            raise ValueError("Observations do not match the adjudication plan")
        if packet not in plan.packets or packet.deterministic_without_model:
            raise ValueError("Managed adjudication requires one non-empty planned packet")

    async def __call__(
        self,
        plan: CriterionAdjudicationPlan,
        packet: CriterionAdjudicationPacket,
        attempt: int,
    ) -> CriterionAdjudicationAttempt:
        self._validate_context(plan, packet)
        criterion = next(
            value for value in self.catalog.criteria if value.criterion_key == packet.criterion_key
        )
        by_id = {value.observation_id: value for value in self.observations.observations}
        evidence_packet = [by_id[value] for value in packet.observation_ids]
        application_schema, provider_schema = criterion_adjudication_schemas(
            criterion_key=packet.criterion_key,
            observation_ids=list(packet.observation_ids),
            provider=plan.role_binding.provider,
        )
        normalization = CriterionDecisionNormalizationCapture(
            admitted_observation_ids=set(packet.observation_ids)
        )

        def validate(value: dict[str, Any]) -> None:
            validate_criterion_adjudication(
                value,
                criterion_key=packet.criterion_key,
                admitted_observation_ids=set(packet.observation_ids),
            )

        _output, skill_run = await self.skill_runner(
            self.db,
            workflow=self.workflow,
            step=self.step,
            skill_version=self.skill_version,
            scope_type="MATTER_DOCUMENT",
            scope_id=plan.matter_document_id,
            stable_context={
                "review_guidance": self.review_guidance,
                "criterion": criterion.model_dump(mode="json"),
            },
            dynamic_input={
                "evidence_packet": [value.model_dump(mode="json") for value in evidence_packet]
            },
            cache_identity={
                "tenant_id": str(plan.tenant_id),
                "matter_id": str(plan.matter_id),
                "criterion_key": packet.criterion_key,
                "skill_version_id": str(self.skill_version.id),
            },
            output_normalizers=(normalization,),
            output_validators=(validate,),
            output_schema=application_schema,
            provider_output_schema=provider_schema,
            limits=self.skill_version.limits,
            usage_job_type="MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW",
            model=self.model if self.model is not None else self.resolved_model,
        )
        adjudication = validate_criterion_adjudication(
            _output,
            criterion_key=packet.criterion_key,
            admitted_observation_ids=set(packet.observation_ids),
        )
        decision = criterion_decision_from_adjudication(adjudication)
        adjustments = tuple(AdjudicationAdjustment.model_validate(value) for value in normalization.adjustments)
        now = datetime.now(timezone.utc)
        return CriterionAdjudicationAttempt(
            plan_hash=plan.plan_hash,
            unit_id=packet.unit_id,
            unit_ordinal=packet.ordinal,
            criterion_key=packet.criterion_key,
            attempt=attempt,
            status="COMPLETED_WITH_ADJUSTMENTS" if adjustments else "COMPLETED",
            adjudication=adjudication,
            decision=decision,
            adjustments=adjustments,
            invocation_ids=_invocation_ids(self.db, skill_run.id),
            started_at=skill_run.started_at or now,
            completed_at=skill_run.completed_at or now,
        )


async def execute_managed_adjudication_shadow(
    plan: CriterionAdjudicationPlan,
    *,
    db: Session,
    workflow: WorkflowRun,
    step: WorkflowStepRun,
    skill_version: SkillDefinitionVersion,
    review_guidance: str,
    catalog: FrozenCriterionCatalog,
    observations: CompactedEvidenceObservations,
    resolved_model: str,
    retry_failed: bool = False,
    max_attempts_per_unit: int = 3,
    model: Any | None = None,
    repository: AdjudicationShadowArtifactRepository | None = None,
) -> AdjudicationShadowExecutionOutcome:
    require_evidence_shadow_enabled()
    adjudicator = ManagedCriterionAdjudicator(
        db=db,
        workflow=workflow,
        step=step,
        skill_version=skill_version,
        review_guidance=review_guidance,
        catalog=catalog,
        observations=observations,
        resolved_model=resolved_model,
        model=model,
    )
    artifact_repository = repository or WorkflowAdjudicationShadowArtifactRepository(
        actor_user_id=workflow.initiated_by_user_id,
        producer_run_id=workflow.id,
    )
    return await execute_adjudication_shadow(
        plan,
        repository=artifact_repository,
        adjudicator=adjudicator,
        retry_failed=retry_failed,
        max_attempts_per_unit=max_attempts_per_unit,
    )
