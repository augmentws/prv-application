"""Deterministic document decisions derived from durable criterion decisions."""

from __future__ import annotations

import json
import uuid
from hashlib import sha256
from io import BytesIO
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifact_gateway import (
    WorkflowArtifactReference,
    copy_workflow_artifact_content,
    find_workflow_artifact_reference,
    store_workflow_artifact,
)
from app.evidence_adjudication_artifacts import (
    CriterionAdjudicationPlan,
    CriterionDecisionsResult,
)
from app.evidence_first_contracts import (
    UNMAPPED_CRITERION_KEY,
    DocumentDecision,
    FrozenCriterionCatalog,
    combine_positive_criterion_decisions,
)
from app.model_execution import content_hash
from artifact_service.schemas import WorkflowArtifactLineageInput
from artifact_service.workflow_artifact_types import ArtifactType

DOCUMENT_DECISION_RESULT_VERSION = "document-decision-result-v1"
DOCUMENT_DECISION_POLICY_VERSION = "positive-any-match-v2"
ANALYSIS_DOCUMENT_DECISION_SCHEMA_VERSION = 1


class DocumentDecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["document-decision-result-v1"] = DOCUMENT_DECISION_RESULT_VERSION
    policy_version: Literal["positive-any-match-v1", "positive-any-match-v2"] = DOCUMENT_DECISION_POLICY_VERSION
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    matter_id: uuid.UUID
    matter_document_id: uuid.UUID
    adjudication_plan_hash: str = Field(min_length=64, max_length=64)
    criterion_decisions_artifact_id: uuid.UUID
    criterion_decisions_content_hash: str = Field(min_length=64, max_length=64)
    criterion_decisions_result_hash: str = Field(min_length=64, max_length=64)
    criterion_catalog_hash: str = Field(min_length=64, max_length=64)
    evidence_complete: bool
    adjudication_complete: bool
    status: Literal["COMPLETED", "INSUFFICIENT", "MODEL_FALLBACK_REQUIRED"]
    decision: DocumentDecision | None = None
    fallback_criterion_keys: tuple[str, ...] = ()
    result_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_result(self) -> DocumentDecisionResult:
        if self.status == "MODEL_FALLBACK_REQUIRED":
            if self.decision is not None or not self.fallback_criterion_keys:
                raise ValueError("Model fallback requires unsupported criteria and no code-owned decision")
        elif self.decision is None or self.fallback_criterion_keys:
            raise ValueError("A deterministic terminal result requires one document decision")
        if self.status == "COMPLETED" and not self.adjudication_complete:
            raise ValueError("A completed document decision requires complete criterion adjudication")
        payload = self.model_dump(mode="json", exclude={"result_hash"})
        if content_hash(payload) != self.result_hash:
            raise ValueError("Document decision hash does not match its immutable content")
        return self


