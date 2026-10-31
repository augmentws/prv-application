from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.workflows import dispatcher


def test_new_embedding_jobs_use_versioned_provider_aware_workflow(monkeypatch) -> None:
    settings = SimpleNamespace(
        dbos_enabled=True,
        dbos_application_name="test-application",
    )
    dbos_client = Mock()
    db = Mock()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "get_dbos_client", lambda: dbos_client)

    dispatcher.enqueue_matter_embedding(db, "matter-embedding:job-id", "job-id")

    options = dbos_client.enqueue_in_transaction.call_args.args[1]
    assert options["workflow_name"] == "matter_embedding_job_v2"
    assert options["queue_name"] == "matter-embedding-plans"


def test_classification_builds_use_transactional_versioned_workflow(monkeypatch) -> None:
    settings = SimpleNamespace(
        dbos_enabled=True,
        dbos_application_name="test-application",
    )
    dbos_client = Mock()
    db = Mock()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "get_dbos_client", lambda: dbos_client)

    dispatcher.enqueue_classification_model_build(
        db,
        "classification-model-build:run-id:attempt:2",
        "run-id",
    )

    options = dbos_client.enqueue_in_transaction.call_args.args[1]
    assert options["workflow_name"] == "classification_model_build_v1"
    assert options["queue_name"] == "classification-model-builds"
    assert options["workflow_id"] == "classification-model-build:run-id:attempt:2"
    dbos_client.enqueue_in_transaction.assert_called_once_with(db, options, "run-id")


def test_classification_training_jobs_own_preparation_before_builds(monkeypatch) -> None:
    settings = SimpleNamespace(
        dbos_enabled=True,
        dbos_application_name="test-application",
    )
    dbos_client = Mock()
    db = Mock()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "get_dbos_client", lambda: dbos_client)

    dispatcher.enqueue_classification_training_job(
        db,
        "classification-training-job:job-id",
        "job-id",
    )

    options = dbos_client.enqueue_in_transaction.call_args.args[1]
    assert options["workflow_name"] == "classification_training_job_v1"
    assert options["queue_name"] == "classification-training-jobs"
    assert options["workflow_id"] == "classification-training-job:job-id"
    dbos_client.enqueue_in_transaction.assert_called_once_with(db, options, "job-id")


def test_classification_scoring_uses_transactional_versioned_workflow(monkeypatch) -> None:
    settings = SimpleNamespace(
        dbos_enabled=True,
        dbos_application_name="test-application",
    )
    dbos_client = Mock()
    db = Mock()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "get_dbos_client", lambda: dbos_client)

    dispatcher.enqueue_classification_model_scoring(
        db,
        "classification-model-score:run-id",
        "run-id",
    )

    options = dbos_client.enqueue_in_transaction.call_args.args[1]
    assert options["workflow_name"] == "classification_model_score_v1"
    assert options["queue_name"] == "classification-model-scoring"
    assert options["workflow_id"] == "classification-model-score:run-id"
    dbos_client.enqueue_in_transaction.assert_called_once_with(db, options, "run-id")


def test_review_program_scoring_preparation_uses_transactional_workflow(monkeypatch) -> None:
    settings = SimpleNamespace(
        dbos_enabled=True,
        dbos_application_name="test-application",
    )
    dbos_client = Mock()
    db = Mock()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "get_dbos_client", lambda: dbos_client)

    dispatcher.enqueue_review_program_scoring_preparation(
        db,
        "review-program-scoring-preparation:run-id:attempt-id",
        "run-id",
        "attempt-id",
    )

    options = dbos_client.enqueue_in_transaction.call_args.args[1]
    assert options["workflow_name"] == "review_program_scoring_preparation_v1"
    assert options["queue_name"] == "review-program-orchestration"
    assert options["workflow_id"] == "review-program-scoring-preparation:run-id:attempt-id"
    dbos_client.enqueue_in_transaction.assert_called_once_with(
        db,
        options,
        "run-id",
        "attempt-id",
    )


