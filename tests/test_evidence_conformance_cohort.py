import json

import pytest

from app.model_execution import ModelExecutionError
from scripts.run_evidence_conformance_cohort import (
    build_rollout_gate,
    load_reusable_report,
    resolve_case_ids,
    summarize_case,
)
from scripts.run_evidence_document_conformance import (
    EVIDENCE_DOCUMENT_CONFORMANCE_VERSION,
    _run_with_bounded_retries,
    resolve_model_bindings,
)


def _corpus() -> dict:
    return {
        "cases": [
            {"matter_document_id": "document-1"},
            {"matter_document_id": "document-2"},
        ]
    }


def test_model_bindings_support_shared_and_role_specific_models() -> None:
    assert resolve_model_bindings(
        model="google:shared",
        extraction_model=None,
        adjudication_model=None,
    ) == ("google:shared", "google:shared")
    assert resolve_model_bindings(
        model="google:shared",
        extraction_model="google:extractor",
        adjudication_model="google:adjudicator",
    ) == ("google:extractor", "google:adjudicator")

    with pytest.raises(ValueError, match="extraction"):
        resolve_model_bindings(model=None, extraction_model=None, adjudication_model="google:judge")


def test_conformance_stage_retries_only_retryable_failures() -> None:
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ModelExecutionError(
                "MODEL_FAILURE",
                "connection interrupted",
                retryable=True,
                details={"exception_type": "ConnectError"},
            )
        return "completed"

    result, invocations, failures, terminal = _run_with_bounded_retries(
        operation,
        max_attempts=3,
    )

    assert result == "completed"
    assert invocations == []
    assert terminal is None
    assert attempts == 2
    assert failures == [
        {
            "attempt": 1,
            "error_code": "MODEL_FAILURE",
            "error_message": "connection interrupted",
            "error_details": {"exception_type": "ConnectError"},
            "retryable": True,
        }
    ]


def test_conformance_stage_does_not_retry_nonretryable_failure() -> None:
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        raise ModelExecutionError("INVALID_REQUEST", "bad request", retryable=False)

    result, _invocations, failures, terminal = _run_with_bounded_retries(
        operation,
        max_attempts=3,
    )

    assert result is None
    assert terminal is not None
    assert attempts == 1
    assert len(failures) == 1


def test_case_selection_requires_selected_or_all_cases() -> None:
    assert resolve_case_ids(
        corpus=_corpus(),
        selected_case_ids=["document-2", "document-2"],
        all_cases=False,
    ) == ["document-2"]
    assert resolve_case_ids(
        corpus=_corpus(),
        selected_case_ids=None,
        all_cases=True,
    ) == ["document-1", "document-2"]

    with pytest.raises(ValueError, match="exactly one"):
        resolve_case_ids(corpus=_corpus(), selected_case_ids=None, all_cases=False)
    with pytest.raises(ValueError, match="absent"):
        resolve_case_ids(
            corpus=_corpus(),
            selected_case_ids=["document-3"],
            all_cases=False,
        )


def test_resume_reuses_only_matching_report(tmp_path) -> None:
    path = tmp_path / "report.json"
    report = {
        "result_version": EVIDENCE_DOCUMENT_CONFORMANCE_VERSION,
        "matter_document_id": "document-1",
        "corpus_hash": "corpus-hash",
        "model_bindings": {
            "extraction": "google:extractor",
            "adjudication": "google:adjudicator",
        },
        "status": "COMPLETED",
    }
    path.write_text(json.dumps(report), encoding="utf-8")

    assert (
        load_reusable_report(
            path,
            matter_document_id="document-1",
            corpus_hash="corpus-hash",
            extraction_model="google:extractor",
            adjudication_model="google:adjudicator",
        )
        == report
    )
    assert (
        load_reusable_report(
            path,
            matter_document_id="document-1",
            corpus_hash="different",
            extraction_model="google:extractor",
            adjudication_model="google:adjudicator",
        )
        is None
    )
    path.write_text(
        json.dumps({**report, "result_version": "evidence-document-conformance-v5"}),
        encoding="utf-8",
    )
    assert (
        load_reusable_report(
            path,
            matter_document_id="document-1",
            corpus_hash="corpus-hash",
            extraction_model="google:extractor",
            adjudication_model="google:adjudicator",
        )
        is None
    )


def test_cohort_summary_reports_determination_and_observation_stability() -> None:
    summary = summarize_case(
        [
            {
                "status": "COMPLETED",
                "candidate_determination": "NON_RESPONSIVE",
                "determination_agreement": True,
                "observation_count": 1,
                "criterion_keys": ["criterion-a"],
                "dropped_observation_count": 0,
                "provider_requests": 2,
                "input_tokens": 100,
                "cached_input_tokens": 50,
                "output_tokens": 10,
                "latency_ms": 20,
            },
            {
                "status": "COMPLETED_WITH_ADJUSTMENTS",
                "candidate_determination": "UNCLEAR",
                "determination_agreement": False,
                "observation_count": 3,
                "criterion_keys": ["criterion-a", "criterion-b"],
                "dropped_observation_count": 1,
                "provider_requests": 3,
                "input_tokens": 200,
                "cached_input_tokens": 100,
                "output_tokens": 20,
                "latency_ms": 30,
            },
        ]
    )

    assert summary["determination_stable"] is False
    assert summary["baseline_agreement_count"] == 1
    assert summary["observation_count_min"] == 1
    assert summary["observation_count_max"] == 3
    assert summary["distinct_criterion_sets"] == 2
    assert summary["runs_with_dropped_observations"] == 1
    assert summary["provider_requests"] == 5


def test_rollout_gate_blocks_incomplete_unstable_and_material_runs() -> None:
    runs = [
        {
            "completed_decision": True,
            "failed_stage_count": 0,
            "material_difference": False,
            "compatibility_output_valid": True,
            "synthesis_input_compatible": True,
            "dropped_observation_count": 0,
            "criterion_match_agreement": False,
        },
        {
            "completed_decision": False,
            "failed_stage_count": 1,
            "material_difference": True,
            "compatibility_output_valid": False,
            "synthesis_input_compatible": False,
            "dropped_observation_count": 0,
            "criterion_match_agreement": False,
        },
    ]
    gate = build_rollout_gate(
        planned_run_count=3,
        runs=runs,
        failures=[],
        case_summaries={"document-1": {"determination_stable": False}},
    )

    assert gate["status"] == "BLOCKED"
    assert {value["code"] for value in gate["blocking_reasons"]} == {
        "MISSING_RUNS",
        "INCOMPLETE_DECISIONS",
        "FAILED_MODEL_STAGES",
        "MATERIAL_DECISION_DIFFERENCES",
        "UNSTABLE_DETERMINATIONS",
        "INVALID_COMPATIBILITY_OUTPUTS",
        "INCOMPATIBLE_SYNTHESIS_INPUTS",
    }
    assert gate["review_items"][0]["count"] == 2


def test_rollout_gate_allows_criterion_only_review_items() -> None:
    runs = [
        {
            "completed_decision": True,
            "failed_stage_count": 0,
            "material_difference": False,
            "compatibility_output_valid": True,
            "synthesis_input_compatible": True,
            "dropped_observation_count": 0,
            "criterion_match_agreement": False,
        }
    ]
    gate = build_rollout_gate(
        planned_run_count=1,
        runs=runs,
        failures=[],
        case_summaries={"document-1": {"determination_stable": True}},
    )

    assert gate["status"] == "PASSED"
    assert gate["blocking_reasons"] == []
    assert gate["review_items"][0]["code"] == "CRITERION_SIGNATURE_DIFFERENCES"
