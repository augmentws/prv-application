import csv
import io
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.artifact_gateway import WorkflowArtifactReference
from app.config import get_settings
from app.models import AuditRecord, MatterFactExport
from classification_service.schemas import ArtifactType
from tests.test_agents_and_matter_definitions import auth
from tests.test_facts_and_entities import _create_fact, _entity, _matter_with_task, document_text  # noqa: F401


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    files: dict[str, bytes] = {}

    def fake_store(**kwargs):
        content = kwargs["content"].read()
        files[kwargs["artifact_metadata"]["file_kind"]] = content
        return (
            WorkflowArtifactReference(
                artifact_id=uuid.uuid4(),
                tenant_id=kwargs["tenant_id"],
                client_id=kwargs["client_id"],
                core_scope_kind=kwargs["core_scope_kind"],
                core_scope_id=kwargs["core_scope_id"],
                artifact_type=ArtifactType.FACT_EXPORT,
                schema_version=1,
                derivation_key=kwargs["derivation_key"],
                content_hash=kwargs["content_hash"],
                media_type="text/csv",
                byte_length=len(content),
                producer_kind="CORE",
                producer_run_id=kwargs["producer_run_id"],
                metadata=kwargs["artifact_metadata"],
                created_at=datetime.now(timezone.utc),
            ),
            True,
        )

    monkeypatch.setattr("app.fact_exports.find_workflow_artifact_reference", lambda **_: None)
    monkeypatch.setattr("app.fact_exports.store_workflow_artifact", fake_store)
    return files


def _rows(content: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content.decode("utf-8"))))


