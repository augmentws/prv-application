from __future__ import annotations

import pytest

from app.evidence_first_contracts import (
    CRITERION_ADJUDICATION_INSTRUCTIONS,
    EVIDENCE_EXTRACTION_INSTRUCTIONS,
    MAX_OBSERVATIONS_PER_UNIT,
    UNMAPPED_CRITERION_KEY,
    CriterionDecisionNormalizationCapture,
    EvidenceObservation,
    ExtractionNormalizationCapture,
    aggregate_observations,
    citation_slot_payload,
    combine_positive_criterion_decisions,
    criterion_adjudication_schemas,
    criterion_catalog_from_retrieval_plan,
    criterion_decision_from_adjudication,
    extraction_output_schemas,
    freeze_criterion_catalog,
    narrative_projection_schemas,
    normalize_extraction_output,
    observation_reference_slot_payload,
    schema_enforcement_report,
    validate_criterion_adjudication,
    validate_criterion_decision,
    validate_document_decision,
    validate_narrative_projection,
)


def test_role_prompts_preserve_required_element_evidence_without_inference() -> None:
    assert "Evidence is a source-grounded factual proposition" in EVIDENCE_EXTRACTION_INSTRUCTIONS
    assert "Work criterion by criterion" in EVIDENCE_EXTRACTION_INSTRUCTIONS
    assert "Extract favorable and unfavorable facts" in EVIDENCE_EXTRACTION_INSTRUCTIONS
    assert "what explicit fact is established" in EVIDENCE_EXTRACTION_INSTRUCTIONS
    assert "COUNTERVAILING" in EVIDENCE_EXTRACTION_INSTRUCTIONS
    assert "absence is not evidence" in EVIDENCE_EXTRACTION_INSTRUCTIONS
    assert "allowed only when the packet affirmatively supports" in CRITERION_ADJUDICATION_INSTRUCTIONS
    assert "using the evidence packet as a whole" in CRITERION_ADJUDICATION_INSTRUCTIONS


def _citations(*values: str) -> dict[str, str | None]:
    return citation_slot_payload(list(values))


def _references(*values: str) -> dict[str, str | None]:
    return observation_reference_slot_payload(list(values))


def _catalog():
    return freeze_criterion_catalog(
        [
            {
                "criterion_key": "attorney_client_privilege",
                "display_label": "Attorney-client privilege",
                "instruction_text": "Confidential legal advice communication.",
                "aliases": ["attorney client", "ACP"],
                "decision_semantics": "POSITIVE",
            },
            {
                "criterion_key": "attorney_work_product",
                "display_label": "Attorney work product",
                "instruction_text": "Material prepared in anticipation of litigation.",
                "aliases": ["work product"],
                "decision_semantics": "POSITIVE",
            },
        ]
    )


def test_criterion_catalog_is_deterministic_and_reserves_unmapped() -> None:
    first = _catalog()
    second = _catalog()

    assert first == second
    assert first.catalog_hash == second.catalog_hash
    assert first.criteria[-1].criterion_key == UNMAPPED_CRITERION_KEY
    assert "signal_type NEAR_MISS" in first.criteria[-1].instruction_text
    assert "ordinary subject matter" in first.criteria[-1].instruction_text
    assert first.alias_index()["attorney_client"] == "attorney_client_privilege"

    with pytest.raises(ValueError, match="reserved"):
        freeze_criterion_catalog([{"criterion_key": UNMAPPED_CRITERION_KEY, "display_label": "Caller supplied"}])


def test_retrieval_plan_freezes_the_same_criterion_keys_used_for_extraction() -> None:
    catalog = criterion_catalog_from_retrieval_plan(
        {
            "criteria": [
                {
                    "criterion_key": "attorney_client_privilege",
                    "criterion_label": "Attorney-client privilege",
                    "description": "Confidential legal advice.",
                }
            ]
        }
    )

    assert catalog.criterion_keys() == {"attorney_client_privilege", UNMAPPED_CRITERION_KEY}


