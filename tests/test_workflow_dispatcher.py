from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.workflows import dispatcher


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
