from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config import get_settings
from app.document_evidence import build_document_map_plan, segment_paragraphs
from app.evidence_context import DocumentEvidenceContext, DocumentEvidenceContextItem
from app.evidence_first_artifacts import (
    AnalysisRoleBinding,
    AnalysisRoleBudget,
    build_evidence_evaluation_plan,
    validate_observation_fragment,
)
from app.evidence_first_contracts import freeze_criterion_catalog
from app.evidence_first_managed import (
    EvidenceShadowDisabledError,
    ManagedEvidenceUnitExtractor,
    require_evidence_shadow_enabled,
)
from app.model_execution import content_hash
from app.workflow_specs import MATTER_DEFINITION_EVIDENCE_SHADOW_SPEC


def _hash(value: str) -> str:
    return value * 64


def _context(document_context: DocumentEvidenceContext | None = None):
    source_text = (
        "Alpha evidence belongs to the first paragraph.\n\n"
        "Beta evidence belongs to the second paragraph.\n\n"
        "Gamma evidence belongs to the third paragraph."
    )
    review_guidance = "Review documents for contract drafting evidence."
    catalog = freeze_criterion_catalog(
        [
            {
                "criterion_key": "draft_contracts_and_documents",
                "display_label": "Draft contracts and documents",
                "instruction_text": review_guidance,
            }
        ]
    )
    paragraph_map = segment_paragraphs(source_text)
    document_map_plan = build_document_map_plan(
        paragraph_map,
        max_characters=100,
        overlap_segments=1,
    )
    skill_version_id = uuid.UUID(int=8)
    binding = AnalysisRoleBinding(
        role_key="evidence_extraction",
        skill_definition_version_id=skill_version_id,
        provider="google",
        model_key="google:gemini-3.5-flash-lite",
        prompt_hash=_hash("2"),
        application_schema_hash=_hash("3"),
        provider_schema_hash=_hash("4"),
        model_settings_hash=_hash("5"),
        capability_profile_hash=_hash("6"),
    )
    plan = build_evidence_evaluation_plan(
        tenant_id=uuid.UUID(int=1),
        client_id=uuid.UUID(int=2),
        matter_id=uuid.UUID(int=3),
        matter_document_id=uuid.UUID(int=4),
        collection_item_id=uuid.UUID(int=5),
        source_artifact_id=uuid.UUID(int=6),
        source_content_hash=hashlib.sha256(source_text.encode()).hexdigest(),
        task_version_key="matter-definition:evidence-first-v1",
        guidance_revision_id=uuid.UUID(int=7),
        guidance_content_hash=hashlib.sha256(review_guidance.encode()).hexdigest(),
        criterion_catalog_hash=catalog.catalog_hash,
        paragraph_map=paragraph_map,
        document_map_plan=document_map_plan,
        role_bindings=(binding,),
        role_budgets=(
            AnalysisRoleBudget(
                role_key="evidence_extraction",
                max_requests=len(document_map_plan.windows),
                repair_request_reserve=0,
                max_input_tokens=100_000,
                max_output_tokens=10_000,
                max_elapsed_seconds=600,
            ),
        ),
        document_context_hash=(document_context.context_hash if document_context is not None else None),
        document_context_citation_ids=(
            tuple(sorted(document_context.citation_ids)) if document_context is not None else ()
        ),
    )
    workflow = SimpleNamespace(
        id=uuid.UUID(int=20),
        workflow_key=MATTER_DEFINITION_EVIDENCE_SHADOW_SPEC.key,
        tenant_id=plan.tenant_id,
        client_id=plan.client_id,
        matter_id=plan.matter_id,
        initiated_by_user_id=uuid.UUID(int=21),
    )
    step = SimpleNamespace(
        id=uuid.UUID(int=22),
        workflow_run_id=workflow.id,
        role_key="evidence_extraction",
    )
    skill_version = SimpleNamespace(
        id=skill_version_id,
        input_schema_key="matter_definition_evidence_extraction_input_v2",
        output_schema_key="matter_definition_evidence_extraction_output_v1",
        limits={"max_requests": 1, "max_output_tokens": 8_000, "max_output_retries": 0},
    )
    return plan, source_text, review_guidance, catalog, workflow, step, skill_version


