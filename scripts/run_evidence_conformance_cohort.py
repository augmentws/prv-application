"""Run a repeated, read-only evidence-first document conformance cohort."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from app.evidence_first_contracts import (
    CRITERION_ADJUDICATION_VERSION,
    CRITERION_DECISION_VERSION,
    EVIDENCE_OBSERVATION_VERSION,
)
from scripts.run_evidence_document_conformance import (
    EVIDENCE_DOCUMENT_CONFORMANCE_VERSION,
    resolve_model_bindings,
)

EVIDENCE_CONFORMANCE_COHORT_VERSION = "evidence-conformance-cohort-v9"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repeat selected frozen replay documents through the evidence-first shadow runner and summarize "
            "determination, observation, criterion, adjustment, request, token, and latency stability."
        )
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--case-id", action="append")
    parser.add_argument(
        "--all-cases",
        action="store_true",
        help="Run every case in the frozen corpus instead of selected --case-id values",
    )
    parser.add_argument(
        "--model",
        help="Use one resolved model for both roles; overridden by a role-specific option",
    )
    parser.add_argument("--extraction-model", help="Resolved evidence-extraction model")
    parser.add_argument("--adjudication-model", help="Resolved criterion-adjudication model")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse compatible existing child reports in the report directory",
    )
    parser.add_argument("--report-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def resolve_case_ids(
    *,
    corpus: dict[str, Any],
    selected_case_ids: list[str] | None,
    all_cases: bool,
) -> list[str]:
    selected = list(dict.fromkeys(selected_case_ids or []))
    if all_cases == bool(selected):
        raise ValueError("Specify exactly one of --all-cases or one or more --case-id values")
    available = [str(value["matter_document_id"]) for value in corpus.get("cases") or []]
    if all_cases:
        if not available:
            raise ValueError("Frozen corpus contains no cases")
        return available
    unknown = sorted(set(selected) - set(available))
    if unknown:
        raise ValueError(f"Selected case IDs are absent from the frozen corpus: {', '.join(unknown)}")
    return selected


def load_reusable_report(
    path: Path,
    *,
    matter_document_id: str,
    corpus_hash: str,
    extraction_model: str,
    adjudication_model: str,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(report, dict):
        return None
    expected_bindings = {
        "extraction": extraction_model,
        "adjudication": adjudication_model,
    }
    if (
        report.get("result_version") != EVIDENCE_DOCUMENT_CONFORMANCE_VERSION
        or report.get("matter_document_id") != matter_document_id
        or report.get("corpus_hash") != corpus_hash
        or report.get("model_bindings") != expected_bindings
    ):
        return None
    return report


def _run_summary(report: dict[str, Any]) -> dict[str, Any]:
    aggregation = report.get("aggregation") or {}
    comparison = report.get("comparison") or {}
    usage = report.get("usage") or {}
    adjustment = report.get("adjustment_summary") or {}
    decision = report.get("document_decision") or {}
    compatibility = report.get("compatibility_output") or {}
    baseline_signature = report.get("baseline_signature") or {}
    candidate_matches = sorted(
        (
            str(value.get("criterion_key") or ""),
            str(value.get("match_type") or ""),
        )
        for value in compatibility.get("criterion_matches") or []
        if isinstance(value, dict)
    )
    baseline_matches = sorted(
        (
            str(value.get("criterion_key") or ""),
            str(value.get("match_type") or ""),
        )
        for value in baseline_signature.get("criterion_matches") or []
        if isinstance(value, dict)
    )
    determination_agreement = comparison.get("determination_agreement")
    criterion_match_agreement = candidate_matches == baseline_matches
    failed_stages = [
        {
            "stage": stage,
            "key": value.get("window_id") or value.get("criterion_key"),
            "error_code": value.get("error_code"),
            "exception_type": (value.get("error_details") or {}).get("exception_type"),
        }
        for stage, values in (
            ("extraction", report.get("extraction") or []),
            ("adjudication", report.get("adjudication") or []),
        )
        for value in values
        if isinstance(value, dict) and value.get("status") == "FAILED"
    ]
    return {
        "status": report.get("status"),
        "candidate_determination": comparison.get("candidate_determination"),
        "baseline_determination": comparison.get("baseline_determination"),
        "determination_agreement": determination_agreement,
        "criterion_match_agreement": criterion_match_agreement,
        "material_difference": determination_agreement is not True,
        "completed_decision": bool(decision),
        "failed_stage_count": len(failed_stages),
        "failed_stages": failed_stages,
        "candidate_criterion_matches": candidate_matches,
        "baseline_criterion_matches": baseline_matches,
        "decision_confidence": decision.get("confidence"),
        "observation_count": aggregation.get("aggregated_observation_count", 0),
        "criterion_keys": sorted((aggregation.get("criterion_counts") or {}).keys()),
        "dropped_observation_count": adjustment.get("dropped_observation_count", 0),
        "provider_requests": usage.get("provider_requests", 0),
        "input_tokens": usage.get("input_tokens", 0),
        "cached_input_tokens": usage.get("cached_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "latency_ms": usage.get("latency_ms", 0),
        "compatibility_output_valid": bool(compatibility),
        "synthesis_input_compatible": bool((report.get("synthesis_compatibility") or {}).get("compatible")),
    }


def build_rollout_gate(
    *,
    planned_run_count: int,
    runs: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    case_summaries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate objective safety gates while leaving criterion-only drift for review."""

    blocking_reasons: list[dict[str, Any]] = []

    def block(code: str, count: int, message: str) -> None:
        if count:
            blocking_reasons.append({"code": code, "count": count, "message": message})

    block(
        "MISSING_RUNS",
        max(0, planned_run_count - len(runs)),
        "One or more planned cohort runs did not produce a report.",
    )
    block(
        "HARNESS_FAILURES",
        len(failures),
        "One or more child conformance processes failed.",
    )
    block(
        "INCOMPLETE_DECISIONS",
        sum(value.get("completed_decision") is not True for value in runs),
        "One or more runs did not produce a completed document decision.",
    )
    block(
        "FAILED_MODEL_STAGES",
        sum(int(value.get("failed_stage_count") or 0) for value in runs),
        "One or more extraction or adjudication stages failed.",
    )
    block(
        "MATERIAL_DECISION_DIFFERENCES",
        sum(value.get("material_difference") is True for value in runs),
        "One or more candidate determinations differ from the frozen baseline.",
    )
    block(
        "UNSTABLE_DETERMINATIONS",
        sum(value.get("determination_stable") is not True for value in case_summaries.values()),
        "One or more documents produced different determinations across repeats.",
    )
    block(
        "INVALID_COMPATIBILITY_OUTPUTS",
        sum(value.get("compatibility_output_valid") is not True for value in runs),
        "One or more runs did not produce a valid direct-compatibility output.",
    )
    block(
        "INCOMPATIBLE_SYNTHESIS_INPUTS",
        sum(value.get("synthesis_input_compatible") is not True for value in runs),
        "One or more runs could not be compiled into the synthesis input contract.",
    )
    block(
        "DROPPED_OBSERVATIONS",
        sum(int(value.get("dropped_observation_count") or 0) > 0 for value in runs),
        "One or more runs dropped model observations during contract admission.",
    )
    criterion_review_count = sum(value.get("criterion_match_agreement") is not True for value in runs)
    return {
        "status": "PASSED" if not blocking_reasons else "BLOCKED",
        "blocking_reasons": blocking_reasons,
        "review_items": (
            []
            if criterion_review_count == 0
            else [
                {
                    "code": "CRITERION_SIGNATURE_DIFFERENCES",
                    "count": criterion_review_count,
                    "message": (
                        "Criterion-only differences require representative review but do not "
                        "independently block the objective safety gate."
                    ),
                }
            ]
        ),
    }


