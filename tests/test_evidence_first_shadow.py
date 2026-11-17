from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from app.artifact_gateway import WorkflowArtifactReference
from app.document_evidence import build_document_map_plan, segment_paragraphs
from app.evidence_first_artifacts import (
    AnalysisRoleBinding,
    AnalysisRoleBudget,
    CompactedEvidenceObservations,
    EvidenceEvaluationPlan,
    EvidenceExtractionUnitResult,
    build_evidence_evaluation_plan,
    observation_fragment_attempt_derivation_key,
    observation_fragment_derivation_key,
)
from app.evidence_first_contracts import EvidenceObservation, observation_id
from app.evidence_first_shadow import compact_evidence_shadow, execute_evidence_shadow, execute_evidence_unit
from artifact_service.workflow_artifact_types import ArtifactType


def _hash(value: str) -> str:
    return value * 64


def _plan() -> EvidenceEvaluationPlan:
    paragraph_map = segment_paragraphs(
        "Alpha evidence belongs to the first paragraph.\n\n"
        "Beta evidence belongs to the second paragraph.\n\n"
        "Gamma evidence belongs to the third paragraph."
    )
    document_map_plan = build_document_map_plan(
        paragraph_map,
        max_characters=100,
        overlap_segments=1,
    )
    binding = AnalysisRoleBinding(
        role_key="evidence_extraction",
        skill_definition_version_id=uuid.UUID(int=8),
        provider="google",
        model_key="google:gemini-3.5-flash-lite",
        prompt_hash=_hash("2"),
        application_schema_hash=_hash("3"),
        provider_schema_hash=_hash("4"),
        model_settings_hash=_hash("5"),
        capability_profile_hash=_hash("6"),
    )
    budget = AnalysisRoleBudget(
        role_key="evidence_extraction",
        max_requests=len(document_map_plan.windows),
        repair_request_reserve=0,
        max_input_tokens=100_000,
        max_output_tokens=10_000,
        max_elapsed_seconds=600,
    )
    return build_evidence_evaluation_plan(
        tenant_id=uuid.UUID(int=1),
        client_id=uuid.UUID(int=2),
        matter_id=uuid.UUID(int=3),
        matter_document_id=uuid.UUID(int=4),
        collection_item_id=uuid.UUID(int=5),
        source_artifact_id=uuid.UUID(int=6),
        source_content_hash=_hash("1"),
        task_version_key="matter-definition:evidence-first-v1",
        guidance_revision_id=uuid.UUID(int=7),
        guidance_content_hash=_hash("7"),
        criterion_catalog_hash=_hash("8"),
        paragraph_map=paragraph_map,
        document_map_plan=document_map_plan,
        role_bindings=(binding,),
        role_budgets=(budget,),
    )


def _unit_result(
    plan: EvidenceEvaluationPlan,
    unit_ordinal: int,
    *,
    attempt: int,
    status: str = "COMPLETED",
    retryable: bool = False,
) -> EvidenceExtractionUnitResult:
    unit = plan.extraction_units[unit_ordinal - 1]
    started_at = datetime(2026, 11, 15, 12, unit_ordinal, tzinfo=timezone.utc)
    observations = ()
    if status == "COMPLETED":
        fact = f"Evidence from extraction unit {unit_ordinal}."
        citation_ids = (unit.segments[0].paragraph_id,)
        observations = (
            EvidenceObservation(
                observation_id=observation_id(
                    source_hash=plan.source_content_hash,
                    source_unit_id=unit.unit_id,
                    criterion_key="draft_contracts_and_documents",
                    signal_type="SUPPORT",
                    fact=fact,
                    citation_ids=citation_ids,
                ),
                source_unit_id=unit.unit_id,
                criterion_key="draft_contracts_and_documents",
                signal_type="SUPPORT",
                fact=fact,
                citation_ids=citation_ids,
            ),
        )
    return EvidenceExtractionUnitResult(
        plan_hash=plan.plan_hash,
        source_content_hash=plan.source_content_hash,
        unit_id=unit.unit_id,
        unit_ordinal=unit.ordinal,
        attempt=attempt,
        status=status,
        observations=observations,
        invocation_ids=(uuid.uuid5(uuid.NAMESPACE_URL, f"{unit.unit_id}:{attempt}"),),
        error_code="PROVIDER_ERROR" if status == "FAILED" else None,
        error_message="Provider request failed." if status == "FAILED" else None,
        retryable=retryable,
        started_at=started_at,
        completed_at=started_at + timedelta(seconds=attempt),
    )


