from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.document_evidence import build_document_map_plan, segment_paragraphs
from app.evidence_first_artifacts import (
    ANALYSIS_EVALUATION_PLAN_SCHEMA_VERSION,
    ANALYSIS_OBSERVATION_FRAGMENT_SCHEMA_VERSION,
    ANALYSIS_OBSERVATIONS_SCHEMA_VERSION,
    AnalysisRoleBinding,
    AnalysisRoleBudget,
    EvidenceEvaluationPlan,
    EvidenceExtractionUnitResult,
    EvidenceUnitAdjustment,
    build_evidence_evaluation_plan,
    compact_observation_results,
    compacted_observations_bytes,
    compacted_observations_derivation_key,
    evaluation_plan_bytes,
    evidence_observation_source_hash,
    load_compacted_observations_bytes,
    load_evaluation_plan_bytes,
    load_observation_fragment_bytes,
    observation_fragment_bytes,
    observation_fragment_derivation_key,
    store_compacted_observations_artifact,
    store_evaluation_plan_artifact,
    store_observation_fragment_artifact,
    validate_observation_fragment,
)
from app.evidence_first_contracts import EvidenceObservation, observation_id
from app.model_execution import content_hash
from artifact_service.workflow_artifact_types import ArtifactType
from artifact_service.workflow_artifacts import allowed_media_types, workflow_artifact_max_bytes


def _hash(value: str) -> str:
    return value * 64


