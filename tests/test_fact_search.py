import csv
import hashlib
import io
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient

from app.document_metadata import add_metadata_event
from app.fact_recall import recall_facts
from app.models import Custodian, Matter, MatterAnalysisTask, MatterDocumentCustodian, MatterFact
from tests.test_agents_and_matter_definitions import auth
from tests.test_fact_exports import stored  # noqa: F401
from tests.test_facts_and_entities import _create_fact, _entity, _matter_with_task, document_text  # noqa: F401


class FakeEmbeddings:
    """Deterministic bag-of-words vectors so similar sentences score higher."""

    def __init__(self) -> None:
        self.settings = SimpleNamespace(model="fake-embedding-model")
        self.embedded: list[str] = []

    @staticmethod
    def vector(text: str) -> list[float]:
        values = [0.0] * 64
        for word in text.lower().replace(".", " ").replace(",", " ").split():
            values[int(hashlib.sha256(word.encode()).hexdigest(), 16) % 64] += 1.0
        return values

    def embed(self, inputs: list[str], input_type: str = "document"):
        assert input_type == "document"
        self.embedded.extend(inputs)
        return SimpleNamespace(embeddings=[self.vector(text) for text in inputs])


def _confirm(client: TestClient, ctx: dict, fact: dict) -> dict:
    response = client.post(
        f"/v1/matters/{ctx['matter_id']}/facts/{fact['id']}/confirm",
        headers=auth(ctx["token"]),
        json={"lock_version": fact["lock_version"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def corpus(client: TestClient, root_token: str) -> dict:
    ctx = _matter_with_task(client, root_token)
    jane = _entity(client, ctx, entity_type="PERSON", display_name="Jane Smith", email="jsmith@acme.com")
    jane = client.post(
        f"/v1/matters/{ctx['matter_id']}/entities/{jane['id']}/variants",
        headers=auth(ctx["token"]),
        json={"lock_version": jane["lock_version"], "email": "jane@gmail.com"},
    ).json()
    acme = _entity(client, ctx, entity_type="ORGANIZATION", display_name="Acme Corp")
    bob = _entity(client, ctx, entity_type="PERSON", display_name="Bob Lee")
    pricing = [{"matter_analysis_task_id": ctx["task_id"], "issue_key": "pricing"}]
    safety = [{"matter_analysis_task_id": ctx["task_id"], "issue_key": "safety"}]

    approval = _create_fact(
        client,
        ctx,
        owner_entity_id=jane["variants"][0]["id"],
        participants=[{"entity_id": bob["id"], "role": "PARTICIPANT_RECIPIENT"}],
        key_fact=True,
    ).json()
    approval = _confirm(client, ctx, approval)
    incident = _create_fact(
        client,
        ctx,
        summary="Bob Lee reported a safety incident.",
        date_precision="MONTH",
        event_date_start="2020-06-15",
        owner_entity_id=acme["id"],
        issues=safety,
        evidence=[
            {
                "matter_document_id": ctx["documents"][1],
                "paragraph_id": "¶3",
                "quote": "Bob Lee reported a safety incident",
                "stance": "CONTRADICTS",
            }
        ],
    ).json()
    final = _create_fact(
        client,
        ctx,
        summary="The increase was described as final.",
        date_precision="UNKNOWN",
        event_date_start=None,
        date_basis=None,
        issues=[*pricing, *safety],
        evidence=[{"matter_document_id": ctx["documents"][2], "paragraph_id": "¶2", "quote": "The increase is final."}],
    ).json()
    rejected = _create_fact(client, ctx, summary="Prices were cut in 2019.", event_date_start="2019-01-05").json()
    rejected = client.post(
        f"/v1/matters/{ctx['matter_id']}/facts/{rejected['id']}/reject",
        headers=auth(ctx["token"]),
        json={"lock_version": rejected["lock_version"]},
    ).json()

    sender = client.post(
        f"/v1/matters/{ctx['matter_id']}/metadata-definitions",
        headers=auth(ctx["token"]),
        json={"key": "sender_domain", "display_name": "Sender domain", "type": "TEXT"},
    ).json()
    with TestingSessionLocal() as db:
        matter = db.get(Matter, uuid.UUID(ctx["matter_id"]))
        assert matter is not None
        custodian = Custodian(client_id=matter.client_id, display_name="Jane Smith", normalized_name="jane smith")
        db.add(custodian)
        db.flush()
        db.add(
            MatterDocumentCustodian(
                matter_document_id=uuid.UUID(ctx["documents"][0]), custodian_id=custodian.id, relationship_type="PRIMARY"
            )
        )
        db.commit()
        custodian_id = str(custodian.id)
    with TestingSessionLocal() as db:
        add_metadata_event(
            db,
            matter_id=uuid.UUID(ctx["matter_id"]),
            document_id=uuid.UUID(ctx["documents"][1]),
            definition_id=uuid.UUID(sender["id"]),
            operation="SET",
            value="plant.example.com",
            source_type="HUMAN",
            actor_id=uuid.UUID(client.get("/v1/auth/me", headers=auth(ctx["token"])).json()["id"]),
            source_id="test",
        )
        db.commit()
    return {
        **ctx,
        "jane": jane,
        "acme": acme,
        "bob": bob,
        "custodian_id": custodian_id,
        "facts": {"approval": approval, "incident": incident, "final": final, "rejected": rejected},
    }


def _search(client: TestClient, ctx: dict, **filters) -> list[str]:
    sort = filters.pop("sort", "DATE_ASC")
    response = client.post(
        f"/v1/matters/{ctx['matter_id']}/facts/search",
        headers=auth(ctx["token"]),
        json={"filters": filters, "sort": sort},
    )
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def _facet(client: TestClient, ctx: dict, facet: str, **filters):
    return client.post(
        f"/v1/matters/{ctx['matter_id']}/facts/facets",
        headers=auth(ctx["token"]),
        json={"filters": filters, "facet": facet},
    )


def test_search_filters_every_field_group(client: TestClient, corpus: dict) -> None:
    facts = {name: fact["id"] for name, fact in corpus["facts"].items()}
    assert _search(client, corpus) == [facts["rejected"], facts["incident"], facts["approval"], facts["final"]]
    assert _search(client, corpus, sort="DATE_DESC")[:3] == [facts["approval"], facts["incident"], facts["rejected"]]
    assert _search(client, corpus, query="FINAL") == [facts["final"]]
    assert _search(client, corpus, query="approved the 5%") == [facts["rejected"], facts["approval"]]
    assert _search(client, corpus, query="100%_") == []
    assert _search(client, corpus, statuses=["CONFIRMED"]) == [facts["approval"]]
    assert _search(client, corpus, issue_keys=["safety"]) == [facts["incident"], facts["final"]]
    assert _search(client, corpus, owner_entity_ids=[corpus["jane"]["id"]]) == [facts["approval"]]
    assert _search(client, corpus, participant_entity_ids=[corpus["bob"]["id"]]) == [facts["approval"]]
    assert _search(client, corpus, participant_role="PARTICIPANT_ACTOR") == []
    assert _search(client, corpus, entity_types=["ORGANIZATION"]) == [facts["incident"]]
    assert _search(client, corpus, event_date_from="2020-01-01", event_date_to="2020-12-31") == [facts["incident"]]
    assert _search(client, corpus, date_precisions=["MONTH"]) == [facts["incident"]]
    assert _search(client, corpus, undated=True) == [facts["final"]]
    assert _search(client, corpus, key_fact=True) == [facts["approval"]]
    assert _search(client, corpus, has_contradiction=True) == [facts["incident"]]
    assert _search(client, corpus, stances=["CONTRADICTS"]) == [facts["incident"]]
    assert _search(client, corpus, author_types=["AGENT"]) == []
    assert len(_search(client, corpus, author_types=["HUMAN"])) == 4
    assert _search(client, corpus, evidence_count_min=2) == []
    assert _search(client, corpus, evidence_custodian_ids=[corpus["custodian_id"]]) == [
        facts["rejected"],
        facts["approval"],
    ]
    assert _search(
        client, corpus, evidence_metadata=[{"field_key": "sender_domain", "values": ["plant.example.com"]}]
    ) == [facts["incident"]]
    assert _search(client, corpus, matter_document_ids=[corpus["documents"][2]]) == [facts["final"]]

    unknown_field = client.post(
        f"/v1/matters/{corpus['matter_id']}/facts/search",
        headers=auth(corpus["token"]),
        json={"filters": {"evidence_metadata": [{"field_key": "no_such_field", "values": ["x"]}]}},
    )
    assert unknown_field.status_code == 404
    wrong_shape = client.post(
        f"/v1/matters/{corpus['matter_id']}/facts/search",
        headers=auth(corpus["token"]),
        json={"filters": {"evidence_metadata": [{"field_key": "sender_domain", "values": [True]}]}},
    )
    assert wrong_shape.status_code == 422


def test_facets_count_facts_and_exclude_their_own_selection(client: TestClient, corpus: dict) -> None:
    def buckets(response) -> dict:
        assert response.status_code == 200, response.text
        return {str(bucket["label"]): bucket["count"] for bucket in response.json()["buckets"]}

    assert buckets(_facet(client, corpus, "status", statuses=["CONFIRMED"])) == {
        "WORKING": 2,
        "CONFIRMED": 1,
        "REJECTED": 1,
    }
    assert buckets(_facet(client, corpus, "issue_key", statuses=["WORKING"])) == {
        "Pricing decisions": 1,
        "Safety reports": 2,
    }
    assert buckets(_facet(client, corpus, "owner")) == {"Jane Smith": 1, "Acme Corp": 1}
    assert buckets(_facet(client, corpus, "participant")) == {"Bob Lee": 1}
    assert buckets(_facet(client, corpus, "entity_type")) == {"PERSON": 1, "ORGANIZATION": 1}
    assert buckets(_facet(client, corpus, "stance")) == {"SUPPORTS": 3, "CONTRADICTS": 1}
    assert buckets(_facet(client, corpus, "key_fact")) == {"False": 3, "True": 1}
    assert buckets(_facet(client, corpus, "evidence_custodian")) == {"Jane Smith": 2}
    assert buckets(_facet(client, corpus, "evidence_metadata:sender_domain")) == {"plant.example.com": 1}
    assert buckets(_facet(client, corpus, "author_type")) == {"HUMAN": 4}
    assert buckets(_facet(client, corpus, "date_precision", issue_keys=["safety"])) == {"MONTH": 1, "UNKNOWN": 1}

    assert _facet(client, corpus, "not_a_facet").status_code == 422
    dated = client.post(
        f"/v1/matters/{corpus['matter_id']}/metadata-definitions",
        headers=auth(corpus["token"]),
        json={"key": "received_on", "display_name": "Received", "type": "DATE"},
    )
    assert dated.status_code == 201
    assert _facet(client, corpus, "evidence_metadata:received_on").status_code == 422


def test_date_histogram_buckets_by_period_with_undated_count(client: TestClient, corpus: dict) -> None:
    url = f"/v1/matters/{corpus['matter_id']}/facts/date-histogram"
    monthly = client.post(url, headers=auth(corpus["token"]), json={"interval": "MONTH"})
    assert monthly.status_code == 200, monthly.text
    assert monthly.json() == {
        "interval": "MONTH",
        "buckets": [
            {"start": "2019-01-01", "count": 1},
            {"start": "2020-06-01", "count": 1},
            {"start": "2021-03-01", "count": 1},
        ],
        "undated_count": 1,
    }
    yearly = client.post(
        url,
        headers=auth(corpus["token"]),
        json={"interval": "YEAR", "filters": {"statuses": ["WORKING", "CONFIRMED"]}},
    ).json()
    assert yearly["buckets"] == [{"start": "2020-01-01", "count": 1}, {"start": "2021-01-01", "count": 1}]
    assert yearly["undated_count"] == 1


def test_export_uses_the_full_search_filter(client: TestClient, corpus: dict, stored: dict) -> None:  # noqa: F811
    job = client.post(
        f"/v1/matters/{corpus['matter_id']}/fact-exports",
        headers=auth(corpus["token"]),
        json={
            "idempotency_key": "filtered",
            "filters": {"issue_keys": ["safety"], "entity_types": ["ORGANIZATION"]},
        },
    )
    assert job.status_code == 201, job.text
    rows = list(csv.DictReader(io.StringIO(stored["FACTS"].decode())))
    assert [row["Fact ID"] for row in rows] == [corpus["facts"]["incident"]["id"]]


def test_recall_ranks_similar_facts_and_caches_embeddings(client: TestClient, corpus: dict) -> None:
    gateway = FakeEmbeddings()
    matter_id = uuid.UUID(corpus["matter_id"])
    with TestingSessionLocal() as db:
        recalled = recall_facts(
            db,
            matter_id=matter_id,
            text="Jane Smith approved a price increase of 5%.",
            issue_keys=["pricing"],
            participant_entity_ids=[uuid.UUID(corpus["jane"]["id"])],
            gateway=gateway,
        )
        db.commit()
    ids = [item.fact.id for item in recalled]
    assert ids[0] == uuid.UUID(corpus["facts"]["approval"]["id"])
    assert recalled[0].shared_participants == 1
    assert uuid.UUID(corpus["facts"]["incident"]["id"]) not in ids
    rejected = next(item for item in recalled if item.fact.id == uuid.UUID(corpus["facts"]["rejected"]["id"]))
    assert rejected.rejected is True
    first_calls = len(gateway.embedded)

    with TestingSessionLocal() as db:
        recall_facts(db, matter_id=matter_id, text="price increase", issue_keys=["pricing"], gateway=gateway)
        db.commit()
    assert len(gateway.embedded) == first_calls + 1  # only the query text; summaries were cached

    approval = client.get(
        f"/v1/matters/{corpus['matter_id']}/facts/{corpus['facts']['approval']['id']}", headers=auth(corpus["token"])
    ).json()
    edited = client.patch(
        f"/v1/matters/{corpus['matter_id']}/facts/{approval['id']}",
        headers=auth(corpus["token"]),
        json={"lock_version": approval["lock_version"], "summary": "Jane Smith signed off on new pricing."},
    )
    assert edited.status_code == 200, edited.text
    with TestingSessionLocal() as db:
        recall_facts(db, matter_id=matter_id, text="pricing", issue_keys=["pricing"], gateway=gateway)
        db.commit()
        cached = db.get(MatterFact, uuid.UUID(approval["id"]))
        assert cached is not None
        assert cached.summary_embedding == FakeEmbeddings.vector("Jane Smith signed off on new pricing.")
    assert "Jane Smith signed off on new pricing." in gateway.embedded

    with TestingSessionLocal() as db:
        windowed = recall_facts(
            db,
            matter_id=matter_id,
            text="price increase",
            issue_keys=["pricing"],
            event_date=date(2021, 3, 1),
            window_days=30,
            include_rejected=False,
            gateway=gateway,
        )
    assert {item.fact.id for item in windowed} == {
        uuid.UUID(corpus["facts"]["approval"]["id"]),
        uuid.UUID(corpus["facts"]["final"]["id"]),
    }


def test_issue_catalog_is_readable_with_fact_view(client: TestClient, corpus: dict) -> None:
    ctx = corpus
    response = client.get(f"/v1/matters/{ctx['matter_id']}/facts/issue-catalog", headers=auth(ctx["token"]))
    assert response.status_code == 200, response.text
    entries = response.json()
    assert [(entry["issue_key"], entry["name"]) for entry in entries] == [
        ("pricing", "Pricing decisions"),
        ("safety", "Safety reports"),
    ]
    assert {entry["matter_analysis_task_id"] for entry in entries} == {ctx["task_id"]}


def _tenant_user(client: TestClient, ctx: dict, email: str, role_key: str | None) -> str:
    created = client.post(
        f"/v1/tenants/{ctx['tenant_id']}/users",
        headers=auth(ctx["token"]),
        json={"email": email, "display_name": email.split("@")[0], "password": "long-enough-test-password"},
    )
    assert created.status_code == 201, created.text
    if role_key is not None:
        roles = {
            role["key"]: role
            for role in client.get(f"/v1/tenants/{ctx['tenant_id']}/roles", headers=auth(ctx["token"])).json()
        }
        granted = client.post(
            f"/v1/tenants/{ctx['tenant_id']}/authorization-grants",
            headers=auth(ctx["token"]),
            json={
                "subject": {"type": "USER", "id": created.json()["id"]},
                "role_id": roles[role_key]["id"],
                "scope": {"type": "MATTER", "id": ctx["matter_id"]},
            },
        )
        assert granted.status_code == 201, granted.text
    login = client.post("/v1/auth/login", json={"email": email, "password": "long-enough-test-password"})
    return login.json()["access_token"]


def test_issue_catalog_needs_fact_view_and_lists_only_published_catalogs(client: TestClient, corpus: dict) -> None:
    ctx = corpus
    url = f"/v1/matters/{ctx['matter_id']}/facts/issue-catalog"
    # Reviewers can read facts without Analysis Task access; the catalog only needs fact.view.
    reviewer = _tenant_user(client, ctx, "catalog-reviewer@example.com", "REVIEWER")
    assert client.get(url, headers=auth(reviewer)).status_code == 200
    outsider = _tenant_user(client, ctx, "catalog-outsider@example.com", None)
    assert client.get(url, headers=auth(outsider)).status_code == 403

    with TestingSessionLocal() as db:
        task = db.get(MatterAnalysisTask, uuid.UUID(ctx["task_id"]))
        task.published_version = None
        db.commit()
    assert client.get(url, headers=auth(ctx["token"])).json() == []
