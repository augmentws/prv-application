import copy
import re
from collections import Counter, defaultdict
from typing import Any

from app.document_analysis_schemas import is_supported_evidence_citation
from app.model_execution import content_hash
from app.standard_skills import SYNTHESIS_EVIDENCE_SCHEMA

SYNTHESIS_MANIFEST_VERSION = "assessment_synthesis_manifest_v1"
SYNTHESIS_WORKFLOW_VERSION = "assessment_synthesis_units_v1"

REFINEMENT_DIMENSIONS = (
    "INCLUSION_EXCLUSION_BOUNDARIES",
    "UNCOVERED_SUBJECTS",
    "CONFLICTING_TREATMENT",
    "TEMPORAL_SCOPE",
    "GEOGRAPHIC_SCOPE",
    "ACTOR_ENTITY_SCOPE",
)

_EXPLICIT_CANONICAL_ALIASES = {
    "attorney_client": "attorney_client_privilege",
    "legal_advice_or_legal_services": "attorney_client_privilege",
    "work_product": "attorney_work_product",
    "work_product_litigation": "attorney_work_product",
    "work_product_protection": "attorney_work_product",
}

OTHER_UNMAPPED_CRITERIA_KEY = "OTHER_UNMAPPED_CRITERIA"
UNMAPPED_REPRESENTATIVE_EVIDENCE_LIMIT = 3


def normalize_criterion_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")


def _canonical_criteria(retrieval_plan: dict[str, Any]) -> list[dict[str, Any]]:
    criteria: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ordinal, raw in enumerate(retrieval_plan.get("criteria") or [], start=1):
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("criterion_key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        criteria.append(
            {
                "criterion_key": key,
                "criterion_label": str(raw.get("criterion_label") or key),
                "description": str(raw.get("description") or ""),
                "instruction_reference": str(raw.get("instruction_reference") or ""),
                "ordinal": ordinal,
            }
        )
    return criteria


def _criterion_alias_index(criteria: list[dict[str, Any]]) -> tuple[dict[str, str], set[str]]:
    candidates: dict[str, set[str]] = defaultdict(set)
    canonical_by_normalized_key: dict[str, str] = {}
    for criterion in criteria:
        key = criterion["criterion_key"]
        normalized_key = normalize_criterion_key(key)
        canonical_by_normalized_key[normalized_key] = key
        candidates[normalized_key].add(key)
        candidates[normalize_criterion_key(criterion["criterion_label"])].add(key)

    for alias, target in _EXPLICIT_CANONICAL_ALIASES.items():
        canonical = canonical_by_normalized_key.get(target)
        if canonical is not None:
            candidates[alias].add(canonical)

    aliases = {alias: next(iter(values)) for alias, values in candidates.items() if len(values) == 1}
    ambiguous = {alias for alias, values in candidates.items() if len(values) > 1}
    return aliases, ambiguous


def _evidence(document_id: str, match: dict[str, Any]) -> dict[str, Any] | None:
    paragraph_ids = list(
        dict.fromkeys(
            str(value)
            for value in match.get("citation_ids") or []
            if is_supported_evidence_citation(value)
        )
    )
    reason = str(match.get("reasoning") or "").strip()
    if not paragraph_ids or not reason:
        return None
    return {
        "matter_document_id": document_id,
        "paragraph_ids": paragraph_ids,
        "reason": reason,
    }


def _supported_evidence_items(evidence: Any) -> list[dict[str, Any]]:
    supported: list[dict[str, Any]] = []
    for value in evidence if isinstance(evidence, list) else []:
        if not isinstance(value, dict):
            continue
        paragraph_ids = list(
            dict.fromkeys(
                str(citation_id)
                for citation_id in value.get("paragraph_ids") or []
                if is_supported_evidence_citation(citation_id)
            )
        )
        if not paragraph_ids:
            continue
        supported.append({**value, "paragraph_ids": paragraph_ids})
    return supported


