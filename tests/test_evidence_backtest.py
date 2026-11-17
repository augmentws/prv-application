from app.evidence_first_contracts import freeze_criterion_catalog
from scripts.compare_evidence_conformance import (
    compare_analysis_signatures,
    compatibility_output_from_report,
)


def _hash(value: str) -> str:
    return value * 64


def _catalog():
    return freeze_criterion_catalog(
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


def test_legacy_content_free_report_projects_into_direct_contract() -> None:
    report = {
        "coverage": {
            "window_count": 2,
            "completed_window_count": 2,
            "complete": True,
            "evidence_complete": True,
        },
        "adjustment_summary": {
            "dropped_observation_count": 0,
            "extraction_adjustment_count": 0,
            "adjudication_adjustment_count": 0,
        },
        "aggregation": {
            "criterion_counts": {"attorney_client_privilege": 1},
            "observation_signatures": [
                {
                    "observation_id": _hash("a"),
                    "criterion_key": "attorney_client_privilege",
                    "signal_type": "NEAR_MISS",
                    "citation_ids": ["¶2"],
                    "source_unit_ids": [_hash("b")],
                }
            ],
        },
        "adjudication": [
            {
                "criterion_key": "attorney_client_privilege",
                "decision": {
                    "version": "criterion-decision-v2",
                    "criterion_key": "attorney_client_privilege",
                    "disposition": "NEAR_MISS",
                    "confidence": 0.9,
                    "primary_observation_id": _hash("a"),
                    "additional_observation_id_1": None,
                    "additional_observation_id_2": None,
                    "additional_observation_id_3": None,
                    "additional_observation_id_4": None,
                    "unresolved_boundary": "The communication does not request legal advice.",
                },
            },
            {
                "criterion_key": "work_product",
                "decision": {
                    "version": "criterion-decision-v2",
                    "criterion_key": "work_product",
                    "disposition": "NO_MATCH",
                    "confidence": 1,
                    "primary_observation_id": None,
                    "additional_observation_id_1": None,
                    "additional_observation_id_2": None,
                    "additional_observation_id_3": None,
                    "additional_observation_id_4": None,
                    "unresolved_boundary": None,
                },
            },
        ],
        "document_decision": {
            "version": "document-decision-v1",
            "determination": "NON_RESPONSIVE",
            "confidence": 0.9,
            "contributing_criterion_keys": [],
            "unresolved_criterion_keys": [],
        },
    }

    output = compatibility_output_from_report(
        report,
        catalog=_catalog(),
        paragraph_map_version="paragraph-v1",
    )

    assert output["determination"] == "NON_RESPONSIVE"
    assert output["summary"] == []
    assert output["criterion_matches"] == [
        {
            "criterion_key": "attorney_client_privilege",
            "label": "Attorney-client privilege",
            "match_type": "NEAR_MISS",
            "reasoning": "The communication does not request legal advice.",
            "citation_ids": ["¶2"],
        }
    ]
    assert output["coverage"]["status"] == "COMPLETE"


def test_signature_comparison_separates_decision_and_criterion_differences() -> None:
    baseline = {
        "determination": "NON_RESPONSIVE",
        "coverage_status": "COMPLETE",
        "criterion_matches": [
            {"criterion_key": "work_product", "match_type": "NEAR_MISS"}
        ],
    }
    candidate = {
        "determination": "NON_RESPONSIVE",
        "coverage_status": "COMPLETE",
        "criterion_matches": [],
    }

    comparison = compare_analysis_signatures(baseline, candidate)

    assert comparison["determination_agreement"] is True
    assert comparison["material_decision_difference"] is False
    assert comparison["criterion_matches_agree"] is False
    assert comparison["baseline_only_criterion_matches"] == [
        ("work_product", "NEAR_MISS")
    ]
