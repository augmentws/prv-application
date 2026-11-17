from datetime import datetime, timezone
from uuid import UUID

from app.artifact_gateway import AnalysisInputItemSnapshot
from app.document_analysis_schemas import DOCUMENT_ANALYSIS_OUTPUT_SCHEMA
from app.evidence_context import build_document_evidence_context
from app.model_execution import validate_structured_output


def test_document_evidence_context_is_citable_ordered_and_hashed() -> None:
    snapshot = AnalysisInputItemSnapshot(
        item_id=UUID("00000000-0000-0000-0000-000000000001"),
        family_id=None,
        record_type="EMAIL",
        original_filename="message.eml",
        file_date=None,
        email_sender="lawyer@example.com",
        email_subject="Advice",
        email_sent_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        email_recipients={
            "TO": ["client@example.com"],
            "CC": ["second@example.com"],
            "BCC": [],
        },
        text_source=None,
    )

    first = build_document_evidence_context(snapshot)
    second = build_document_evidence_context(snapshot)

    assert first == second
    assert first.context_hash == second.context_hash
    assert "metadata:email.sender" in first.citation_ids
    assert "metadata:email.to:1" in first.citation_ids
    assert "metadata:email.subject" in first.citation_ids
    assert "metadata:original_filename" in first.citation_ids
    assert "metadata:email.sent_at" not in first.citation_ids
    assert "metadata:file_date" not in first.citation_ids
    assert "metadata:record_type" not in first.citation_ids
    assert first.as_model_input()[-1] == {
        "citation_id": "metadata:email.cc:1",
        "label": "Email CC recipient 1",
        "value": "second@example.com",
    }


def test_persisted_analysis_contract_accepts_metadata_citations_but_coverage_stays_paragraph_only() -> None:
    value = {
        "summary": [{"text": "The imported sender is counsel.", "citation_ids": ["metadata:email.sender"]}],
        "determination": "RESPONSIVE",
        "confidence": 0.9,
        "responsiveness_summary": [{"text": "The communication seeks advice.", "citation_ids": ["¶1"]}],
        "criterion_matches": [],
        "scope_analysis": [],
        "countervailing_considerations": [],
        "clarification_requests": [],
        "limitations": [],
        "coverage": {
            "status": "COMPLETE",
            "map_plan_version": "document-map-v1",
            "paragraph_map_version": "paragraph-v1",
            "window_count": 1,
            "successful_window_count": 1,
            "analyzed_paragraph_ids": ["¶1"],
            "partial_paragraph_ids": [],
            "omitted_ranges": [],
        },
    }

    validate_structured_output(value, DOCUMENT_ANALYSIS_OUTPUT_SCHEMA)
