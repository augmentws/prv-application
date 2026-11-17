"""Pure contracts and comparison helpers for assessment model replay corpora."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from app.model_execution import content_hash

ASSESSMENT_REPLAY_CORPUS_VERSION = "assessment-replay-corpus-v1"
ASSESSMENT_REPLAY_RESULT_VERSION = "assessment-replay-result-v1"
DEFAULT_REPLAY_CORPUS_SIZE = 24


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def replay_corpus_hash(manifest: Mapping[str, Any]) -> str:
    """Hash the complete corpus contract, excluding its self-referential hash."""

    payload = deepcopy(dict(manifest))
    payload.pop("corpus_hash", None)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def seal_replay_corpus(manifest: Mapping[str, Any]) -> dict[str, Any]:
    sealed = deepcopy(dict(manifest))
    sealed["corpus_hash"] = replay_corpus_hash(sealed)
    return sealed


def validate_replay_corpus(manifest: Mapping[str, Any]) -> None:
    if manifest.get("corpus_version") != ASSESSMENT_REPLAY_CORPUS_VERSION:
        raise ValueError("Unsupported assessment replay corpus version")
    expected_hash = manifest.get("corpus_hash")
    if not isinstance(expected_hash, str) or expected_hash != replay_corpus_hash(manifest):
        raise ValueError("Assessment replay corpus hash does not match its contents")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Assessment replay corpus must contain at least one case")
    identifiers: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise TypeError("Assessment replay case must be an object")
        document_id = case.get("matter_document_id")
        if not isinstance(document_id, str) or not document_id:
            raise ValueError("Assessment replay case is missing matter_document_id")
        if document_id in identifiers:
            raise ValueError(f"Assessment replay corpus contains duplicate case {document_id}")
        identifiers.add(document_id)
        if not isinstance(case.get("source_hash"), str) or not case["source_hash"]:
            raise ValueError(f"Assessment replay case {document_id} is missing source_hash")
        if not isinstance(case.get("selection_reasons"), list) or not case["selection_reasons"]:
            raise ValueError(f"Assessment replay case {document_id} has no selection reason")


def _stable_rank(seed: str, document_id: str) -> str:
    return hashlib.sha256(f"{seed}:{document_id}".encode()).hexdigest()


def _ranked(
    candidates: Iterable[Mapping[str, Any]],
    *,
    key,
) -> list[Mapping[str, Any]]:
    return sorted(candidates, key=key)


def select_replay_cases(
    candidates: Iterable[Mapping[str, Any]],
    *,
    seed: str,
    target_size: int = DEFAULT_REPLAY_CORPUS_SIZE,
) -> list[dict[str, Any]]:
    """Select a deterministic, failure-heavy corpus with stable controls.

    Failed and replayed documents are always retained. Remaining slots cover
    repair-heavy, multi-request/large, slow, and stable control documents.
    """

    if target_size <= 0:
        raise ValueError("target_size must be positive")
    values = [dict(candidate) for candidate in candidates]
    if not values:
        raise ValueError("No assessment documents are available for replay selection")
    by_id = {str(value["matter_document_id"]): value for value in values}
    if len(by_id) != len(values):
        raise ValueError("Replay selection candidates contain duplicate document identifiers")

    selected: dict[str, set[str]] = {}

    def add(candidate: Mapping[str, Any], reason: str) -> None:
        document_id = str(candidate["matter_document_id"])
        selected.setdefault(document_id, set()).add(reason)

    for candidate in _ranked(
        (value for value in values if value.get("final_status") == "FAILED"),
        key=lambda value: str(value["matter_document_id"]),
    ):
        add(candidate, "FAILED")
    for candidate in _ranked(
        (value for value in values if int(value.get("failed_skill_run_count") or 0) > 0),
        key=lambda value: (
            -int(value.get("failed_skill_run_count") or 0),
            str(value["matter_document_id"]),
        ),
    ):
        add(candidate, "HISTORICAL_FAILURE")
    for candidate in _ranked(
        (value for value in values if int(value.get("skill_run_count") or 0) > 1),
        key=lambda value: (-int(value.get("skill_run_count") or 0), str(value["matter_document_id"])),
    ):
        add(candidate, "REPLAYED")

    strata = (
        (
            "REPAIR_HEAVY",
            8,
            (value for value in values if int(value.get("repair_invocation_count") or 0) > 0),
            lambda value: (
                -int(value.get("repair_invocation_count") or 0),
                -int(value.get("output_tokens") or 0),
                str(value["matter_document_id"]),
            ),
        ),
        (
            "MULTI_REQUEST",
            8,
            (value for value in values if int(value.get("baseline_invocation_count") or 0) > 1),
            lambda value: (
                -int(value.get("baseline_invocation_count") or 0),
                -int(value.get("input_tokens") or 0),
                str(value["matter_document_id"]),
            ),
        ),
        (
            "SLOW",
            6,
            iter(values),
            lambda value: (-int(value.get("latency_ms") or 0), str(value["matter_document_id"])),
        ),
    )
    for reason, limit, pool, sort_key in strata:
        added = 0
        for candidate in _ranked(pool, key=sort_key):
            document_id = str(candidate["matter_document_id"])
            if reason in selected.get(document_id, set()):
                continue
            add(candidate, reason)
            added += 1
            if added >= limit:
                break

    # Trim optional strata deterministically if their union exceeds the target.
    mandatory = {
        document_id
        for document_id, reasons in selected.items()
        if reasons.intersection({"FAILED", "HISTORICAL_FAILURE", "REPLAYED"})
    }
    optional_order = sorted(
        (document_id for document_id in selected if document_id not in mandatory),
        key=lambda document_id: (
            min(
                {"REPAIR_HEAVY": 0, "MULTI_REQUEST": 1, "SLOW": 2}.get(reason, 3)
                for reason in selected[document_id]
            ),
            _stable_rank(seed, document_id),
        ),
    )
    retained = set(mandatory)
    retained.update(optional_order[: max(0, target_size - len(retained))])

    remaining = [value for value in values if str(value["matter_document_id"]) not in retained]
    stable_controls = [
        value
        for value in remaining
        if value.get("final_status") == "COMPLETED"
        and int(value.get("skill_run_count") or 0) == 1
        and int(value.get("failed_skill_run_count") or 0) == 0
        and int(value.get("baseline_invocation_count") or 0) == 1
        and int(value.get("repair_invocation_count") or 0) == 0
    ]
    other_remaining = [value for value in remaining if value not in stable_controls]
    control_order = [
        *sorted(
            stable_controls,
            key=lambda value: _stable_rank(seed, str(value["matter_document_id"])),
        ),
        *sorted(
            other_remaining,
            key=lambda value: _stable_rank(seed, str(value["matter_document_id"])),
        ),
    ]
    for candidate in control_order:
        if len(retained) >= max(target_size, len(mandatory)):
            break
        document_id = str(candidate["matter_document_id"])
        selected[document_id] = {"STABLE_CONTROL"}
        retained.add(document_id)

    result = []
    for document_id in sorted(retained):
        candidate = dict(by_id[document_id])
        candidate["selection_reasons"] = sorted(selected.get(document_id) or {"STABLE_CONTROL"})
        result.append(candidate)
    return result


def analysis_signature(output: Mapping[str, Any]) -> dict[str, Any]:
    matches = output.get("criterion_matches")
    criterion_matches = sorted(
        {
            (str(item.get("criterion_key") or ""), str(item.get("match_type") or ""))
            for item in matches or []
            if isinstance(item, dict)
        }
    )
    coverage = output.get("coverage") if isinstance(output.get("coverage"), dict) else {}
    return {
        "output_hash": content_hash(dict(output)),
        "determination": output.get("determination"),
        "criterion_matches": [
            {"criterion_key": criterion_key, "match_type": match_type}
            for criterion_key, match_type in criterion_matches
        ],
        "clarification_count": len(output.get("clarification_requests") or []),
        "limitation_count": len(output.get("limitations") or []),
        "coverage_status": coverage.get("status"),
    }


def compare_analysis_signatures(
    baseline: Mapping[str, Any] | None,
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    if baseline is None:
        return {
            "baseline_available": False,
            "exact_output_match": None,
            "determination_agreement": None,
            "criterion_match_jaccard": None,
        }
    baseline_pairs = {
        (str(item.get("criterion_key") or ""), str(item.get("match_type") or ""))
        for item in baseline.get("criterion_matches") or []
        if isinstance(item, dict)
    }
    candidate_pairs = {
        (str(item.get("criterion_key") or ""), str(item.get("match_type") or ""))
        for item in candidate.get("criterion_matches") or []
        if isinstance(item, dict)
    }
    union = baseline_pairs | candidate_pairs
    return {
        "baseline_available": True,
        "exact_output_match": baseline.get("output_hash") == candidate.get("output_hash"),
        "determination_agreement": baseline.get("determination") == candidate.get("determination"),
        "criterion_match_jaccard": (
            1.0 if not union else len(baseline_pairs & candidate_pairs) / len(union)
        ),
        "baseline_clarification_count": baseline.get("clarification_count"),
        "candidate_clarification_count": candidate.get("clarification_count"),
    }