def _plan(*, prompt_hash: str = _hash("2")):
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
        prompt_hash=prompt_hash,
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
    plan = build_evidence_evaluation_plan(
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
    return plan, paragraph_map


def test_evaluation_plan_is_deterministic_and_excludes_source_text() -> None:
    first, paragraph_map = _plan()
    second, _ = _plan()

    assert first == second
    assert first.plan_hash == second.plan_hash
    assert [unit.ordinal for unit in first.extraction_units] == list(
        range(1, len(first.extraction_units) + 1)
    )
    content = evaluation_plan_bytes(first)
    assert b"Alpha evidence" not in content
    assert load_evaluation_plan_bytes(content) == first
    assert min(
        segment.char_start
        for unit in first.extraction_units
        for segment in unit.segments
    ) == 0
    assert max(
        segment.char_end
        for unit in first.extraction_units
        for segment in unit.segments
    ) == paragraph_map.source_length


def test_evaluation_plan_loader_accepts_pre_context_legacy_hash() -> None:
    plan, _ = _plan()
    value = json.loads(evaluation_plan_bytes(plan))
    value.pop("document_context_hash")
    value.pop("document_context_citation_ids")
    legacy_payload = {key: item for key, item in value.items() if key != "plan_hash"}
    value["plan_hash"] = content_hash(legacy_payload)

    loaded = load_evaluation_plan_bytes(json.dumps(value).encode())

    assert loaded.plan_hash == value["plan_hash"]
    assert loaded.document_context_hash is None
    assert loaded.document_context_citation_ids == ()


def test_evaluation_plan_identity_changes_with_extraction_semantics() -> None:
    first, _ = _plan(prompt_hash=_hash("2"))
    second, _ = _plan(prompt_hash=_hash("9"))

    assert first.plan_hash != second.plan_hash
    assert [unit.unit_id for unit in first.extraction_units] != [
        unit.unit_id for unit in second.extraction_units
    ]


def test_evaluation_plan_rejects_tampered_content() -> None:
    plan, _ = _plan()
    value = json.loads(evaluation_plan_bytes(plan))
    value["max_unit_characters"] += 1

    with pytest.raises(ValidationError, match="hash does not match"):
        load_evaluation_plan_bytes(json.dumps(value).encode())


def test_evidence_first_artifact_types_enforce_expected_media() -> None:
    assert ANALYSIS_EVALUATION_PLAN_SCHEMA_VERSION == 1
    assert ANALYSIS_OBSERVATION_FRAGMENT_SCHEMA_VERSION == 2
    assert ANALYSIS_OBSERVATIONS_SCHEMA_VERSION == 1
    assert allowed_media_types(ArtifactType.ANALYSIS_EVALUATION_PLAN) == {"application/json"}
    assert allowed_media_types(ArtifactType.ANALYSIS_OBSERVATION_FRAGMENT) == {
        "application/vnd.apache.parquet",
        "application/vnd.apache.arrow.file",
        "application/vnd.apache.arrow.stream",
    }
    assert allowed_media_types(ArtifactType.ANALYSIS_OBSERVATIONS) == {
        "application/vnd.apache.parquet",
        "application/vnd.apache.arrow.file",
        "application/vnd.apache.arrow.stream",
    }
    assert workflow_artifact_max_bytes(ArtifactType.ANALYSIS_EVALUATION_PLAN) > 0


def _unit_result(
    *, adjusted: bool = False
) -> tuple[EvidenceEvaluationPlan, EvidenceExtractionUnitResult]:
    plan, _ = _plan()
    unit = plan.extraction_units[0]
    citation_ids = (unit.segments[0].paragraph_id,)
    fact = "The document contains a contract-drafting signal."
    observation = EvidenceObservation(
        observation_id=observation_id(
            source_hash=plan.source_content_hash,
            source_unit_id=unit.unit_id,
            criterion_key="draft_contracts_and_documents",
            signal_type="NEAR_MISS",
            fact=fact,
            citation_ids=citation_ids,
        ),
        source_unit_id=unit.unit_id,
        criterion_key="draft_contracts_and_documents",
        signal_type="NEAR_MISS",
        fact=fact,
        citation_ids=citation_ids,
    )
    started_at = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    adjustments = (
        (
            EvidenceUnitAdjustment(
                item_index=1,
                code="DUPLICATE_CITATIONS_REMOVED",
                policy="NORMALIZE",
                path="observations.1",
                detail="Removed a duplicate citation.",
            ),
        )
        if adjusted
        else ()
    )
    result = EvidenceExtractionUnitResult(
        plan_hash=plan.plan_hash,
        source_content_hash=plan.source_content_hash,
        unit_id=unit.unit_id,
        unit_ordinal=unit.ordinal,
        attempt=1,
        status="COMPLETED_WITH_ADJUSTMENTS" if adjusted else "COMPLETED",
        observations=(observation,),
        adjustments=adjustments,
        invocation_ids=(uuid.UUID(int=9),),
        started_at=started_at,
        completed_at=started_at + timedelta(seconds=2),
    )
    return plan, result


def _result_for_unit(
    plan: EvidenceEvaluationPlan,
    unit_ordinal: int,
    *,
    attempt: int = 1,
    status: str = "COMPLETED",
    adjustment_policy: str | None = None,
) -> EvidenceExtractionUnitResult:
    unit = plan.extraction_units[unit_ordinal - 1]
    started_at = datetime(2026, 11, 15, 12, unit_ordinal, tzinfo=timezone.utc)
    observations = ()
    if status in {"COMPLETED", "COMPLETED_WITH_ADJUSTMENTS"}:
        citation_ids = (unit.segments[0].paragraph_id,)
        fact = f"Evidence from extraction unit {unit_ordinal}."
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
    adjustments = ()
    if adjustment_policy is not None:
        adjustments = (
            EvidenceUnitAdjustment(
                item_index=0,
                code="TEST_ADJUSTMENT",
                policy=adjustment_policy,
                path="observations.0",
                detail="Test adjustment.",
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
        adjustments=adjustments,
        invocation_ids=(uuid.uuid5(uuid.NAMESPACE_URL, f"{unit.unit_id}:{attempt}"),),
        error_code="PROVIDER_ERROR" if status == "FAILED" else None,
        error_message="Provider request failed." if status == "FAILED" else None,
        started_at=started_at,
        completed_at=started_at + timedelta(seconds=attempt),
    )


def test_observation_fragment_round_trips_unit_results() -> None:
    plan, result = _unit_result(adjusted=True)

    content = observation_fragment_bytes(plan, [result])
    restored = load_observation_fragment_bytes(content)

    assert restored == (result,)
    assert observation_fragment_derivation_key(plan, [result]) == observation_fragment_derivation_key(
        plan, restored
    )


def test_observation_fragment_round_trips_retryable_failure() -> None:
    plan, _ = _plan()
    result = _result_for_unit(
        plan,
        1,
        status="FAILED",
    ).model_copy(update={"retryable": True})

    restored = load_observation_fragment_bytes(observation_fragment_bytes(plan, [result]))

    assert restored == (result,)
    assert restored[0].retryable is True


def test_observation_fragment_rejects_citations_outside_unit() -> None:
    plan, result = _unit_result()
    observation = result.observations[0].model_copy(update={"citation_ids": ("¶999",)})
    invalid = result.model_copy(update={"observations": (observation,)})

    with pytest.raises(ValueError, match="outside its extraction unit"):
        validate_observation_fragment(plan, [invalid])


def test_observation_identity_preserves_legacy_plans_without_document_context() -> None:
    plan, _ = _plan()

    assert evidence_observation_source_hash(
        source_content_hash=plan.source_content_hash,
        document_context_hash=plan.document_context_hash,
    ) == plan.source_content_hash


def test_observation_fragment_rejects_duplicate_unit_attempt() -> None:
    plan, result = _unit_result()

    with pytest.raises(ValueError, match="repeats an extraction unit attempt"):
        observation_fragment_bytes(plan, [result, result])


def test_compaction_selects_latest_attempt_and_round_trips() -> None:
    plan, _ = _plan()
    results = [
        _result_for_unit(plan, 1, attempt=1, status="FAILED"),
        _result_for_unit(plan, 1, attempt=2),
        *[
            _result_for_unit(plan, unit.ordinal)
            for unit in plan.extraction_units
            if unit.ordinal != 1
        ],
    ]
    results.append(results[-1])

    compacted = compact_observation_results(plan, results)
    restored = load_compacted_observations_bytes(compacted_observations_bytes(compacted))

    assert compacted.coverage_status == "COMPLETE"
    assert compacted.evidence_complete is True
    assert compacted.selected_attempts[0].attempt == 2
    assert len(compacted.selected_attempts) == len(plan.extraction_units)
    assert restored == compacted
    assert compacted_observations_derivation_key(restored) == (
        compacted_observations_derivation_key(compacted)
    )


def test_compaction_reports_exact_gaps_and_material_adjustments() -> None:
    plan, _ = _plan()
    partial = compact_observation_results(
        plan,
        [_result_for_unit(plan, 1)],
    )
    adjusted_results = [
        _result_for_unit(
            plan,
            unit.ordinal,
            status="COMPLETED_WITH_ADJUSTMENTS" if unit.ordinal == 1 else "COMPLETED",
            adjustment_policy="DROP_ITEM" if unit.ordinal == 1 else None,
        )
        for unit in plan.extraction_units
    ]
    adjusted = compact_observation_results(plan, adjusted_results)

    assert partial.coverage_status == "PARTIAL"
    assert partial.evidence_complete is False
    assert partial.missing_unit_ids
    assert partial.omitted_ranges
    assert adjusted.coverage_status == "COMPLETE"
    assert adjusted.evidence_complete is False
    assert adjusted.material_adjustment_count == 1


def test_compaction_rejects_conflicting_duplicate_attempts() -> None:
    plan, _ = _plan()
    first = _result_for_unit(plan, 1)
    original = first.observations[0]
    changed_fact = "Conflicting evidence for the same durable attempt."
    conflicting_observation = original.model_copy(
        update={
            "fact": changed_fact,
            "observation_id": observation_id(
                source_hash=plan.source_content_hash,
                source_unit_id=first.unit_id,
                criterion_key=original.criterion_key,
                signal_type=original.signal_type,
                fact=changed_fact,
                citation_ids=original.citation_ids,
            ),
        }
    )
    conflicting = first.model_copy(update={"observations": (conflicting_observation,)})

    with pytest.raises(ValueError, match="Conflicting observation fragments"):
        compact_observation_results(plan, [first, conflicting])


def test_plan_and_fragment_store_with_verified_lineage(monkeypatch) -> None:
    plan, result = _unit_result()
    stored: list[dict] = []

    def fake_store_workflow_artifact(**kwargs):
        stored.append(kwargs)
        return f"reference-{len(stored)}", True

    monkeypatch.setattr(
        "app.evidence_first_artifacts.store_workflow_artifact",
        fake_store_workflow_artifact,
    )
    producer_run_id = uuid.UUID(int=10)
    actor_user_id = uuid.UUID(int=11)

    plan_reference, plan_created = store_evaluation_plan_artifact(
        plan,
        producer_run_id=producer_run_id,
        actor_user_id=actor_user_id,
    )
    fragment_reference, fragment_created = store_observation_fragment_artifact(
        plan,
        plan_artifact_id=uuid.UUID(int=12),
        results=[result],
        producer_run_id=producer_run_id,
        actor_user_id=actor_user_id,
    )
    compacted = compact_observation_results(
        plan,
        [_result_for_unit(plan, unit.ordinal) for unit in plan.extraction_units],
    )
    observations_reference, observations_created = store_compacted_observations_artifact(
        plan,
        result=compacted,
        plan_artifact_id=uuid.UUID(int=12),
        fragment_artifact_ids=[uuid.UUID(int=13)],
        producer_run_id=producer_run_id,
        actor_user_id=actor_user_id,
    )

    assert (plan_reference, plan_created) == ("reference-1", True)
    assert (fragment_reference, fragment_created) == ("reference-2", True)
    assert (observations_reference, observations_created) == ("reference-3", True)
    assert stored[0]["artifact_type"] == ArtifactType.ANALYSIS_EVALUATION_PLAN
    assert stored[0]["derivation_key"] == plan.plan_hash
    assert stored[0]["lineage"][0].source_kind == "ARTIFACT"
    assert stored[0]["content"].read() == evaluation_plan_bytes(plan)
    assert stored[1]["artifact_type"] == ArtifactType.ANALYSIS_OBSERVATION_FRAGMENT
    assert stored[1]["schema_version"] == 2
    assert stored[1]["lineage"][0].source_kind == "WORKFLOW_ARTIFACT"
    assert load_observation_fragment_bytes(stored[1]["content"].read()) == (result,)
    assert stored[2]["artifact_type"] == ArtifactType.ANALYSIS_OBSERVATIONS
    assert [value.source_artifact_id for value in stored[2]["lineage"]] == [
        uuid.UUID(int=12),
        uuid.UUID(int=13),
    ]
    assert load_compacted_observations_bytes(stored[2]["content"].read()) == compacted
