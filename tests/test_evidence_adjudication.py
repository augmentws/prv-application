from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.artifact_gateway import WorkflowArtifactReference
from app.evidence_adjudication_artifacts import (
    ANALYSIS_ADJUDICATION_PLAN_SCHEMA_VERSION,
    ANALYSIS_CRITERION_ADJUDICATION_FRAGMENT_SCHEMA_VERSION,
    ANALYSIS_CRITERION_DECISIONS_SCHEMA_VERSION,
    AdjudicationAdjustment,
    CriterionAdjudicationAttempt,
    CriterionAdjudicationPlan,
    CriterionDecisionsResult,
    adjudication_plan_bytes,
    build_adjudication_plan,
    compact_adjudication_attempts,
    criterion_decisions_bytes,
    deterministic_empty_attempt,
)
from app.evidence_adjudication_launcher import (
    PreparedAdjudicationShadowLaunch,
    adjudication_plan_report,
)
from app.evidence_adjudication_managed import ManagedCriterionAdjudicator
from app.evidence_adjudication_shadow import (
    WorkflowAdjudicationShadowArtifactRepository,
    execute_adjudication_shadow,
)
from app.evidence_compatibility_projection import (
    ANALYSIS_COMPATIBILITY_RESULT_SCHEMA_VERSION,
    apply_narrative_projection,
    build_evidence_compatibility_projection,
    compatibility_projection_bytes,
    load_compatibility_projection_bytes,
)
from app.evidence_document_decision import (
    ANALYSIS_DOCUMENT_DECISION_SCHEMA_VERSION,
    build_document_decision_result,
    document_decision_bytes,
    load_document_decision_bytes,
)
from app.evidence_first_artifacts import (
    AnalysisRoleBinding,
    CompactedEvidenceObservations,
)
from app.evidence_first_contracts import (
    AggregatedEvidenceObservation,
    CriterionAdjudicationOutput,
    CriterionDecision,
    NarrativeProjection,
    freeze_criterion_catalog,
)
from app.evidence_narrative_launcher import (
    PreparedNarrativeShadowLaunch,
    execute_prepared_narrative_shadow,
)
from app.evidence_narrative_managed import ManagedEvidenceNarrator
from app.evidence_narrative_projection import (
    ANALYSIS_NARRATIVE_PROJECTION_SCHEMA_VERSION,
    build_evidence_narrative_result,
    load_narrative_result_bytes,
    narrative_result_bytes,
)
from app.model_execution import content_hash
from app.workflow_specs import (
    MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW_SPEC,
    MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW_SPEC,
)
from artifact_service.workflow_artifact_types import ArtifactType
from artifact_service.workflow_artifacts import allowed_media_types, workflow_artifact_max_bytes


def _hash(value: str) -> str:
    return value * 64


def _observations(*, complete: bool = True) -> CompactedEvidenceObservations:
    observation = AggregatedEvidenceObservation(
        observation_id=_hash("a"),
        criterion_key="attorney_client_privilege",
        signal_type="SUPPORT",
        fact="A client requests legal advice from counsel.",
        citation_ids=("¶1",),
        source_unit_ids=(_hash("b"),),
    )
    payload = {
        "version": "analysis-observations-v1",
        "plan_hash": _hash("1"),
        "source_content_hash": _hash("2"),
        "coverage_status": "COMPLETE" if complete else "PARTIAL",
        "evidence_complete": complete,
        "selected_attempts": [],
        "missing_unit_ids": [] if complete else [_hash("c")],
        "omitted_ranges": (
            []
            if complete
            else [{"paragraph_id": "¶2", "char_start": 10, "char_end": 20}]
        ),
        "material_adjustment_count": 0,
        "observations": [observation.model_dump(mode="json")],
    }
    return CompactedEvidenceObservations(**payload, result_hash=content_hash(payload))


def _artifact_reference(artifact_type: ArtifactType, *, artifact_id: int = 50):
    return WorkflowArtifactReference(
        artifact_id=uuid.UUID(int=artifact_id),
        tenant_id=uuid.UUID(int=1),
        client_id=uuid.UUID(int=2),
        core_scope_kind="MATTER",
        core_scope_id=uuid.UUID(int=3),
        artifact_type=artifact_type,
        schema_version=1,
        derivation_key=_hash("d"),
        content_hash=_hash("e"),
        media_type="application/json",
        byte_length=1,
        producer_kind="CORE",
        producer_run_id=uuid.UUID(int=40),
        metadata={},
        created_at=datetime(2026, 11, 15, tzinfo=timezone.utc),
    )


