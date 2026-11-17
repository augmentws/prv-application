from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest

from app.artifact_gateway import PreferredTextSource
from app.evidence_context import DocumentEvidenceContext, DocumentEvidenceContextItem
from app.evidence_first_contracts import freeze_criterion_catalog
from app.evidence_first_launcher import (
    MAX_SHADOW_DOCUMENTS,
    PreparedEvidenceShadowDocument,
    PreparedEvidenceShadowLaunch,
    _analysis_role_binding,
    _build_document_plan,
    _require_shadow_operator,
    execute_prepared_evidence_shadow,
    shadow_launch_key,
    shadow_plan_report,
    validate_shadow_selection,
)
from app.model_execution import content_hash


def _prepared_launch() -> PreparedEvidenceShadowLaunch:
    source_text = (
        "Alpha evidence belongs to the first paragraph.\n\n"
        "Beta evidence belongs to the second paragraph.\n\n"
        "Gamma evidence belongs to the third paragraph."
    )
    guidance = "Review documents for contract drafting evidence."
    tenant_id = uuid.UUID(int=1)
    client_id = uuid.UUID(int=2)
    matter_id = uuid.UUID(int=3)
    document_id = uuid.UUID(int=4)
    skill_version = SimpleNamespace(
        id=uuid.UUID(int=5),
        instructions="Extract bounded evidence.",
        model_policy={"temperature": 0},
        required_capabilities=["structured_output", "long_context"],
        limits={"max_requests": 1, "max_output_tokens": 8_000, "max_output_retries": 0},
    )
    catalog = freeze_criterion_catalog(
        [
            {
                "criterion_key": "draft_contracts_and_documents",
                "display_label": "Draft contracts and documents",
                "instruction_text": guidance,
            }
        ]
    )
    assessment = SimpleNamespace(
        id=uuid.UUID(int=6),
        definition_content_hash=hashlib.sha256(guidance.encode()).hexdigest(),
    )
    matter = SimpleNamespace(
        id=matter_id,
        client_id=client_id,
        client=SimpleNamespace(tenant_id=tenant_id),
    )
    revision = SimpleNamespace(id=uuid.UUID(int=7), content_markdown=guidance)
    document = SimpleNamespace(
        id=document_id,
        collection_item_id=uuid.UUID(int=8),
    )
    native_artifact_hash = "a" * 64
    source = PreferredTextSource(
        artifact_id=uuid.UUID(int=9),
        content_hash=native_artifact_hash,
        artifact_role="NATIVE",
        media_type="application/pdf",
        text=source_text,
    )
    binding = _analysis_role_binding(
        skill_version,
        resolved_model="google:gemini-3.7-flash",
    )
    context_items = (
        DocumentEvidenceContextItem(
            citation_id="metadata:email.sender",
            label="Email sender",
            value="lawyer@example.com",
        ),
    )
    document_context = DocumentEvidenceContext(
        items=context_items,
        context_hash=content_hash([item.as_model_input() for item in context_items]),
    )
    plan = _build_document_plan(
        assessment=assessment,
        matter=matter,
        revision=revision,
        catalog=catalog,
        document=document,
        source=source,
        role_binding=binding,
        skill_version=skill_version,
        map_max_characters=100,
        document_context=document_context,
    )
    launch_key = shadow_launch_key(
        actor_user_id=uuid.UUID(int=10),
        assessment_id=assessment.id,
        document_plans=[(document.id, plan.plan_hash)],
    )
    return PreparedEvidenceShadowLaunch(
        actor_user_id=uuid.UUID(int=10),
        assessment=assessment,
        matter=matter,
        revision=revision,
        catalog=catalog,
        skill_version=skill_version,
        resolved_model="google:gemini-3.7-flash",
        binding_snapshot={},
        documents=(
            PreparedEvidenceShadowDocument(
                document=document,
                source=source,
                plan=plan,
                document_context=document_context,
            ),
        ),
        launch_key=launch_key,
    )


