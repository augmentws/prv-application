import uuid

import pytest
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import get_settings
from app.fact_document_state import (
    CONFIRMED,
    UNCONFIRMED,
    FactStateTracker,
    cited_document_ids,
    document_fact_states,
)
from app.facts import Actor, get_fact, suggest_merge, suggest_revision
from app.models import (
    Matter,
    MatterFact,
    MetadataDefinition,
    MetadataGroupField,
    SearchProjectionOperation,
)
from app.search.service import SearchIndexManager
from tests.test_agents_and_matter_definitions import auth
from tests.test_facts_and_entities import _create_fact, _matter_with_task, document_text  # noqa: F401


def _fact(client: TestClient, ctx: dict, document_index: int = 0, **overrides) -> dict:
    paragraph, quote = {0: ("¶1", "Jane Smith approved the 5% price increase"), 1: ("¶2", "The increase is final.")}[
        min(document_index, 1)
    ]
    response = _create_fact(
        client,
        ctx,
        evidence=[{"matter_document_id": ctx["documents"][document_index], "paragraph_id": paragraph, "quote": quote}],
        **overrides,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _action(client: TestClient, ctx: dict, fact: dict, action: str) -> dict:
    response = client.post(
        f"/v1/matters/{ctx['matter_id']}/facts/{fact['id']}/{action}",
        headers=auth(ctx["token"]),
        json={"lock_version": fact["lock_version"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _states(ctx: dict) -> dict[str, str | None]:
    with TestingSessionLocal() as db:
        states = document_fact_states(db, [uuid.UUID(value) for value in ctx["documents"]])
    return {str(key): value for key, value in states.items()}


def _fact_state_upserts(matter_id: str) -> list[SearchProjectionOperation]:
    with TestingSessionLocal() as db:
        return [
            operation
            for operation in db.scalars(
                select(SearchProjectionOperation)
                .where(
                    SearchProjectionOperation.matter_id == uuid.UUID(matter_id),
                    SearchProjectionOperation.kind == "DOCUMENT_UPSERT",
                )
                .order_by(SearchProjectionOperation.created_at)
            )
            if operation.payload.get("reason") == "FACT_STATE"
        ]


def test_fact_state_follows_live_facts_only(client: TestClient, root_token: str) -> None:
    ctx = _matter_with_task(client, root_token)
    first, second, third = ctx["documents"]
    assert _states(ctx) == {first: None, second: None, third: None}

    working = _fact(client, ctx, 0)
    assert _states(ctx)[first] == UNCONFIRMED
    confirmed = _action(client, ctx, _fact(client, ctx, 0, summary="Prices rose."), "confirm")
    assert _states(ctx)[first] == CONFIRMED  # One confirmed fact is enough.

    _action(client, ctx, confirmed, "reject")
    assert _states(ctx)[first] == UNCONFIRMED  # Rejected facts do not count; the working fact remains.
    _action(client, ctx, working, "reject")
    assert _states(ctx)[first] is None

    source = _fact(client, ctx, 1, summary="An increase was final.")
    target = _fact(client, ctx, 0, summary="Jane approved prices.")
    merged = client.post(
        f"/v1/matters/{ctx['matter_id']}/facts/{source['id']}/merge",
        headers=auth(ctx["token"]),
        json={
            "lock_version": source["lock_version"],
            "target_fact_id": target["id"],
            "target_lock_version": target["lock_version"],
        },
    )
    assert merged.status_code == 200, merged.text
    # The merged fact no longer counts, but its quote moved to the surviving working fact.
    assert _states(ctx)[second] == UNCONFIRMED
    with TestingSessionLocal() as db:
        assert set(cited_document_ids(db, uuid.UUID(ctx["matter_id"]))) == {uuid.UUID(first), uuid.UUID(second)}


def test_upserts_are_queued_only_when_a_document_state_changes(
    client: TestClient,
    root_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    priorities: list[int | None] = []
    monkeypatch.setattr(
        "app.search.operations.enqueue_search_projection",
        lambda db, workflow_id, operation_id, priority=None: priorities.append(priority),
    )
    ctx = _matter_with_task(client, root_token)
    first = ctx["documents"][0]

    fact = _fact(client, ctx, 0)
    upserts = _fact_state_upserts(ctx["matter_id"])
    assert [operation.payload["document_ids"] for operation in upserts] == [[first]]
    assert upserts[0].created_by_user_id is not None
    assert priorities[-1] is None  # Reviewer changes project ahead of agent work.

    _fact(client, ctx, 0, summary="Another working fact on the same document.")
    assert len(_fact_state_upserts(ctx["matter_id"])) == 1  # Still UNCONFIRMED: nothing to reindex.

    _action(client, ctx, fact, "confirm")
    upserts = _fact_state_upserts(ctx["matter_id"])
    assert len(upserts) == 2 and upserts[-1].payload["document_ids"] == [first]

    with TestingSessionLocal() as db:
        run_free_actor = FactStateTracker(
            db,
            matter_id=uuid.UUID(ctx["matter_id"]),
            actor_user_id=None,
            workflow_run_id=uuid.uuid4(),
            document_ids=[uuid.UUID(ctx["documents"][2])],
        )
        # No change for an uncited document: nothing is queued, whoever the actor is.
        assert run_free_actor.publish() is None


def test_agent_changes_use_agent_priority(client: TestClient, root_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    priorities: list[int | None] = []
    monkeypatch.setattr(
        "app.search.operations.enqueue_search_projection",
        lambda db, workflow_id, operation_id, priority=None: priorities.append(priority),
    )
    ctx = _matter_with_task(client, root_token)
    with TestingSessionLocal() as db:
        tracker = FactStateTracker(
            db,
            matter_id=uuid.UUID(ctx["matter_id"]),
            actor_user_id=uuid.UUID(client.get("/v1/auth/me", headers=auth(ctx["token"])).json()["id"]),
            workflow_run_id=uuid.uuid4(),
            document_ids=[uuid.UUID(ctx["documents"][0])],
        )
    _fact(client, ctx, 0)  # Makes the document UNCONFIRMED outside the tracker.
    with TestingSessionLocal() as db:
        tracker.db = db
        operation = tracker.publish()
        db.commit()
    assert operation is not None
    assert priorities[-1] == 100


def test_first_fact_installs_the_facts_field_on_an_older_matter(client: TestClient, root_token: str) -> None:
    ctx = _matter_with_task(client, root_token)
    matter_id = uuid.UUID(ctx["matter_id"])
    with TestingSessionLocal() as db:
        definition = db.scalar(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter_id,
                MetadataDefinition.key == "fact_state",
            )
        )
        assert definition.value_source == "SYSTEM" and definition.type == "ENUM"
        db.execute(delete(MetadataGroupField).where(MetadataGroupField.metadata_definition_id == definition.id))
        db.delete(definition)
        db.commit()

    _fact(client, ctx, 0)

    with TestingSessionLocal() as db:
        installed = db.scalar(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter_id,
                MetadataDefinition.key == "fact_state",
            )
        )
        assert installed is not None and installed.facetable
        sync = db.scalar(
            select(SearchProjectionOperation).where(
                SearchProjectionOperation.matter_id == matter_id,
                SearchProjectionOperation.kind == "SCHEMA_SYNC",
                SearchProjectionOperation.payload["reason"].as_string() == "FACT_STATE_UPDATE",
            )
        )
        assert sync is not None
        assert sync.payload["metadata_backfill_fields"] == ["fact_state"]


class RecordingClient:
    def __init__(self) -> None:
        self.operations: list[tuple] = []
        self.refreshed: list[str] = []

    def bulk(self, index: str, operations) -> None:
        self.operations.extend(operations)

    def refresh(self, index: str) -> None:
        self.refreshed.append(index)


def test_backfill_projects_only_cited_documents(client: TestClient, root_token: str) -> None:
    ctx = _matter_with_task(client, root_token)
    _action(client, ctx, _fact(client, ctx, 0), "confirm")
    _fact(client, ctx, 1)
    recording = RecordingClient()
    with TestingSessionLocal() as db:
        matter = db.get(Matter, uuid.UUID(ctx["matter_id"]))
        definitions = list(db.scalars(select(MetadataDefinition).where(MetadataDefinition.matter_id == matter.id)))
        operation = SearchProjectionOperation(
            matter_id=matter.id,
            kind="SCHEMA_SYNC",
            payload={"metadata_backfill_fields": ["fact_state"]},
            status="RUNNING",
            workflow_id=f"test:{uuid.uuid4()}",
        )
        db.add(operation)
        db.commit()
        manager = SearchIndexManager(db, recording, get_settings())  # type: ignore[arg-type]
        updated = manager.backfill_requested_metadata_fields(
            "matter-index",
            matter,
            definitions,
            ("fact_state",),
            operation=operation,
        )

    assert updated == 2
    assert sorted((document_id, body["doc"]["metadata"]["fact_state"]) for _, document_id, body in recording.operations) == sorted(
        [(ctx["documents"][0], CONFIRMED), (ctx["documents"][1], UNCONFIRMED)]
    )
    assert recording.refreshed == ["matter-index"]


def test_document_facts_lists_live_facts_with_only_that_documents_quotes(client: TestClient, root_token: str) -> None:
    ctx = _matter_with_task(client, root_token)
    first, second, _ = ctx["documents"]
    later = _fact(client, ctx, 0, summary="Later approval.", event_date_start="2021-06-01")
    earlier = _fact(client, ctx, 0, summary="Earlier approval.", event_date_start="2021-01-15")
    _action(client, ctx, _fact(client, ctx, 0, summary="A rejected fact."), "reject")
    added = client.post(
        f"/v1/matters/{ctx['matter_id']}/facts/{earlier['id']}/evidence",
        headers=auth(ctx["token"]),
        json={
            "lock_version": earlier["lock_version"],
            "evidence": {"matter_document_id": second, "paragraph_id": "¶2", "quote": "The increase is final."},
        },
    )
    assert added.status_code == 201, added.text

    url = f"/v1/matters/{ctx['matter_id']}/documents/{first}/facts"
    response = client.get(url, headers=auth(ctx["token"]))
    assert response.status_code == 200, response.text
    items = response.json()
    assert [item["fact"]["summary"] for item in items] == ["Earlier approval.", "Later approval."]
    assert all(item["fact"]["status"] == "WORKING" for item in items)
    # Only quotes from this document are returned, even though the earlier fact also cites another document.
    assert {quote["matter_document_id"] for item in items for quote in item["quotes"]} == {first}
    assert items[0]["fact"]["evidence_count"] == 2
    assert later["id"] == items[1]["fact"]["id"]

    missing = client.get(f"/v1/matters/{ctx['matter_id']}/documents/{uuid.uuid4()}/facts", headers=auth(ctx["token"]))
    assert missing.status_code == 404
    outsider = client.post(
        f"/v1/tenants/{ctx['tenant_id']}/users",
        headers=auth(ctx["token"]),
        json={"email": "fact-outsider@example.com", "display_name": "Outsider", "password": "long-enough-test-password"},
    )
    assert outsider.status_code == 201, outsider.text
    token = client.post(
        "/v1/auth/login",
        json={"email": "fact-outsider@example.com", "password": "long-enough-test-password"},
    ).json()["access_token"]
    assert client.get(url, headers=auth(token)).status_code == 403


def test_pending_suggestion_filter_and_counts(client: TestClient, root_token: str) -> None:
    ctx = _matter_with_task(client, root_token)
    confirmed = _action(client, ctx, _fact(client, ctx, 0), "confirm")
    plain = _fact(client, ctx, 1, summary="Unrelated working fact.")
    working = _fact(client, ctx, 0, summary="A near duplicate.")
    with TestingSessionLocal() as db:
        matter_id = uuid.UUID(ctx["matter_id"])
        agent = Actor("AGENT", workflow_run_id=None)
        fact = get_fact(db, matter_id=matter_id, fact_id=uuid.UUID(confirmed["id"]))
        suggest_revision(db, fact=fact, changes={"summary": "Corrected summary."}, evidence=[], rationale=None, actor=agent)
        suggest_merge(
            db,
            source=get_fact(db, matter_id=matter_id, fact_id=uuid.UUID(working["id"])),
            target=fact,
            rationale=None,
            actor=agent,
        )
        db.commit()

    def search(**filters) -> dict[str, int]:
        response = client.post(
            f"/v1/matters/{ctx['matter_id']}/facts/search",
            headers=auth(ctx["token"]),
            json={"filters": filters, "limit": 50},
        )
        assert response.status_code == 200, response.text
        return {item["id"]: item["pending_suggestion_count"] for item in response.json()["items"]}

    assert search(has_pending_suggestion=True) == {confirmed["id"]: 2, working["id"]: 1}
    assert search(has_pending_suggestion=False) == {plain["id"]: 0}
    with TestingSessionLocal() as db:
        assert db.query(MatterFact).count() == 3