def compile_synthesis_manifest(
    analyses: list[dict[str, Any]],
    coverage: dict[str, Any],
    retrieval_plan: dict[str, Any],
) -> dict[str, Any]:
    criteria = _canonical_criteria(retrieval_plan)
    aliases, ambiguous_aliases = _criterion_alias_index(criteria)
    criterion_by_key = {criterion["criterion_key"]: criterion for criterion in criteria}
    determination_counts: Counter[str] = Counter()
    match_type_counts: Counter[str] = Counter()
    assignments: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    near_misses: dict[str, list[dict[str, Any]]] = defaultdict(list)
    clarification_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    alias_counts: Counter[tuple[str, str]] = Counter()
    unmapped_counts: Counter[str] = Counter()
    unmapped_occurrences: dict[str, list[dict[str, Any]]] = defaultdict(list)
    limitation_count = 0
    limitation_examples: list[dict[str, Any]] = []

    for row in sorted(analyses, key=lambda value: str(value.get("matter_document_id") or "")):
        document_id = str(row["matter_document_id"])
        analysis = row.get("analysis") or {}
        determination_counts[str(analysis.get("determination") or "UNKNOWN")] += 1
        limitation_values = [str(value) for value in analysis.get("limitations") or [] if str(value).strip()]
        limitation_count += len(limitation_values)
        if limitation_values and len(limitation_examples) < 50:
            limitation_examples.append({"matter_document_id": document_id, "limitations": limitation_values})

        for match in analysis.get("criterion_matches") or []:
            if not isinstance(match, dict):
                continue
            raw_key = str(match.get("criterion_key") or "").strip()
            normalized = normalize_criterion_key(raw_key)
            canonical_key = aliases.get(normalized)
            match_type = str(match.get("match_type") or "UNKNOWN")
            match_type_counts[match_type] += 1
            if canonical_key is None:
                source_key = raw_key or "UNSPECIFIED"
                unmapped_counts[source_key] += 1
                evidence = _evidence(document_id, match)
                unmapped_occurrences[source_key].append(
                    {
                        "matter_document_id": document_id,
                        "source_key": source_key,
                        "label": str(match.get("label") or source_key),
                        "match_type": match_type,
                        "confidence": max(
                            0.0,
                            min(
                                1.0,
                                float(match.get("confidence") or analysis.get("confidence") or 0),
                            ),
                        ),
                        "evidence": [evidence] if evidence is not None else [],
                    }
                )
                continue
            if raw_key != canonical_key:
                alias_counts[(raw_key, canonical_key)] += 1
            evidence = _evidence(document_id, match)
            if evidence is None:
                continue
            if match_type in {"PRIMARY", "SECONDARY"}:
                existing = assignments[canonical_key].get(document_id)
                confidence = float(match.get("confidence") or analysis.get("confidence") or 0)
                candidate = {
                    "matter_document_id": document_id,
                    "confidence": max(0.0, min(1.0, confidence)),
                    "match_type": match_type,
                    "evidence": [evidence],
                }
                if existing is None:
                    assignments[canonical_key][document_id] = candidate
                else:
                    existing["confidence"] = max(existing["confidence"], candidate["confidence"])
                    existing["evidence"].append(evidence)
                    if match_type == "PRIMARY":
                        existing["match_type"] = "PRIMARY"
            elif match_type == "NEAR_MISS":
                near_misses[canonical_key].append(
                    {
                        "matter_document_id": document_id,
                        "original_criterion_key": raw_key,
                        "label": str(match.get("label") or criterion_by_key[canonical_key]["criterion_label"]),
                        "evidence": evidence,
                    }
                )

        for candidate in analysis.get("clarification_requests") or []:
            if not isinstance(candidate, dict):
                continue
            raw_key = str(candidate.get("criterion_key") or "").strip()
            canonical_key = aliases.get(normalize_criterion_key(raw_key)) if raw_key else None
            group_key = canonical_key or "GENERAL"
            citation_ids = list(
                dict.fromkeys(
                    str(value)
                    for value in candidate.get("citation_ids") or []
                    if is_supported_evidence_citation(value)
                )
            )
            clarification_candidates[group_key].append(
                {
                    "matter_document_id": document_id,
                    "question": str(candidate.get("question") or ""),
                    "rationale": str(candidate.get("rationale") or ""),
                    "blocking": bool(candidate.get("blocking", False)),
                    "instruction_references": list(candidate.get("instruction_references") or []),
                    "paragraph_ids": citation_ids,
                }
            )

    topic_units = []
    for criterion in criteria:
        key = criterion["criterion_key"]
        topic_units.append(
            {
                "unit_type": "TOPIC_FINDING",
                "unit_key": key,
                "ordinal": criterion["ordinal"],
                "criterion": criterion,
                "assignments": sorted(
                    assignments.get(key, {}).values(),
                    key=lambda value: (value["matter_document_id"], value["match_type"]),
                ),
            }
        )

    unmapped_groups = []
    for source_key, occurrences in sorted(unmapped_occurrences.items()):
        unmapped_groups.append(
            {
                "source_key": source_key,
                "occurrence_count": len(occurrences),
                "document_count": len({str(value["matter_document_id"]) for value in occurrences}),
                "match_type_counts": dict(sorted(Counter(str(value["match_type"]) for value in occurrences).items())),
                "representative_evidence": occurrences[:UNMAPPED_REPRESENTATIVE_EVIDENCE_LIMIT],
            }
        )
    other_unmapped_criteria = {
        "criterion_key": "other_unmapped_criteria",
        "label": "Other / unmapped criteria",
        "description": (
            "Reporting-only matches whose labels could not be safely mapped to an authoritative "
            "Review Guidance criterion."
        ),
        "reporting_only": True,
        "occurrence_count": sum(unmapped_counts.values()),
        "document_count": len(
            {str(value["matter_document_id"]) for occurrences in unmapped_occurrences.values() for value in occurrences}
        ),
        "items": unmapped_groups,
    }

    signal_units = []
    signal_ordinal = 0
    signal_keys = sorted(set(near_misses) | set(clarification_candidates))
    for key in signal_keys:
        occurrences = near_misses.get(key, [])
        document_count = len({value["matter_document_id"] for value in occurrences})
        candidates = clarification_candidates.get(key, [])
        if document_count < 2 and not candidates:
            continue
        signal_ordinal += 1
        signal_id = f"NEAR_MISS_{normalize_criterion_key(key).upper()}"
        signal_units.append(
            {
                "unit_type": "SIGNAL_DISPOSITION",
                "unit_key": signal_id,
                "ordinal": signal_ordinal,
                "criterion_key": key,
                "criterion": criterion_by_key.get(key),
                "occurrence_count": len(occurrences),
                "document_count": document_count,
                "representative_evidence": occurrences[:8],
                "clarification_candidates": candidates[:20],
            }
        )
    if unmapped_groups:
        signal_ordinal += 1
        signal_units.append(
            {
                "unit_type": "SIGNAL_DISPOSITION",
                "unit_key": OTHER_UNMAPPED_CRITERIA_KEY,
                "ordinal": signal_ordinal,
                "criterion_key": "other_unmapped_criteria",
                "criterion": None,
                "occurrence_count": other_unmapped_criteria["occurrence_count"],
                "document_count": other_unmapped_criteria["document_count"],
                "representative_evidence": [
                    evidence for group in unmapped_groups for evidence in group["representative_evidence"]
                ],
                "clarification_candidates": [],
                "unmapped_criteria": unmapped_groups,
                "reporting_only": True,
            }
        )

    dimension_units = [
        {
            "unit_type": "REFINEMENT_DIMENSION",
            "unit_key": dimension,
            "ordinal": ordinal,
            "dimension": dimension,
        }
        for ordinal, dimension in enumerate(REFINEMENT_DIMENSIONS, start=1)
    ]
    units = [
        *topic_units,
        *signal_units,
        *dimension_units,
        {"unit_type": "FINAL_NARRATIVE", "unit_key": "FINAL", "ordinal": 1},
    ]
    manifest = {
        "version": SYNTHESIS_MANIFEST_VERSION,
        "coverage": coverage,
        "criteria": criteria,
        "statistics": {
            "selected_document_count": int(coverage.get("selected_document_count", 0)),
            "analyzed_document_count": len(analyses),
            "determination_counts": dict(sorted(determination_counts.items())),
            "criterion_match_type_counts": dict(sorted(match_type_counts.items())),
            "document_limitation_count": limitation_count,
            "document_limitation_examples": limitation_examples,
        },
        "canonicalization": {
            "aliases": [
                {
                    "source_key": source,
                    "canonical_key": target,
                    "occurrence_count": count,
                }
                for (source, target), count in sorted(alias_counts.items())
            ],
            "ambiguous_aliases": sorted(ambiguous_aliases),
            "unmapped": [
                {"source_key": source, "occurrence_count": count} for source, count in sorted(unmapped_counts.items())
            ],
        },
        "unmapped_criterion_occurrences": [
            {"source_key": source_key, "occurrences": occurrences}
            for source_key, occurrences in sorted(unmapped_occurrences.items())
        ],
        "other_unmapped_criteria": other_unmapped_criteria,
        "units": units,
    }
    manifest["content_hash"] = content_hash(manifest)
    return manifest


def manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    units = manifest.get("units") or []
    counts = Counter(str(unit.get("unit_type")) for unit in units if isinstance(unit, dict))
    return {
        "version": manifest.get("version"),
        "content_hash": manifest.get("content_hash"),
        "criterion_count": len(manifest.get("criteria") or []),
        "unit_counts": dict(sorted(counts.items())),
        "canonicalization": manifest.get("canonicalization") or {},
        "other_unmapped_criteria": manifest.get("other_unmapped_criteria") or {},
    }


TOPIC_FINDING_SCHEMA = {
    "type": "object",
    "required": ["topic_key", "label", "description", "finding", "limitations", "evidence"],
    "properties": {
        "topic_key": {"type": "string", "minLength": 1},
        "label": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "finding": {"type": "string"},
        "limitations": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "evidence": {"type": "array", "items": SYNTHESIS_EVIDENCE_SCHEMA},
    },
    "additionalProperties": False,
}

SIGNAL_DISPOSITION_SCHEMA = {
    "type": "object",
    "required": [
        "signal_id",
        "disposition",
        "rationale",
        "question",
        "priority",
        "blocking",
        "instruction_references",
        "evidence",
        "suggested_answers",
    ],
    "properties": {
        "signal_id": {"type": "string", "minLength": 1},
        "disposition": {"enum": ["QUESTION_PROPOSED", "GUIDANCE_SUFFICIENT", "INSUFFICIENT_EVIDENCE"]},
        "rationale": {"type": "string", "minLength": 1},
        "question": {"type": "string"},
        "priority": {"enum": ["HIGH", "MEDIUM", "LOW"]},
        "blocking": {"type": "boolean"},
        "instruction_references": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "evidence": {"type": "array", "items": SYNTHESIS_EVIDENCE_SCHEMA},
        "suggested_answers": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
    },
    "additionalProperties": False,
}

