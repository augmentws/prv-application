from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, call

from app.evidence_synthesis_inputs import EVIDENCE_FIRST_SYNTHESIS_SOURCE
from app.model_execution import content_hash
from app.workflows import evidence_assessments


class _ImmediateHandle:
    def __init__(self, callback):
        self.callback = callback

    def get_result(self):
        return self.callback()


class _ImmediateQueue:
    def enqueue(self, workflow, *args):
        return _ImmediateHandle(lambda: workflow.__wrapped__(*args))


def _mock_fanout(monkeypatch, *, extraction=None) -> None:
    monkeypatch.setattr(evidence_assessments, "SetWorkflowID", lambda _value: nullcontext())
    monkeypatch.setattr(evidence_assessments, "EVIDENCE_EXTRACTION_UNIT_QUEUE", _ImmediateQueue())
    monkeypatch.setattr(evidence_assessments, "EVIDENCE_ADJUDICATION_QUEUE", _ImmediateQueue())
    monkeypatch.setattr(evidence_assessments, "EVIDENCE_NARRATIVE_QUEUE", _ImmediateQueue())
    monkeypatch.setattr(
        evidence_assessments,
        "prepare_extraction_fanout",
        Mock(return_value={"workflow_run_id": "workflow-id", "plan_hash": "plan-hash", "unit_ids": []}),
    )
    monkeypatch.setattr(
        evidence_assessments,
        "finalize_extraction_fanout",
        Mock(
            return_value=extraction
            or {"documents": [{"observations_artifact_id": "observations-id"}]}
        ),
    )


def test_document_pipeline_runs_ordered_stages(monkeypatch) -> None:
    _mock_fanout(monkeypatch)
    monkeypatch.setattr(
        evidence_assessments,
        "execute_adjudication",
        Mock(
            return_value={
                "compatibility_projection_status": "COMPLETED",
                "compatibility_result_artifact_id": "compatibility-id",
            }
        ),
    )
    monkeypatch.setattr(
        evidence_assessments,
        "execute_narrative",
        Mock(
            return_value={
                "narrative_status": "COMPLETED",
                "narrative_artifact_id": "narrative-id",
            }
        ),
    )

    result = evidence_assessments.evidence_document_workflow.__wrapped__("assessment-id", "document-id", "actor-id")

    assert result == {
        "matter_document_id": "document-id",
        "status": "READY",
        "compatibility_result_artifact_id": "compatibility-id",
        "narrative_artifact_id": "narrative-id",
    }
    evidence_assessments.execute_adjudication.assert_called_once_with(
        "assessment-id",
        "observations-id",
        "actor-id",
        None,
    )
    evidence_assessments.execute_narrative.assert_called_once_with("assessment-id", "compatibility-id", "actor-id")


def test_v3_document_pipeline_publishes_terminal_progress(monkeypatch) -> None:
    _mock_fanout(monkeypatch)
    monkeypatch.setattr(
        evidence_assessments,
        "execute_adjudication",
        Mock(
            return_value={
                "compatibility_projection_status": "COMPLETED",
                "compatibility_result_artifact_id": "compatibility-id",
            }
        ),
    )
    monkeypatch.setattr(
        evidence_assessments,
        "execute_narrative",
        Mock(return_value={"narrative_status": "COMPLETED", "narrative_artifact_id": "narrative-id"}),
    )
    record = Mock(return_value={"completed": 1})
    monkeypatch.setattr(evidence_assessments, "record_document_progress", record)

    result = evidence_assessments.evidence_document_workflow_v3.__wrapped__(
        "assessment-id", "document-id", "actor-id"
    )

    assert result["status"] == "READY"
    record.assert_called_once_with("assessment-id", "document-id", result)