def test_shadow_selection_requires_one_bounded_strategy() -> None:
    first = uuid.UUID(int=1)
    second = uuid.UUID(int=2)

    assert validate_shadow_selection(document_ids=[first, first, second], max_documents=None) == (
        first,
        second,
    )
    assert validate_shadow_selection(document_ids=None, max_documents=3) is None
    with pytest.raises(ValueError, match="exactly one"):
        validate_shadow_selection(document_ids=[first], max_documents=1)
    with pytest.raises(ValueError, match="exactly one"):
        validate_shadow_selection(document_ids=None, max_documents=None)
    with pytest.raises(ValueError, match=f"between 1 and {MAX_SHADOW_DOCUMENTS}"):
        validate_shadow_selection(document_ids=None, max_documents=MAX_SHADOW_DOCUMENTS + 1)


def test_document_plan_retains_native_artifact_hash_not_rendered_text_hash() -> None:
    prepared = _prepared_launch()
    value = prepared.documents[0]

    assert value.plan.source_content_hash == value.source.content_hash
    assert value.plan.source_content_hash != hashlib.sha256(value.source.text.encode()).hexdigest()
    assert value.plan.source_length == len(value.source.text)
    assert value.plan.source_artifact_id == value.source.artifact_id


def test_dry_run_report_is_deterministic_and_excludes_source_text() -> None:
    prepared = _prepared_launch()
    report = shadow_plan_report(prepared)
    serialized = json.dumps(report, sort_keys=True)

    assert report["mode"] == "DRY_RUN"
    assert report["launch_key"] == prepared.launch_key
    assert report["document_count"] == 1
    assert report["documents"][0]["plan_hash"] == prepared.documents[0].plan.plan_hash
    assert prepared.documents[0].source.text not in serialized
    assert "Alpha evidence" not in serialized
    assert "lawyer@example.com" not in serialized
    assert report["documents"][0]["document_context_item_count"] == 1


def test_launch_key_changes_when_selected_plan_order_changes() -> None:
    actor_user_id = uuid.UUID(int=4)
    assessment_id = uuid.UUID(int=1)
    first = (uuid.UUID(int=2), "a" * 64)
    second = (uuid.UUID(int=3), "b" * 64)

    assert shadow_launch_key(
        actor_user_id=actor_user_id,
        assessment_id=assessment_id,
        document_plans=[first, second],
    ) != shadow_launch_key(
        actor_user_id=actor_user_id,
        assessment_id=assessment_id,
        document_plans=[second, first],
    )


def test_shadow_operator_requires_active_root_or_owning_tenant_admin() -> None:
    tenant_id = uuid.UUID(int=1)
    owning_admin = SimpleNamespace(
        id=uuid.UUID(int=2),
        status="ACTIVE",
        tenant_role="ADMIN",
        tenant_id=tenant_id,
        tenant=SimpleNamespace(status="ACTIVE", is_root=False),
    )
    root_admin = SimpleNamespace(
        id=uuid.UUID(int=3),
        status="ACTIVE",
        tenant_role="ADMIN",
        tenant_id=uuid.UUID(int=4),
        tenant=SimpleNamespace(status="ACTIVE", is_root=True),
    )

    assert _require_shadow_operator(owning_admin, tenant_id=tenant_id) is owning_admin
    assert _require_shadow_operator(root_admin, tenant_id=tenant_id) is root_admin

    invalid_actors = (
        None,
        SimpleNamespace(
            status="SUSPENDED",
            tenant_role="ADMIN",
            tenant_id=tenant_id,
            tenant=SimpleNamespace(status="ACTIVE", is_root=False),
        ),
        SimpleNamespace(
            status="ACTIVE",
            tenant_role="USER",
            tenant_id=tenant_id,
            tenant=SimpleNamespace(status="ACTIVE", is_root=False),
        ),
    )
    for invalid in invalid_actors:
        with pytest.raises(PermissionError):
            _require_shadow_operator(invalid, tenant_id=tenant_id)


def test_execute_guard_rejects_before_workflow_creation(monkeypatch) -> None:
    prepared = _prepared_launch()
    workflow_created = False

    def unexpected_workflow(*_args, **_kwargs):
        nonlocal workflow_created
        workflow_created = True
        raise AssertionError("workflow creation must remain behind the feature guard")

    monkeypatch.setattr(
        "app.evidence_first_managed.get_settings",
        lambda: SimpleNamespace(definition_assessment_evidence_shadow_enabled=False),
    )
    monkeypatch.setattr("app.evidence_first_launcher._shadow_workflow", unexpected_workflow)

    with pytest.raises(RuntimeError, match="disabled by configuration"):
        asyncio.run(execute_prepared_evidence_shadow(object(), prepared))
    assert workflow_created is False