def test_item_aware_normalization_retains_valid_sibling_and_drops_extra_citation_slot() -> None:
    result = normalize_extraction_output(
        {
            "observations": [
                {
                    "criterion_key": "ACP",
                    "signal_type": "SUPPORT",
                    "fact": "Counsel provided legal advice.",
                    **_citations("¶1", "¶1", "¶2"),
                },
                {
                    "criterion_key": "attorney_work_product",
                    "signal_type": "SUPPORT",
                    "fact": "The entire window concerns litigation.",
                    **_citations("¶1", "¶2", "¶3", "¶4", "¶5"),
                    "additional_citation_id_5": "¶6",
                },
            ]
        },
        catalog=_catalog(),
        allowed_paragraph_ids={f"¶{index}" for index in range(1, 10)},
        source_hash="source",
        source_unit_id="unit-1",
    )

    assert result.unit_error is None
    assert result.accepted_item_count == 1
    assert result.dropped_item_count == 1
    assert result.observations[0].criterion_key == "attorney_client_privilege"
    assert result.observations[0].citation_ids == ("¶1", "¶2")
    assert {value.code for value in result.adjustments} == {
        "CRITERION_ALIAS_NORMALIZED",
        "DUPLICATE_CITATIONS_REMOVED",
        "OBSERVATION_SCHEMA_INVALID",
    }


def test_item_aware_normalization_drops_unknown_identifier_without_losing_valid_item() -> None:
    result = normalize_extraction_output(
        {
            "observations": [
                {
                    "criterion_key": "invented_criterion",
                    "signal_type": "SUPPORT",
                    "fact": "Unsupported criterion.",
                    **_citations("¶1"),
                },
                {
                    "criterion_key": "attorney_work_product",
                    "signal_type": "NEAR_MISS",
                    "fact": "Business planning predates litigation.",
                    **_citations("¶2"),
                },
                {
                    "criterion_key": "attorney_work_product",
                    "signal_type": "SUPPORT",
                    "fact": "Unknown source reference.",
                    **_citations("¶999"),
                },
            ]
        },
        catalog=_catalog(),
        allowed_paragraph_ids={"¶1", "¶2"},
        source_hash="source",
        source_unit_id="unit-1",
    )

    assert result.accepted_item_count == 1
    assert result.dropped_item_count == 2
    assert {value.code for value in result.adjustments} == {
        "UNKNOWN_CRITERION_KEY",
        "UNKNOWN_CITATION_ID",
    }


def test_normalization_capture_returns_only_admitted_items_and_retains_diagnostics() -> None:
    capture = ExtractionNormalizationCapture(
        catalog=_catalog(),
        allowed_paragraph_ids={"¶1"},
        source_hash="source",
        source_unit_id="window-1",
    )

    normalized = capture(
        {
            "observations": [
                {
                    "criterion_key": "attorney_client_privilege",
                    "signal_type": "SUPPORT",
                    "fact": "Counsel supplied legal advice.",
                    **_citations("¶1"),
                },
                {
                    "criterion_key": "invented",
                    "signal_type": "SUPPORT",
                    "fact": "This item is invalid.",
                    **_citations("¶1"),
                },
            ]
        }
    )

    assert len(normalized["observations"]) == 1
    assert capture.result is not None
    assert capture.result.source_item_count == 2
    assert capture.result.dropped_item_count == 1


def test_exact_overlap_observations_are_deduplicated_without_merging_similar_facts() -> None:
    first = EvidenceObservation(
        observation_id="a" * 64,
        source_unit_id="window-1",
        criterion_key="attorney_client_privilege",
        signal_type="SUPPORT",
        fact="Counsel supplied legal advice.",
        citation_ids=("¶1",),
    )
    overlap = first.model_copy(update={"observation_id": "b" * 64, "source_unit_id": "window-2"})
    similar = first.model_copy(
        update={
            "observation_id": "c" * 64,
            "source_unit_id": "window-2",
            "fact": "Counsel supplied different legal advice.",
        }
    )

    aggregated = aggregate_observations([first, overlap, similar], source_hash="source")

    assert len(aggregated) == 2
    duplicate = next(value for value in aggregated if value.fact == first.fact)
    assert duplicate.source_unit_ids == ("window-1", "window-2")


