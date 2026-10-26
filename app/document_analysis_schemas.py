from copy import deepcopy

DOCUMENT_ANALYSIS_PIPELINE_VERSION = "document-analysis-pipeline-v3"

PARAGRAPH_IDS_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "maxItems": 30,
    "uniqueItems": True,
    "items": {"type": "string", "pattern": "^¶[1-9][0-9]*$"},
}

CITED_TEXT_SCHEMA = {
    "type": "object",
    "required": ["text", "citation_ids"],
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 4000},
        "citation_ids": {"$ref": "#/$defs/paragraph_ids"},
    },
    "additionalProperties": False,
}

CRITERION_MATCH_SCHEMA = {
    "type": "object",
    "required": ["criterion_key", "label", "match_type", "reasoning", "citation_ids"],
    "properties": {
        "criterion_key": {"type": "string", "minLength": 1, "maxLength": 200},
        "label": {"type": "string", "minLength": 1, "maxLength": 500},
        "match_type": {"enum": ["PRIMARY", "SECONDARY", "NEAR_MISS"]},
        "reasoning": {"type": "string", "minLength": 1, "maxLength": 4000},
        "citation_ids": {"$ref": "#/$defs/paragraph_ids"},
    },
    "additionalProperties": False,
}

SCOPE_ANALYSIS_SCHEMA = {
    "type": "object",
    "required": ["dimension", "conclusion", "reasoning", "citation_ids"],
    "properties": {
        "dimension": {"enum": ["TEMPORAL", "GEOGRAPHIC", "SUBJECT_MATTER", "OTHER"]},
        "conclusion": {"enum": ["IN_SCOPE", "OUT_OF_SCOPE", "UNCLEAR", "NOT_APPLICABLE"]},
        "reasoning": {"type": "string", "minLength": 1, "maxLength": 4000},
        "citation_ids": {"$ref": "#/$defs/paragraph_ids"},
    },
    "additionalProperties": False,
}

CLARIFICATION_SCHEMA = {
    "type": "object",
    "required": ["question", "rationale", "blocking", "instruction_references", "citation_ids"],
    "properties": {
        "question": {"type": "string", "minLength": 1, "maxLength": 2000},
        "rationale": {"type": "string", "minLength": 1, "maxLength": 4000},
        "blocking": {"type": "boolean"},
        "instruction_references": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 1000},
        },
        "citation_ids": {"$ref": "#/$defs/paragraph_ids"},
    },
    "additionalProperties": False,
}

ANALYSIS_DEFS = {
    "paragraph_ids": PARAGRAPH_IDS_SCHEMA,
    "cited_text": CITED_TEXT_SCHEMA,
    "criterion_match": CRITERION_MATCH_SCHEMA,
    "scope_analysis": SCOPE_ANALYSIS_SCHEMA,
    "clarification": CLARIFICATION_SCHEMA,
}

DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA = {
    "type": "object",
    "required": [
        "summary",
        "determination",
        "confidence",
        "responsiveness_summary",
        "criterion_matches",
        "scope_analysis",
        "countervailing_considerations",
        "clarification_requests",
        "limitations",
    ],
    "properties": {
        "summary": {
            "type": "array",
            "maxItems": 12,
            "items": {"$ref": "#/$defs/cited_text"},
        },
        "determination": {"enum": ["RESPONSIVE", "NON_RESPONSIVE", "UNCLEAR"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "responsiveness_summary": {
            "type": "array",
            "maxItems": 12,
            "items": {"$ref": "#/$defs/cited_text"},
        },
        "criterion_matches": {
            "type": "array",
            "maxItems": 100,
            "items": {"$ref": "#/$defs/criterion_match"},
        },
        "scope_analysis": {
            "type": "array",
            "maxItems": 12,
            "items": {"$ref": "#/$defs/scope_analysis"},
        },
        "countervailing_considerations": {
            "type": "array",
            "maxItems": 12,
            "items": {"$ref": "#/$defs/cited_text"},
        },
        "clarification_requests": {
            "type": "array",
            "maxItems": 12,
            "items": {"$ref": "#/$defs/clarification"},
        },
        "limitations": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string", "maxLength": 2000},
        },
    },
    "$defs": deepcopy(ANALYSIS_DEFS),
    "additionalProperties": False,
}

DOCUMENT_ANALYSIS_MAP_OUTPUT_SCHEMA = {
    "type": "object",
    "required": [
        "findings",
        "criterion_matches",
        "scope_analysis",
        "countervailing_considerations",
        "clarification_requests",
        "limitations",
    ],
    "properties": {
        "findings": {
            "type": "array",
            "maxItems": 20,
            "items": {"$ref": "#/$defs/cited_text"},
        },
        "criterion_matches": {
            "type": "array",
            "maxItems": 50,
            "items": {"$ref": "#/$defs/criterion_match"},
        },
        "scope_analysis": {
            "type": "array",
            "maxItems": 8,
            "items": {"$ref": "#/$defs/scope_analysis"},
        },
        "countervailing_considerations": {
            "type": "array",
            "maxItems": 12,
            "items": {"$ref": "#/$defs/cited_text"},
        },
        "clarification_requests": {
            "type": "array",
            "maxItems": 8,
            "items": {"$ref": "#/$defs/clarification"},
        },
        "limitations": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "maxLength": 2000},
        },
    },
    "$defs": deepcopy(ANALYSIS_DEFS),
    "additionalProperties": False,
}

