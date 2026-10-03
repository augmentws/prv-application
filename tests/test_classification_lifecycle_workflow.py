from unittest.mock import Mock

from app.workflows import classification_lifecycle


def test_promotion_workflow_delegates_to_atomic_command_step(monkeypatch) -> None:
    apply_promotion = Mock(return_value="decision-id")
    monkeypatch.setattr(classification_lifecycle, "apply_promotion", apply_promotion)

    result = classification_lifecycle.classification_model_promote.__wrapped__(
        "workflow-id",
        "matter-id",
        "model-id",
        "version-id",
        "ACTIVATION",
        4,
        "a" * 64,
        "Approved for controlled use.",
        "actor-id",
    )

    assert result == "decision-id"
    apply_promotion.assert_called_once_with(
        "workflow-id",
        "matter-id",
        "model-id",
        "version-id",
        "ACTIVATION",
        4,
        "a" * 64,
        "Approved for controlled use.",
        "actor-id",
    )
