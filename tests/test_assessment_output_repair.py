import asyncio
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from app import assessment_output_repair
from app.assessment_output_repair import (
    apply_focused_repair,
    focused_repair_plan,
    focused_repair_schema,
    normalize_or_repair_document_analysis,
    safe_normalize_document_analysis,
)
from app.document_analysis_schemas import DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA
from app.document_evidence import segment_paragraphs


def _output(*, determination: str = "NON_RESPONSIVE", citation_count: int = 1):
    citations = [f"¶{index}" for index in range(1, citation_count + 1)]
    return {
        "summary": [{"text": "Summary", "citation_ids": citations}],
        "determination": determination,
        "confidence": 0.9,
        "responsiveness_summary": [{"text": "Not within scope", "citation_ids": ["¶1"]}],
        "criterion_matches": [],
        "scope_analysis": [],
        "countervailing_considerations": [],
        "clarification_requests": [],
        "limitations": [],
    }


def test_safe_normalization_only_maps_unambiguous_alias():
    normalized, changes = safe_normalize_document_analysis(_output(determination="NOT_RESPONSIVE"))
    assert normalized["determination"] == "NON_RESPONSIVE"
    assert changes == [{"path": "determination", "from": "NOT_RESPONSIVE", "to": "NON_RESPONSIVE"}]

    untouched, changes = safe_normalize_document_analysis(_output(determination="NOT_PRIVILEGED"))
    assert untouched["determination"] == "NOT_PRIVILEGED"
    assert changes == []


def test_focused_plan_and_patch_repair_only_invalid_fields():
    original = _output(determination="PRIVILEGED", citation_count=35)
    plan = focused_repair_plan(original)
    assert plan.repairable is True
    assert plan.determination_required is True
    assert plan.citation_paths == (("summary", 0, "citation_ids"),)
    schema = focused_repair_schema(original, plan)
    patch = {
        "determination": "RESPONSIVE",
        "citation_repairs": [
            {"path": "summary.0.citation_ids", "citation_ids": ["¶1", "¶35"]},
        ],
    }
    assert list(Draft202012Validator(schema).iter_errors(patch)) == []

    repaired = apply_focused_repair(original, plan, patch)
    assert repaired["determination"] == "RESPONSIVE"
    assert repaired["summary"][0]["citation_ids"] == ["¶1", "¶35"]
    assert repaired["summary"][0]["text"] == original["summary"][0]["text"]
    assert list(Draft202012Validator(DOCUMENT_ANALYSIS_MODEL_OUTPUT_SCHEMA).iter_errors(repaired)) == []


def test_focused_patch_cannot_invent_citations():
    original = _output(citation_count=31)
    plan = focused_repair_plan(original)
    with pytest.raises(ValueError, match="invented citations"):
        apply_focused_repair(
            original,
            plan,
            {
                "citation_repairs": [
                    {"path": "summary.0.citation_ids", "citation_ids": ["¶999"]},
                ]
            },
        )


def test_unsupported_schema_failure_is_not_silently_repaired():
    original = _output()
    original["confidence"] = 2
    plan = focused_repair_plan(original)
    assert plan.repairable is False
    assert plan.unsupported_errors


def test_focused_model_repair_requests_and_applies_only_invalid_field(monkeypatch):
    original = _output(determination="PRIVILEGED")
    captured = {}

    async def fake_execute(request, *, model):
        captured["request"] = request
        captured["model"] = model
        return (
            SimpleNamespace(
                output={"determination": "RESPONSIVE"},
                request_count=1,
                input_tokens=100,
                cached_input_tokens=0,
                output_tokens=10,
                provider="test-provider",
                provider_model="test-model",
                invocations=(),
            ),
            SimpleNamespace(cache_fingerprint="repair-cache-key"),
        )

    monkeypatch.setattr(assessment_output_repair, "execute_structured_model", fake_execute)
    paragraph_map = segment_paragraphs("Evidence")

    outcome = asyncio.run(
        normalize_or_repair_document_analysis(
            output=original,
            paragraph_map=paragraph_map,
            matter_definition="Review guidance",
            model_key="document-analysis",
            model_policy={},
            limits={"max_output_tokens": 20_000},
            cache_identity={"assessment_id": "assessment-id"},
            run_id="repair-run",
            model="provider:model",
        )
    )

    assert outcome.output["determination"] == "RESPONSIVE"
    assert outcome.output["summary"] == original["summary"]
    assert outcome.focused_repair_applied is True
    assert captured["model"] == "provider:model"
    assert captured["request"].dynamic_input["repair_targets"] == {
        "determination": True,
        "citation_paths": [],
    }
    assert captured["request"].dynamic_input["referenced_evidence"] == [
        {"paragraph_id": "¶1", "text": "Evidence"}
    ]
