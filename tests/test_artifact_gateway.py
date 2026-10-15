import uuid
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app import artifact_gateway


def test_embedded_grouping_snapshots_use_one_array_join(monkeypatch) -> None:
    item_ids = [uuid.uuid4() for _ in range(5)]
    statements: list[str] = []
    parameter_sets: list[dict] = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, statement, parameters):
            statements.append(str(statement.compile(dialect=postgresql.dialect())))
            parameter_sets.append(parameters)
            return [
                (item_id, None, "FILE", f"hash-{item_id}")
                for item_id in parameters["grouping_item_ids"]
            ]

    monkeypatch.setattr(
        artifact_gateway,
        "get_settings",
        lambda: SimpleNamespace(artifact_mode="embedded"),
    )
    monkeypatch.setattr(artifact_gateway, "ArtifactSessionLocal", FakeSession)

    snapshots = artifact_gateway.get_collection_item_grouping_snapshots(
        item_ids=[*item_ids, item_ids[0]],
        actor_user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        client_id=uuid.uuid4(),
    )

    assert len(statements) == 1
    assert "JOIN collection_item" in statements[0]
    assert "unnest(" in statements[0]
    assert " IN " not in statements[0]
    assert parameter_sets == [{"grouping_item_ids": item_ids}]
    assert set(snapshots) == set(item_ids)
