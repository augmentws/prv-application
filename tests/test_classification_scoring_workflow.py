from unittest.mock import Mock

import pytest
from dbos._error import DBOSMaxStepRetriesExceeded

from app.workflows import classification_scoring
from classification_service.backend import ClassificationRuntimeError


def test_scoring_workflow_prepares_invokes_and_finalizes(monkeypatch) -> None:
    invocation = {"request": "frozen"}
    runtime_output = {"result": "published"}
    monkeypatch.setattr(classification_scoring, "prepare", Mock(return_value=invocation))
    monkeypatch.setattr(classification_scoring, "score", Mock(return_value=runtime_output))
    monkeypatch.setattr(classification_scoring, "finalize", Mock())

    classification_scoring.classification_model_score.__wrapped__("run-id")

    classification_scoring.score.assert_called_once_with(invocation)
    classification_scoring.finalize.assert_called_once_with("run-id", runtime_output)


def test_scoring_workflow_persists_failure(monkeypatch) -> None:
    failure = RuntimeError("runtime unavailable")
    fail = Mock()
    monkeypatch.setattr(classification_scoring, "prepare", Mock(return_value={"request": "frozen"}))
    monkeypatch.setattr(classification_scoring, "score", Mock(side_effect=failure))
    monkeypatch.setattr(classification_scoring, "fail", fail)

    with pytest.raises(RuntimeError, match="runtime unavailable"):
        classification_scoring.classification_model_score.__wrapped__("run-id")

    fail.assert_called_once_with("run-id", "runtime unavailable", "CLASSIFICATION_SCORING_FAILED", True)


def test_scoring_workflow_persists_the_underlying_retry_failure(monkeypatch) -> None:
    failure = DBOSMaxStepRetriesExceeded(
        "invoke_classification_model_scoring_runtime",
        3,
        [ClassificationRuntimeError("invalid scoring input", code="SCORING_INPUT_INVALID")],
    )
    fail = Mock()
    monkeypatch.setattr(classification_scoring, "prepare", Mock(return_value={"request": "frozen"}))
    monkeypatch.setattr(classification_scoring, "score", Mock(side_effect=failure))
    monkeypatch.setattr(classification_scoring, "fail", fail)

    with pytest.raises(DBOSMaxStepRetriesExceeded):
        classification_scoring.classification_model_score.__wrapped__("run-id")

    fail.assert_called_once_with(
        "run-id",
        "invalid scoring input",
        "SCORING_INPUT_INVALID",
        False,
    )
