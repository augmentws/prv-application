import uuid
from types import SimpleNamespace

import pytest
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import MatterEntityEvent, MatterFactEvent, MatterFactRevision
from app.rbac_manifest import SYSTEM_ROLE_BY_KEY
from tests.test_agents_and_matter_definitions import auth, create_tenant_context
from tests.test_fact_development_tasks import (
    _create_fact_task,
    _create_issue_field,
    _create_published_guidance,
    _put_specification,
    _specification,
)
from tests.test_review_batches import add_documents

DOCUMENT_TEXT = (
    "Jane Smith approved the 5% price increase on March 3, 2021.\n\n"
    "The increase applies to all Acme customers. The increase is final.\n\n"
    "Bob Lee reported a safety incident at the plant."
)


@pytest.fixture(autouse=True)
def document_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.fact_evidence.get_preferred_text_source",
        lambda **_: SimpleNamespace(
            artifact_id=uuid.UUID("00000000-0000-0000-0000-00000000a001"),
            content_hash="a" * 64,
            text=DOCUMENT_TEXT,
        ),
    )


def _matter_with_task(client: TestClient, root_token: str) -> dict:
    tenant_id, token, matter_id = create_tenant_context(client, root_token)
    user_id = client.get("/v1/auth/me", headers=auth(token)).json()["id"]
    _create_issue_field(client, token, matter_id)
    guidance = _create_published_guidance(client, token, matter_id)
    task = _create_fact_task(client, token, matter_id, guidance["id"]).json()
    assert _put_specification(client, token, matter_id, task["id"], _specification()).status_code == 200
    assert client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/publish",
        headers=auth(token),
    ).status_code == 200
    documents = add_documents(matter_id, uuid.UUID(user_id), count=3)
    return {
        "tenant_id": tenant_id,
        "token": token,
        "matter_id": matter_id,
        "task_id": task["id"],
        "documents": documents,
    }


