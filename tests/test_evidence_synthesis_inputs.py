from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app import assessment_execution, evidence_synthesis_inputs
from app.evidence_routing import DIRECT_SYNTHESIS_SOURCE
from app.evidence_synthesis_inputs import (
    EVIDENCE_FIRST_SYNTHESIS_SOURCE,
    EvidenceSynthesisInputs,
    assessment_uses_evidence_first_synthesis,
    load_evidence_first_synthesis_inputs,
)
from app.matter_definition_assessments import AssessmentError


def _records(*, source: str | None = EVIDENCE_FIRST_SYNTHESIS_SOURCE):
    tenant_id = uuid.uuid4()
    client_id = uuid.uuid4()
    matter_id = uuid.uuid4()
    assessment = SimpleNamespace(
        id=uuid.uuid4(),
        matter_id=matter_id,
        review_batch_run_id=None,
        initiated_by_user_id=uuid.uuid4(),
        selected_count=3,
        configuration_snapshot=(
            {"synthesis_document_source": source} if source is not None else {}
        ),
    )
    matter = SimpleNamespace(
        id=matter_id,
        client_id=client_id,
        client=SimpleNamespace(tenant_id=tenant_id),
    )
    return assessment, matter


def test_evidence_first_selection_requires_explicit_frozen_setting() -> None:
    assessment, _matter = _records()
    assert assessment_uses_evidence_first_synthesis(assessment)

    assessment.configuration_snapshot = {}
    assert not assessment_uses_evidence_first_synthesis(assessment)

    assessment.configuration_snapshot = {"synthesis_document_source": "UNKNOWN"}
    assert not assessment_uses_evidence_first_synthesis(assessment)


def test_loader_prefers_manifest_selection_and_reports_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, matter = _records()
    narrative_id, fallback_id, action_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    documents = (
        SimpleNamespace(
            matter_document_id=narrative_id,
            extraction=SimpleNamespace(status="COMPLETED"),
            synthesis_readiness="READY",
            selected_output_artifact_id=uuid.uuid4(),
            selected_output_kind="NARRATIVE_PRESENTATION",
        ),
        SimpleNamespace(
            matter_document_id=fallback_id,
            extraction=SimpleNamespace(status="COMPLETED"),
            synthesis_readiness="READY_WITH_FALLBACK",
            selected_output_artifact_id=uuid.uuid4(),
            selected_output_kind="DETERMINISTIC_COMPATIBILITY",
        ),
        SimpleNamespace(
            matter_document_id=action_id,
            extraction=SimpleNamespace(status="FAILED"),
            synthesis_readiness="ACTION_REQUIRED",
            selected_output_artifact_id=None,
            selected_output_kind=None,
        ),
    )
    manifest = SimpleNamespace(
        documents=documents,
        document_count=3,
        readiness_counts={"READY": 1, "READY_WITH_FALLBACK": 1, "ACTION_REQUIRED": 1},
        result_hash="a" * 64,
        source_workflow_ids=(uuid.uuid4(), uuid.uuid4()),
    )
    monkeypatch.setattr(
        evidence_synthesis_inputs,
        "prepare_evidence_outcome_manifest",
        lambda *_args, **_kwargs: SimpleNamespace(manifest=manifest),
    )
    monkeypatch.setattr(
        evidence_synthesis_inputs,
        "get_workflow_artifact_reference",
        lambda **kwargs: SimpleNamespace(artifact_id=kwargs["artifact_id"]),
    )

    def selected_output(**kwargs):
        return {
            "coverage": {
                "status": "PARTIAL"
                if kwargs["selected_kind"] == "DETERMINISTIC_COMPATIBILITY"
                else "COMPLETE"
            }
        }

    monkeypatch.setattr(evidence_synthesis_inputs, "_selected_output", selected_output)

    loaded = load_evidence_first_synthesis_inputs(
        SimpleNamespace(),
        assessment=assessment,
        matter=matter,
    )

    assert [value["matter_document_id"] for value in loaded.analyses] == [
        str(narrative_id),
        str(fallback_id),
    ]
    assert loaded.partial_count == 1
    assert loaded.invalid_count == 1
    assert loaded.diagnostics == {
        "source": EVIDENCE_FIRST_SYNTHESIS_SOURCE,
        "manifest_result_hash": "a" * 64,
        "population_document_count": 3,
        "loaded_document_count": 2,
        "narrative_document_count": 1,
        "deterministic_document_count": 1,
        "fallback_document_count": 1,
        "action_required_document_count": 1,
        "artifact_error_document_count": 0,
        "pending_document_count": 0,
        "source_workflow_count": 2,
    }


