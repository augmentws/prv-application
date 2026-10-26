import re
from copy import deepcopy
from dataclasses import dataclass
from itertools import pairwise
from typing import Any
from urllib.parse import quote

PARAGRAPH_MAP_VERSION = "paragraph-v1"
DOCUMENT_MAP_VERSION = "document-map-v1"
_PARAGRAPH_SEPARATOR = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)*")
_CITATION_KEYS = frozenset(
    {"citation_ids", "evidence_paragraph_ids", "analyzed_paragraph_ids", "partial_paragraph_ids"}
)


class CitationValidationError(ValueError):
    pass


@dataclass(frozen=True)
class EvidenceParagraph:
    paragraph_id: str
    text: str
    char_start: int
    char_end: int
    line_start: int
    line_end: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "paragraph_id": self.paragraph_id,
            "text": self.text,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "viewer_location": {
                "type": "TEXT_RANGE",
                "char_start": self.char_start,
                "char_end": self.char_end,
                "line_start": self.line_start,
                "line_end": self.line_end,
            },
        }


@dataclass(frozen=True)
class ParagraphMap:
    version: str
    source_length: int
    paragraphs: tuple[EvidenceParagraph, ...]

    @property
    def paragraph_ids(self) -> frozenset[str]:
        return frozenset(paragraph.paragraph_id for paragraph in self.paragraphs)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_length": self.source_length,
            "paragraphs": [paragraph.as_dict() for paragraph in self.paragraphs],
        }


@dataclass(frozen=True)
class EvidenceSegment:
    paragraph_id: str
    part: int
    part_count: int
    text: str
    char_start: int
    char_end: int

    def model_text(self) -> str:
        part_label = f" [part {self.part}/{self.part_count}]" if self.part_count > 1 else ""
        return f"{self.paragraph_id}{part_label} {self.text}"


@dataclass(frozen=True)
class EvidenceWindow:
    window_id: str
    ordinal: int
    segments: tuple[EvidenceSegment, ...]

    @property
    def text(self) -> str:
        return "\n\n".join(segment.model_text() for segment in self.segments)


@dataclass(frozen=True)
class DocumentMapPlan:
    version: str
    paragraph_map_version: str
    max_characters: int
    overlap_segments: int
    windows: tuple[EvidenceWindow, ...]


def segment_paragraphs(source_text: str) -> ParagraphMap:
    if not isinstance(source_text, str):
        raise TypeError("source_text must be a string")
    paragraphs: list[EvidenceParagraph] = []
    cursor = 0
    for separator in list(_PARAGRAPH_SEPARATOR.finditer(source_text)) + [None]:
        block_end = separator.start() if separator is not None else len(source_text)
        block = source_text[cursor:block_end]
        leading = len(block) - len(block.lstrip())
        trailing = len(block) - len(block.rstrip())
        start = cursor + leading
        end = block_end - trailing
        if end > start:
            text = source_text[start:end]
            line_start = source_text.count("\n", 0, start) + 1
            line_end = line_start + text.count("\n")
            paragraphs.append(
                EvidenceParagraph(
                    paragraph_id=f"¶{len(paragraphs) + 1}",
                    text=text,
                    char_start=start,
                    char_end=end,
                    line_start=line_start,
                    line_end=line_end,
                )
            )
        if separator is None:
            break
        cursor = separator.end()
    return ParagraphMap(
        version=PARAGRAPH_MAP_VERSION,
        source_length=len(source_text),
        paragraphs=tuple(paragraphs),
    )


def render_model_document(paragraph_map: ParagraphMap) -> str:
    return "\n\n".join(f"{paragraph.paragraph_id} {paragraph.text}" for paragraph in paragraph_map.paragraphs)


