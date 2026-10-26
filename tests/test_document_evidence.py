import pytest

from app.document_analysis_schemas import (
    DOCUMENT_ANALYSIS_MAP_OUTPUT_SCHEMA,
    DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA,
    DOCUMENT_ANALYSIS_OUTPUT_SCHEMA,
)
from app.document_evidence import (
    CitationValidationError,
    build_document_map_plan,
    build_document_reduce_input,
    document_map_coverage,
    preserve_document_map_signals,
    render_document_analysis_markdown,
    render_model_document,
    segment_paragraphs,
    validate_document_analysis_citations,
    validate_document_map_citations,
)
from app.model_execution import validate_structured_output
from app.standard_skills import STANDARD_ASSESSMENT_SKILLS


def analysis_result() -> dict:
    return {
        "summary": [{"text": "The sender reports a non-renewal.", "citation_ids": ["¶1"]}],
        "determination": "RESPONSIVE",
        "confidence": 0.94,
        "responsiveness_summary": [
            {"text": "The document provides firsthand evidence.", "citation_ids": ["¶1", "¶2"]}
        ],
        "criterion_matches": [
            {
                "criterion_key": "issue_1_ii",
                "label": "Issue 1(ii)",
                "match_type": "PRIMARY",
                "reasoning": "It describes a policy non-renewal.",
                "citation_ids": ["¶1"],
            }
        ],
        "scope_analysis": [
            {
                "dimension": "TEMPORAL",
                "conclusion": "IN_SCOPE",
                "reasoning": "The letter is dated in the relevant period.",
                "citation_ids": ["¶1"],
            }
        ],
        "countervailing_considerations": [
            {"text": "The buildings are commercial rather than residential.", "citation_ids": ["¶2"]}
        ],
        "clarification_requests": [
            {
                "question": "Does the commercial-property criterion have the same scope?",
                "rationale": "The definition emphasizes homeowners.",
                "blocking": False,
                "instruction_references": ["Issue 1"],
                "citation_ids": ["¶2"],
            }
        ],
        "limitations": [],
        "coverage": {
            "status": "COMPLETE",
            "map_plan_version": "document-map-v1",
            "paragraph_map_version": "paragraph-v1",
            "window_count": 1,
            "successful_window_count": 1,
            "analyzed_paragraph_ids": ["¶1", "¶2"],
            "partial_paragraph_ids": [],
            "omitted_ranges": [],
        },
    }


def test_paragraph_segmentation_preserves_offsets_and_model_ids() -> None:
    source = "  First paragraph.  \n\nSecond paragraph\ncontinues.\n\n"
    paragraph_map = segment_paragraphs(source)

    assert [paragraph.paragraph_id for paragraph in paragraph_map.paragraphs] == ["¶1", "¶2"]
    assert [source[p.char_start : p.char_end] for p in paragraph_map.paragraphs] == [
        "First paragraph.",
        "Second paragraph\ncontinues.",
    ]
    assert paragraph_map.paragraphs[1].line_start == 3
    assert render_model_document(paragraph_map).startswith("¶1 First paragraph.")


def test_citation_validation_and_markdown_rendering() -> None:
    paragraph_map = segment_paragraphs("First paragraph.\n\nSecond paragraph.")
    result = analysis_result()
    document_skill = next(
        skill for skill in STANDARD_ASSESSMENT_SKILLS if skill["role_key"] == "document_analysis"
    )
    validate_structured_output(result, document_skill["output_schema"])
    validate_document_analysis_citations(result, paragraph_map)

    markdown = render_document_analysis_markdown(result)
    assert "## Document Summary" in markdown
    assert "This document is **RESPONSIVE**." in markdown
    assert "[¶1](#paragraph-%C2%B61)" in markdown
    assert "Does the commercial-property criterion" in markdown

    result["summary"][0]["citation_ids"] = ["¶99"]
    with pytest.raises(CitationValidationError, match="unknown paragraph IDs: ¶99"):
        validate_document_analysis_citations(result, paragraph_map)


def test_material_analysis_sections_require_citations() -> None:
    paragraph_map = segment_paragraphs("First paragraph.\n\nSecond paragraph.")
    result = analysis_result()
    result["responsiveness_summary"][0]["citation_ids"] = []
    with pytest.raises(CitationValidationError, match="must contain at least one paragraph ID"):
        validate_document_analysis_citations(result, paragraph_map)