REFINEMENT_DIMENSION_SCHEMA = {
    "type": "object",
    "required": [
        "dimension",
        "conclusion",
        "rationale",
        "evidence",
        "question",
        "priority",
        "blocking",
        "instruction_references",
        "suggested_answers",
    ],
    "properties": {
        "dimension": {"enum": list(REFINEMENT_DIMENSIONS)},
        "conclusion": {"enum": ["QUESTION_NEEDED", "NO_REFINEMENT_NEEDED", "NOT_EVALUATED"]},
        "rationale": {"type": "string", "minLength": 1},
        "evidence": {"type": "array", "items": SYNTHESIS_EVIDENCE_SCHEMA},
        "question": {"type": "string"},
        "priority": {"enum": ["HIGH", "MEDIUM", "LOW"]},
        "blocking": {"type": "boolean"},
        "instruction_references": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "suggested_answers": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
    },
    "additionalProperties": False,
}

FINAL_NARRATIVE_SCHEMA = {
    "type": "object",
    "required": ["narrative"],
    "properties": {"narrative": {"type": "string", "minLength": 1}},
    "additionalProperties": False,
}

UNIT_OUTPUT_SCHEMAS = {
    "TOPIC_FINDING": TOPIC_FINDING_SCHEMA,
    "SIGNAL_DISPOSITION": SIGNAL_DISPOSITION_SCHEMA,
    "REFINEMENT_DIMENSION": REFINEMENT_DIMENSION_SCHEMA,
    "FINAL_NARRATIVE": FINAL_NARRATIVE_SCHEMA,
}

UNIT_EXECUTION_LIMITS = {
    "TOPIC_FINDING": {"max_requests": 4, "max_output_retries": 2},
    "SIGNAL_DISPOSITION": {"max_requests": 4, "max_output_retries": 2},
    "REFINEMENT_DIMENSION": {"max_requests": 4, "max_output_retries": 2},
    "FINAL_NARRATIVE": {"max_requests": 4, "max_output_retries": 2},
}

REQUIRED_UNIT_TYPES = {"TOPIC_FINDING", "REFINEMENT_DIMENSION", "FINAL_NARRATIVE"}
REPRESENTATIVE_EVIDENCE_LIMIT = 3
REPRESENTATIVE_PARAGRAPH_LIMIT = 3


def synthesis_unit_execution_limits(unit_type: str, skill_limits: dict[str, Any]) -> dict[str, Any]:
    """Use stage retry limits while inheriting token safety ceilings from the managed skill."""

    limits = dict(UNIT_EXECUTION_LIMITS[unit_type])
    for key in ("max_input_tokens", "max_output_tokens", "max_total_tokens"):
        if skill_limits.get(key) is not None:
            limits[key] = skill_limits[key]
    return limits


def _evidence_references(value: Any) -> set[tuple[str, str]]:
    references: set[tuple[str, str]] = set()
    if isinstance(value, dict):
        document_id = str(value.get("matter_document_id") or "")
        paragraph_ids = value.get("paragraph_ids")
        if document_id and isinstance(paragraph_ids, list):
            references.update((document_id, str(paragraph_id)) for paragraph_id in paragraph_ids)
        for child in value.values():
            references.update(_evidence_references(child))
    elif isinstance(value, list):
        for child in value:
            references.update(_evidence_references(child))
    return references


def _normalization_warning(
    code: str,
    path: str,
    message: str,
    *,
    original_value: Any,
    normalized_value: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "path": path,
        "message": message,
        "original_value": original_value,
        "normalized_value": normalized_value,
    }