def test_classification_projection_uses_transactional_versioned_workflow(monkeypatch) -> None:
    settings = SimpleNamespace(
        dbos_enabled=True,
        dbos_application_name="test-application",
    )
    dbos_client = Mock()
    db = Mock()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "get_dbos_client", lambda: dbos_client)

    dispatcher.enqueue_classification_projection(
        db,
        "classification-prediction-project:run-id",
        "run-id",
    )

    options = dbos_client.enqueue_in_transaction.call_args.args[1]
    assert options["workflow_name"] == "classification_prediction_project_v1"
    assert options["queue_name"] == "classification-projections"
    assert options["workflow_id"] == "classification-prediction-project:run-id"
    dbos_client.enqueue_in_transaction.assert_called_once_with(db, options, "run-id")


def test_analysis_task_promotions_use_transactional_versioned_workflow(monkeypatch) -> None:
    settings = SimpleNamespace(
        dbos_enabled=True,
        dbos_application_name="test-application",
    )
    dbos_client = Mock()
    db = Mock()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "get_dbos_client", lambda: dbos_client)

    dispatcher.enqueue_analysis_task_promotion(
        db,
        "analysis-task-promotion:promotion-id:attempt-id",
        "promotion-id",
    )

    options = dbos_client.enqueue_in_transaction.call_args.args[1]
    assert options["workflow_name"] == "analysis_task_run_promotion_v1"
    assert options["queue_name"] == "analysis-task-promotions"
    assert options["workflow_id"] == "analysis-task-promotion:promotion-id:attempt-id"
    dbos_client.enqueue_in_transaction.assert_called_once_with(db, options, "promotion-id")


def test_classification_promotions_use_transactional_versioned_workflow(monkeypatch) -> None:
    settings = SimpleNamespace(
        dbos_enabled=True,
        dbos_application_name="test-application",
    )
    dbos_client = Mock()
    db = Mock()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "get_dbos_client", lambda: dbos_client)

    dispatcher.enqueue_classification_model_promotion(
        db,
        "classification-model-promote:operation-id",
        matter_id="matter-id",
        classification_model_id="model-id",
        model_version_id="version-id",
        action="ACTIVATION",
        expected_model_row_version=3,
        preview_token="a" * 64,
        reason="Approved for controlled use.",
        actor_user_id="user-id",
    )

    options = dbos_client.enqueue_in_transaction.call_args.args[1]
    assert options["workflow_name"] == "classification_model_promote_v1"
    assert options["queue_name"] == "classification-model-promotions"
    assert options["workflow_id"] == "classification-model-promote:operation-id"
    dbos_client.enqueue_in_transaction.assert_called_once_with(
        db,
        options,
        "classification-model-promote:operation-id",
        "matter-id",
        "model-id",
        "version-id",
        "ACTIVATION",
        3,
        "a" * 64,
        "Approved for controlled use.",
        "user-id",
    )


def test_topic_application_uses_parallelized_application_workflow(monkeypatch) -> None:
    settings = SimpleNamespace(dbos_enabled=True, dbos_application_name="test-application")
    dbos_client = Mock()
    db = Mock()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "get_dbos_client", lambda: dbos_client)

    dispatcher.enqueue_matter_topic_application(db, "job-id")

    options = dbos_client.enqueue_in_transaction.call_args.args[1]
    assert options["workflow_name"] == "matter_topic_application"
    assert options["queue_name"] == "matter-topic-plans"


@pytest.mark.parametrize(("priority", "expected_priority"), [(None, None), (100, 100)])
def test_search_projection_only_sends_explicit_priority(
    monkeypatch: pytest.MonkeyPatch,
    priority: int | None,
    expected_priority: int | None,
) -> None:
    settings = SimpleNamespace(
        dbos_enabled=True,
        search_enabled=True,
        dbos_application_name="test-application",
    )
    dbos_client = Mock()
    db = Mock()
    monkeypatch.setattr(dispatcher, "get_settings", lambda: settings)
    monkeypatch.setattr(dispatcher, "get_dbos_client", lambda: dbos_client)

    dispatcher.enqueue_search_projection(
        db,
        "search-projection:operation-id",
        "operation-id",
        priority=priority,
    )

    options = dbos_client.enqueue_in_transaction.call_args.args[1]
    if expected_priority is None:
        assert "priority" not in options
    else:
        assert options["priority"] == expected_priority