def _plan(
    *,
    complete: bool = True,
    second_semantics: str = "POSITIVE",
) -> tuple[CriterionAdjudicationPlan, CompactedEvidenceObservations]:
    observations = _observations(complete=complete)
    catalog = freeze_criterion_catalog(
        [
            {
                "criterion_key": "attorney_client_privilege",
                "display_label": "Attorney-client privilege",
                "instruction_text": "Identify confidential requests for legal advice.",
            },
            {
                "criterion_key": "work_product",
                "display_label": "Work product",
                "instruction_text": "Identify material prepared in anticipation of litigation.",
                "decision_semantics": second_semantics,
            },
        ]
    )
    binding = AnalysisRoleBinding(
        role_key="criterion_adjudication",
        skill_definition_version_id=uuid.UUID(int=8),
        provider="google",
        model_key="google:gemini-3.7-flash",
        prompt_hash=_hash("3"),
        application_schema_hash=_hash("4"),
        provider_schema_hash=_hash("5"),
        model_settings_hash=_hash("6"),
        capability_profile_hash=_hash("7"),
    )
    packet_hashes = {
        ("attorney_client_privilege", (_hash("a"),)): (_hash("8"), _hash("9")),
        ("work_product", ()): (_hash("8"), _hash("9")),
    }
    return (
        build_adjudication_plan(
            extraction_plan_hash=observations.plan_hash,
            observations_artifact=_artifact_reference(ArtifactType.ANALYSIS_OBSERVATIONS),
            observations=observations,
            catalog=catalog,
            guidance_revision_id=uuid.UUID(int=7),
            guidance_content_hash=hashlib.sha256(b"guidance").hexdigest(),
            role_binding=binding,
            packet_schema_hashes=packet_hashes,
            tenant_id=uuid.UUID(int=1),
            client_id=uuid.UUID(int=2),
            matter_id=uuid.UUID(int=3),
            matter_document_id=uuid.UUID(int=4),
        ),
        observations,
    )


def _evaluation_plan(
    adjudication_plan: CriterionAdjudicationPlan,
    observations: CompactedEvidenceObservations,
) -> SimpleNamespace:
    return SimpleNamespace(
        version="analysis-evaluation-plan-v1",
        plan_hash=observations.plan_hash,
        tenant_id=adjudication_plan.tenant_id,
        client_id=adjudication_plan.client_id,
        matter_id=adjudication_plan.matter_id,
        matter_document_id=adjudication_plan.matter_document_id,
        paragraph_map_version="paragraph-map-v1",
        extraction_units=(
            SimpleNamespace(
                unit_id=_hash("b"),
                segments=(SimpleNamespace(paragraph_id="¶1"),),
            ),
        ),
    )


def _model_attempt(
    plan: CriterionAdjudicationPlan,
    *,
    attempt: int = 1,
    status: str = "COMPLETED",
    retryable: bool = False,
) -> CriterionAdjudicationAttempt:
    packet = plan.packets[0]
    started = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    if status == "FAILED":
        return CriterionAdjudicationAttempt(
            plan_hash=plan.plan_hash,
            unit_id=packet.unit_id,
            unit_ordinal=packet.ordinal,
            criterion_key=packet.criterion_key,
            attempt=attempt,
            status="FAILED",
            error_code="PROVIDER_ERROR",
            error_message="ProviderError",
            retryable=retryable,
            started_at=started,
            completed_at=started + timedelta(seconds=1),
        )
    adjudication = CriterionAdjudicationOutput(
        criterion_key=packet.criterion_key,
        relationship="MATCH",
        confidence=0.9,
        primary_observation_id=packet.observation_ids[0],
        additional_observation_id_1=None,
        additional_observation_id_2=None,
        additional_observation_id_3=None,
        additional_observation_id_4=None,
        boundary_explanation="The evidence contains a confidential request for legal advice.",
    )
    decision = CriterionDecision(
        criterion_key=packet.criterion_key,
        disposition="MATCH",
        confidence=0.9,
        primary_observation_id=packet.observation_ids[0],
        additional_observation_id_1=None,
        additional_observation_id_2=None,
        additional_observation_id_3=None,
        additional_observation_id_4=None,
    )
    return CriterionAdjudicationAttempt(
        plan_hash=plan.plan_hash,
        unit_id=packet.unit_id,
        unit_ordinal=packet.ordinal,
        criterion_key=packet.criterion_key,
        attempt=attempt,
        status="COMPLETED",
        adjudication=adjudication,
        decision=decision,
        invocation_ids=(uuid.UUID(int=30),),
        started_at=started,
        completed_at=started + timedelta(seconds=1),
    )


class MemoryAdjudicationRepository:
    def __init__(self) -> None:
        self.next_id = 100
        self.attempts: dict[tuple[str, int], tuple[CriterionAdjudicationAttempt, WorkflowArtifactReference]] = {}
        self.results: list[CriterionDecisionsResult] = []

    def _reference(self, plan, artifact_type):
        self.next_id += 1
        reference = _artifact_reference(artifact_type, artifact_id=self.next_id)
        return WorkflowArtifactReference(
            **{
                **reference.__dict__,
                "tenant_id": plan.tenant_id,
                "client_id": plan.client_id,
                "core_scope_id": plan.matter_id,
            }
        )

    def persist_plan(self, plan):
        return self._reference(plan, ArtifactType.ANALYSIS_ADJUDICATION_PLAN)

    def load_attempt(self, _plan, packet, attempt):
        return self.attempts.get((packet.unit_id, attempt))

    def persist_attempt(self, plan, _plan_artifact_id, result):
        reference = self._reference(
            plan, ArtifactType.ANALYSIS_CRITERION_ADJUDICATION_FRAGMENT
        )
        self.attempts[(result.unit_id, result.attempt)] = (result, reference)
        return reference

    def persist_result(self, plan, _plan_artifact_id, _fragment_artifact_ids, result):
        self.results.append(result)
        return self._reference(plan, ArtifactType.ANALYSIS_CRITERION_DECISIONS)


