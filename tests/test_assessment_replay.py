from __future__ import annotations

from copy import deepcopy

import pytest

from app.assessment_replay import (
    ASSESSMENT_REPLAY_CORPUS_VERSION,
    analysis_signature,
    compare_analysis_signatures,
    seal_replay_corpus,
    select_replay_cases,
    validate_replay_corpus,
)


def _candidate(index: int, **overrides):
    return {
        "matter_document_id": f"00000000-0000-0000-0000-{index:012d}",
        "final_status": "COMPLETED",
        "skill_run_count": 1,
        "failed_skill_run_count": 0,
        "baseline_invocation_count": 1,
        "repair_invocation_count": 0,
        "input_tokens": 100 + index,
        "output_tokens": 20,
        "latency_ms": 1_000 + index,
        **overrides,
    }


def test_replay_selection_is_deterministic_and_retains_failures_and_replays() -> None:
    candidates = [_candidate(index) for index in range(1, 31)]
    candidates[0]["final_status"] = "FAILED"
    candidates[1]["skill_run_count"] = 2
    candidates[2]["repair_invocation_count"] = 3
    candidates[3]["baseline_invocation_count"] = 8
    candidates[4]["failed_skill_run_count"] = 1

    first = select_replay_cases(candidates, seed="assessment", target_size=12)
    second = select_replay_cases(reversed(candidates), seed="assessment", target_size=12)

    assert first == second
    assert len(first) == 12
    by_id = {value["matter_document_id"]: value for value in first}
    assert "FAILED" in by_id[candidates[0]["matter_document_id"]]["selection_reasons"]
    assert "REPLAYED" in by_id[candidates[1]["matter_document_id"]]["selection_reasons"]
    assert "HISTORICAL_FAILURE" in by_id[candidates[4]["matter_document_id"]]["selection_reasons"]
    controls = [value for value in first if value["selection_reasons"] == ["STABLE_CONTROL"]]
    assert controls
    assert all(value["baseline_invocation_count"] == 1 for value in controls)
    assert all(value["repair_invocation_count"] == 0 for value in controls)


def test_replay_selection_keeps_all_mandatory_cases_over_target() -> None:
    candidates = [_candidate(index, final_status="FAILED") for index in range(1, 5)]

    selected = select_replay_cases(candidates, seed="assessment", target_size=2)

    assert len(selected) == 4
    assert all("FAILED" in value["selection_reasons"] for value in selected)


def test_corpus_hash_detects_manifest_changes() -> None:
    manifest = seal_replay_corpus(
        {
            "corpus_version": ASSESSMENT_REPLAY_CORPUS_VERSION,
            "cases": [
                {
                    "matter_document_id": "document-1",
                    "source_hash": "source-hash",
                    "selection_reasons": ["FAILED"],
                }
            ],
        }
    )
    validate_replay_corpus(manifest)

    changed = deepcopy(manifest)
    changed["cases"][0]["source_hash"] = "changed"
    with pytest.raises(ValueError, match="hash"):
        validate_replay_corpus(changed)


def test_analysis_comparison_reports_decision_and_criterion_agreement() -> None:
    baseline_output = {
        "determination": "RESPONSIVE",
        "criterion_matches": [
            {"criterion_key": "legal-advice", "match_type": "PRIMARY"},
            {"criterion_key": "confidential", "match_type": "SECONDARY"},
        ],
        "clarification_requests": [],
        "limitations": [],
        "coverage": {"status": "COMPLETE"},
    }
    candidate_output = {
        **baseline_output,
        "criterion_matches": [
            {"criterion_key": "legal-advice", "match_type": "PRIMARY"},
            {"criterion_key": "work-product", "match_type": "SECONDARY"},
        ],
    }

    comparison = compare_analysis_signatures(
        analysis_signature(baseline_output),
        analysis_signature(candidate_output),
    )

    assert comparison["determination_agreement"] is True
    assert comparison["exact_output_match"] is False
    assert comparison["criterion_match_jaccard"] == pytest.approx(1 / 3)
