"""Compare evidence-first conformance reports with frozen direct-analysis baselines."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from app.artifact_gateway import read_artifact_bytes
from app.assessment_execution import _records
from app.assessment_replay import analysis_signature, validate_replay_corpus
from app.assessment_synthesis import compile_synthesis_manifest, manifest_summary
from app.database import SessionLocal
from app.document_analysis_schemas import DOCUMENT_ANALYSIS_OUTPUT_SCHEMA
from app.evidence_compatibility_projection import assemble_direct_compatibility_output
from app.evidence_first_contracts import (
    UNMAPPED_CRITERION_KEY,
    AggregatedEvidenceObservation,
    CriterionDecision,
    DocumentDecision,
    criterion_catalog_from_retrieval_plan,
)
from app.model_execution import content_hash, validate_structured_output

EVIDENCE_BACKTEST_VERSION = "evidence-compatibility-backtest-v1"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare one repeat of content-free evidence conformance reports with the frozen direct outputs, "
            "and compile both populations through the current synthesis-manifest contract. No model is called "
            "and no assessment state is modified."
        )
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--report-directory", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _coverage_from_report(report: dict[str, Any], *, paragraph_map_version: str) -> dict[str, Any]:
    coverage = report.get("coverage") or {}
    complete = bool(coverage.get("complete"))
    return {
        "status": "COMPLETE" if complete else "PARTIAL",
        "map_plan_version": "evidence-conformance-report-v1",
        "paragraph_map_version": paragraph_map_version,
        "window_count": max(1, int(coverage.get("window_count") or 1)),
        "successful_window_count": int(coverage.get("completed_window_count") or 0),
        "analyzed_paragraph_ids": [],
        "partial_paragraph_ids": [],
        "omitted_ranges": [],
    }


def compatibility_output_from_report(
    report: dict[str, Any],
    *,
    catalog,
    paragraph_map_version: str,
) -> dict[str, Any]:
    existing = report.get("compatibility_output")
    if isinstance(existing, dict):
        validate_structured_output(existing, DOCUMENT_ANALYSIS_OUTPUT_SCHEMA)
        return existing
    raw_decision = report.get("document_decision")
    if not isinstance(raw_decision, dict):
        raise TypeError("Conformance report has no completed document decision")
    decisions = [
        CriterionDecision.model_validate(value["decision"])
        for value in report.get("adjudication") or []
        if isinstance(value, dict) and isinstance(value.get("decision"), dict)
    ]
    observations = [
        AggregatedEvidenceObservation(
            observation_id=str(value["observation_id"]),
            criterion_key=str(value["criterion_key"]),
            signal_type=str(value.get("signal_type") or "SUPPORT"),
            fact="Evidence fact withheld from content-free conformance report.",
            citation_ids=tuple(value.get("citation_ids") or ()),
            source_unit_ids=tuple(value.get("source_unit_ids") or ("content-free-report",)),
        )
        for value in (report.get("aggregation") or {}).get("observation_signatures") or []
        if isinstance(value, dict)
    ]
    adjustment = report.get("adjustment_summary") or {}
    criterion_counts = (report.get("aggregation") or {}).get("criterion_counts") or {}
    return assemble_direct_compatibility_output(
        document_decision=DocumentDecision.model_validate(raw_decision),
        criterion_decisions=decisions,
        observations=observations,
        catalog=catalog,
        coverage=_coverage_from_report(
            report,
            paragraph_map_version=paragraph_map_version,
        ),
        evidence_complete=bool((report.get("coverage") or {}).get("evidence_complete")),
        material_adjustment_count=sum(
            int(adjustment.get(key) or 0)
            for key in (
                "dropped_observation_count",
                "extraction_adjustment_count",
                "adjudication_adjustment_count",
            )
        ),
        unmapped_observation_count=int(criterion_counts.get(UNMAPPED_CRITERION_KEY) or 0),
        include_evidence_digest=False,
    )


def compare_analysis_signatures(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    baseline_matches = {
        (str(value["criterion_key"]), str(value["match_type"]))
        for value in baseline.get("criterion_matches") or []
    }
    candidate_matches = {
        (str(value["criterion_key"]), str(value["match_type"]))
        for value in candidate.get("criterion_matches") or []
    }
    baseline_positive = {
        key for key, match_type in baseline_matches if match_type in {"PRIMARY", "SECONDARY"}
    }
    candidate_positive = {
        key for key, match_type in candidate_matches if match_type in {"PRIMARY", "SECONDARY"}
    }
    determination_agreement = baseline.get("determination") == candidate.get("determination")
    return {
        "baseline_determination": baseline.get("determination"),
        "candidate_determination": candidate.get("determination"),
        "determination_agreement": determination_agreement,
        "material_decision_difference": not determination_agreement,
        "criterion_matches_agree": baseline_matches == candidate_matches,
        "positive_criterion_keys_agree": baseline_positive == candidate_positive,
        "baseline_only_criterion_matches": sorted(baseline_matches - candidate_matches),
        "candidate_only_criterion_matches": sorted(candidate_matches - baseline_matches),
        "baseline_coverage_status": baseline.get("coverage_status"),
        "candidate_coverage_status": candidate.get("coverage_status"),
        "coverage_status_agreement": (
            baseline.get("coverage_status") == candidate.get("coverage_status")
        ),
    }


def _load_baseline(
    case: dict[str, Any],
    *,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> dict[str, Any]:
    baseline = case.get("baseline") or {}
    artifact_id = baseline.get("output_artifact_id")
    expected_hash = baseline.get("artifact_sha256")
    if not artifact_id or not expected_hash:
        raise ValueError("Frozen case does not have a complete direct baseline artifact")
    raw = read_artifact_bytes(
        artifact_id=uuid.UUID(str(artifact_id)),
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError("Direct baseline artifact hash no longer matches the frozen corpus")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        raise TypeError("Direct baseline artifact does not contain a document result")
    return payload["result"]


def main() -> None:
    arguments = _arguments()
    if arguments.repeat <= 0:
        raise ValueError("--repeat must be positive")
    corpus = json.loads(arguments.corpus.read_text(encoding="utf-8"))
    validate_replay_corpus(corpus)
    cases = {
        str(value["matter_document_id"]): value for value in corpus.get("cases") or []
    }
    assessment_id = uuid.UUID(str(corpus["source_assessment"]["assessment_id"]))
    comparisons: list[dict[str, Any]] = []
    baseline_analyses: list[dict[str, Any]] = []
    candidate_analyses: list[dict[str, Any]] = []
    missing_reports: list[str] = []
    invalid_cases: list[dict[str, str]] = []

    with SessionLocal() as db:
        assessment, workflow, _revision, matter = _records(db, assessment_id)
        retrieval_plan = assessment.configuration_snapshot.get("retrieval_plan")
        if not isinstance(retrieval_plan, dict):
            raise TypeError("Assessment does not contain a frozen retrieval plan")
        catalog = criterion_catalog_from_retrieval_plan(retrieval_plan)
        for document_id, case in cases.items():
            report_path = (
                arguments.report_directory
                / f"{document_id}-repeat-{arguments.repeat}.json"
            )
            if not report_path.exists():
                missing_reports.append(document_id)
                continue
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                if report.get("corpus_hash") != corpus.get("corpus_hash"):
                    raise ValueError("Conformance report belongs to a different frozen corpus")
                if report.get("matter_document_id") != document_id:
                    raise ValueError("Conformance report document identity does not match its filename")
                baseline_output = _load_baseline(
                    case,
                    actor_user_id=assessment.initiated_by_user_id,
                    tenant_id=workflow.tenant_id,
                    client_id=matter.client_id,
                )
                candidate_output = compatibility_output_from_report(
                    report,
                    catalog=catalog,
                    paragraph_map_version=str(
                        corpus.get("contract", {}).get("paragraph_map_version")
                        or "paragraph-v1"
                    ),
                )
                baseline_signature = analysis_signature(baseline_output)
                candidate_signature = analysis_signature(candidate_output)
                comparison = compare_analysis_signatures(
                    baseline_signature,
                    candidate_signature,
                )
                comparisons.append(
                    {
                        "matter_document_id": document_id,
                        "report_version": report.get("result_version"),
                        "report_status": report.get("status"),
                        "comparison": comparison,
                    }
                )
                baseline_analyses.append(
                    {"matter_document_id": document_id, "analysis": baseline_output}
                )
                candidate_analyses.append(
                    {"matter_document_id": document_id, "analysis": candidate_output}
                )
            except Exception as exc:  # noqa: BLE001 - preserve every case-level diagnostic
                invalid_cases.append(
                    {
                        "matter_document_id": document_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        coverage = {
            "selected_document_count": len(cases),
            "analyzed_document_count": len(candidate_analyses),
            "skipped_document_count": len(missing_reports),
            "failed_document_count": len(invalid_cases),
            "partial_coverage_document_count": sum(
                value["comparison"]["candidate_coverage_status"] != "COMPLETE"
                for value in comparisons
            ),
            "invalid_result_count": len(invalid_cases),
        }
        baseline_manifest = compile_synthesis_manifest(
            baseline_analyses,
            coverage,
            retrieval_plan,
        )
        candidate_manifest = compile_synthesis_manifest(
            candidate_analyses,
            coverage,
            retrieval_plan,
        )

    material_differences = [
        value
        for value in comparisons
        if value["comparison"]["material_decision_difference"]
    ]
    criterion_differences = [
        value
        for value in comparisons
        if not value["comparison"]["criterion_matches_agree"]
    ]
    report = {
        "result_version": EVIDENCE_BACKTEST_VERSION,
        "corpus_hash": corpus["corpus_hash"],
        "source_assessment_id": str(assessment_id),
        "repeat": arguments.repeat,
        "summary": {
            "frozen_case_count": len(cases),
            "compared_case_count": len(comparisons),
            "missing_report_count": len(missing_reports),
            "invalid_case_count": len(invalid_cases),
            "determination_agreement_count": sum(
                value["comparison"]["determination_agreement"]
                for value in comparisons
            ),
            "material_decision_difference_count": len(material_differences),
            "criterion_match_agreement_count": sum(
                value["comparison"]["criterion_matches_agree"]
                for value in comparisons
            ),
            "criterion_difference_count": len(criterion_differences),
            "baseline_synthesis_input_compatible": True,
            "candidate_synthesis_input_compatible": True,
        },
        "synthesis_manifests": {
            "baseline": manifest_summary(baseline_manifest),
            "candidate": manifest_summary(candidate_manifest),
            "unit_counts_agree": (
                manifest_summary(baseline_manifest)["unit_counts"]
                == manifest_summary(candidate_manifest)["unit_counts"]
            ),
        },
        "material_decision_differences": material_differences,
        "criterion_differences": criterion_differences,
        "comparisons": comparisons,
        "missing_reports": missing_reports,
        "invalid_cases": invalid_cases,
    }
    report["report_hash"] = content_hash(report)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