def test_map_and_final_model_schemas_leave_coverage_to_the_application() -> None:
    paragraph_map = segment_paragraphs("First paragraph.")
    map_result = {
        "findings": [{"text": "A material fact.", "citation_ids": ["¶1"]}],
        "criterion_matches": [],
        "scope_analysis": [],
        "countervailing_considerations": [],
        "clarification_requests": [],
        "limitations": [],
    }

    validate_structured_output(map_result, DOCUMENT_ANALYSIS_MAP_OUTPUT_SCHEMA)
    validate_document_map_citations(map_result, paragraph_map)
    assert "coverage" not in DOCUMENT_ANALYSIS_MAP_OUTPUT_SCHEMA["properties"]
    assert "coverage" not in DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA["properties"]
    assert "coverage" in DOCUMENT_ANALYSIS_OUTPUT_SCHEMA["properties"]


def test_map_near_misses_and_clarifications_survive_reduction() -> None:
    near_miss = {
        "criterion_key": "issue_5",
        "label": "Citizens operations",
        "match_type": "NEAR_MISS",
        "reasoning": "The document raises an unresolved employment boundary.",
        "citation_ids": ["¶2"],
    }
    clarification = {
        "question": "Should internal employment grievances involving Citizens be responsive?",
        "rationale": "The current boundary is ambiguous.",
        "blocking": False,
        "instruction_references": ["Issue 5"],
        "citation_ids": ["¶2"],
    }
    reduced = {
        **analysis_result(),
        "criterion_matches": [],
        "clarification_requests": [],
    }
    map_results = {
        "window-1": {
            "criterion_matches": [near_miss],
            "clarification_requests": [clarification],
        },
        # An overlapping window may repeat the exact same signals.
        "window-2": {
            "criterion_matches": [near_miss],
            "clarification_requests": [clarification],
        },
    }

    merged, diagnostics = preserve_document_map_signals(reduced, map_results)

    assert merged["criterion_matches"] == [near_miss]
    assert merged["clarification_requests"] == [clarification]
    assert diagnostics["map_near_miss_count"] == 1
    assert diagnostics["map_clarification_count"] == 1
    assert diagnostics["restored_near_miss_count"] == 1
    assert diagnostics["restored_clarification_count"] == 1
    assert "maxItems" not in DOCUMENT_ANALYSIS_OUTPUT_SCHEMA["properties"]["criterion_matches"]
    assert "maxItems" not in DOCUMENT_ANALYSIS_OUTPUT_SCHEMA["properties"]["clarification_requests"]


def test_long_document_map_plan_preserves_all_source_ranges_and_reports_partial_coverage() -> None:
    source = ("Alpha " * 80).strip() + "\n\n" + ("Beta " * 80).strip()
    paragraph_map = segment_paragraphs(source)
    plan = build_document_map_plan(paragraph_map, max_characters=180, overlap_segments=0)

    assert len(plan.windows) > 2
    assert all(len(window.text) <= 180 for window in plan.windows)
    all_coverage = document_map_coverage(
        paragraph_map,
        plan,
        successful_window_ids={window.window_id for window in plan.windows},
    )
    assert all_coverage["status"] == "COMPLETE"
    assert all_coverage["analyzed_paragraph_ids"] == ["¶1", "¶2"]
    assert all_coverage["omitted_ranges"] == []

    partial_coverage = document_map_coverage(
        paragraph_map,
        plan,
        successful_window_ids={window.window_id for window in plan.windows[1:]},
    )
    assert partial_coverage["status"] == "PARTIAL"
    assert partial_coverage["successful_window_count"] == len(plan.windows) - 1
    assert partial_coverage["omitted_ranges"]

    reduce_input = build_document_reduce_input(
        paragraph_map,
        plan,
        map_results={window.window_id: {"findings": []} for window in plan.windows[1:]},
    )
    assert reduce_input["coverage"] == partial_coverage
    assert reduce_input["failed_window_ids"] == [plan.windows[0].window_id]
    assert [result["window_id"] for result in reduce_input["map_results"]] == [
        window.window_id for window in plan.windows[1:]
    ]