class MemoryShadowRepository:
    def __init__(self) -> None:
        self._next_id = 100
        self.plan_reference: WorkflowArtifactReference | None = None
        self.attempts: dict[
            tuple[str, int], tuple[EvidenceExtractionUnitResult, WorkflowArtifactReference]
        ] = {}
        self.compacted: list[CompactedEvidenceObservations] = []

    def _reference(
        self,
        plan: EvidenceEvaluationPlan,
        artifact_type: ArtifactType,
        derivation_key: str,
    ) -> WorkflowArtifactReference:
        self._next_id += 1
        return WorkflowArtifactReference(
            artifact_id=uuid.UUID(int=self._next_id),
            tenant_id=plan.tenant_id,
            client_id=plan.client_id,
            core_scope_kind="MATTER",
            core_scope_id=plan.matter_id,
            artifact_type=artifact_type,
            schema_version=1,
            derivation_key=derivation_key,
            content_hash=_hash("a"),
            media_type="application/vnd.apache.parquet",
            byte_length=1,
            producer_kind="CORE",
            producer_run_id=uuid.UUID(int=90),
            metadata={},
            created_at=datetime(2026, 11, 15, tzinfo=timezone.utc),
        )

    def persist_plan(self, plan: EvidenceEvaluationPlan) -> WorkflowArtifactReference:
        if self.plan_reference is None:
            self.plan_reference = self._reference(
                plan,
                ArtifactType.ANALYSIS_EVALUATION_PLAN,
                plan.plan_hash,
            )
        return self.plan_reference

    def load_attempt(self, plan, unit, attempt):
        return self.attempts.get((unit.unit_id, attempt))

    def persist_attempt(self, plan, plan_artifact_id, result):
        key = (result.unit_id, result.attempt)
        reference = self._reference(
            plan,
            ArtifactType.ANALYSIS_OBSERVATION_FRAGMENT,
            observation_fragment_attempt_derivation_key(
                plan,
                unit_id=result.unit_id,
                attempt=result.attempt,
            ),
        )
        self.attempts[key] = (result, reference)
        return reference

    def persist_compacted(
        self,
        plan,
        plan_artifact_id,
        fragment_artifact_ids,
        result,
    ):
        self.compacted.append(result)
        return self._reference(
            plan,
            ArtifactType.ANALYSIS_OBSERVATIONS,
            result.result_hash,
        )


def test_shadow_execution_reuses_completed_unit_attempts_on_restart() -> None:
    plan = _plan()
    repository = MemoryShadowRepository()
    calls: list[tuple[str, int]] = []

    async def extract(_plan, unit, attempt):
        calls.append((unit.unit_id, attempt))
        return _unit_result(plan, unit.ordinal, attempt=attempt)

    first = asyncio.run(
        execute_evidence_shadow(plan, repository=repository, extractor=extract)
    )
    first_calls = tuple(calls)
    calls.clear()
    second = asyncio.run(
        execute_evidence_shadow(plan, repository=repository, extractor=extract)
    )

    assert len(first_calls) == len(plan.extraction_units)
    assert first.compacted.evidence_complete is True
    assert calls == []
    assert second.executed_unit_ids == ()
    assert second.reused_unit_ids == tuple(unit.unit_id for unit in plan.extraction_units)
    assert second.compacted.result_hash == first.compacted.result_hash


def test_independent_unit_execution_compacts_only_after_all_units_arrive() -> None:
    plan = _plan()
    repository = MemoryShadowRepository()

    async def extract(_plan, unit, attempt):
        return _unit_result(plan, unit.ordinal, attempt=attempt)

    first = asyncio.run(
        execute_evidence_unit(
            plan,
            plan.extraction_units[0],
            repository=repository,
            extractor=extract,
        )
    )
    partial = compact_evidence_shadow(plan, repository=repository)
    for unit in plan.extraction_units[1:]:
        asyncio.run(
            execute_evidence_unit(
                plan,
                unit,
                repository=repository,
                extractor=extract,
            )
        )
    complete = compact_evidence_shadow(plan, repository=repository)

    assert first.executed is True
    assert partial.compacted.evidence_complete is False
    assert complete.compacted.evidence_complete is True
    assert complete.reused_unit_ids == tuple(unit.unit_id for unit in plan.extraction_units)