def test_positive_criterion_combination_is_deterministic() -> None:
    catalog = _catalog()
    decisions = [
        {
            "version": "criterion-decision-v2",
            "criterion_key": "attorney_client_privilege",
            "disposition": "NO_MATCH",
            "confidence": 0.9,
            **_references(),
        },
        {
            "version": "criterion-decision-v2",
            "criterion_key": "attorney_work_product",
            "disposition": "NEAR_MISS",
            "confidence": 0.8,
            **_references("observation-1"),
        },
    ]

    validated = [
        validate_criterion_decision(
            value,
            criterion_key=value["criterion_key"],
            admitted_observation_ids={"observation-1"},
        )
        for value in decisions
    ]
    decision = combine_positive_criterion_decisions(
        validated,
        catalog=catalog,
        coverage_complete=True,
    )

    assert decision.determination == "NON_RESPONSIVE"
    assert decision.confidence == 0.8

    incomplete = combine_positive_criterion_decisions(
        validated,
        catalog=catalog,
        coverage_complete=False,
    )
    assert incomplete.determination == "UNCLEAR"
    assert incomplete.confidence == 0
    assert incomplete.unresolved_criterion_keys == [
        "attorney_client_privilege",
        "attorney_work_product",
    ]

    unmapped = combine_positive_criterion_decisions(
        validated,
        catalog=catalog,
        coverage_complete=True,
        has_unmapped_observations=True,
    )
    assert unmapped.determination == "NON_RESPONSIVE"
    assert unmapped.confidence == 0.8
    assert unmapped.unresolved_criterion_keys == []


def test_criterion_adjudication_schema_admits_only_packet_ids() -> None:
    application, _provider = criterion_adjudication_schemas(
        criterion_key="attorney_client_privilege",
        observation_ids=["observation-1", "observation-2"],
        provider="google",
    )

    assert application["properties"]["criterion_key"]["const"] == "attorney_client_privilege"
    primary = application["properties"]["primary_observation_id"]["anyOf"]
    assert primary[0]["enum"] == ["observation-1", "observation-2"]
    assert primary[1] == {"type": "null"}
    assert set(application["properties"]["relationship"]["enum"]) == {
        "MATCH",
        "RELATED_MISSING_REQUIRED_ELEMENT",
        "UNRELATED",
        "CONFLICTING",
        "INSUFFICIENT",
    }
    assert set(application["required"]) >= {
        "primary_observation_id",
        "additional_observation_id_1",
        "additional_observation_id_2",
        "additional_observation_id_3",
        "additional_observation_id_4",
    }


def test_provider_contracts_are_stable_across_unit_specific_ids() -> None:
    catalog = _catalog()
    _first_application, first_extraction = extraction_output_schemas(
        catalog=catalog,
        paragraph_ids=["¶1"],
        provider="google",
    )
    _second_application, second_extraction = extraction_output_schemas(
        catalog=catalog,
        paragraph_ids=["¶999"],
        provider="google",
    )
    _first_application, first_adjudication = criterion_adjudication_schemas(
        criterion_key="attorney_client_privilege",
        observation_ids=["observation-a"],
        provider="google",
    )
    _second_application, second_adjudication = criterion_adjudication_schemas(
        criterion_key="attorney_work_product",
        observation_ids=["observation-b"],
        provider="google",
    )

    assert first_extraction == second_extraction
    assert first_adjudication == second_adjudication


def test_related_missing_element_deterministically_becomes_near_miss() -> None:
    adjudication = validate_criterion_adjudication(
        {
            "version": "criterion-adjudication-v3",
            "criterion_key": "attorney_client_privilege",
            "relationship": "RELATED_MISSING_REQUIRED_ELEMENT",
            "confidence": 0.9,
            **_references("observation-1"),
            "boundary_explanation": "No evidence establishes attorney involvement.",
        },
        criterion_key="attorney_client_privilege",
        admitted_observation_ids={"observation-1"},
    )

    decision = criterion_decision_from_adjudication(adjudication)

    assert decision.disposition == "NEAR_MISS"
    assert decision.unresolved_boundary == "No evidence establishes attorney involvement."
    assert decision.observation_ids() == ("observation-1",)