def test_plan_uses_model_only_for_nonempty_criteria_and_is_deterministic() -> None:
    first, _observations_value = _plan()
    second, _ = _plan()

    assert first == second
    assert [packet.criterion_key for packet in first.packets] == [
        "attorney_client_privilege",
        "work_product",
    ]
    assert first.packets[0].deterministic_without_model is False
    assert first.packets[1].deterministic_without_model is True
    assert first.packets[1].observation_ids == ()


def test_empty_packet_is_no_match_only_when_evidence_is_complete() -> None:
    complete, _ = _plan(complete=True)
    incomplete, _ = _plan(complete=False)
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)

    complete_attempt = deterministic_empty_attempt(complete, complete.packets[1], attempt=1, now=now)
    incomplete_attempt = deterministic_empty_attempt(
        incomplete, incomplete.packets[1], attempt=1, now=now
    )

    assert complete_attempt.decision.disposition == "NO_MATCH"
    assert incomplete_attempt.decision.disposition == "INSUFFICIENT"
    assert incomplete_attempt.decision.unresolved_boundary == "Source evidence is incomplete."


def test_compaction_requires_every_packet_and_preserves_provenance() -> None:
    plan, _ = _plan()
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    result = compact_adjudication_attempts(
        plan,
        [_model_attempt(plan), deterministic_empty_attempt(plan, plan.packets[1], attempt=1, now=now)],
    )

    assert result.adjudication_complete is True
    assert [decision.disposition for decision in result.criterion_decisions] == [
        "MATCH",
        "NO_MATCH",
    ]
    assert result.criterion_decisions[0].primary_observation_id == _hash("a")


def test_adjudication_artifact_types_are_bounded_json() -> None:
    assert ANALYSIS_ADJUDICATION_PLAN_SCHEMA_VERSION == 1
    assert ANALYSIS_CRITERION_ADJUDICATION_FRAGMENT_SCHEMA_VERSION == 1
    assert ANALYSIS_CRITERION_DECISIONS_SCHEMA_VERSION == 1
    for artifact_type in (
        ArtifactType.ANALYSIS_ADJUDICATION_PLAN,
        ArtifactType.ANALYSIS_CRITERION_ADJUDICATION_FRAGMENT,
        ArtifactType.ANALYSIS_CRITERION_DECISIONS,
    ):
        assert allowed_media_types(artifact_type) == {"application/json"}
        assert workflow_artifact_max_bytes(artifact_type) > 0