def _entity(client: TestClient, ctx: dict, **payload) -> dict:
    response = client.post(f"/v1/matters/{ctx['matter_id']}/entities", headers=auth(ctx["token"]), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _fact_payload(ctx: dict, **overrides) -> dict:
    payload = {
        "summary": "Jane Smith approved a 5% price increase.",
        "date_precision": "DAY",
        "event_date_start": "2021-03-03",
        "date_basis": "STATED_IN_TEXT",
        "issues": [{"matter_analysis_task_id": ctx["task_id"], "issue_key": "pricing"}],
        "evidence": [
            {
                "matter_document_id": ctx["documents"][0],
                "paragraph_id": "¶1",
                "quote": "Jane Smith approved the 5% price increase",
            }
        ],
    }
    payload.update(overrides)
    return payload


def _create_fact(client: TestClient, ctx: dict, token: str | None = None, **overrides):
    return client.post(
        f"/v1/matters/{ctx['matter_id']}/facts",
        headers=auth(token or ctx["token"]),
        json=_fact_payload(ctx, **overrides),
    )


def test_entities_have_variants_unique_emails_and_reversible_merges(client: TestClient, root_token: str) -> None:
    ctx = _matter_with_task(client, root_token)
    matter_url = f"/v1/matters/{ctx['matter_id']}/entities"
    acme = _entity(client, ctx, entity_type="ORGANIZATION", display_name="Acme Corp")
    jane = _entity(
        client,
        ctx,
        entity_type="PERSON",
        display_name="Jane  Smith",
        email="JSmith@Acme.com",
        title="General Counsel",
        organization_id=acme["id"],
    )
    assert jane["display_name"] == "Jane Smith"
    assert jane["status"] == "CONFIRMED"

    bad_org = client.post(
        matter_url,
        headers=auth(ctx["token"]),
        json={"entity_type": "PERSON", "display_name": "Bob", "organization_id": jane["id"]},
    )
    assert bad_org.status_code == 422

    with_variant = client.post(
        f"{matter_url}/{jane['id']}/variants",
        headers=auth(ctx["token"]),
        json={"lock_version": jane["lock_version"], "display_name": "J. Smith", "email": "jane@gmail.com"},
    )
    assert with_variant.status_code == 201, with_variant.text
    jane = with_variant.json()
    assert [variant["email"] for variant in jane["variants"]] == ["jane@gmail.com"]

    duplicate_email = client.post(
        matter_url,
        headers=auth(ctx["token"]),
        json={"entity_type": "PERSON", "display_name": "Other", "email": "jsmith@acme.com"},
    )
    assert duplicate_email.status_code == 409

    found = client.get(matter_url, headers=auth(ctx["token"]), params={"q": "gmail"}).json()
    assert [item["id"] for item in found["items"]] == [jane["id"]]
    variant_id = jane["variants"][0]["id"]
    assert client.get(f"{matter_url}/{variant_id}", headers=auth(ctx["token"])).json()["id"] == jane["id"]

    duplicate_jane = _entity(client, ctx, entity_type="PERSON", display_name="Jane S.", status="PROPOSED")
    stale = client.post(
        f"{matter_url}/{duplicate_jane['id']}/merge",
        headers=auth(ctx["token"]),
        json={"lock_version": 99, "target_entity_id": jane["id"]},
    )
    assert stale.status_code == 409
    merged = client.post(
        f"{matter_url}/{duplicate_jane['id']}/merge",
        headers=auth(ctx["token"]),
        json={"lock_version": duplicate_jane["lock_version"], "target_entity_id": jane["id"]},
    )
    assert merged.status_code == 200, merged.text
    assert {variant["id"] for variant in merged.json()["variants"]} == {variant_id, duplicate_jane["id"]}
    assert client.get(matter_url, headers=auth(ctx["token"]), params={"entity_type": "PERSON"}).json()["total"] == 1

    reloaded = client.get(f"{matter_url}/{jane['id']}", headers=auth(ctx["token"])).json()
    split_lock = next(item for item in reloaded["variants"] if item["id"] == duplicate_jane["id"])["lock_version"]
    split = client.post(
        f"{matter_url}/{duplicate_jane['id']}/unmerge",
        headers=auth(ctx["token"]),
        json={"lock_version": split_lock},
    )
    assert split.status_code == 200, split.text
    assert split.json()["id"] == duplicate_jane["id"]
    assert split.json()["status"] == "CONFIRMED"

    person_into_org = client.post(
        f"{matter_url}/{duplicate_jane['id']}/merge",
        headers=auth(ctx["token"]),
        json={"lock_version": split.json()["lock_version"], "target_entity_id": acme["id"]},
    )
    assert person_into_org.status_code == 422

    with TestingSessionLocal() as db:
        events = list(
            db.scalars(
                select(MatterEntityEvent.event_type).where(
                    MatterEntityEvent.entity_id == uuid.UUID(duplicate_jane["id"])
                )
            )
        )
    assert {"CREATED", "MERGED", "UNMERGED"} <= set(events)


def test_fact_evidence_must_be_verbatim_in_one_paragraph(client: TestClient, root_token: str) -> None:
    ctx = _matter_with_task(client, root_token)

    def evidence(**overrides) -> list[dict]:
        item = {"matter_document_id": ctx["documents"][0], "paragraph_id": "¶1", "quote": "approved"}
        item.update(overrides)
        return [item]

    paraphrased = _create_fact(client, ctx, evidence=evidence(quote="Jane approved a price increase"))
    assert paraphrased.status_code == 422
    assert "does not appear verbatim" in paraphrased.json()["error"]["message"]

    spanning = _create_fact(client, ctx, evidence=evidence(quote="March 3, 2021.\n\nThe increase"))
    assert spanning.status_code == 422

    unknown_paragraph = _create_fact(client, ctx, evidence=evidence(paragraph_id="¶9"))
    assert unknown_paragraph.status_code == 422
    assert "does not exist" in unknown_paragraph.json()["error"]["message"]

    wrong_matter_document = _create_fact(client, ctx, evidence=evidence(matter_document_id=str(uuid.uuid4())))
    assert wrong_matter_document.status_code == 422

    second_occurrence = _create_fact(
        client,
        ctx,
        evidence=evidence(paragraph_id="¶2", quote="The increase", occurrence=2),
    )
    assert second_occurrence.status_code == 201, second_occurrence.text
    quote = second_occurrence.json()["evidence"][0]
    paragraph_two_start = DOCUMENT_TEXT.index("The increase applies")
    expected_start = DOCUMENT_TEXT.index("The increase", paragraph_two_start + 1)
    assert (quote["char_start"], quote["char_end"]) == (expected_start, expected_start + len("The increase"))
    assert DOCUMENT_TEXT[quote["char_start"] : quote["char_end"]] == "The increase"
    assert quote["source_content_hash"] == "a" * 64
    assert quote["paragraph_map_version"] == "paragraph-v1"

    third = _create_fact(client, ctx, evidence=evidence(paragraph_id="¶2", quote="The increase", occurrence=3))
    assert third.status_code == 422


def test_fact_lifecycle_revisions_confirmation_and_date_order(client: TestClient, root_token: str) -> None:
    ctx = _matter_with_task(client, root_token)
    facts_url = f"/v1/matters/{ctx['matter_id']}/facts"
    jane = _entity(client, ctx, entity_type="PERSON", display_name="Jane Smith", email="jsmith@acme.com")

    unknown_issue = _create_fact(
        client, ctx, issues=[{"matter_analysis_task_id": ctx["task_id"], "issue_key": "no_such_issue"}]
    )
    assert unknown_issue.status_code == 422
    bad_range = _create_fact(client, ctx, date_precision="RANGE", event_date_end="2021-01-01")
    assert bad_range.status_code == 422

    created = _create_fact(client, ctx, owner_entity_id=jane["id"])
    assert created.status_code == 201, created.text
    fact = created.json()
    assert fact["status"] == "WORKING"
    assert fact["owner"]["canonical_display_name"] == "Jane Smith"
    assert (fact["evidence_count"], fact["document_count"], fact["current_revision"]) == (1, 1, 1)

    month = _create_fact(
        client,
        ctx,
        summary="Prices were reviewed in March.",
        date_precision="MONTH",
        event_date_start="2021-03-17",
    ).json()
    assert month["event_date_start"] == "2021-03-01"
    undated = _create_fact(client, ctx, summary="Prices were discussed.", date_precision="UNKNOWN", event_date_start=None, date_basis=None).json()
    earlier = _create_fact(client, ctx, summary="Earlier decision.", event_date_start="2020-12-01").json()
    order = [item["id"] for item in client.get(facts_url, headers=auth(ctx["token"])).json()["items"]]
    assert order == [earlier["id"], month["id"], fact["id"], undated["id"]]

    stale = client.patch(
        f"{facts_url}/{fact['id']}",
        headers=auth(ctx["token"]),
        json={"lock_version": fact["lock_version"] + 5, "summary": "Changed"},
    )
    assert stale.status_code == 409
    revised = client.patch(
        f"{facts_url}/{fact['id']}",
        headers=auth(ctx["token"]),
        json={
            "lock_version": fact["lock_version"],
            "summary": "Jane Smith approved a 5% increase for all customers.",
            "issues": [
                {"matter_analysis_task_id": ctx["task_id"], "issue_key": "pricing"},
                {"matter_analysis_task_id": ctx["task_id"], "issue_key": "safety"},
            ],
            "participants": [{"entity_id": jane["id"], "role": "PARTICIPANT_ACTOR"}],
            "reason": "Broadened scope",
        },
    )
    assert revised.status_code == 200, revised.text
    fact = revised.json()
    assert fact["current_revision"] == 2
    assert {issue["issue_key"] for issue in fact["issues"]} == {"pricing", "safety"}
    assert [revision["revision"] for revision in fact["revisions"]] == [2, 1]
    assert fact["revisions"][0]["reason"] == "Broadened scope"
    assert {"CREATED", "REVISED"} <= {event["event_type"] for event in fact["events"]}

    confirmed = client.post(
        f"{facts_url}/{fact['id']}/confirm",
        headers=auth(ctx["token"]),
        json={"lock_version": fact["lock_version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    fact = confirmed.json()
    assert fact["status"] == "CONFIRMED"
    assert fact["confirmed_at"] is not None
    only_quote = fact["evidence"][0]["id"]
    keep_one = client.delete(
        f"{facts_url}/{fact['id']}/evidence/{only_quote}",
        headers=auth(ctx["token"]),
        params={"lock_version": fact["lock_version"]},
    )
    assert keep_one.status_code == 409

    contradicting = client.post(
        f"{facts_url}/{fact['id']}/evidence",
        headers=auth(ctx["token"]),
        json={
            "lock_version": fact["lock_version"],
            "evidence": {
                "matter_document_id": ctx["documents"][1],
                "paragraph_id": "¶2",
                "quote": "The increase is final.",
                "stance": "CONTRADICTS",
            },
        },
    )
    assert contradicting.status_code == 201, contradicting.text
    fact = contradicting.json()
    assert (fact["evidence_count"], fact["document_count"], fact["has_contradiction"]) == (2, 2, True)
    duplicate = client.post(
        f"{facts_url}/{fact['id']}/evidence",
        headers=auth(ctx["token"]),
        json={
            "lock_version": fact["lock_version"],
            "evidence": {"matter_document_id": ctx["documents"][1], "paragraph_id": "¶2", "quote": "The increase is final."},
        },
    )
    assert duplicate.status_code == 409

    confirmed_only = client.get(facts_url, headers=auth(ctx["token"]), params={"status": "CONFIRMED"}).json()
    assert [item["id"] for item in confirmed_only["items"]] == [fact["id"]]
    by_document = client.get(
        facts_url, headers=auth(ctx["token"]), params={"matter_document_id": ctx["documents"][1]}
    ).json()
    assert [item["id"] for item in by_document["items"]] == [fact["id"]]

    rejected = client.post(
        f"{facts_url}/{fact['id']}/reject",
        headers=auth(ctx["token"]),
        json={"lock_version": fact["lock_version"], "reason": "Not supported"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["confirmed_at"] is None


def test_fact_merge_moves_evidence_and_unmerge_restores_it(client: TestClient, root_token: str) -> None:
    ctx = _matter_with_task(client, root_token)
    facts_url = f"/v1/matters/{ctx['matter_id']}/facts"
    target = _create_fact(client, ctx).json()
    source = _create_fact(
        client,
        ctx,
        summary="The 5% increase was approved.",
        issues=[{"matter_analysis_task_id": ctx["task_id"], "issue_key": "safety"}],
        evidence=[
            {"matter_document_id": ctx["documents"][2], "paragraph_id": "¶1", "quote": "the 5% price increase"},
            {"matter_document_id": ctx["documents"][0], "paragraph_id": "¶1", "quote": "Jane Smith approved the 5% price increase"},
        ],
    ).json()

    merged = client.post(
        f"{facts_url}/{source['id']}/merge",
        headers=auth(ctx["token"]),
        json={
            "lock_version": source["lock_version"],
            "target_fact_id": target["id"],
            "target_lock_version": target["lock_version"],
        },
    )
    assert merged.status_code == 200, merged.text
    target = merged.json()
    assert target["evidence_count"] == 2
    assert {issue["issue_key"] for issue in target["issues"]} == {"pricing", "safety"}
    source = client.get(f"{facts_url}/{source['id']}", headers=auth(ctx["token"])).json()
    assert source["status"] == "MERGED"
    assert source["merged_into_fact_id"] == target["id"]
    assert source["evidence_count"] == 1
    listed = [item["id"] for item in client.get(facts_url, headers=auth(ctx["token"])).json()["items"]]
    assert source["id"] not in listed

    blocked = client.patch(
        f"{facts_url}/{source['id']}",
        headers=auth(ctx["token"]),
        json={"lock_version": source["lock_version"], "summary": "Edited"},
    )
    assert blocked.status_code == 409

    unmerged = client.post(
        f"{facts_url}/{source['id']}/unmerge",
        headers=auth(ctx["token"]),
        json={"lock_version": source["lock_version"]},
    )
    assert unmerged.status_code == 200, unmerged.text
    assert unmerged.json()["status"] == "WORKING"
    assert unmerged.json()["evidence_count"] == 2
    target = client.get(f"{facts_url}/{target['id']}", headers=auth(ctx["token"])).json()
    assert target["evidence_count"] == 1
    assert [issue["issue_key"] for issue in target["issues"]] == ["pricing"]


def test_rejecting_an_entity_detaches_it_from_facts_as_a_revision(client: TestClient, root_token: str) -> None:
    ctx = _matter_with_task(client, root_token)
    entities_url = f"/v1/matters/{ctx['matter_id']}/entities"
    bob = _entity(client, ctx, entity_type="PERSON", display_name="Bob Lee", status="PROPOSED")
    with_variant = client.post(
        f"{entities_url}/{bob['id']}/variants",
        headers=auth(ctx["token"]),
        json={"lock_version": bob["lock_version"], "email": "blee@example.com"},
    ).json()
    variant_id = with_variant["variants"][0]["id"]
    fact = _create_fact(
        client,
        ctx,
        owner_entity_id=variant_id,
        participants=[{"entity_id": bob["id"], "role": "PARTICIPANT_RECIPIENT"}],
    ).json()
    assert fact["owner"]["canonical_entity_id"] == bob["id"]
    assert client.get(entities_url, headers=auth(ctx["token"])).json()["items"][0]["fact_count"] == 1

    rejected = client.post(
        f"{entities_url}/{bob['id']}/reject",
        headers=auth(ctx["token"]),
        json={"lock_version": with_variant["lock_version"], "reason": "Not a real person"},
    )
    assert rejected.status_code == 200, rejected.text
    detached = client.get(f"/v1/matters/{ctx['matter_id']}/facts/{fact['id']}", headers=auth(ctx["token"])).json()
    assert detached["owner"] is None
    assert detached["participants"] == []
    assert detached["current_revision"] == 2
    assert "ENTITIES_DETACHED" in {event["event_type"] for event in detached["events"]}
    with TestingSessionLocal() as db:
        assert db.scalar(
            select(MatterFactRevision.reason).where(
                MatterFactRevision.fact_id == uuid.UUID(fact["id"]), MatterFactRevision.revision == 2
            )
        ) == "Entity Bob Lee was rejected"

    reuse = _create_fact(client, ctx, owner_entity_id=bob["id"])
    assert reuse.status_code == 422


def test_fact_and_entity_permissions_follow_system_roles(client: TestClient, root_token: str) -> None:
    ctx = _matter_with_task(client, root_token)
    assert "fact.export" not in SYSTEM_ROLE_BY_KEY["REVIEWER"].permission_keys
    assert "fact.export" in SYSTEM_ROLE_BY_KEY["MATTER_ADMINISTRATOR"].permission_keys
    roles = {
        role["key"]: role
        for role in client.get(f"/v1/tenants/{ctx['tenant_id']}/roles", headers=auth(ctx["token"])).json()
    }

    def user_with_role(email: str, role_key: str) -> str:
        user = client.post(
            f"/v1/tenants/{ctx['tenant_id']}/users",
            headers=auth(ctx["token"]),
            json={"email": email, "display_name": email.split("@")[0], "password": "long-enough-test-password"},
        )
        assert user.status_code == 201, user.text
        grant = client.post(
            f"/v1/tenants/{ctx['tenant_id']}/authorization-grants",
            headers=auth(ctx["token"]),
            json={
                "subject": {"type": "USER", "id": user.json()["id"]},
                "role_id": roles[role_key]["id"],
                "scope": {"type": "MATTER", "id": ctx["matter_id"]},
            },
        )
        assert grant.status_code == 201, grant.text
        login = client.post("/v1/auth/login", json={"email": email, "password": "long-enough-test-password"})
        return login.json()["access_token"]

    reviewer = user_with_role("fact-reviewer@example.com", "REVIEWER")
    reader = user_with_role("fact-reader@example.com", "READ_ONLY")
    operator = user_with_role("fact-operator@example.com", "ANALYSIS_OPERATOR")
    facts_url = f"/v1/matters/{ctx['matter_id']}/facts"
    entities_url = f"/v1/matters/{ctx['matter_id']}/entities"

    created = _create_fact(client, ctx, token=reviewer)
    assert created.status_code == 201, created.text
    assert client.post(
        f"{facts_url}/{created.json()['id']}/confirm",
        headers=auth(reviewer),
        json={"lock_version": created.json()["lock_version"]},
    ).status_code == 200
    assert client.post(
        entities_url, headers=auth(reviewer), json={"entity_type": "PERSON", "display_name": "Reviewer Made"}
    ).status_code == 201

    for token in (reader, operator):
        assert client.get(facts_url, headers=auth(token)).status_code == 200
        assert client.get(entities_url, headers=auth(token)).status_code == 200
        denied = _create_fact(client, ctx, token=token)
        assert denied.status_code == 403
        assert "fact.review" in denied.json()["error"]["message"]
        assert client.post(
            entities_url, headers=auth(token), json={"entity_type": "PERSON", "display_name": "Nope"}
        ).status_code == 403

    with TestingSessionLocal() as db:
        actors = set(
            db.scalars(
                select(MatterFactEvent.actor_type).where(MatterFactEvent.fact_id == uuid.UUID(created.json()["id"]))
            )
        )
    assert actors == {"HUMAN"}