def test_shadow_execution_retries_only_failed_units_when_requested() -> None:
    plan = _plan()
    repository = MemoryShadowRepository()
    failed_unit_id = plan.extraction_units[0].unit_id
    calls: list[tuple[str, int]] = []

    async def fail_one(_plan, unit, attempt):
        calls.append((unit.unit_id, attempt))
        status = "FAILED" if unit.unit_id == failed_unit_id else "COMPLETED"
        return _unit_result(plan, unit.ordinal, attempt=attempt, status=status)

    first = asyncio.run(
        execute_evidence_shadow(plan, repository=repository, extractor=fail_one)
    )
    calls.clear()
    unchanged = asyncio.run(
        execute_evidence_shadow(plan, repository=repository, extractor=fail_one)
    )

    async def repair(_plan, unit, attempt):
        calls.append((unit.unit_id, attempt))
        return _unit_result(plan, unit.ordinal, attempt=attempt)

    repaired = asyncio.run(
        execute_evidence_shadow(
            plan,
            repository=repository,
            extractor=repair,
            retry_failed=True,
        )
    )

    assert first.compacted.evidence_complete is False
    assert unchanged.compacted.result_hash == first.compacted.result_hash
    assert calls == [(failed_unit_id, 2)]
    assert repaired.executed_unit_ids == (failed_unit_id,)
    assert repaired.compacted.evidence_complete is True
    assert repository.attempts[(failed_unit_id, 1)][0].status == "FAILED"
    assert repository.attempts[(failed_unit_id, 2)][0].status == "COMPLETED"


def test_shadow_execution_automatically_retries_retryable_unit_failure() -> None:
    plan = _plan()
    repository = MemoryShadowRepository()
    failed_unit_id = plan.extraction_units[0].unit_id
    calls: list[tuple[str, int]] = []

    class TemporaryFailure(RuntimeError):
        code = "MODEL_FAILURE"
        retryable = True

    async def extract(_plan, unit, attempt):
        calls.append((unit.unit_id, attempt))
        if unit.unit_id == failed_unit_id and attempt == 1:
            raise TemporaryFailure("connection interrupted")
        return _unit_result(plan, unit.ordinal, attempt=attempt)

    outcome = asyncio.run(
        execute_evidence_shadow(plan, repository=repository, extractor=extract)
    )

    assert outcome.compacted.evidence_complete is True
    assert calls[:2] == [(failed_unit_id, 1), (failed_unit_id, 2)]
    failed = repository.attempts[(failed_unit_id, 1)][0]
    assert failed.status == "FAILED"
    assert failed.error_code == "MODEL_FAILURE"
    assert failed.retryable is True
    assert repository.attempts[(failed_unit_id, 2)][0].status == "COMPLETED"


def test_shadow_restart_resumes_persisted_retryable_unit_without_operator_flag() -> None:
    plan = _plan()
    repository = MemoryShadowRepository()
    failed_unit_id = plan.extraction_units[0].unit_id

    async def fail_first(_plan, unit, attempt):
        if unit.unit_id == failed_unit_id:
            return _unit_result(
                plan,
                unit.ordinal,
                attempt=attempt,
                status="FAILED",
                retryable=True,
            )
        return _unit_result(plan, unit.ordinal, attempt=attempt)

    first = asyncio.run(
        execute_evidence_shadow(
            plan,
            repository=repository,
            extractor=fail_first,
            max_attempts_per_unit=1,
        )
    )
    calls: list[tuple[str, int]] = []

    async def repair(_plan, unit, attempt):
        calls.append((unit.unit_id, attempt))
        return _unit_result(plan, unit.ordinal, attempt=attempt)

    resumed = asyncio.run(
        execute_evidence_shadow(
            plan,
            repository=repository,
            extractor=repair,
            max_attempts_per_unit=3,
        )
    )

    assert first.compacted.evidence_complete is False
    assert calls == [(failed_unit_id, 2)]
    assert resumed.compacted.evidence_complete is True


def test_shadow_execution_persists_invalid_extractor_output_as_failed_attempt() -> None:
    plan = _plan()
    repository = MemoryShadowRepository()

    async def invalid(_plan, unit, attempt):
        other = (
            plan.extraction_units[-1]
            if unit.ordinal != plan.extraction_units[-1].ordinal
            else plan.extraction_units[0]
        )
        return _unit_result(plan, other.ordinal, attempt=attempt)

    outcome = asyncio.run(
        execute_evidence_shadow(
            plan,
            repository=repository,
            extractor=invalid,
            max_attempts_per_unit=1,
        )
    )

    assert outcome.compacted.evidence_complete is False
    assert outcome.compacted.coverage_status == "PARTIAL"
    assert all(
        result.error_code == "SHADOW_EXTRACTION_INVALID_RESULT"
        for result, _reference in repository.attempts.values()
    )


def test_single_attempt_lookup_key_matches_fragment_derivation() -> None:
    plan = _plan()
    result = _unit_result(plan, 1, attempt=1)

    assert observation_fragment_attempt_derivation_key(
        plan,
        unit_id=result.unit_id,
        attempt=result.attempt,
    ) == observation_fragment_derivation_key(plan, [result])