def test_managed_extractor_uses_pinned_skill_and_normalizes_unit_output() -> None:
    plan, source_text, review_guidance, catalog, workflow, step, skill_version = _context()
    unit = plan.extraction_units[0]
    paragraph_id = unit.segments[0].paragraph_id
    captured: dict = {}
    invocation_id = uuid.UUID(int=30)

    async def skill_runner(_db, **kwargs):
        captured.update(kwargs)
        raw = {
            "observations": [
                {
                    "criterion_key": "draft_contracts_and_documents",
                    "signal_type": "NEAR_MISS",
                    "fact": "The source unit discusses drafting a contract.",
                    "primary_citation_id": paragraph_id,
                    "additional_citation_id_1": paragraph_id,
                    "additional_citation_id_2": None,
                    "additional_citation_id_3": None,
                    "additional_citation_id_4": None,
                }
            ]
        }
        output = kwargs["output_normalizers"][0](raw)
        started_at = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
        return output, SimpleNamespace(
            id=uuid.UUID(int=29),
            started_at=started_at,
            completed_at=started_at + timedelta(seconds=1),
        )

    extractor = ManagedEvidenceUnitExtractor(
        db=MagicMock(),
        workflow=workflow,
        step=step,
        skill_version=skill_version,
        review_guidance=review_guidance,
        catalog=catalog,
        source_text=source_text,
        source_artifact_id=plan.source_artifact_id,
        source_content_hash=plan.source_content_hash,
        resolved_model="google:gemini-3.5-flash-lite",
        skill_runner=skill_runner,
        invocation_id_loader=lambda _db, _skill_run_id: (invocation_id,),
    )
    result = asyncio.run(extractor(plan, unit, 1))

    assert result.status == "COMPLETED_WITH_ADJUSTMENTS"
    assert result.invocation_ids == (invocation_id,)
    assert result.observations[0].citation_ids == (paragraph_id,)
    assert result.adjustments[0].code == "DUPLICATE_CITATIONS_REMOVED"
    assert captured["skill_version"] is skill_version
    assert captured["scope_id"] == plan.matter_document_id
    assert captured["model"] == "google:gemini-3.5-flash-lite"
    assert captured["dynamic_input"]["source_unit"]["unit_id"] == unit.unit_id
    assert captured["dynamic_input"]["metadata_evidence"] == []
    assert captured["dynamic_input"]["source_unit"]["text"].startswith(paragraph_id)
    assert captured["rate_limit_policy"].requests_per_minute == (
        get_settings().definition_assessment_evidence_extraction_requests_per_minute
    )
    assert captured["rate_limit_policy"].input_tokens_per_minute == (
        get_settings().definition_assessment_evidence_extraction_input_tokens_per_minute
    )
    candidate_schema = captured["output_schema"]["$defs"]["EvidenceObservationCandidate"]
    assert candidate_schema["properties"]["criterion_key"]["enum"] == sorted(catalog.criterion_keys())


def test_managed_extractor_rejects_skill_version_drift_before_dispatch() -> None:
    plan, source_text, review_guidance, catalog, workflow, step, skill_version = _context()
    skill_version.id = uuid.UUID(int=999)
    called = False

    async def skill_runner(_db, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("skill runner must not be called")

    extractor = ManagedEvidenceUnitExtractor(
        db=MagicMock(),
        workflow=workflow,
        step=step,
        skill_version=skill_version,
        review_guidance=review_guidance,
        catalog=catalog,
        source_text=source_text,
        source_artifact_id=plan.source_artifact_id,
        source_content_hash=plan.source_content_hash,
        resolved_model="google:gemini-3.5-flash-lite",
        skill_runner=skill_runner,
    )

    with pytest.raises(ValueError, match="does not pin"):
        asyncio.run(extractor(plan, plan.extraction_units[0], 1))
    assert called is False


def test_managed_extractor_admits_hash_pinned_metadata_citations() -> None:
    items = (
        DocumentEvidenceContextItem(
            citation_id="metadata:email.sender",
            label="Email sender",
            value="lawyer@example.com",
        ),
    )
    document_context = DocumentEvidenceContext(
        items=items,
        context_hash=content_hash([item.as_model_input() for item in items]),
    )
    plan, source_text, review_guidance, catalog, workflow, step, skill_version = _context(document_context)
    unit = plan.extraction_units[0]
    captured: dict = {}

    async def skill_runner(_db, **kwargs):
        captured.update(kwargs)
        output = kwargs["output_normalizers"][0](
            {
                "observations": [
                    {
                        "criterion_key": "draft_contracts_and_documents",
                        "signal_type": "SUPPORT",
                        "fact": "The imported sender is a lawyer.",
                        "primary_citation_id": "metadata:email.sender",
                        "additional_citation_id_1": None,
                        "additional_citation_id_2": None,
                        "additional_citation_id_3": None,
                        "additional_citation_id_4": None,
                    }
                ]
            }
        )
        now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
        return output, SimpleNamespace(id=uuid.UUID(int=31), started_at=now, completed_at=now)

    extractor = ManagedEvidenceUnitExtractor(
        db=MagicMock(),
        workflow=workflow,
        step=step,
        skill_version=skill_version,
        review_guidance=review_guidance,
        catalog=catalog,
        source_text=source_text,
        source_artifact_id=plan.source_artifact_id,
        source_content_hash=plan.source_content_hash,
        resolved_model="google:gemini-3.5-flash-lite",
        document_context=document_context,
        skill_runner=skill_runner,
        invocation_id_loader=lambda _db, _skill_run_id: (),
    )

    result = asyncio.run(extractor(plan, unit, 1))

    assert result.observations[0].citation_ids == ("metadata:email.sender",)
    assert validate_observation_fragment(plan, [result]) == (result,)
    assert captured["dynamic_input"]["metadata_evidence"] == [
        {
            "citation_id": "metadata:email.sender",
            "label": "Email sender",
            "value": "lawyer@example.com",
        }
    ]


def test_evidence_shadow_guard_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.evidence_first_managed.get_settings",
        lambda: SimpleNamespace(definition_assessment_evidence_shadow_enabled=False),
    )

    with pytest.raises(EvidenceShadowDisabledError, match="disabled by configuration"):
        require_evidence_shadow_enabled()