def summarize_case(runs: list[dict[str, Any]]) -> dict[str, Any]:
    observations = [int(value.get("observation_count") or 0) for value in runs]
    determinations = [value.get("candidate_determination") for value in runs]
    criterion_sets = {json.dumps(value.get("criterion_keys") or [], separators=(",", ":")) for value in runs}
    return {
        "run_count": len(runs),
        "status_counts": dict(sorted(Counter(str(value.get("status")) for value in runs).items())),
        "determination_counts": dict(sorted(Counter(str(value) for value in determinations).items())),
        "determination_stable": len(set(determinations)) == 1,
        "baseline_agreement_count": sum(value.get("determination_agreement") is True for value in runs),
        "criterion_match_agreement_count": sum(value.get("criterion_match_agreement") is True for value in runs),
        "material_difference_count": sum(value.get("material_difference") is True for value in runs),
        "compatibility_output_valid_count": sum(value.get("compatibility_output_valid") is True for value in runs),
        "synthesis_input_compatible_count": sum(value.get("synthesis_input_compatible") is True for value in runs),
        "observation_count_min": min(observations, default=0),
        "observation_count_max": max(observations, default=0),
        "distinct_criterion_sets": len(criterion_sets),
        "runs_with_dropped_observations": sum(int(value.get("dropped_observation_count") or 0) > 0 for value in runs),
        "provider_requests": sum(int(value.get("provider_requests") or 0) for value in runs),
        "input_tokens": sum(int(value.get("input_tokens") or 0) for value in runs),
        "cached_input_tokens": sum(int(value.get("cached_input_tokens") or 0) for value in runs),
        "output_tokens": sum(int(value.get("output_tokens") or 0) for value in runs),
        "latency_ms": sum(int(value.get("latency_ms") or 0) for value in runs),
        "runs": runs,
    }