def test_decision_normalization_drops_unknown_and_duplicate_references() -> None:
    capture = CriterionDecisionNormalizationCapture(admitted_observation_ids={"observation-1"})

    normalized = capture(
        {
            "version": "criterion-decision-v2",
            "criterion_key": "attorney_client_privilege",
            "disposition": "NO_MATCH",
            "confidence": 0.9,
            **_references("observation-1", "observation-1", "invented"),
        }
    )

    assert normalized["primary_observation_id"] == "observation-1"
    assert normalized["additional_observation_id_1"] is None
    assert normalized["additional_observation_id_2"] is None
    assert capture.adjustments == [
        {
            "code": "DUPLICATE_OBSERVATION_REFERENCE_REMOVED",
            "policy": "NORMALIZE",
            "path": "additional_observation_id_1",
            "detail": "Removed a duplicate observation reference",
        },
        {
            "code": "UNKNOWN_OBSERVATION_REFERENCE_DROPPED",
            "policy": "DROP_ITEM",
            "path": "additional_observation_id_2",
            "detail": "Dropped references outside the admitted criterion packet",
        },
    ]


def test_adjudication_normalization_attaches_sole_missing_reference() -> None:
    capture = CriterionDecisionNormalizationCapture(admitted_observation_ids={"observation-1"})
    raw = {
        "version": "criterion-adjudication-v3",
        "criterion_key": "legal_invoices_and_fees",
        "relationship": "UNRELATED",
        "confidence": 0.9,
        **_references(),
        "boundary_explanation": "The evidence concerns commercial payment terms, not legal fees.",
    }

    normalized = capture(raw)
    adjudication = validate_criterion_adjudication(
        normalized,
        criterion_key="legal_invoices_and_fees",
        admitted_observation_ids={"observation-1"},
    )
    decision = criterion_decision_from_adjudication(adjudication)

    assert adjudication.observation_ids() == ("observation-1",)
    assert decision.disposition == "NO_MATCH"
    assert capture.adjustments == [
        {
            "code": "SOLE_OBSERVATION_REFERENCE_ATTACHED",
            "policy": "NORMALIZE",
            "path": "primary_observation_id",
            "detail": ("Attached the sole admitted observation to a relationship that requires provenance"),
        }
    ]


def test_adjudication_normalization_does_not_guess_among_missing_references() -> None:
    capture = CriterionDecisionNormalizationCapture(admitted_observation_ids={"observation-1", "observation-2"})
    raw = {
        "version": "criterion-adjudication-v3",
        "criterion_key": "legal_invoices_and_fees",
        "relationship": "UNRELATED",
        "confidence": 0.9,
        **_references(),
        "boundary_explanation": "The evidence does not concern legal fees.",
    }

    normalized = capture(raw)

    with pytest.raises(ValueError, match="UNRELATED requires at least one referenced observation"):
        validate_criterion_adjudication(
            normalized,
            criterion_key="legal_invoices_and_fees",
            admitted_observation_ids={"observation-1", "observation-2"},
        )
    assert capture.adjustments == []


def test_adjudication_normalization_allows_insufficient_without_reference() -> None:
    capture = CriterionDecisionNormalizationCapture(admitted_observation_ids={"observation-1"})
    raw = {
        "version": "criterion-adjudication-v3",
        "criterion_key": "legal_invoices_and_fees",
        "relationship": "INSUFFICIENT",
        "confidence": 0.6,
        **_references(),
        "boundary_explanation": "The packet lacks enough context to evaluate the criterion.",
    }

    normalized = capture(raw)
    adjudication = validate_criterion_adjudication(
        normalized,
        criterion_key="legal_invoices_and_fees",
        admitted_observation_ids={"observation-1"},
    )

    assert adjudication.observation_ids() == ()
    assert capture.adjustments == []


def test_outer_observation_overflow_fails_only_the_unit() -> None:
    result = normalize_extraction_output(
        {"observations": [{} for _ in range(MAX_OBSERVATIONS_PER_UNIT + 1)]},
        catalog=_catalog(),
        allowed_paragraph_ids={"¶1"},
        source_hash="source",
        source_unit_id="unit-1",
    )

    assert result.unit_error == "ANALYSIS_OUTPUT_SCHEMA_INVALID"
    assert result.observations == ()
    assert result.adjustments[0].policy == "FAIL_UNIT"