def _split_paragraph(paragraph: EvidenceParagraph, max_characters: int) -> list[EvidenceSegment]:
    prefix_allowance = len(paragraph.paragraph_id) + 24
    content_limit = max(1, max_characters - prefix_allowance)
    ranges: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(paragraph.text):
        end = min(len(paragraph.text), cursor + content_limit)
        if end < len(paragraph.text):
            boundary = paragraph.text.rfind(" ", cursor + max(1, content_limit // 2), end)
            if boundary > cursor:
                end = boundary + 1
        ranges.append((cursor, end))
        cursor = end
    return [
        EvidenceSegment(
            paragraph_id=paragraph.paragraph_id,
            part=index + 1,
            part_count=len(ranges),
            text=paragraph.text[start:end],
            char_start=paragraph.char_start + start,
            char_end=paragraph.char_start + end,
        )
        for index, (start, end) in enumerate(ranges)
    ]


def build_document_map_plan(
    paragraph_map: ParagraphMap,
    *,
    max_characters: int,
    overlap_segments: int = 1,
) -> DocumentMapPlan:
    if max_characters < 100:
        raise ValueError("max_characters must be at least 100")
    if overlap_segments < 0:
        raise ValueError("overlap_segments cannot be negative")
    segments = [
        segment
        for paragraph in paragraph_map.paragraphs
        for segment in _split_paragraph(paragraph, max_characters)
    ]
    windows: list[EvidenceWindow] = []
    start = 0
    while start < len(segments):
        selected: list[EvidenceSegment] = []
        rendered_length = 0
        end = start
        while end < len(segments):
            segment_length = len(segments[end].model_text()) + (2 if selected else 0)
            if selected and rendered_length + segment_length > max_characters:
                break
            selected.append(segments[end])
            rendered_length += segment_length
            end += 1
        windows.append(
            EvidenceWindow(
                window_id=f"window-{len(windows) + 1}",
                ordinal=len(windows) + 1,
                segments=tuple(selected),
            )
        )
        if end >= len(segments):
            break
        next_start = end - min(overlap_segments, max(0, len(selected) - 1))
        start = max(start + 1, next_start)
    return DocumentMapPlan(
        version=DOCUMENT_MAP_VERSION,
        paragraph_map_version=paragraph_map.version,
        max_characters=max_characters,
        overlap_segments=overlap_segments,
        windows=tuple(windows),
    )


def document_map_coverage(
    paragraph_map: ParagraphMap,
    plan: DocumentMapPlan,
    *,
    successful_window_ids: set[str],
) -> dict[str, Any]:
    known_window_ids = {window.window_id for window in plan.windows}
    unknown_window_ids = successful_window_ids - known_window_ids
    if unknown_window_ids:
        raise ValueError(f"Unknown document-map windows: {', '.join(sorted(unknown_window_ids))}")
    covered: dict[str, list[tuple[int, int]]] = {}
    for window in plan.windows:
        if window.window_id not in successful_window_ids:
            continue
        for segment in window.segments:
            covered.setdefault(segment.paragraph_id, []).append((segment.char_start, segment.char_end))

    analyzed_paragraph_ids: list[str] = []
    partial_paragraph_ids: list[str] = []
    omitted_ranges: list[dict[str, Any]] = []
    for paragraph in paragraph_map.paragraphs:
        ranges = sorted(covered.get(paragraph.paragraph_id, []))
        merged: list[list[int]] = []
        for start, end in ranges:
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        cursor = paragraph.char_start
        for start, end in merged:
            if start > cursor:
                omitted_ranges.append(
                    {"paragraph_id": paragraph.paragraph_id, "char_start": cursor, "char_end": start}
                )
            cursor = max(cursor, end)
        if cursor < paragraph.char_end:
            omitted_ranges.append(
                {
                    "paragraph_id": paragraph.paragraph_id,
                    "char_start": cursor,
                    "char_end": paragraph.char_end,
                }
            )
        has_coverage = bool(merged)
        fully_covered = (
            has_coverage
            and merged[0][0] <= paragraph.char_start
            and merged[-1][1] >= paragraph.char_end
            and not any(current[1] < following[0] for current, following in pairwise(merged))
        )
        if fully_covered:
            analyzed_paragraph_ids.append(paragraph.paragraph_id)
        elif has_coverage:
            partial_paragraph_ids.append(paragraph.paragraph_id)
    return {
        "status": "COMPLETE" if not omitted_ranges else "PARTIAL",
        "map_plan_version": plan.version,
        "paragraph_map_version": paragraph_map.version,
        "window_count": len(plan.windows),
        "successful_window_count": len(successful_window_ids),
        "analyzed_paragraph_ids": analyzed_paragraph_ids,
        "partial_paragraph_ids": partial_paragraph_ids,
        "omitted_ranges": omitted_ranges,
    }


def build_document_reduce_input(
    paragraph_map: ParagraphMap,
    plan: DocumentMapPlan,
    *,
    map_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    known_window_ids = {window.window_id for window in plan.windows}
    unknown_window_ids = set(map_results) - known_window_ids
    if unknown_window_ids:
        raise ValueError(f"Unknown document-map windows: {', '.join(sorted(unknown_window_ids))}")
    ordered_results = [
        {
            "window_id": window.window_id,
            "paragraph_ids": list(dict.fromkeys(segment.paragraph_id for segment in window.segments)),
            "result": map_results[window.window_id],
        }
        for window in plan.windows
        if window.window_id in map_results
    ]
    return {
        "map_plan_version": plan.version,
        "paragraph_map_version": paragraph_map.version,
        "coverage": document_map_coverage(
            paragraph_map,
            plan,
            successful_window_ids=set(map_results),
        ),
        "map_results": ordered_results,
        "failed_window_ids": [
            window.window_id for window in plan.windows if window.window_id not in map_results
        ],
    }


def _citation_values(value: Any, *, path: str = "$") -> list[tuple[str, Any]]:
    citations: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in _CITATION_KEYS:
                citations.append((child_path, child))
            citations.extend(_citation_values(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            citations.extend(_citation_values(child, path=f"{path}[{index}]"))
    return citations


def document_citation_ids(value: Any) -> frozenset[str]:
    return frozenset(
        citation_id
        for _, citations in _citation_values(value)
        if isinstance(citations, list)
        for citation_id in citations
        if isinstance(citation_id, str)
    )


def _normalized_signal_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _near_miss_key(item: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    return (
        _normalized_signal_text(item.get("criterion_key")),
        tuple(sorted(str(value) for value in item.get("citation_ids") or [])),
    )


def _clarification_key(item: dict[str, Any]) -> str:
    return _normalized_signal_text(item.get("question"))


def preserve_document_map_signals(
    reduced_result: dict[str, Any],
    map_results: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Restore high-value map signals that a generative reducer omitted.

    Overlapping windows can repeat the same signal, so preservation is based on
    stable semantic keys rather than raw object equality. The reducer remains
    responsible for the narrative and overall determination; the application
    owns the lossless union of near misses and clarification candidates.
    """

    merged = deepcopy(reduced_result)
    reduced_matches = list(merged.get("criterion_matches") or [])
    reduced_clarifications = list(merged.get("clarification_requests") or [])
    merged["criterion_matches"] = reduced_matches
    merged["clarification_requests"] = reduced_clarifications

    final_near_miss_keys = {
        _near_miss_key(item)
        for item in reduced_matches
        if isinstance(item, dict) and item.get("match_type") == "NEAR_MISS"
    }
    final_clarification_keys = {
        _clarification_key(item)
        for item in reduced_clarifications
        if isinstance(item, dict) and _clarification_key(item)
    }
    map_near_miss_keys: set[tuple[str, tuple[str, ...]]] = set()
    map_clarification_keys: set[str] = set()
    restored_near_misses: list[dict[str, Any]] = []
    restored_clarifications: list[dict[str, Any]] = []

    for map_result in map_results.values():
        for item in map_result.get("criterion_matches") or []:
            if not isinstance(item, dict) or item.get("match_type") != "NEAR_MISS":
                continue
            key = _near_miss_key(item)
            map_near_miss_keys.add(key)
            if key in final_near_miss_keys:
                continue
            copied = deepcopy(item)
            reduced_matches.append(copied)
            final_near_miss_keys.add(key)
            restored_near_misses.append(
                {
                    "criterion_key": copied.get("criterion_key"),
                    "citation_ids": list(copied.get("citation_ids") or []),
                }
            )

        for item in map_result.get("clarification_requests") or []:
            if not isinstance(item, dict):
                continue
            key = _clarification_key(item)
            if not key:
                continue
            map_clarification_keys.add(key)
            if key in final_clarification_keys:
                continue
            copied = deepcopy(item)
            reduced_clarifications.append(copied)
            final_clarification_keys.add(key)
            restored_clarifications.append(
                {
                    "question": copied.get("question"),
                    "citation_ids": list(copied.get("citation_ids") or []),
                }
            )

    diagnostics = {
        "map_near_miss_count": len(map_near_miss_keys),
        "map_clarification_count": len(map_clarification_keys),
        "reducer_near_miss_count": len(final_near_miss_keys) - len(restored_near_misses),
        "reducer_clarification_count": len(final_clarification_keys) - len(restored_clarifications),
        "restored_near_miss_count": len(restored_near_misses),
        "restored_clarification_count": len(restored_clarifications),
        "restored_near_misses": restored_near_misses,
        "restored_clarifications": restored_clarifications,
        "final_near_miss_count": len(final_near_miss_keys),
        "final_clarification_count": len(final_clarification_keys),
    }
    if not map_near_miss_keys.issubset(final_near_miss_keys):
        raise ValueError("Final document analysis dropped map-level near misses")
    if not map_clarification_keys.issubset(final_clarification_keys):
        raise ValueError("Final document analysis dropped map-level clarification candidates")
    return merged, diagnostics


def _require_citations(result: dict[str, Any], field: str, errors: list[str]) -> None:
    values = result.get(field)
    if not isinstance(values, list):
        errors.append(f"$.{field} must be an array")
        return
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            errors.append(f"$.{field}[{index}] must be an object")
            continue
        citations = item.get("citation_ids")
        if not isinstance(citations, list) or not citations:
            errors.append(f"$.{field}[{index}].citation_ids must contain at least one paragraph ID")


def _validate_citations_for_fields(
    result: dict[str, Any],
    paragraph_map: ParagraphMap,
    *,
    cited_fields: tuple[str, ...],
) -> None:
    valid_ids = paragraph_map.paragraph_ids
    errors: list[str] = []
    for field in cited_fields:
        _require_citations(result, field, errors)
    for path, citations in _citation_values(result):
        if not isinstance(citations, list) or any(not isinstance(value, str) for value in citations):
            errors.append(f"{path} must be an array of paragraph IDs")
            continue
        unknown = sorted(set(citations) - valid_ids)
        if unknown:
            errors.append(f"{path} contains unknown paragraph IDs: {', '.join(unknown)}")
    if errors:
        raise CitationValidationError("; ".join(errors))


def validate_document_analysis_citations(
    result: dict[str, Any],
    paragraph_map: ParagraphMap,
) -> None:
    _validate_citations_for_fields(
        result,
        paragraph_map,
        cited_fields=(
            "summary",
            "responsiveness_summary",
            "criterion_matches",
            "scope_analysis",
            "countervailing_considerations",
            "clarification_requests",
        ),
    )


def validate_document_map_citations(
    result: dict[str, Any],
    paragraph_map: ParagraphMap,
) -> None:
    _validate_citations_for_fields(
        result,
        paragraph_map,
        cited_fields=(
            "findings",
            "criterion_matches",
            "scope_analysis",
            "countervailing_considerations",
            "clarification_requests",
        ),
    )


def _citation_links(citation_ids: list[str]) -> str:
    links = [f"[{paragraph_id}](#paragraph-{quote(paragraph_id, safe='')})" for paragraph_id in citation_ids]
    return f" [Link: {', '.join(links)}]" if links else ""


def _render_cited_paragraphs(values: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"{item['text']}{_citation_links(item.get('citation_ids', []))}" for item in values
    )


def render_document_analysis_markdown(result: dict[str, Any]) -> str:
    sections = ["## Document Summary", "", _render_cited_paragraphs(result["summary"])]
    sections.extend(
        [
            "",
            "## Responsiveness Summary",
            "",
            f"This document is **{result['determination']}**.",
            f"Confidence: **{result['confidence']:.0%}**.",
            "",
            _render_cited_paragraphs(result["responsiveness_summary"]),
        ]
    )
    criterion_matches = result.get("criterion_matches", [])
    if criterion_matches:
        sections.extend(["", "### Criterion matches", ""])
        for match in criterion_matches:
            label = match.get("label") or match.get("criterion_key") or "Criterion"
            reasoning = match.get("reasoning") or match.get("text") or ""
            sections.append(f"- **{label}:** {reasoning}{_citation_links(match.get('citation_ids', []))}")
    scope_analysis = result.get("scope_analysis", [])
    if scope_analysis:
        sections.extend(["", "### Scope analysis", ""])
        for scope in scope_analysis:
            dimension = scope.get("dimension", "Scope").replace("_", " ").title()
            conclusion = scope.get("conclusion", "UNCLEAR").replace("_", " ").lower()
            sections.append(
                f"- **{dimension} — {conclusion}:** {scope.get('reasoning', '')}"
                f"{_citation_links(scope.get('citation_ids', []))}"
            )
    countervailing = result.get("countervailing_considerations", [])
    if countervailing:
        sections.extend(["", "### Countervailing considerations", ""])
        for item in countervailing:
            sections.append(f"- {item['text']}{_citation_links(item.get('citation_ids', []))}")
    limitations = result.get("limitations", [])
    if limitations:
        sections.extend(["", "### Document limitations", ""])
        sections.extend(f"- {limitation}" for limitation in limitations)
    sections.extend(["", "## Clarification Requests", ""])
    clarifications = result.get("clarification_requests", [])
    if clarifications:
        for request in clarifications:
            question = request.get("question") or request.get("text") or "Clarification required"
            rationale = request.get("rationale")
            suffix = f" — {rationale}" if rationale else ""
            sections.append(
                f"- {question}{suffix}{_citation_links(request.get('citation_ids', []))}"
            )
    else:
        sections.append("No document-specific clarification is required.")
    return "\n".join(sections).strip() + "\n"
