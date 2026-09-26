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