def test_criterion_decision_can_reference_only_packet_observations() -> None:
    valid = {
        "version": "criterion-decision-v2",
        "criterion_key": "attorney_client_privilege",
        "disposition": "MATCH",
        "confidence": 0.9,
        **_references("observation-1"),
    }
    decision = validate_criterion_decision(
        valid,
        criterion_key="attorney_client_privilege",
        admitted_observation_ids={"observation-1"},
    )
    assert decision.disposition == "MATCH"

    with pytest.raises(ValueError, match="unknown observation"):
        validate_criterion_decision(
            {**valid, **_references("invented")},
            criterion_key="attorney_client_privilege",
            admitted_observation_ids={"observation-1"},
        )


def test_document_decision_rejects_unknown_and_unmapped_positive_criteria() -> None:
    with pytest.raises(ValueError, match="unknown criterion"):
        validate_document_decision(
            {
                "version": "document-decision-v1",
                "determination": "RESPONSIVE",
                "confidence": 0.8,
                "contributing_criterion_keys": ["invented"],
                "unresolved_criterion_keys": [],
            },
            admitted_criterion_keys=_catalog().criterion_keys(),
        )

    with pytest.raises(ValueError, match="UNMAPPED"):
        validate_document_decision(
            {
                "version": "document-decision-v1",
                "determination": "RESPONSIVE",
                "confidence": 0.8,
                "contributing_criterion_keys": [UNMAPPED_CRITERION_KEY],
                "unresolved_criterion_keys": [],
            },
            admitted_criterion_keys=_catalog().criterion_keys(),
        )


def test_narrative_can_reference_only_admitted_observations() -> None:
    with pytest.raises(ValueError, match="unknown observation"):
        validate_narrative_projection(
            {
                "version": "narrative-projection-v1",
                "summary": [{"text": "Legal advice was requested.", "observation_ids": ["invented"]}],
                "explanation": [],
            },
            admitted_observation_ids={"observation-1"},
        )


def test_narrative_schema_admits_only_selected_observation_ids() -> None:
    application, admitted = narrative_projection_schemas(
        observation_ids=["observation-1", "observation-2", "observation-1"],
        provider="google",
    )
    references = application["$defs"]["NarrativeStatement"]["properties"]["observation_ids"]
    assert references["items"]["enum"] == ["observation-1", "observation-2"]
    assert references["uniqueItems"] is True
    assert references["minItems"] == 1
    assert admitted["type"] == "object"

    projection = validate_narrative_projection(
        {
            "version": "narrative-projection-v1",
            "summary": [
                {
                    "text": "Legal advice was requested.",
                    "observation_ids": ["observation-1"],
                }
            ],
            "explanation": [],
        },
        admitted_observation_ids={"observation-1", "observation-2"},
    )
    assert projection.summary[0].observation_ids == ["observation-1"]


def test_google_schema_has_dynamic_enums_and_explicit_local_enforcement() -> None:
    application, admitted = extraction_output_schemas(
        catalog=_catalog(),
        paragraph_ids=["¶1", "¶2"],
        provider="google",
    )
    candidate = application["$defs"]["EvidenceObservationCandidate"]
    assert set(candidate["properties"]["criterion_key"]["enum"]) == _catalog().criterion_keys()
    assert candidate["properties"]["primary_citation_id"]["enum"] == ["¶1", "¶2"]
    assert candidate["properties"]["additional_citation_id_1"]["anyOf"] == [
        {"type": "string", "enum": ["¶1", "¶2"]},
        {"type": "null"},
    ]
    assert set(candidate["required"]) >= {
        "primary_citation_id",
        "additional_citation_id_1",
        "additional_citation_id_2",
        "additional_citation_id_3",
        "additional_citation_id_4",
    }

    report = schema_enforcement_report(application, admitted)
    assert any(value["keyword"] == "maxItems" for value in report)
    assert all(value["policy"] for value in report)
    assert not any(value["keyword"] == "maxItems" and "citation" in value["path"] for value in report)
    observation_max = next(
        value for value in report if value["keyword"] == "maxItems" and value["path"].endswith("observations.maxItems")
    )
    assert observation_max["policy"] == "FAIL_UNIT"
