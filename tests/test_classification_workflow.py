from unittest.mock import Mock

import pytest

from app.classification_builds import ClassificationBuildConflictError
from app.workflows import classification_models


def test_workflow_returns_the_finalized_version(monkeypatch) -> None:
    invocation = {"completed": False, "request": {}, "access": {}}
    runtime_output = {"completed": False, "result": {}, "package": {}}
    monkeypatch.setattr(classification_models, "prepare", Mock(return_value=invocation))
    monkeypatch.setattr(classification_models, "mark_training_started", Mock())
    monkeypatch.setattr(classification_models, "train", Mock(return_value=runtime_output))
    monkeypatch.setattr(classification_models, "mark_runtime_completed", Mock())
    monkeypatch.setattr(classification_models, "finalize", Mock(return_value="version-id"))

    result = classification_models.classification_model_build.__wrapped__("run-id")

    assert result == "version-id"
    classification_models.mark_training_started.assert_called_once_with("run-id")
    classification_models.train.assert_called_once_with(invocation)
    classification_models.mark_runtime_completed.assert_called_once_with("run-id")
    classification_models.finalize.assert_called_once_with("run-id", runtime_output)


def test_workflow_persists_failure_and_remains_failed_in_dbos(monkeypatch) -> None:
    failure = ClassificationBuildConflictError(
        "runtime identity mismatch",
        code="RUNTIME_OUTPUT_REFERENCE_MISMATCH",
    )
    fail = Mock()
    monkeypatch.setattr(
        classification_models,
        "prepare",
        Mock(return_value={"completed": False, "request": {}, "access": {}}),
    )
    monkeypatch.setattr(classification_models, "train", Mock(side_effect=failure))
    monkeypatch.setattr(classification_models, "mark_training_started", Mock())
    monkeypatch.setattr(classification_models, "fail", fail)

    with pytest.raises(ClassificationBuildConflictError, match="runtime identity mismatch"):
        classification_models.classification_model_build.__wrapped__("run-id")

    fail.assert_called_once_with(
        "run-id",
        "runtime identity mismatch",
        "RUNTIME_OUTPUT_REFERENCE_MISMATCH",
        False,
    )