def _normalize_evidence(
    evidence: Any,
    *,
    allowed_context: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    allowed_references = _evidence_references(allowed_context)
    allowed_by_document: dict[str, set[str]] = defaultdict(set)
    for document_id, paragraph_id in allowed_references:
        allowed_by_document[document_id].add(paragraph_id)

    normalized: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for index, raw_item in enumerate(evidence if isinstance(evidence, list) else []):
        if not isinstance(raw_item, dict):
            continue
        item = copy.deepcopy(raw_item)
        document_id = str(item.get("matter_document_id") or "")
        raw_paragraph_ids = [str(value) for value in item.get("paragraph_ids") or []]
        valid_paragraph_ids = list(
            dict.fromkeys(
                paragraph_id
                for paragraph_id in raw_paragraph_ids
                if paragraph_id in allowed_by_document.get(document_id, set())
                and is_supported_evidence_citation(paragraph_id)
            )
        )
        if not valid_paragraph_ids:
            warnings.append(
                _normalization_warning(
                    "UNSUPPORTED_EVIDENCE_DROPPED",
                    f"evidence.{index}",
                    "The evidence item did not reference a document paragraph supplied to this unit.",
                    original_value=raw_item,
                    normalized_value=None,
                )
            )
            continue
        if valid_paragraph_ids != raw_paragraph_ids:
            warnings.append(
                _normalization_warning(
                    "UNSUPPORTED_PARAGRAPH_REFERENCES_DROPPED",
                    f"evidence.{index}.paragraph_ids",
                    "Paragraph references outside the unit input were removed.",
                    original_value=raw_paragraph_ids,
                    normalized_value=valid_paragraph_ids,
                )
            )
        if len(valid_paragraph_ids) > REPRESENTATIVE_PARAGRAPH_LIMIT:
            retained = valid_paragraph_ids[:REPRESENTATIVE_PARAGRAPH_LIMIT]
            warnings.append(
                _normalization_warning(
                    "EXCESS_PARAGRAPH_REFERENCES_DROPPED",
                    f"evidence.{index}.paragraph_ids",
                    "Only representative paragraph references are retained.",
                    original_value=valid_paragraph_ids,
                    normalized_value=retained,
                )
            )
            valid_paragraph_ids = retained
        item["paragraph_ids"] = valid_paragraph_ids
        normalized.append(item)

    if len(normalized) > REPRESENTATIVE_EVIDENCE_LIMIT:
        retained = normalized[:REPRESENTATIVE_EVIDENCE_LIMIT]
        warnings.append(
            _normalization_warning(
                "EXCESS_EVIDENCE_DROPPED",
                "evidence",
                "Only representative evidence items are retained.",
                original_value=normalized,
                normalized_value=retained,
            )
        )
        normalized = retained
    return normalized, warnings


def normalize_unit_output(
    unit: dict[str, Any],
    output: dict[str, Any],
    *,
    allowed_context: Any | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Repair recoverable unit-output drift without inventing substantive content."""

    normalized = copy.deepcopy(output)
    warnings: list[dict[str, Any]] = []
    unit_type = str(unit["unit_type"])
    unit_key = str(unit["unit_key"])

    authoritative_fields = {
        "TOPIC_FINDING": "topic_key",
        "SIGNAL_DISPOSITION": "signal_id",
        "REFINEMENT_DIMENSION": "dimension",
    }
    authoritative_field = authoritative_fields.get(unit_type)
    if authoritative_field is not None and normalized.get(authoritative_field) != unit_key:
        original = normalized.get(authoritative_field)
        normalized[authoritative_field] = unit_key
        warnings.append(
            _normalization_warning(
                "AUTHORITATIVE_UNIT_KEY_RESTORED",
                authoritative_field,
                "The model-supplied unit identifier was replaced with the manifest identifier.",
                original_value=original,
                normalized_value=unit_key,
            )
        )

    if unit_type == "TOPIC_FINDING":
        criterion = unit.get("criterion") or {}
        for field, authoritative_value in (
            ("label", criterion.get("criterion_label")),
            ("description", criterion.get("description")),
        ):
            if authoritative_value is not None and normalized.get(field) != authoritative_value:
                original = normalized.get(field)
                normalized[field] = authoritative_value
                warnings.append(
                    _normalization_warning(
                        "AUTHORITATIVE_TOPIC_METADATA_RESTORED",
                        field,
                        "Model-supplied topic metadata was replaced with manifest metadata.",
                        original_value=original,
                        normalized_value=authoritative_value,
                    )
                )

    if unit_type in {"TOPIC_FINDING", "SIGNAL_DISPOSITION", "REFINEMENT_DIMENSION"}:
        evidence, evidence_warnings = _normalize_evidence(
            normalized.get("evidence"),
            allowed_context=allowed_context if allowed_context is not None else unit,
        )
        normalized["evidence"] = evidence
        warnings.extend(evidence_warnings)

    if unit_type == "TOPIC_FINDING" and not normalized.get("evidence"):
        finding = normalized.get("finding")
        if finding:
            normalized["finding"] = ""
            warnings.append(
                _normalization_warning(
                    "UNSUPPORTED_FINDING_DROPPED",
                    "finding",
                    "The finding was dropped because no supplied evidence supports it.",
                    original_value=finding,
                    normalized_value="",
                )
            )

    if unit_type == "SIGNAL_DISPOSITION":
        disposition = normalized.get("disposition")
        question = str(normalized.get("question") or "").strip()
        answers = normalized.get("suggested_answers")
        valid_answers = isinstance(answers, list) and 1 <= len(answers) <= 3
        if disposition in {"QUESTION_PROPOSED", "GUIDANCE_SUFFICIENT"} and not normalized.get("evidence"):
            normalized["disposition"] = "INSUFFICIENT_EVIDENCE"
            warnings.append(
                _normalization_warning(
                    "UNSUPPORTED_SIGNAL_DISPOSITION_DOWNGRADED",
                    "disposition",
                    "A disposition requiring evidence was downgraded after unsupported evidence was removed.",
                    original_value=disposition,
                    normalized_value="INSUFFICIENT_EVIDENCE",
                )
            )
            disposition = "INSUFFICIENT_EVIDENCE"
        elif disposition == "QUESTION_PROPOSED" and (not question or not valid_answers):
            normalized["disposition"] = "INSUFFICIENT_EVIDENCE"
            warnings.append(
                _normalization_warning(
                    "INCOMPLETE_QUESTION_DROPPED",
                    "disposition",
                    "An incomplete proposed question was downgraded instead of forcing a policy choice.",
                    original_value=disposition,
                    normalized_value="INSUFFICIENT_EVIDENCE",
                )
            )
            disposition = "INSUFFICIENT_EVIDENCE"
        if disposition != "QUESTION_PROPOSED":
            for field, empty_value in (("question", ""), ("suggested_answers", [])):
                if normalized.get(field):
                    original = normalized.get(field)
                    normalized[field] = empty_value
                    warnings.append(
                        _normalization_warning(
                            "INAPPLICABLE_QUESTION_CONTENT_DROPPED",
                            field,
                            "Question content was removed because the disposition does not propose a question.",
                            original_value=original,
                            normalized_value=empty_value,
                        )
                    )

    if unit_type == "REFINEMENT_DIMENSION":
        conclusion = normalized.get("conclusion")
        question = str(normalized.get("question") or "").strip()
        answers = normalized.get("suggested_answers")
        valid_answers = isinstance(answers, list) and 1 <= len(answers) <= 3
        if conclusion == "QUESTION_NEEDED" and (
            not normalized.get("evidence") or not question or not valid_answers
        ):
            normalized["conclusion"] = "NOT_EVALUATED"
            warnings.append(
                _normalization_warning(
                    "UNSUPPORTED_REFINEMENT_QUESTION_DOWNGRADED",
                    "conclusion",
                    "A refinement question lacking complete support was downgraded instead of forced.",
                    original_value=conclusion,
                    normalized_value="NOT_EVALUATED",
                )
            )
            conclusion = "NOT_EVALUATED"
        if conclusion != "QUESTION_NEEDED":
            for field, empty_value in (("question", ""), ("suggested_answers", [])):
                if normalized.get(field):
                    original = normalized.get(field)
                    normalized[field] = empty_value
                    warnings.append(
                        _normalization_warning(
                            "INAPPLICABLE_QUESTION_CONTENT_DROPPED",
                            field,
                            "Question content was removed because the conclusion does not request a question.",
                            original_value=original,
                            normalized_value=empty_value,
                        )
                    )

    return normalized, warnings


def validate_unit_output(
    unit: dict[str, Any],
    output: dict[str, Any],
    *,
    allowed_context: Any | None = None,
) -> None:
    unit_type = str(unit["unit_type"])
    unit_key = str(unit["unit_key"])
    if unit_type == "TOPIC_FINDING" and output.get("topic_key") != unit_key:
        raise ValueError(f"topic finding must return topic_key {unit_key!r}")
    if unit_type == "SIGNAL_DISPOSITION":
        if output.get("signal_id") != unit_key:
            raise ValueError(f"signal disposition must return signal_id {unit_key!r}")
        disposition = output.get("disposition")
        if disposition == "QUESTION_PROPOSED":
            if not str(output.get("question") or "").strip():
                raise ValueError("QUESTION_PROPOSED requires a question")
            suggestions = output.get("suggested_answers")
            if not isinstance(suggestions, list) or not 1 <= len(suggestions) <= 3:
                raise ValueError("QUESTION_PROPOSED requires one to three suggested answers")
            if not output.get("evidence"):
                raise ValueError("QUESTION_PROPOSED requires evidence")
        elif disposition == "GUIDANCE_SUFFICIENT" and not output.get("evidence"):
            raise ValueError("GUIDANCE_SUFFICIENT requires evidence")
        elif output.get("question") or output.get("suggested_answers"):
            raise ValueError(f"{disposition} must not include a question or suggested answers")
    if unit_type == "REFINEMENT_DIMENSION":
        if output.get("dimension") != unit_key:
            raise ValueError(f"refinement dimension must return dimension {unit_key!r}")
        if output.get("conclusion") == "QUESTION_NEEDED":
            if not str(output.get("question") or "").strip():
                raise ValueError("QUESTION_NEEDED requires a question")
            suggestions = output.get("suggested_answers")
            if not isinstance(suggestions, list) or not 1 <= len(suggestions) <= 3:
                raise ValueError("QUESTION_NEEDED requires one to three suggested answers")
            if not output.get("evidence"):
                raise ValueError("QUESTION_NEEDED requires evidence")
        elif output.get("question") or output.get("suggested_answers"):
            raise ValueError("A dimension without QUESTION_NEEDED must not include a question or answers")
    cited = _evidence_references(output)
    if cited:
        allowed = _evidence_references(allowed_context if allowed_context is not None else unit)
        unknown = sorted(cited - allowed)
        if unknown:
            details = ", ".join(f"{document_id}:{paragraph_id}" for document_id, paragraph_id in unknown)
            raise ValueError(f"unit output contains evidence not present in its input: {details}")


def unit_instructions(unit: dict[str, Any]) -> str:
    unit_type = str(unit["unit_type"])
    common = (
        "This is one bounded stage of a durable synthesis. Return only the object required by the supplied "
        "stage schema. Do not attempt the complete assessment. Use only supplied evidence and paragraph IDs. "
        "Do not force a finding or policy question merely to fill the schema: a supported no-finding or "
        "no-question result is valid and preferred to speculation."
    )
    instructions = {
        "TOPIC_FINDING": (
            "Write at most one concise finding for the single criterion. The assignments are deterministic and "
            "must not be reclassified. Cite no more than three representative evidence items and three paragraph "
            "IDs per item. When the supplied assignments do not support a defensible finding, return an empty "
            "finding and evidence list instead of trying to infer one."
        ),
        "SIGNAL_DISPOSITION": (
            "Decide whether this single recurring signal warrants a reviewer policy question. A recurring signal "
            "does not automatically require a question: use GUIDANCE_SUFFICIENT when the Matter Definition already "
            "resolves it, and INSUFFICIENT_EVIDENCE when the evidence cannot support an answerable policy choice. "
            "For either non-question disposition, return an empty question and suggested_answers list. Do not "
            "overfit a policy question to isolated wording or weak evidence."
        ),
        "REFINEMENT_DIMENSION": (
            "Evaluate only the named refinement dimension. Return QUESTION_NEEDED only for an evidence-grounded "
            "policy ambiguity and include one complete, answerable policy question with suggested answers. Otherwise "
            "return NO_REFINEMENT_NEEDED with empty question and suggested_answers fields."
        ),
        "FINAL_NARRATIVE": (
            "Write a concise assessment narrative from the supplied completed unit outputs and coverage. Do not add "
            "new findings, topics, questions, assignments, or evidence."
        ),
    }
    detail = instructions[unit_type]
    if unit_type == "SIGNAL_DISPOSITION" and unit.get("unit_key") == OTHER_UNMAPPED_CRITERIA_KEY:
        detail = (
            "Evaluate the reporting-only groups of criterion labels that could not be safely mapped to the "
            "authoritative Review Guidance criteria. Do not classify every unmatched label as a positive finding. "
            "Use the original labels, match types, and evidence to decide whether the drift warrants one concrete "
            "guidance question; otherwise report that existing guidance is sufficient or the evidence is "
            "insufficient."
        )
    return f"{common}\n\n{detail}"


def unit_dynamic_input(
    manifest: dict[str, Any],
    unit: dict[str, Any],
    completed_outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    unit_type = str(unit["unit_type"])
    payload: dict[str, Any] = {
        "manifest_version": manifest["version"],
        "coverage": manifest["coverage"],
        "corpus_statistics": manifest["statistics"],
        "unit": unit,
    }
    if unit_type == "REFINEMENT_DIMENSION":
        payload["signals"] = [
            candidate for candidate in manifest["units"] if candidate.get("unit_type") == "SIGNAL_DISPOSITION"
        ]
        payload["topic_evidence"] = [
            {
                "criterion": candidate.get("criterion"),
                "representative_assignments": list(candidate.get("assignments") or [])[:8],
            }
            for candidate in manifest["units"]
            if candidate.get("unit_type") == "TOPIC_FINDING"
        ]
    if unit_type == "FINAL_NARRATIVE":
        payload["completed_units"] = completed_outputs
    return payload


def assemble_synthesis_result(
    manifest: dict[str, Any],
    outputs: dict[tuple[str, str], dict[str, Any]],
    failed_units: list[dict[str, Any]],
    normalization_warnings: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    normalization_warnings = normalization_warnings or []
    topics: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    dimensions: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []

    for unit in manifest.get("units") or []:
        unit_type = str(unit.get("unit_type"))
        unit_key = str(unit.get("unit_key"))
        output = outputs.get((unit_type, unit_key))
        if unit_type == "TOPIC_FINDING":
            criterion = unit["criterion"]
            assignment_values = [
                {
                    "matter_document_id": value["matter_document_id"],
                    "confidence": value["confidence"],
                    "evidence": _supported_evidence_items(value["evidence"]),
                }
                for value in unit.get("assignments") or []
            ]
            topics.append(
                {
                    "topic_key": unit_key,
                    "label": criterion["criterion_label"],
                    "description": criterion["description"],
                    "assignments": assignment_values,
                }
            )
            if output is not None and str(output.get("finding") or "").strip():
                findings.append(output)
        elif unit_type == "SIGNAL_DISPOSITION" and output is not None:
            if output.get("disposition") == "QUESTION_PROPOSED":
                questions.append(
                    {
                        "question": output["question"],
                        "rationale": output["rationale"],
                        "priority": output["priority"],
                        "blocking": output["blocking"],
                        "instruction_references": output["instruction_references"],
                        "evidence": output["evidence"],
                        "suggested_answers": output["suggested_answers"],
                    }
                )
        elif unit_type == "REFINEMENT_DIMENSION":
            if output is None:
                dimensions.append(
                    {
                        "dimension": unit_key,
                        "conclusion": "NOT_EVALUATED",
                        "rationale": "This dimension could not be completed; operator action is required.",
                        "evidence": [],
                    }
                )
            else:
                dimensions.append(
                    {
                        "dimension": output["dimension"],
                        "conclusion": output["conclusion"],
                        "rationale": output["rationale"],
                        "evidence": output["evidence"],
                    }
                )
                if output.get("conclusion") == "QUESTION_NEEDED":
                    questions.append(
                        {
                            "question": output["question"],
                            "rationale": output["rationale"],
                            "priority": output["priority"],
                            "blocking": output["blocking"],
                            "instruction_references": output["instruction_references"],
                            "evidence": output["evidence"],
                            "suggested_answers": output["suggested_answers"],
                        }
                    )

    required_failures = [unit for unit in failed_units if str(unit.get("unit_type")) in REQUIRED_UNIT_TYPES]
    optional_failures = [unit for unit in failed_units if unit not in required_failures]
    unmapped = manifest.get("canonicalization", {}).get("unmapped") or []
    other_unmapped_criteria = dict(manifest.get("other_unmapped_criteria") or {})
    other_unmapped_criteria["synthesis"] = outputs.get(("SIGNAL_DISPOSITION", OTHER_UNMAPPED_CRITERIA_KEY))
    if required_failures:
        disposition = "ACTION_REQUIRED"
    elif optional_failures or unmapped or normalization_warnings:
        disposition = "READY_WITH_WARNINGS"
    else:
        disposition = "READY"

    deduplicated_questions: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    for question in questions:
        normalized = " ".join(str(question.get("question") or "").casefold().split())
        if not normalized or normalized in seen_questions:
            continue
        seen_questions.add(normalized)
        deduplicated_questions.append(question)
    questions = deduplicated_questions

    if questions:
        outcome = "QUESTIONS_PROPOSED"
    elif required_failures:
        outcome = "INSUFFICIENT_EVIDENCE"
    else:
        outcome = "NO_REFINEMENT_WARRANTED"
    narrative_output = outputs.get(("FINAL_NARRATIVE", "FINAL"))
    narrative = (
        str(narrative_output.get("narrative"))
        if narrative_output is not None
        else "Assessment synthesis is incomplete. Review the failed synthesis units before continuing."
    )
    diagnostics = {
        "manifest_version": manifest["version"],
        "manifest_hash": manifest["content_hash"],
        "failed_units": failed_units,
        "required_failure_count": len(required_failures),
        "optional_failure_count": len(optional_failures),
        "normalization_warning_count": len(normalization_warnings),
        "normalization_warnings": normalization_warnings,
        "canonicalization": manifest.get("canonicalization") or {},
        "other_unmapped_criteria": other_unmapped_criteria,
    }
    result = {
        "coverage": manifest["coverage"],
        "corpus_statistics": manifest["statistics"],
        "status": "COMPLETED" if not failed_units else "COMPLETED_WITH_ERRORS",
        "narrative": narrative,
        "findings": findings,
        "topics": topics,
        "refinement_assessment": {
            "outcome": outcome,
            "rationale": (
                "One or more required synthesis units failed and must be retried."
                if required_failures
                else "The staged assessment evaluated every required refinement dimension."
            ),
            "evaluated_dimensions": dimensions,
        },
        "clarification_questions": questions,
        "other_unmapped_criteria": other_unmapped_criteria,
        "synthesis_diagnostics": diagnostics,
    }
    return result, disposition, diagnostics
