from unittest.mock import Mock

from app.workflows import evidence_stage_retries


def test_evidence_retry_workflow_returns_worker_result(monkeypatch) -> None:
    result = {"status": "COMPLETED", "reused_unit_count": 4}
    execute = Mock(return_value=result)
    fail = Mock()
    monkeypatch.setattr(evidence_stage_retries, "execute_evidence_stage_retry", execute)
    monkeypatch.setattr(evidence_stage_retries, "fail_evidence_stage_retry", fail)

    actual = evidence_stage_retries.evidence_stage_retry_workflow.__wrapped__(
        "assessment-id",
        "workflow-id",
        "EXTRACTION",
        "actor-id",
    )

    assert actual == result
    execute.assert_called_once_with(
        "assessment-id",
        "workflow-id",
        "EXTRACTION",
        "actor-id",
    )
    fail.assert_not_called()


def test_evidence_retry_workflow_records_terminal_failure(monkeypatch) -> None:
    execute = Mock(side_effect=RuntimeError("provider unavailable"))
    fail = Mock()
    monkeypatch.setattr(evidence_stage_retries, "execute_evidence_stage_retry", execute)
    monkeypatch.setattr(evidence_stage_retries, "fail_evidence_stage_retry", fail)

    actual = evidence_stage_retries.evidence_stage_retry_workflow.__wrapped__(
        "assessment-id",
        "workflow-id",
        "NARRATIVE",
        "actor-id",
    )

    assert actual is None
    fail.assert_called_once_with("workflow-id", "NARRATIVE", "RuntimeError")


def test_extraction_retry_rebuilds_downstream_chain(monkeypatch) -> None:
    execute = Mock(
        return_value={
            "status": "COMPLETED",
            "documents": [
                {
                    "matter_document_id": "document-id",
                    "observations_artifact_id": "observations-id",
                }
            ],
        }
    )
    adjudicate = Mock(
        return_value={
            "compatibility_projection_status": "COMPLETED",
            "compatibility_result_artifact_id": "compatibility-id",
        }
    )
    narrate = Mock(
        return_value={
            "narrative_status": "COMPLETED",
            "narrative_artifact_id": "narrative-id",
        }
    )
    monkeypatch.setattr(evidence_stage_retries, "execute_evidence_stage_retry", execute)
    monkeypatch.setattr(
        evidence_stage_retries, "propagate_evidence_retry_adjudication", adjudicate
    )
    monkeypatch.setattr(
        evidence_stage_retries, "propagate_evidence_retry_narrative", narrate
    )
    monkeypatch.setattr(
        evidence_stage_retries, "evidence_retry_is_authoritative", Mock(return_value=False)
    )

    actual = evidence_stage_retries.evidence_stage_retry_workflow.__wrapped__(
        "assessment-id",
        "workflow-id",
        "EXTRACTION",
        "actor-id",
        "document-id",
    )

    assert actual["propagated_stages"] == ["ADJUDICATION", "NARRATIVE"]
    assert actual["document_result"] == {
        "matter_document_id": "document-id",
        "status": "READY",
        "compatibility_result_artifact_id": "compatibility-id",
        "narrative_artifact_id": "narrative-id",
    }
    assert actual["assessment_resynthesized"] is False
    adjudicate.assert_called_once_with("assessment-id", "observations-id", "actor-id")
    narrate.assert_called_once_with("assessment-id", "compatibility-id", "actor-id")


def test_authoritative_retry_reconciles_and_resynthesizes(monkeypatch) -> None:
    monkeypatch.setattr(
        evidence_stage_retries,
        "execute_evidence_stage_retry",
        Mock(
            return_value={
                "narrative_status": "COMPLETED",
                "narrative_artifact_id": "narrative-id",
                "fallback_compatibility_result_artifact_id": "compatibility-id",
            }
        ),
    )
    monkeypatch.setattr(
        evidence_stage_retries, "evidence_retry_is_authoritative", Mock(return_value=True)
    )
    reconcile = Mock()
    prepare = Mock(return_value=True)
    synthesize = Mock()
    sync = Mock()
    complete = Mock()
    monkeypatch.setattr(evidence_stage_retries, "reconcile_evidence_outcomes", reconcile)
    monkeypatch.setattr(evidence_stage_retries, "prepare_evidence_retry_resynthesis", prepare)
    monkeypatch.setattr(evidence_stage_retries, "synthesize", synthesize)
    monkeypatch.setattr(evidence_stage_retries, "sync_batch_search", sync)
    monkeypatch.setattr(evidence_stage_retries, "complete", complete)

    actual = evidence_stage_retries.evidence_stage_retry_workflow.__wrapped__(
        "assessment-id",
        "workflow-id",
        "NARRATIVE",
        "actor-id",
        "document-id",
    )

    assert actual["assessment_resynthesized"] is True
    reconcile.assert_called_once_with(
        "assessment-id",
        "actor-id",
        [
            {
                "matter_document_id": "document-id",
                "status": "READY",
                "compatibility_result_artifact_id": "compatibility-id",
                "narrative_artifact_id": "narrative-id",
            }
        ],
    )
    prepare.assert_called_once_with(
        "assessment-id", "document-id", "NARRATIVE", "actor-id"
    )
    synthesize.assert_called_once_with("assessment-id")
    sync.assert_called_once_with("assessment-id")
    complete.assert_called_once_with("assessment-id")