def build_document_decision_result(
    plan: CriterionAdjudicationPlan,
    criterion_result: CriterionDecisionsResult,
    *,
    criterion_decisions_artifact: WorkflowArtifactReference,
    catalog: FrozenCriterionCatalog,
) -> DocumentDecisionResult:
    if criterion_result.plan_hash != plan.plan_hash:
        raise ValueError("Criterion decisions do not belong to the adjudication plan")
    if criterion_result.observations_result_hash != plan.observations_result_hash:
        raise ValueError("Criterion decisions do not belong to the plan observations")
    if catalog.catalog_hash != plan.criterion_catalog_hash:
        raise ValueError("Criterion catalog does not match the adjudication plan")
    if criterion_decisions_artifact.artifact_type != ArtifactType.ANALYSIS_CRITERION_DECISIONS:
        raise ValueError("Document decisions require a criterion-decisions artifact")
    unsupported = tuple(
        sorted(
            criterion.criterion_key
            for criterion in catalog.criteria
            if criterion.criterion_key != UNMAPPED_CRITERION_KEY
            and criterion.decision_semantics != "POSITIVE"
        )
    )
    status: str
    decision: DocumentDecision | None
    if unsupported:
        status = "MODEL_FALLBACK_REQUIRED"
        decision = None
    elif not criterion_result.adjudication_complete:
        status = "INSUFFICIENT"
        decision = DocumentDecision(
            determination="UNCLEAR",
            confidence=0,
            contributing_criterion_keys=[],
            unresolved_criterion_keys=sorted(
                criterion.criterion_key
                for criterion in catalog.criteria
                if criterion.criterion_key != UNMAPPED_CRITERION_KEY
            ),
        )
    else:
        decision = combine_positive_criterion_decisions(
            criterion_result.criterion_decisions,
            catalog=catalog,
            coverage_complete=criterion_result.evidence_complete,
            has_unmapped_observations=criterion_result.unmapped_observation_count > 0,
        )
        status = "COMPLETED" if criterion_result.evidence_complete else "INSUFFICIENT"
    payload = {
        "version": DOCUMENT_DECISION_RESULT_VERSION,
        "policy_version": DOCUMENT_DECISION_POLICY_VERSION,
        "tenant_id": str(plan.tenant_id),
        "client_id": str(plan.client_id),
        "matter_id": str(plan.matter_id),
        "matter_document_id": str(plan.matter_document_id),
        "adjudication_plan_hash": plan.plan_hash,
        "criterion_decisions_artifact_id": str(criterion_decisions_artifact.artifact_id),
        "criterion_decisions_content_hash": criterion_decisions_artifact.content_hash,
        "criterion_decisions_result_hash": criterion_result.result_hash,
        "criterion_catalog_hash": catalog.catalog_hash,
        "evidence_complete": criterion_result.evidence_complete,
        "adjudication_complete": criterion_result.adjudication_complete,
        "status": status,
        "decision": decision.model_dump(mode="json") if decision else None,
        "fallback_criterion_keys": unsupported,
    }
    return DocumentDecisionResult(**payload, result_hash=content_hash(payload))


def document_decision_bytes(result: DocumentDecisionResult) -> bytes:
    return json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def load_document_decision_bytes(content: bytes) -> DocumentDecisionResult:
    return DocumentDecisionResult.model_validate_json(content)


def document_decision_derivation_key(result: DocumentDecisionResult) -> str:
    return content_hash(
        {
            "artifact_type": "ANALYSIS_DOCUMENT_DECISION",
            "policy_version": result.policy_version,
            "criterion_decisions_result_hash": result.criterion_decisions_result_hash,
            "result_hash": result.result_hash,
        }
    )


def store_document_decision_artifact(
    result: DocumentDecisionResult,
    *,
    producer_run_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowArtifactReference, bool]:
    content = document_decision_bytes(result)
    derivation_key = document_decision_derivation_key(result)
    existing = find_workflow_artifact_reference(
        artifact_type=ArtifactType.ANALYSIS_DOCUMENT_DECISION,
        derivation_key=derivation_key,
        actor_user_id=actor_user_id,
        tenant_id=result.tenant_id,
        client_id=result.client_id,
    )
    if existing is not None:
        destination = BytesIO()
        copy_workflow_artifact_content(
            artifact_id=existing.artifact_id,
            destination=destination,
            actor_user_id=actor_user_id,
            tenant_id=result.tenant_id,
            client_id=result.client_id,
        )
        if destination.getvalue() != content or existing.content_hash != sha256(content).hexdigest():
            raise ValueError("Existing document decision does not match its deterministic identity")
        return existing, False
    return store_workflow_artifact(
        content=BytesIO(content),
        artifact_type=ArtifactType.ANALYSIS_DOCUMENT_DECISION,
        core_scope_kind="MATTER",
        core_scope_id=result.matter_id,
        schema_version=ANALYSIS_DOCUMENT_DECISION_SCHEMA_VERSION,
        derivation_key=derivation_key,
        content_hash=sha256(content).hexdigest(),
        byte_length=len(content),
        media_type="application/json",
        producer_kind="CORE",
        producer_run_id=producer_run_id,
        artifact_metadata={
            "matter_document_id": str(result.matter_document_id),
            "result_hash": result.result_hash,
            "status": result.status,
            "determination": result.decision.determination if result.decision else None,
            "policy_version": result.policy_version,
        },
        lineage=[
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=result.criterion_decisions_artifact_id,
            )
        ],
        actor_user_id=actor_user_id,
        tenant_id=result.tenant_id,
        client_id=result.client_id,
        filename=f"{result.matter_document_id}-document-decision.json",
    )
