"""Managed optional narrative for admitted evidence-first observations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.evidence_adjudication_artifacts import CriterionDecisionsResult
from app.evidence_document_decision import DocumentDecisionResult
from app.evidence_first_artifacts import CompactedEvidenceObservations
from app.evidence_first_contracts import (
    NarrativeProjection,
    narrative_projection_schemas,
    validate_narrative_projection,
)
from app.evidence_first_managed import require_evidence_shadow_enabled
from app.evidence_narrative_projection import selected_narrative_observation_ids
from app.models import SkillDefinitionVersion, SkillRun, WorkflowRun, WorkflowStepRun
from app.skill_execution import execute_skill_run
from app.workflow_specs import MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW_SPEC

SkillRunner = Callable[..., Awaitable[tuple[dict[str, Any], SkillRun]]]


@dataclass
class ManagedEvidenceNarrator:
    db: Session
    workflow: WorkflowRun
    step: WorkflowStepRun
    skill_version: SkillDefinitionVersion
    observations: CompactedEvidenceObservations
    criterion_decisions: CriterionDecisionsResult
    document_decision: DocumentDecisionResult
    resolved_model: str
    model: Any | None = None
    skill_runner: SkillRunner = execute_skill_run

    def _validate_context(self) -> tuple[str, ...]:
        if self.workflow.workflow_key != MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW_SPEC.key:
            raise ValueError("Narrative must run in the isolated evidence narrative workflow")
        if self.step.workflow_run_id != self.workflow.id or self.step.role_key != "narrative":
            raise ValueError("Narrative step does not belong to the narrative workflow")
        if self.skill_version.input_schema_key != "matter_definition_evidence_narrative_input_v1":
            raise ValueError("Managed narrative skill uses an incompatible input contract")
        if self.skill_version.output_schema_key != "matter_definition_evidence_narrative_output_v1":
            raise ValueError("Managed narrative skill uses an incompatible output contract")
        if self.document_decision.decision is None:
            raise ValueError("Narrative requires a completed code-owned document decision")
        if (
            self.document_decision.criterion_decisions_result_hash
            != self.criterion_decisions.result_hash
        ):
            raise ValueError("Narrative inputs do not share the same criterion decisions")
        if self.criterion_decisions.observations_result_hash != self.observations.result_hash:
            raise ValueError("Narrative inputs do not share the same admitted observations")
        selected = selected_narrative_observation_ids(self.criterion_decisions)
        known = {value.observation_id for value in self.observations.observations}
        if not set(selected) <= known:
            raise ValueError("Narrative selection references observations absent from admitted evidence")
        return selected

    async def __call__(self) -> tuple[NarrativeProjection, SkillRun | None]:
        selected = self._validate_context()
        if not selected:
            return NarrativeProjection(), None
        by_id = {value.observation_id: value for value in self.observations.observations}
        admitted = [by_id[value] for value in selected]
        application_schema, provider_schema = narrative_projection_schemas(
            observation_ids=list(selected),
            provider=self.resolved_model.partition(":")[0],
        )

        def validate(value: dict[str, Any]) -> None:
            validate_narrative_projection(value, admitted_observation_ids=set(selected))

        output, skill_run = await self.skill_runner(
            self.db,
            workflow=self.workflow,
            step=self.step,
            skill_version=self.skill_version,
            scope_type="MATTER_DOCUMENT",
            scope_id=self.document_decision.matter_document_id,
            stable_context={
                "document_decision": self.document_decision.decision.model_dump(mode="json"),
                "criterion_decisions": [
                    value.model_dump(mode="json")
                    for value in self.criterion_decisions.criterion_decisions
                ],
            },
            dynamic_input={
                "admitted_observations": [value.model_dump(mode="json") for value in admitted]
            },
            cache_identity={
                "tenant_id": str(self.workflow.tenant_id),
                "matter_id": str(self.document_decision.matter_id),
                "matter_document_id": str(self.document_decision.matter_document_id),
                "observations_result_hash": self.observations.result_hash,
                "criterion_decisions_result_hash": self.criterion_decisions.result_hash,
                "document_decision_result_hash": self.document_decision.result_hash,
                "skill_version_id": str(self.skill_version.id),
            },
            output_validators=(validate,),
            output_schema=application_schema,
            provider_output_schema=provider_schema,
            limits=self.skill_version.limits,
            usage_job_type="MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW",
            model=self.model if self.model is not None else self.resolved_model,
        )
        return (
            validate_narrative_projection(output, admitted_observation_ids=set(selected)),
            skill_run,
        )


async def execute_managed_evidence_narrative(
    *,
    db: Session,
    workflow: WorkflowRun,
    step: WorkflowStepRun,
    skill_version: SkillDefinitionVersion,
    observations: CompactedEvidenceObservations,
    criterion_decisions: CriterionDecisionsResult,
    document_decision: DocumentDecisionResult,
    resolved_model: str,
    model: Any | None = None,
) -> tuple[NarrativeProjection, SkillRun | None]:
    require_evidence_shadow_enabled()
    return await ManagedEvidenceNarrator(
        db=db,
        workflow=workflow,
        step=step,
        skill_version=skill_version,
        observations=observations,
        criterion_decisions=criterion_decisions,
        document_decision=document_decision,
        resolved_model=resolved_model,
        model=model,
    )()