def test_loader_refuses_to_synthesize_a_pending_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, matter = _records()
    manifest = SimpleNamespace(
        readiness_counts={"PENDING": 2},
        documents=(
            SimpleNamespace(extraction=SimpleNamespace(status="RUNNING")),
            SimpleNamespace(extraction=SimpleNamespace(status="COMPLETED")),
        ),
    )
    monkeypatch.setattr(
        evidence_synthesis_inputs,
        "prepare_evidence_outcome_manifest",
        lambda *_args, **_kwargs: SimpleNamespace(manifest=manifest),
    )

    with pytest.raises(AssessmentError, match="not ready for 2 document"):
        load_evidence_first_synthesis_inputs(
            SimpleNamespace(),
            assessment=assessment,
            matter=matter,
        )


def test_terminal_preparation_failure_does_not_leave_synthesis_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, matter = _records()
    assessment.review_batch_run_id = uuid.uuid4()
    document_id = uuid.uuid4()
    manifest = SimpleNamespace(
        readiness_counts={"READY": 1},
        documents=(
            SimpleNamespace(
                matter_document_id=document_id,
                extraction=SimpleNamespace(status="NOT_STARTED"),
                synthesis_readiness="READY",
                selected_output_artifact_id=uuid.uuid4(),
                selected_output_kind="DETERMINISTIC_COMPATIBILITY",
            ),
        ),
        document_count=1,
        result_hash="a" * 64,
        source_workflow_ids=(),
    )
    monkeypatch.setattr(
        evidence_synthesis_inputs,
        "prepare_evidence_outcome_manifest",
        lambda *_args, **_kwargs: SimpleNamespace(manifest=manifest),
    )
    db = SimpleNamespace(scalars=lambda _statement: [document_id])

    loaded = load_evidence_first_synthesis_inputs(
        db,
        assessment=assessment,
        matter=matter,
    )

    assert loaded.analyses == []
    assert loaded.invalid_count == 1
    assert loaded.diagnostics["action_required_document_count"] == 1


def test_synthesis_loader_does_not_mix_direct_and_evidence_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment, matter = _records()
    evidence = EvidenceSynthesisInputs(
        analyses=[{"matter_document_id": "evidence"}],
        partial_count=1,
        invalid_count=2,
        diagnostics={"source": EVIDENCE_FIRST_SYNTHESIS_SOURCE},
    )
    monkeypatch.setattr(
        assessment_execution,
        "load_evidence_first_synthesis_inputs",
        lambda *_args, **_kwargs: evidence,
    )
    monkeypatch.setattr(
        assessment_execution,
        "_load_document_analyses",
        lambda *_args, **_kwargs: pytest.fail("direct inputs must not be loaded"),
    )

    loaded = assessment_execution._load_synthesis_document_analyses(
        SimpleNamespace(), assessment, matter
    )
    assert loaded == (evidence.analyses, 1, 2, evidence.diagnostics)

    assessment.configuration_snapshot = {}
    monkeypatch.setattr(
        assessment_execution,
        "_load_document_analyses",
        lambda *_args, **_kwargs: ([{"matter_document_id": "direct"}], 0, 0),
    )
    direct = assessment_execution._load_synthesis_document_analyses(
        SimpleNamespace(), assessment, matter
    )
    assert direct[0] == [{"matter_document_id": "direct"}]
    assert direct[3]["source"] == DIRECT_SYNTHESIS_SOURCE