def main() -> None:
    arguments = _arguments()
    if arguments.repeats <= 0:
        raise ValueError("--repeats must be positive")
    corpus = json.loads(arguments.corpus.read_text(encoding="utf-8"))
    if not isinstance(corpus, dict):
        raise TypeError("Replay corpus must be a JSON object")
    case_ids = resolve_case_ids(
        corpus=corpus,
        selected_case_ids=arguments.case_id,
        all_cases=arguments.all_cases,
    )
    extraction_model, adjudication_model = resolve_model_bindings(
        model=arguments.model,
        extraction_model=arguments.extraction_model,
        adjudication_model=arguments.adjudication_model,
    )
    arguments.report_directory.mkdir(parents=True, exist_ok=True)
    runs_by_case: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in case_ids}
    failures: list[dict[str, Any]] = []

    for repeat in range(1, arguments.repeats + 1):
        for case_id in case_ids:
            report_path = arguments.report_directory / f"{case_id}-repeat-{repeat}.json"
            report = (
                load_reusable_report(
                    report_path,
                    matter_document_id=case_id,
                    corpus_hash=str(corpus.get("corpus_hash") or ""),
                    extraction_model=extraction_model,
                    adjudication_model=adjudication_model,
                )
                if arguments.resume
                else None
            )
            if report is None:
                command = [
                    sys.executable,
                    "-m",
                    "scripts.run_evidence_document_conformance",
                    "--corpus",
                    str(arguments.corpus),
                    "--case-id",
                    case_id,
                    "--extraction-model",
                    extraction_model,
                    "--adjudication-model",
                    adjudication_model,
                    "--output",
                    str(report_path),
                ]
                completed = subprocess.run(command, check=False)
                if completed.returncode != 0 or not report_path.exists():
                    failures.append(
                        {
                            "matter_document_id": case_id,
                            "repeat": repeat,
                            "returncode": completed.returncode,
                        }
                    )
                    continue
                report = json.loads(report_path.read_text(encoding="utf-8"))
            summary = _run_summary(report)
            summary["repeat"] = repeat
            summary["report_path"] = str(report_path)
            runs_by_case[case_id].append(summary)

    case_summaries = {case_id: summarize_case(runs) for case_id, runs in runs_by_case.items()}
    all_runs = [run for runs in runs_by_case.values() for run in runs]
    planned_run_count = arguments.repeats * len(case_ids)
    rollout_gate = build_rollout_gate(
        planned_run_count=planned_run_count,
        runs=all_runs,
        failures=failures,
        case_summaries=case_summaries,
    )
    report = {
        "result_version": EVIDENCE_CONFORMANCE_COHORT_VERSION,
        "evidence_observation_version": EVIDENCE_OBSERVATION_VERSION,
        "criterion_adjudication_version": CRITERION_ADJUDICATION_VERSION,
        "criterion_decision_version": CRITERION_DECISION_VERSION,
        "corpus": str(arguments.corpus),
        "candidate_model": (
            extraction_model
            if extraction_model == adjudication_model
            else f"extraction={extraction_model};adjudication={adjudication_model}"
        ),
        "model_bindings": {
            "extraction": extraction_model,
            "adjudication": adjudication_model,
        },
        "repeat_count": arguments.repeats,
        "case_ids": case_ids,
        "summary": {
            "rollout_gate_status": rollout_gate["status"],
            "rollout_gate_blocking_reason_count": len(rollout_gate["blocking_reasons"]),
            "planned_run_count": planned_run_count,
            "completed_run_count": len(all_runs),
            "harness_failure_count": len(failures),
            "completed_with_errors_count": sum(value.get("status") == "COMPLETED_WITH_ERRORS" for value in all_runs),
            "incomplete_decision_count": sum(value.get("completed_decision") is not True for value in all_runs),
            "failed_stage_count": sum(int(value.get("failed_stage_count") or 0) for value in all_runs),
            "baseline_agreement_count": sum(value.get("determination_agreement") is True for value in all_runs),
            "criterion_match_agreement_count": sum(
                value.get("criterion_match_agreement") is True for value in all_runs
            ),
            "material_difference_count": sum(value.get("material_difference") is True for value in all_runs),
            "compatibility_output_valid_count": sum(
                value.get("compatibility_output_valid") is True for value in all_runs
            ),
            "synthesis_input_compatible_count": sum(
                value.get("synthesis_input_compatible") is True for value in all_runs
            ),
            "determination_stable_case_count": sum(value["determination_stable"] for value in case_summaries.values()),
            "cases_with_dropped_observations": sum(
                value["runs_with_dropped_observations"] > 0 for value in case_summaries.values()
            ),
            "provider_requests": sum(int(value.get("provider_requests") or 0) for value in all_runs),
            "input_tokens": sum(int(value.get("input_tokens") or 0) for value in all_runs),
            "cached_input_tokens": sum(int(value.get("cached_input_tokens") or 0) for value in all_runs),
            "output_tokens": sum(int(value.get("output_tokens") or 0) for value in all_runs),
            "latency_ms": sum(int(value.get("latency_ms") or 0) for value in all_runs),
        },
        "cases": case_summaries,
        "harness_failures": failures,
        "rollout_gate": rollout_gate,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