def test_document_pipeline_preserves_compatibility_when_narrative_fails(monkeypatch) -> None:
    _mock_fanout(monkeypatch)
    monkeypatch.setattr(
        evidence_assessments,
        "execute_adjudication",
        Mock(
            return_value={
                "compatibility_projection_status": "COMPLETED",
                "compatibility_result_artifact_id": "compatibility-id",
            }
        ),
    )
    monkeypatch.setattr(
        evidence_assessments,
        "execute_narrative",
        Mock(side_effect=RuntimeError("provider unavailable")),
    )

    result = evidence_assessments.evidence_document_workflow.__wrapped__("assessment-id", "document-id", "actor-id")

    assert result == {
        "matter_document_id": "document-id",
        "status": "READY_WITH_FALLBACK",
        "compatibility_result_artifact_id": "compatibility-id",
        "failed_stage": "NARRATIVE",
        "error_type": "RuntimeError",
    }


def test_document_pipeline_stops_after_failed_extraction(monkeypatch) -> None:
    _mock_fanout(monkeypatch)
    monkeypatch.setattr(
        evidence_assessments,
        "prepare_extraction_fanout",
        Mock(side_effect=ValueError("no source")),
    )
    monkeypatch.setattr(evidence_assessments, "execute_adjudication", Mock())

    result = evidence_assessments.evidence_document_workflow.__wrapped__("assessment-id", "document-id", "actor-id")

    assert result == {
        "matter_document_id": "document-id",
        "status": "ACTION_REQUIRED",
        "failed_stage": "EXTRACTION",
        "error_type": "ValueError",
        "error_summary": "no source",
    }
    evidence_assessments.execute_adjudication.assert_not_called()


def test_document_pipeline_dispatches_all_extraction_units_before_waiting(monkeypatch) -> None:
    events: list[str] = []

    class UnitHandle:
        def __init__(self, unit_id: str):
            self.unit_id = unit_id

        def get_result(self):
            events.append(f"wait:{self.unit_id}")
            return {"unit_id": self.unit_id, "status": "COMPLETED"}

    class UnitQueue:
        def enqueue(self, _workflow, _assessment, _document, _actor, _run, unit_id):
            events.append(f"enqueue:{unit_id}")
            return UnitHandle(unit_id)

    monkeypatch.setattr(evidence_assessments, "SetWorkflowID", lambda _value: nullcontext())
    monkeypatch.setattr(evidence_assessments, "EVIDENCE_EXTRACTION_UNIT_QUEUE", UnitQueue())
    monkeypatch.setattr(evidence_assessments, "EVIDENCE_ADJUDICATION_QUEUE", _ImmediateQueue())
    monkeypatch.setattr(evidence_assessments, "EVIDENCE_NARRATIVE_QUEUE", _ImmediateQueue())
    monkeypatch.setattr(
        evidence_assessments,
        "prepare_extraction_fanout",
        lambda *_args: {
            "workflow_run_id": "workflow-id",
            "plan_hash": "plan-hash",
            "unit_ids": ["unit-1", "unit-2"],
        },
    )
    monkeypatch.setattr(
        evidence_assessments,
        "finalize_extraction_fanout",
        lambda *_args: events.append("finalize")
        or {"documents": [{"observations_artifact_id": "observations-id"}]},
    )
    monkeypatch.setattr(
        evidence_assessments,
        "execute_adjudication",
        lambda *_args: {
            "compatibility_projection_status": "COMPLETED",
            "compatibility_result_artifact_id": "compatibility-id",
        },
    )
    monkeypatch.setattr(
        evidence_assessments,
        "execute_narrative",
        lambda *_args: {"narrative_status": "COMPLETED", "narrative_artifact_id": "narrative-id"},
    )

    result = evidence_assessments.evidence_document_workflow.__wrapped__(
        "assessment-id", "document-id", "actor-id"
    )

    assert events == ["enqueue:unit-1", "enqueue:unit-2", "wait:unit-1", "wait:unit-2", "finalize"]
    assert result["status"] == "READY"


