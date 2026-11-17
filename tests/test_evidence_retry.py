import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.evidence_retry import (
    EvidenceStageRetryError,
    _supersede_downstream_workflows,
    schedule_evidence_stage_retry,
)
from app.models import utcnow


def _outcomes(*, document_id: uuid.UUID, workflow_id: uuid.UUID, retryable: bool = True):
    stage = SimpleNamespace(workflow_run_id=workflow_id)
    document = SimpleNamespace(
        matter_document_id=document_id,
        extraction=SimpleNamespace(workflow_run_id=None),
        adjudication=SimpleNamespace(workflow_run_id=None),
        narrative=stage,
        retryable_stages=("NARRATIVE",) if retryable else (),
    )
    return SimpleNamespace(manifest=SimpleNamespace(documents=(document,)))


def test_schedule_stage_retry_queues_durable_operation_and_preserves_reuse(monkeypatch) -> None:
    assessment_id = uuid.uuid4()
    document_id = uuid.uuid4()
    workflow_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    workflow = SimpleNamespace(
        id=workflow_id,
        tenant_id=uuid.uuid4(),
        status="COMPLETED_WITH_ERRORS",
        current_stage="COMPLETED",
        completed_at=object(),
        failure_code="OPTIONAL_NARRATIVE_FAILED",
        failure_summary="Narrative failed",
        failure_retriable=True,
        error_message="Narrative failed",
        configuration_snapshot={},
        progress={"narrative_status": "FAILED"},
        input_snapshot={"compatibility_result_artifact_id": str(uuid.uuid4())},
    )
    db = Mock()
    db.scalar.return_value = workflow
    db.scalars.return_value = []
    enqueue = Mock()
    monkeypatch.setattr(
        "app.evidence_retry.prepare_evidence_outcome_manifest",
        lambda *_args, **_kwargs: _outcomes(
            document_id=document_id,
            workflow_id=workflow_id,
        ),
    )
    monkeypatch.setattr("app.evidence_retry._validate_frozen_retry_input", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.evidence_retry.record_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.evidence_retry.enqueue_evidence_stage_retry", enqueue)

    result = schedule_evidence_stage_retry(
        db,
        actor_user_id=actor_id,
        assessment_id=assessment_id,
        matter_document_id=document_id,
        stage="NARRATIVE",
        settings=SimpleNamespace(
            definition_assessment_evidence_shadow_enabled=True,
            dbos_enabled=True,
        ),
    )

    assert result.stage == "NARRATIVE"
    assert result.target_workflow_run_id == workflow_id
    assert result.reused_completed_work is True
    assert workflow.status == "QUEUED"
    assert workflow.current_stage == "NARRATIVE"
    assert workflow.failure_code is None
    assert workflow.configuration_snapshot["retry_request"]["requested_by_user_id"] == str(actor_id)
    enqueue.assert_called_once()
    assert enqueue.call_args.args[1] == result.operation_workflow_id
    assert enqueue.call_args.kwargs["target_workflow_run_id"] == str(workflow_id)
    assert enqueue.call_args.kwargs["matter_document_id"] == str(document_id)


def test_schedule_stage_retry_rejects_stage_without_retryable_failure(monkeypatch) -> None:
    assessment_id = uuid.uuid4()
    document_id = uuid.uuid4()
    workflow_id = uuid.uuid4()
    monkeypatch.setattr(
        "app.evidence_retry.prepare_evidence_outcome_manifest",
        lambda *_args, **_kwargs: _outcomes(
            document_id=document_id,
            workflow_id=workflow_id,
            retryable=False,
        ),
    )
    enqueue = Mock()
    monkeypatch.setattr("app.evidence_retry.enqueue_evidence_stage_retry", enqueue)

    with pytest.raises(EvidenceStageRetryError, match="not currently retryable"):
        schedule_evidence_stage_retry(
            Mock(),
            actor_user_id=uuid.uuid4(),
            assessment_id=assessment_id,
            matter_document_id=document_id,
            stage="NARRATIVE",
            settings=SimpleNamespace(
                definition_assessment_evidence_shadow_enabled=True,
                dbos_enabled=True,
            ),
        )

    enqueue.assert_not_called()


def test_supersede_downstream_workflows_preserves_auditable_lineage() -> None:
    workflow = SimpleNamespace(
        id=uuid.uuid4(),
        status="COMPLETED",
        current_stage="COMPLETED",
        completed_at=utcnow(),
        failure_code=None,
        failure_summary=None,
        failure_retriable=False,
        error_message=None,
        configuration_snapshot={"launcher_version": "v1"},
    )
    requested_at = utcnow()

    superseded = _supersede_downstream_workflows(
        [workflow],
        stage="ADJUDICATION",
        attempt_id=uuid.uuid4(),
        operation_workflow_id="retry-operation",
        requested_at=requested_at,
    )

    assert superseded == (workflow.id,)
    assert workflow.status == "CANCELED"
    assert workflow.failure_code == "UPSTREAM_RETRY_SUPERSEDED"
    assert workflow.configuration_snapshot["launcher_version"] == "v1"
    assert (
        workflow.configuration_snapshot["superseded_by_evidence_retry"][
            "operation_workflow_id"
        ]
        == "retry-operation"
    )


def test_supersede_downstream_workflows_rejects_active_descendant() -> None:
    workflow = SimpleNamespace(id=uuid.uuid4(), status="RUNNING")

    with pytest.raises(EvidenceStageRetryError, match="still active"):
        _supersede_downstream_workflows(
            [workflow],
            stage="EXTRACTION",
            attempt_id=uuid.uuid4(),
            operation_workflow_id="retry-operation",
            requested_at=utcnow(),
        )