def _confirm(client: TestClient, ctx: dict, fact: dict) -> dict:
    response = client.post(
        f"/v1/matters/{ctx['matter_id']}/facts/{fact['id']}/confirm",
        headers=auth(ctx["token"]),
        json={"lock_version": fact["lock_version"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _request_export(client: TestClient, ctx: dict, *, token: str | None = None, **payload):
    return client.post(
        f"/v1/matters/{ctx['matter_id']}/fact-exports",
        headers=auth(token or ctx["token"]),
        json={"idempotency_key": "export-1", **payload},
    )


def _audit_actions(target_id: str | None = None) -> list[str]:
    with TestingSessionLocal() as db:
        statement = select(AuditRecord.action).where(AuditRecord.action.like("fact_export.%"))
        if target_id is not None:
            statement = statement.where(AuditRecord.target_id == uuid.UUID(target_id))
        return list(db.scalars(statement.order_by(AuditRecord.created_at)))


def test_fact_export_writes_filtered_facts_and_quotes_in_date_order(
    client: TestClient,
    root_token: str,
    stored: dict[str, bytes],
) -> None:
    ctx = _matter_with_task(client, root_token)
    jane = _entity(client, ctx, entity_type="PERSON", display_name="Jane Smith")
    later = _confirm(client, ctx, _create_fact(client, ctx, owner_entity_id=jane["id"]).json())
    earlier = _confirm(
        client,
        ctx,
        _create_fact(
            client,
            ctx,
            summary="=HYPERLINK(\"x\") price memo",
            event_date_start="2020-06-01",
            evidence=[
                {"matter_document_id": ctx["documents"][1], "paragraph_id": "¶2", "quote": "The increase is final."},
                {"matter_document_id": ctx["documents"][2], "paragraph_id": "¶1", "quote": "price increase"},
            ],
        ).json(),
    )
    _create_fact(client, ctx, summary="Unconfirmed working fact.")

    created = _request_export(client, ctx, filters={"statuses": ["CONFIRMED"]}, requested_filename="chronology.csv")
    assert created.status_code == 201, created.text
    export = created.json()
    assert export["filter_snapshot"]["statuses"] == ["CONFIRMED"]
    assert (export["fact_count"], export["evidence_count"]) == (2, 3)
    assert export["facts_filename"].startswith("chronology-facts-as-of-")
    assert export["evidence_filename"].startswith("chronology-evidence-as-of-")
    assert export["facts_byte_length"] == len(stored["FACTS"])

    facts = _rows(stored["FACTS"])
    assert [row["Fact ID"] for row in facts] == [earlier["id"], later["id"]]
    assert facts[0]["Summary"] == "'=HYPERLINK(\"x\") price memo"
    assert facts[1]["Owner"] == "Jane Smith"
    assert facts[1]["Owner type"] == "PERSON"
    assert facts[1]["Issues"] == "Pricing decisions"
    assert facts[1]["Status"] == "CONFIRMED"
    assert facts[1]["Confirmed by"] == "Agent Admin"
    evidence = _rows(stored["EVIDENCE"])
    assert [row["Fact ID"] for row in evidence] == [earlier["id"], earlier["id"], later["id"]]
    assert evidence[0]["Quote"] == "The increase is final."
    assert stored["FACTS"].endswith(b"\r\n")

    stored.clear()
    same = _request_export(client, ctx, filters={"statuses": ["CONFIRMED"]}, requested_filename="chronology.csv")
    assert same.status_code == 201
    assert same.json()["id"] == export["id"]
    assert stored == {}, "a repeated request must not write the files again"
    assert _request_export(client, ctx, filters={"statuses": ["WORKING"]}).status_code == 409
    assert _audit_actions(export["id"]) == ["fact_export.created"]

    listed = client.get(f"/v1/matters/{ctx['matter_id']}/fact-exports", headers=auth(ctx["token"])).json()
    assert [item["id"] for item in listed] == [export["id"]]


def test_fact_export_requires_export_permission(
    client: TestClient,
    root_token: str,
    stored: dict[str, bytes],
) -> None:
    ctx = _matter_with_task(client, root_token)
    roles = {
        role["key"]: role
        for role in client.get(f"/v1/tenants/{ctx['tenant_id']}/roles", headers=auth(ctx["token"])).json()
    }
    user = client.post(
        f"/v1/tenants/{ctx['tenant_id']}/users",
        headers=auth(ctx["token"]),
        json={"email": "reviewer@example.com", "display_name": "Reviewer", "password": "long-enough-test-password"},
    ).json()
    assert client.post(
        f"/v1/tenants/{ctx['tenant_id']}/authorization-grants",
        headers=auth(ctx["token"]),
        json={
            "subject": {"type": "USER", "id": user["id"]},
            "role_id": roles["REVIEWER"]["id"],
            "scope": {"type": "MATTER", "id": ctx["matter_id"]},
        },
    ).status_code == 201
    reviewer = client.post(
        "/v1/auth/login", json={"email": "reviewer@example.com", "password": "long-enough-test-password"}
    ).json()["access_token"]
    denied = _request_export(client, ctx, token=reviewer)
    assert denied.status_code == 403
    assert "fact.export" in denied.json()["error"]["message"]
    assert client.get(f"/v1/matters/{ctx['matter_id']}/fact-exports", headers=auth(reviewer)).status_code == 403
    assert stored == {}


def test_fact_export_failure_is_audited_and_leaves_no_export(
    client: TestClient,
    root_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _matter_with_task(client, root_token)
    _create_fact(client, ctx)

    def unavailable(**_):
        raise httpx.ConnectError("artifact service unavailable")

    monkeypatch.setattr("app.fact_exports.find_workflow_artifact_reference", lambda **_: None)
    monkeypatch.setattr("app.fact_exports.store_workflow_artifact", unavailable)
    failed = _request_export(client, ctx)
    assert failed.status_code == 503, failed.text
    with TestingSessionLocal() as db:
        assert db.scalar(select(MatterFactExport.id)) is None
    assert _audit_actions() == ["fact_export.failed"]


def test_fact_export_over_the_size_limit_is_refused(
    client: TestClient,
    root_token: str,
    stored: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _matter_with_task(client, root_token)
    _create_fact(client, ctx)
    settings = get_settings()
    monkeypatch.setattr(settings, "fact_export_max_bytes", 64)  # smaller than the header rows alone
    refused = _request_export(client, ctx)
    assert refused.status_code == 413, refused.text
    assert stored == {}
    assert _audit_actions() == ["fact_export.failed"]


def test_fact_export_download_checks_provenance_and_expiry(
    client: TestClient,
    root_token: str,
    stored: dict[str, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _matter_with_task(client, root_token)
    _create_fact(client, ctx)
    created = _request_export(client, ctx)
    assert created.status_code == 201, created.text
    export = created.json()
    url = f"/v1/matters/{ctx['matter_id']}/fact-exports/{export['id']}/download"
    with TestingSessionLocal() as db:
        state = db.get(MatterFactExport, uuid.UUID(export["id"]))
        assert state is not None
        artifact_ids = {"FACTS": state.facts_artifact_id, "EVIDENCE": state.evidence_artifact_id}
        hashes = {"FACTS": state.facts_content_hash, "EVIDENCE": state.evidence_content_hash}
        tenant_id = state.tenant_id

    def reference(*, artifact_id, **_):
        kind = "FACTS" if artifact_id == artifact_ids["FACTS"] else "EVIDENCE"
        return WorkflowArtifactReference(
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            client_id=uuid.uuid4(),
            core_scope_kind="MATTER",
            core_scope_id=uuid.UUID(ctx["matter_id"]),
            artifact_type=ArtifactType.FACT_EXPORT,
            schema_version=1,
            derivation_key="c" * 64,
            content_hash=hashes[kind],
            media_type="text/csv",
            byte_length=len(stored[kind]),
            producer_kind="CORE",
            producer_run_id=uuid.UUID(export["id"]),
            metadata={"export_id": export["id"], "file_kind": kind},
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr("app.routers.fact_exports.get_workflow_artifact_reference", reference)
    monkeypatch.setattr(
        "app.routers.fact_exports.iter_workflow_artifact_content",
        lambda *, artifact_id, **_: iter([stored["FACTS"] if artifact_id == artifact_ids["FACTS"] else stored["EVIDENCE"]]),
    )
    facts = client.get(url, headers=auth(ctx["token"]))
    assert facts.status_code == 200, facts.text
    assert facts.content == stored["FACTS"]
    assert export["facts_filename"] in facts.headers["content-disposition"]
    evidence = client.get(url, headers=auth(ctx["token"]), params={"file": "evidence"})
    assert evidence.content == stored["EVIDENCE"]
    assert _audit_actions(export["id"]) == ["fact_export.created", "fact_export.downloaded", "fact_export.downloaded"]

    monkeypatch.setattr(
        "app.routers.fact_exports.get_workflow_artifact_reference",
        lambda **kwargs: replace(reference(**kwargs), metadata={"export_id": export["id"], "file_kind": "OTHER"}),
    )
    assert client.get(url, headers=auth(ctx["token"])).status_code == 409

    with TestingSessionLocal() as db:
        state = db.get(MatterFactExport, uuid.UUID(export["id"]))
        assert state is not None
        state.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    assert client.get(url, headers=auth(ctx["token"])).status_code == 410