def test_parent_waits_for_documents_before_reconciliation_and_synthesis(monkeypatch) -> None:
    events: list[str] = []

    class Handle:
        def __init__(self, document_id: str):
            self.document_id = document_id

        def get_result(self):
            events.append(f"wait:{self.document_id}")
            return {"matter_document_id": self.document_id, "status": "READY"}

    queue = SimpleNamespace(enqueue=lambda _workflow, _assessment, document, _actor: Handle(document))
    monkeypatch.setattr(evidence_assessments, "EVIDENCE_DOCUMENT_QUEUE", queue)
    monkeypatch.setattr(evidence_assessments, "SetWorkflowID", lambda _value: nullcontext())
    monkeypatch.setattr(
        evidence_assessments,
        "activate_evidence_first_assessment",
        lambda *_args: events.append("activate"),
    )
    monkeypatch.setattr(evidence_assessments, "plan", lambda *_args: events.append("plan"))
    monkeypatch.setattr(
        evidence_assessments,
        "retrieve",
        lambda *_args: events.append("retrieve") or ["doc-1", "doc-2"],
    )
    monkeypatch.setattr(
        evidence_assessments,
        "sync_batch_search",
        lambda *_args: events.append("sync"),
    )
    monkeypatch.setattr(
        evidence_assessments,
        "reconcile_evidence_outcomes",
        lambda *_args: events.append("reconcile"),
    )
    monkeypatch.setattr(
        evidence_assessments,
        "synthesize",
        lambda *_args: events.append("synthesize"),
    )
    monkeypatch.setattr(
        evidence_assessments,
        "complete",
        lambda *_args: events.append("complete"),
    )
    fail = Mock()
    monkeypatch.setattr(evidence_assessments, "fail", fail)

    evidence_assessments.evidence_assessment_workflow.__wrapped__("assessment-id", "actor-id", "attempt-id")

    assert events == [
        "activate",
        "plan",
        "retrieve",
        "sync",
        "wait:doc-1",
        "wait:doc-2",
        "reconcile",
        "synthesize",
        "sync",
        "complete",
    ]
    fail.assert_not_called()


def test_stage_configuration_must_match_frozen_binding() -> None:
    binding = {"evidence_extraction": {"skill_definition_version_id": "version-id"}}
    model = "google:model"
    expected_hash = content_hash({"binding_snapshot": binding, "resolved_model": model})
    assessment = SimpleNamespace(
        configuration_snapshot={
            "synthesis_document_source": EVIDENCE_FIRST_SYNTHESIS_SOURCE,
            "evidence_first_orchestration": {"stages": {"EXTRACTION": {"configuration_hash": expected_hash}}},
        }
    )

    evidence_assessments._validate_stage_configuration(
        assessment,
        stage="EXTRACTION",
        prepared_binding_snapshot=binding,
        resolved_model=model,
    )

    try:
        evidence_assessments._validate_stage_configuration(
            assessment,
            stage="EXTRACTION",
            prepared_binding_snapshot=binding,
            resolved_model="google:different-model",
        )
    except ValueError as exc:
        assert "differs from the frozen assessment" in str(exc)
    else:
        raise AssertionError("Binding drift must stop the evidence-first parent")


def test_child_terminal_failure_overrides_a_stale_ready_manifest() -> None:
    outcome = SimpleNamespace(synthesis_readiness="READY")

    assert (
        evidence_assessments._reconciled_readiness(
            outcome,
            {"status": "ACTION_REQUIRED"},
        )
        == "ACTION_REQUIRED"
    )
    assert (
        evidence_assessments._reconciled_readiness(
            outcome,
            {"status": "READY_WITH_FALLBACK"},
        )
        == "READY_WITH_FALLBACK"
    )


def test_parent_failure_is_recorded(monkeypatch) -> None:
    monkeypatch.setattr(
        evidence_assessments,
        "activate_evidence_first_assessment",
        Mock(side_effect=RuntimeError("activation failed")),
    )
    fail = Mock()
    monkeypatch.setattr(evidence_assessments, "fail", fail)

    try:
        evidence_assessments.evidence_assessment_workflow.__wrapped__("assessment-id", "actor-id", "attempt-id")
    except RuntimeError:
        pass
    else:
        raise AssertionError("Parent workflow must preserve DBOS failure semantics")

    assert fail.call_args == call("assessment-id", "activation failed")