def test_code_owned_document_decision_uses_positive_any_match_policy() -> None:
    plan, _ = _plan()
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    criterion_result = compact_adjudication_attempts(
        plan,
        [_model_attempt(plan), deterministic_empty_attempt(plan, plan.packets[1], attempt=1, now=now)],
    )
    catalog = freeze_criterion_catalog(
        [
            {
                "criterion_key": "attorney_client_privilege",
                "display_label": "Attorney-client privilege",
                "instruction_text": "Identify confidential requests for legal advice.",
            },
            {
                "criterion_key": "work_product",
                "display_label": "Work product",
                "instruction_text": "Identify material prepared in anticipation of litigation.",
            },
        ]
    )
    artifact = _artifact_reference(ArtifactType.ANALYSIS_CRITERION_DECISIONS)

    result = build_document_decision_result(
        plan,
        criterion_result,
        criterion_decisions_artifact=artifact,
        catalog=catalog,
    )

    assert result.status == "COMPLETED"
    assert result.decision.determination == "RESPONSIVE"
    assert result.decision.contributing_criterion_keys == ["attorney_client_privilege"]
    assert load_document_decision_bytes(document_decision_bytes(result)) == result

    legacy_payload = result.model_dump(mode="json", exclude={"result_hash"})
    legacy_payload["policy_version"] = "positive-any-match-v1"
    legacy_result = load_document_decision_bytes(
        json.dumps(
            {**legacy_payload, "result_hash": content_hash(legacy_payload)},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    assert legacy_result.policy_version == "positive-any-match-v1"
    assert legacy_result.decision == result.decision


def test_incomplete_evidence_produces_terminal_unclear_decision() -> None:
    plan, _ = _plan(complete=False)
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    criterion_result = compact_adjudication_attempts(
        plan,
        [_model_attempt(plan), deterministic_empty_attempt(plan, plan.packets[1], attempt=1, now=now)],
    )
    catalog = freeze_criterion_catalog(
        [
            {
                "criterion_key": "attorney_client_privilege",
                "display_label": "Attorney-client privilege",
                "instruction_text": "Identify confidential requests for legal advice.",
            },
            {
                "criterion_key": "work_product",
                "display_label": "Work product",
                "instruction_text": "Identify material prepared in anticipation of litigation.",
            },
        ]
    )

    result = build_document_decision_result(
        plan,
        criterion_result,
        criterion_decisions_artifact=_artifact_reference(
            ArtifactType.ANALYSIS_CRITERION_DECISIONS
        ),
        catalog=catalog,
    )

    assert result.status == "INSUFFICIENT"
    assert result.decision.determination == "UNCLEAR"
    assert result.decision.confidence == 0
    assert result.decision.unresolved_criterion_keys == [
        "attorney_client_privilege",
        "work_product",
    ]


def test_non_positive_criterion_requires_explicit_model_fallback() -> None:
    plan, _ = _plan(second_semantics="CONTEXTUAL")
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    criterion_result = compact_adjudication_attempts(
        plan,
        [_model_attempt(plan), deterministic_empty_attempt(plan, plan.packets[1], attempt=1, now=now)],
    )
    catalog = freeze_criterion_catalog(
        [
            {
                "criterion_key": "attorney_client_privilege",
                "display_label": "Attorney-client privilege",
                "instruction_text": "Identify confidential requests for legal advice.",
            },
            {
                "criterion_key": "work_product",
                "display_label": "Work product",
                "instruction_text": "Identify material prepared in anticipation of litigation.",
                "decision_semantics": "CONTEXTUAL",
            },
        ]
    )

    result = build_document_decision_result(
        plan,
        criterion_result,
        criterion_decisions_artifact=_artifact_reference(
            ArtifactType.ANALYSIS_CRITERION_DECISIONS
        ),
        catalog=catalog,
    )

    assert result.status == "MODEL_FALLBACK_REQUIRED"
    assert result.decision is None
    assert result.fallback_criterion_keys == ("work_product",)


def test_document_decision_artifact_is_bounded_json() -> None:
    assert ANALYSIS_DOCUMENT_DECISION_SCHEMA_VERSION == 1
    assert allowed_media_types(ArtifactType.ANALYSIS_DOCUMENT_DECISION) == {
        "application/json"
    }
    assert workflow_artifact_max_bytes(ArtifactType.ANALYSIS_DOCUMENT_DECISION) > 0


def test_compatibility_projection_preserves_adjudicated_match_and_lineage() -> None:
    plan, observations = _plan()
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    criterion_result = compact_adjudication_attempts(
        plan,
        [
            _model_attempt(plan),
            deterministic_empty_attempt(plan, plan.packets[1], attempt=1, now=now),
        ],
    )
    catalog = freeze_criterion_catalog(
        [
            {
                "criterion_key": "attorney_client_privilege",
                "display_label": "Attorney-client privilege",
                "instruction_text": "Identify confidential requests for legal advice.",
            },
            {
                "criterion_key": "work_product",
                "display_label": "Work product",
                "instruction_text": "Identify material prepared in anticipation of litigation.",
            },
        ]
    )
    criterion_artifact = _artifact_reference(
        ArtifactType.ANALYSIS_CRITERION_DECISIONS,
        artifact_id=301,
    )
    document_result = build_document_decision_result(
        plan,
        criterion_result,
        criterion_decisions_artifact=criterion_artifact,
        catalog=catalog,
    )
    document_artifact = _artifact_reference(
        ArtifactType.ANALYSIS_DOCUMENT_DECISION,
        artifact_id=302,
    )

    result = build_evidence_compatibility_projection(
        plan=_evaluation_plan(plan, observations),
        observations=observations,
        observations_artifact=_artifact_reference(
            ArtifactType.ANALYSIS_OBSERVATIONS,
            artifact_id=300,
        ),
        criterion_decisions=criterion_result,
        criterion_decisions_artifact=criterion_artifact,
        document_decision=document_result,
        document_decision_artifact=document_artifact,
        catalog=catalog,
    )

    assert result.status == "COMPLETED"
    assert result.output is not None
    assert result.output["determination"] == "RESPONSIVE"
    assert result.output["summary"] == [
        {
            "text": "A client requests legal advice from counsel.",
            "citation_ids": ["¶1"],
        }
    ]
    assert result.output["criterion_matches"][0]["match_type"] == "PRIMARY"
    assert result.output["clarification_requests"] == []
    assert result.narrative_status == "NOT_REQUESTED"
    assert load_compatibility_projection_bytes(compatibility_projection_bytes(result)) == result

    narrative_result = build_evidence_narrative_result(
        compatibility_result=result,
        compatibility_artifact=_artifact_reference(
            ArtifactType.ANALYSIS_COMPATIBILITY_RESULT,
            artifact_id=303,
        ),
        observations=observations,
        observations_artifact=_artifact_reference(
            ArtifactType.ANALYSIS_OBSERVATIONS,
            artifact_id=300,
        ),
        criterion_decisions=criterion_result,
        projection=NarrativeProjection(
            summary=[
                {
                    "text": "The client requests legal advice.",
                    "observation_ids": [observations.observations[0].observation_id],
                }
            ],
            explanation=[],
        ),
    )
    assert narrative_result.selected_observation_ids == (
        observations.observations[0].observation_id,
    )
    assert narrative_result.presented_output["summary"] == [
        {"text": "The client requests legal advice.", "citation_ids": ["¶1"]}
    ]
    assert narrative_result.presented_output["determination"] == "RESPONSIVE"
    assert load_narrative_result_bytes(narrative_result_bytes(narrative_result)) == narrative_result


def test_compatibility_projection_blocks_unsupported_decision_semantics() -> None:
    plan, observations = _plan(second_semantics="CONTEXTUAL")
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    criterion_result = compact_adjudication_attempts(
        plan,
        [
            _model_attempt(plan),
            deterministic_empty_attempt(plan, plan.packets[1], attempt=1, now=now),
        ],
    )
    catalog = freeze_criterion_catalog(
        [
            {
                "criterion_key": "attorney_client_privilege",
                "display_label": "Attorney-client privilege",
                "instruction_text": "Identify confidential requests for legal advice.",
            },
            {
                "criterion_key": "work_product",
                "display_label": "Work product",
                "instruction_text": "Identify material prepared in anticipation of litigation.",
                "decision_semantics": "CONTEXTUAL",
            },
        ]
    )
    criterion_artifact = _artifact_reference(
        ArtifactType.ANALYSIS_CRITERION_DECISIONS,
        artifact_id=311,
    )
    document_result = build_document_decision_result(
        plan,
        criterion_result,
        criterion_decisions_artifact=criterion_artifact,
        catalog=catalog,
    )

    result = build_evidence_compatibility_projection(
        plan=_evaluation_plan(plan, observations),
        observations=observations,
        observations_artifact=_artifact_reference(
            ArtifactType.ANALYSIS_OBSERVATIONS,
            artifact_id=310,
        ),
        criterion_decisions=criterion_result,
        criterion_decisions_artifact=criterion_artifact,
        document_decision=document_result,
        document_decision_artifact=_artifact_reference(
            ArtifactType.ANALYSIS_DOCUMENT_DECISION,
            artifact_id=312,
        ),
        catalog=catalog,
    )

    assert result.status == "BLOCKED"
    assert result.output is None
    assert result.blocking_reasons == (
        "Criterion work_product requires the bounded document-decider fallback",
    )


def test_compatibility_result_artifact_is_bounded_json() -> None:
    assert ANALYSIS_COMPATIBILITY_RESULT_SCHEMA_VERSION == 1
    assert allowed_media_types(ArtifactType.ANALYSIS_COMPATIBILITY_RESULT) == {
        "application/json"
    }
    assert workflow_artifact_max_bytes(ArtifactType.ANALYSIS_COMPATIBILITY_RESULT) > 0

    assert ANALYSIS_NARRATIVE_PROJECTION_SCHEMA_VERSION == 1
    assert allowed_media_types(ArtifactType.ANALYSIS_NARRATIVE_PROJECTION) == {
        "application/json"
    }
    assert workflow_artifact_max_bytes(ArtifactType.ANALYSIS_NARRATIVE_PROJECTION) > 0


def test_narrative_overlay_can_change_only_presentation_fields() -> None:
    observations = _observations()
    observation = observations.observations[0]
    base = {
        "summary": [{"text": observation.fact, "citation_ids": ["¶1"]}],
        "determination": "RESPONSIVE",
        "confidence": 0.9,
        "responsiveness_summary": [{"text": observation.fact, "citation_ids": ["¶1"]}],
        "criterion_matches": [
            {
                "criterion_key": "attorney_client_privilege",
                "label": "Attorney-client privilege",
                "match_type": "PRIMARY",
                "reasoning": "The criterion was satisfied.",
                "citation_ids": ["¶1"],
            }
        ],
        "scope_analysis": [],
        "countervailing_considerations": [],
        "clarification_requests": [],
        "limitations": [],
        "coverage": {
            "status": "COMPLETE",
            "map_plan_version": "analysis-evaluation-plan-v1",
            "paragraph_map_version": "paragraph-map-v1",
            "window_count": 1,
            "successful_window_count": 1,
            "analyzed_paragraph_ids": ["¶1"],
            "partial_paragraph_ids": [],
            "omitted_ranges": [],
        },
    }
    narrative = NarrativeProjection(
        summary=[
            {
                "text": "The communication requests legal advice.",
                "observation_ids": [observation.observation_id],
            }
        ],
        explanation=[],
    )

    projected = apply_narrative_projection(
        base_output=base,
        narrative=narrative,
        observations=observations.observations,
    )

    assert projected["summary"] == [
        {"text": "The communication requests legal advice.", "citation_ids": ["¶1"]}
    ]
    assert projected["responsiveness_summary"] == base["responsiveness_summary"]
    for field in set(base) - {"summary", "responsiveness_summary"}:
        assert projected[field] == base[field]


def test_managed_narrative_receives_only_selected_observations() -> None:
    plan, observations = _plan()
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    criterion_result = compact_adjudication_attempts(
        plan,
        [
            _model_attempt(plan),
            deterministic_empty_attempt(plan, plan.packets[1], attempt=1, now=now),
        ],
    )
    catalog = freeze_criterion_catalog(
        [
            {
                "criterion_key": "attorney_client_privilege",
                "display_label": "Attorney-client privilege",
                "instruction_text": "Identify confidential requests for legal advice.",
            },
            {
                "criterion_key": "work_product",
                "display_label": "Work product",
                "instruction_text": "Identify material prepared in anticipation of litigation.",
            },
        ]
    )
    document_result = build_document_decision_result(
        plan,
        criterion_result,
        criterion_decisions_artifact=_artifact_reference(
            ArtifactType.ANALYSIS_CRITERION_DECISIONS
        ),
        catalog=catalog,
    )
    workflow = SimpleNamespace(
        id=uuid.UUID(int=700),
        workflow_key=MATTER_DEFINITION_EVIDENCE_NARRATIVE_SHADOW_SPEC.key,
        tenant_id=plan.tenant_id,
    )
    step = SimpleNamespace(workflow_run_id=workflow.id, role_key="narrative")
    skill_version = SimpleNamespace(
        id=uuid.UUID(int=701),
        input_schema_key="matter_definition_evidence_narrative_input_v1",
        output_schema_key="matter_definition_evidence_narrative_output_v1",
        limits={"max_requests": 1},
    )
    captured: dict = {}

    async def run_skill(*_args, **kwargs):
        captured.update(kwargs)
        return (
            {
                "version": "narrative-projection-v1",
                "summary": [
                    {
                        "text": "The client requests legal advice.",
                        "observation_ids": [observations.observations[0].observation_id],
                    }
                ],
                "explanation": [],
            },
            SimpleNamespace(id=uuid.UUID(int=702)),
        )

    narrator = ManagedEvidenceNarrator(
        db=MagicMock(),
        workflow=workflow,
        step=step,
        skill_version=skill_version,
        observations=observations,
        criterion_decisions=criterion_result,
        document_decision=document_result,
        resolved_model="google:gemini-3.7-flash",
        skill_runner=run_skill,
    )
    projection, _skill_run = asyncio.run(narrator())

    assert projection.summary[0].observation_ids == [observations.observations[0].observation_id]
    assert len(captured["dynamic_input"]["admitted_observations"]) == 1
    assert captured["output_schema"]["$defs"]["NarrativeStatement"]["properties"][
        "observation_ids"
    ]["items"]["enum"] == [observations.observations[0].observation_id]


def test_optional_narrative_failure_preserves_deterministic_fallback(monkeypatch) -> None:
    workflow = SimpleNamespace(
        id=uuid.UUID(int=800),
        tenant_id=uuid.UUID(int=1),
        status="QUEUED",
        progress={},
        started_at=None,
        completed_at=None,
        failure_code=None,
        failure_summary=None,
        failure_retriable=False,
        error_message=None,
        progress_completed=0,
    )
    step = SimpleNamespace(
        status="QUEUED",
        started_at=None,
        completed_at=None,
        completed_count=0,
        failed_count=0,
        error_message=None,
    )
    fallback = _artifact_reference(
        ArtifactType.ANALYSIS_COMPATIBILITY_RESULT,
        artifact_id=801,
    )
    prepared = PreparedNarrativeShadowLaunch(
        actor_user_id=uuid.UUID(int=20),
        assessment=SimpleNamespace(id=uuid.UUID(int=21)),
        matter=SimpleNamespace(id=uuid.UUID(int=3)),
        compatibility_artifact=fallback,
        compatibility_result=SimpleNamespace(matter_document_id=uuid.UUID(int=4)),
        observations_artifact=_artifact_reference(ArtifactType.ANALYSIS_OBSERVATIONS),
        observations=SimpleNamespace(),
        criterion_decisions=SimpleNamespace(),
        document_decision=SimpleNamespace(),
        skill_version=SimpleNamespace(),
        resolved_model="google:gemini-3.7-flash",
        binding_snapshot={},
        selected_observation_ids=(_hash("a"),),
        launch_key=_hash("b"),
    )

    async def fail_narrative(**_kwargs):
        raise ValueError("invalid optional prose")

    monkeypatch.setattr("app.evidence_narrative_launcher.require_evidence_shadow_enabled", lambda: None)
    monkeypatch.setattr(
        "app.evidence_narrative_launcher._workflow",
        lambda _db, _prepared: (workflow, step),
    )
    monkeypatch.setattr(
        "app.evidence_narrative_launcher.execute_managed_evidence_narrative",
        fail_narrative,
    )
    monkeypatch.setattr(
        "app.evidence_narrative_launcher.refresh_workflow_execution_usage",
        lambda _db, _workflow_id: None,
    )

    report = asyncio.run(
        execute_prepared_narrative_shadow(MagicMock(), prepared, retry_failed=False)
    )

    assert report["status"] == "COMPLETED_WITH_ERRORS"
    assert report["narrative_status"] == "FAILED"
    assert report["fallback_compatibility_result_artifact_id"] == str(fallback.artifact_id)
    assert workflow.failure_code == "OPTIONAL_NARRATIVE_FAILED"
    assert step.status == "COMPLETED_WITH_ERRORS"


def test_dry_run_report_excludes_observation_facts() -> None:
    plan, observations = _plan()
    prepared = PreparedAdjudicationShadowLaunch(
        actor_user_id=uuid.UUID(int=20),
        assessment=SimpleNamespace(id=uuid.UUID(int=21)),
        matter=SimpleNamespace(id=plan.matter_id),
        revision=SimpleNamespace(),
        catalog=SimpleNamespace(),
        observations_artifact=_artifact_reference(ArtifactType.ANALYSIS_OBSERVATIONS),
        observations=observations,
        extraction_plan=SimpleNamespace(),
        skill_version=SimpleNamespace(),
        resolved_model=plan.role_binding.model_key,
        binding_snapshot={},
        plan=plan,
        launch_key=_hash("f"),
    )

    report = adjudication_plan_report(prepared)

    assert report["mode"] == "DRY_RUN"
    assert report["model_packet_count"] == 1
    assert report["deterministic_packet_count"] == 1
    assert "A client requests legal advice" not in str(report)


def test_shadow_restart_reuses_success_and_retry_targets_only_failure() -> None:
    plan, _ = _plan()
    repository = MemoryAdjudicationRepository()
    calls: list[tuple[str, int]] = []

    async def first_adjudicator(_plan, packet, attempt):
        calls.append((packet.unit_id, attempt))
        return _model_attempt(plan, attempt=attempt, status="FAILED")

    first = asyncio.run(
        execute_adjudication_shadow(plan, repository=repository, adjudicator=first_adjudicator)
    )
    calls.clear()
    unchanged = asyncio.run(
        execute_adjudication_shadow(plan, repository=repository, adjudicator=first_adjudicator)
    )

    async def repair(_plan, packet, attempt):
        calls.append((packet.unit_id, attempt))
        return _model_attempt(plan, attempt=attempt)

    repaired = asyncio.run(
        execute_adjudication_shadow(
            plan,
            repository=repository,
            adjudicator=repair,
            retry_failed=True,
        )
    )

    assert first.result.adjudication_complete is False
    assert unchanged.executed_unit_ids == ()
    assert calls == [(plan.packets[0].unit_id, 2)]
    assert repaired.result.adjudication_complete is True
    assert plan.packets[1].unit_id in repaired.reused_unit_ids


def test_adjudication_shadow_automatically_retries_retryable_failure() -> None:
    plan, _ = _plan()
    repository = MemoryAdjudicationRepository()
    calls: list[int] = []

    class TemporaryFailure(RuntimeError):
        code = "MODEL_FAILURE"
        retryable = True

    async def adjudicate(_plan, _packet, attempt):
        calls.append(attempt)
        if attempt == 1:
            raise TemporaryFailure("connection interrupted")
        return _model_attempt(plan, attempt=attempt)

    outcome = asyncio.run(
        execute_adjudication_shadow(plan, repository=repository, adjudicator=adjudicate)
    )

    assert calls == [1, 2]
    assert outcome.result.adjudication_complete is True
    failed = repository.attempts[(plan.packets[0].unit_id, 1)][0]
    assert failed.status == "FAILED"
    assert failed.retryable is True
    assert repository.attempts[(plan.packets[0].unit_id, 2)][0].status == "COMPLETED"


def test_shadow_converts_invalid_model_result_to_failed_fragment() -> None:
    plan, _ = _plan()
    repository = MemoryAdjudicationRepository()

    async def invalid(_plan, _packet, attempt):
        return _model_attempt(plan, attempt=attempt).model_copy(
            update={"unit_id": _hash("f")}
        )

    outcome = asyncio.run(
        execute_adjudication_shadow(plan, repository=repository, adjudicator=invalid)
    )

    stored, _reference = repository.attempts[(plan.packets[0].unit_id, 1)]
    assert stored.status == "FAILED"
    assert stored.error_code == "CRITERION_ADJUDICATION_FAILED"
    assert outcome.result.adjudication_complete is False


def test_artifact_repository_reuses_verified_outputs_from_an_earlier_workflow(
    monkeypatch,
) -> None:
    plan, _ = _plan()
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    result = compact_adjudication_attempts(
        plan,
        [_model_attempt(plan), deterministic_empty_attempt(plan, plan.packets[1], attempt=1, now=now)],
    )
    plan_content = adjudication_plan_bytes(plan)
    result_content = criterion_decisions_bytes(result)
    plan_reference = replace(
        _artifact_reference(ArtifactType.ANALYSIS_ADJUDICATION_PLAN, artifact_id=201),
        derivation_key=plan.plan_hash,
        content_hash=hashlib.sha256(plan_content).hexdigest(),
        producer_run_id=uuid.UUID(int=91),
    )
    result_reference = replace(
        _artifact_reference(ArtifactType.ANALYSIS_CRITERION_DECISIONS, artifact_id=202),
        content_hash=hashlib.sha256(result_content).hexdigest(),
        producer_run_id=uuid.UUID(int=91),
    )

    def find_reference(*, artifact_type, **_kwargs):
        if artifact_type == ArtifactType.ANALYSIS_ADJUDICATION_PLAN:
            return plan_reference
        if artifact_type == ArtifactType.ANALYSIS_CRITERION_DECISIONS:
            return result_reference
        return None

    def copy_content(*, artifact_id, destination, **_kwargs):
        destination.write(
            plan_content if artifact_id == plan_reference.artifact_id else result_content
        )

    def unexpected_store(*_args, **_kwargs):
        raise AssertionError("verified cross-workflow artifacts must be reused")

    monkeypatch.setattr(
        "app.evidence_adjudication_shadow.find_workflow_artifact_reference",
        find_reference,
    )
    monkeypatch.setattr(
        "app.evidence_adjudication_shadow.copy_workflow_artifact_content",
        copy_content,
    )
    monkeypatch.setattr(
        "app.evidence_adjudication_shadow.store_adjudication_plan_artifact",
        unexpected_store,
    )
    monkeypatch.setattr(
        "app.evidence_adjudication_shadow.store_criterion_decisions_artifact",
        unexpected_store,
    )
    repository = WorkflowAdjudicationShadowArtifactRepository(
        actor_user_id=uuid.UUID(int=20),
        producer_run_id=uuid.UUID(int=92),
    )

    assert repository.persist_plan(plan) == plan_reference
    assert (
        repository.persist_result(
            plan,
            plan_reference.artifact_id,
            (),
            result,
        )
        == result_reference
    )


def test_managed_adjudicator_attaches_sole_missing_reference(monkeypatch) -> None:
    plan, observations = _plan()
    catalog = freeze_criterion_catalog(
        [
            {
                "criterion_key": "attorney_client_privilege",
                "display_label": "Attorney-client privilege",
                "instruction_text": "Identify confidential requests for legal advice.",
            },
            {
                "criterion_key": "work_product",
                "display_label": "Work product",
                "instruction_text": "Identify material prepared in anticipation of litigation.",
            },
        ]
    )
    workflow = SimpleNamespace(
        id=uuid.UUID(int=20),
        workflow_key=MATTER_DEFINITION_EVIDENCE_ADJUDICATION_SHADOW_SPEC.key,
        tenant_id=plan.tenant_id,
        client_id=plan.client_id,
        matter_id=plan.matter_id,
    )
    step = SimpleNamespace(
        id=uuid.UUID(int=21),
        workflow_run_id=workflow.id,
        role_key="criterion_adjudication",
    )
    skill_version = SimpleNamespace(
        id=plan.role_binding.skill_definition_version_id,
        input_schema_key="matter_definition_criterion_adjudication_input_v1",
        output_schema_key="matter_definition_criterion_adjudication_output_v1",
        limits={"max_requests": 1, "max_output_tokens": 8_000, "max_output_retries": 0},
    )
    captured = {}

    async def skill_runner(_db, **kwargs):
        captured.update(kwargs)
        raw = {
            "version": "criterion-adjudication-v3",
            "criterion_key": "attorney_client_privilege",
            "relationship": "MATCH",
            "confidence": 0.9,
            "primary_observation_id": None,
            "additional_observation_id_1": None,
            "additional_observation_id_2": None,
            "additional_observation_id_3": None,
            "additional_observation_id_4": None,
            "boundary_explanation": "The evidence satisfies the criterion.",
        }
        normalized = kwargs["output_normalizers"][0](raw)
        kwargs["output_validators"][0](normalized)
        started = datetime(2026, 11, 15, tzinfo=timezone.utc)
        return normalized, SimpleNamespace(
            id=uuid.UUID(int=29),
            started_at=started,
            completed_at=started + timedelta(seconds=1),
        )

    monkeypatch.setattr(
        "app.evidence_adjudication_managed._invocation_ids",
        lambda _db, _skill_run_id: (uuid.UUID(int=30),),
    )
    adjudicator = ManagedCriterionAdjudicator(
        db=MagicMock(),
        workflow=workflow,
        step=step,
        skill_version=skill_version,
        review_guidance="guidance",
        catalog=catalog,
        observations=observations,
        resolved_model=plan.role_binding.model_key,
        skill_runner=skill_runner,
    )
    result = asyncio.run(adjudicator(plan, plan.packets[0], 1))

    assert result.status == "COMPLETED_WITH_ADJUSTMENTS"
    assert result.decision.primary_observation_id == _hash("a")
    assert result.adjustments == (
        AdjudicationAdjustment(
            code="SOLE_OBSERVATION_REFERENCE_ATTACHED",
            policy="NORMALIZE",
            path="primary_observation_id",
            detail="Attached the sole admitted observation to a relationship that requires provenance",
        ),
    )
    assert captured["dynamic_input"]["evidence_packet"][0]["fact"].startswith("A client")
    assert captured["model"] == "google:gemini-3.7-flash"