COVERAGE_SCHEMA = {
    "type": "object",
    "required": [
        "status",
        "map_plan_version",
        "paragraph_map_version",
        "window_count",
        "successful_window_count",
        "analyzed_paragraph_ids",
        "partial_paragraph_ids",
        "omitted_ranges",
    ],
    "properties": {
        "status": {"enum": ["COMPLETE", "PARTIAL"]},
        "map_plan_version": {"type": "string", "minLength": 1},
        "paragraph_map_version": {"type": "string", "minLength": 1},
        "window_count": {"type": "integer", "minimum": 1},
        "successful_window_count": {"type": "integer", "minimum": 0},
        "analyzed_paragraph_ids": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "pattern": "^¶[1-9][0-9]*$"},
        },
        "partial_paragraph_ids": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "pattern": "^¶[1-9][0-9]*$"},
        },
        "omitted_ranges": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    },
    "additionalProperties": False,
}

DOCUMENT_ANALYSIS_OUTPUT_SCHEMA = deepcopy(DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA)
# Map/reduce signals are merged by the application after the model returns. The
# model-facing bounds keep each request compact, while the persisted contract
# must be able to retain every unique near miss and clarification candidate
# found across all windows.
DOCUMENT_ANALYSIS_OUTPUT_SCHEMA["properties"]["criterion_matches"].pop("maxItems", None)
DOCUMENT_ANALYSIS_OUTPUT_SCHEMA["properties"]["clarification_requests"].pop("maxItems", None)
DOCUMENT_ANALYSIS_OUTPUT_SCHEMA["required"] = [
    *DOCUMENT_ANALYSIS_OUTPUT_SCHEMA["required"],
    "coverage",
]
DOCUMENT_ANALYSIS_OUTPUT_SCHEMA["properties"]["coverage"] = {"$ref": "#/$defs/coverage"}
DOCUMENT_ANALYSIS_OUTPUT_SCHEMA["$defs"]["coverage"] = COVERAGE_SCHEMA

DOCUMENT_ANALYSIS_DOCUMENT_INSTRUCTIONS = """Produce the final analysis for the complete supplied document. Evaluate
every applicable Matter Definition criterion. Be concise in narrative fields, but never omit a genuine near miss or
clarification candidate for brevity. The application calculates document coverage; do not generate a coverage object.
Return only the final-analysis schema supplied for this call."""

DOCUMENT_ANALYSIS_MAP_INSTRUCTIONS = """Analyze only the supplied document window against every applicable Matter
Definition criterion. Extract a compact set of material findings, criterion matches, scope signals, countervailing
evidence, clarification candidates, and limitations with paragraph citations. Never omit a genuine near miss or
clarification candidate for brevity; the application preserves those signals across windows. Do not make an overall
document determination, estimate confidence, summarize text outside this window, or generate document coverage.
Return only the window-analysis schema supplied for this call."""

DOCUMENT_ANALYSIS_REDUCE_INSTRUCTIONS = """Combine the supplied window analyses into one final document analysis.
Deduplicate repeated findings from overlapping windows, reconcile conflicting signals, and base every conclusion only
on the supplied window results. Preserve every genuine near miss and clarification candidate reported by a window;
the application also enforces this after reduction. Cite only paragraph identifiers present in those results. Be
concise in narrative fields. The application calculates document coverage; do not generate a coverage object. Return
only the final-analysis schema supplied for this call."""
