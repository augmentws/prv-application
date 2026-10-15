import csv
import io
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient

from app.artifact_gateway import WorkflowArtifactReference
from app.config import get_settings
from app.fact_exports import fail_fact_export_job, run_fact_export
from app.job_executions import MATTER_FACT_EXPORT_JOB_TYPE
from app.models import MatterFactExportJob, User
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

    created = _request_export(client, ctx, filters={"status": ["CONFIRMED"]}, requested_filename="chronology.csv")
    assert created.status_code == 202, created.text
    job = created.json()
    assert job["status"] == "QUEUED"
    assert job["filter_snapshot"]["statuses"] == ["CONFIRMED"]
    assert job["execution"]["job_type"] == MATTER_FACT_EXPORT_JOB_TYPE
    same = _request_export(client, ctx, filters={"status": ["CONFIRMED"]}, requested_filename="chronology.csv")
    assert same.json()["id"] == job["id"]
    assert _request_export(client, ctx, filters={"status": ["WORKING"]}).status_code == 409

    run_fact_export(uuid.UUID(job["id"]), get_settings(), session_factory=TestingSessionLocal)

    done = client.get(f"/v1/matters/{ctx['matter_id']}/fact-exports/{job['id']}", headers=auth(ctx["token"])).json()
    assert done["status"] == "COMPLETED", done
    assert (done["fact_count"], done["evidence_count"]) == (2, 3)
    assert done["facts_filename"].startswith("chronology-facts-as-of-")
    assert done["evidence_filename"].startswith("chronology-evidence-as-of-")

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


def test_fact_export_requires_export_permission_and_reauthorizes_the_worker(
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

    job = _request_export(client, ctx).json()
    with TestingSessionLocal() as db:
        actor = db.get(User, uuid.UUID(job["created_by_user_id"]))
        assert actor is not None
        actor.status = "SUSPENDED"
        db.commit()
    with pytest.raises(PermissionError):
        run_fact_export(uuid.UUID(job["id"]), get_settings(), session_factory=TestingSessionLocal)
    with TestingSessionLocal() as db:
        fail_fact_export_job(db, uuid.UUID(job["id"]), PermissionError("Fact export requires fact.export"))
        db.commit()
        failed = db.get(MatterFactExportJob, uuid.UUID(job["id"]))
        assert failed is not None
        assert failed.status == "FAILED"
        assert failed.failure_code == "FACT_EXPORT_AUTHORIZATION_REVOKED"
        assert failed.failure_retriable is False
    assert stored == {}


def test_fact_export_cancel_and_retry(client: TestClient, root_token: str, stored: dict[str, bytes]) -> None:
    ctx = _matter_with_task(client, root_token)
    _create_fact(client, ctx)
    job = _request_export(client, ctx).json()
    canceled = client.post(f"/v1/jobs/{MATTER_FACT_EXPORT_JOB_TYPE}/{job['id']}/cancel", headers=auth(ctx["token"]))
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "CANCELED"
    with TestingSessionLocal() as db:
        state = db.get(MatterFactExportJob, uuid.UUID(job["id"]))
        assert state is not None and state.status == "CANCELED"

    retried = client.post(f"/v1/jobs/{MATTER_FACT_EXPORT_JOB_TYPE}/{job['id']}/retry", headers=auth(ctx["token"]))
    assert retried.status_code == 200, retried.text
    assert retried.json()["attempt_number"] == 2
    run_fact_export(uuid.UUID(job["id"]), get_settings(), session_factory=TestingSessionLocal)
    done = client.get(f"/v1/matters/{ctx['matter_id']}/fact-exports/{job['id']}", headers=auth(ctx["token"])).json()
    assert done["status"] == "COMPLETED"
    assert done["fact_count"] == 1


def test_fact_export_download_checks_provenance_and_expiry(
    client: TestClient,
    root_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _matter_with_task(client, root_token)
    job = _request_export(client, ctx).json()
    url = f"/v1/matters/{ctx['matter_id']}/fact-exports/{job['id']}/download"
    assert client.get(url, headers=auth(ctx["token"])).status_code == 409

    content = {"FACTS": b"Fact ID\r\nf-1\r\n", "EVIDENCE": b"Fact ID,Evidence ID\r\nf-1,e-1\r\n"}
    artifact_ids = {"FACTS": uuid.uuid4(), "EVIDENCE": uuid.uuid4()}
    with TestingSessionLocal() as db:
        state = db.get(MatterFactExportJob, uuid.UUID(job["id"]))
        assert state is not None
        state.status = "COMPLETED"
        state.facts_artifact_id = artifact_ids["FACTS"]
        state.facts_content_hash = "a" * 64
        state.facts_byte_length = len(content["FACTS"])
        state.facts_filename = "facts-facts-as-of-20261015T000000Z.csv"
        state.evidence_artifact_id = artifact_ids["EVIDENCE"]
        state.evidence_content_hash = "b" * 64
        state.evidence_byte_length = len(content["EVIDENCE"])
        state.evidence_filename = "facts-evidence-as-of-20261015T000000Z.csv"
        state.expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        db.commit()
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
            content_hash="a" * 64 if kind == "FACTS" else "b" * 64,
            media_type="text/csv",
            byte_length=len(content[kind]),
            producer_kind="CORE",
            producer_run_id=uuid.UUID(job["id"]),
            metadata={"export_job_id": job["id"], "file_kind": kind},
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr("app.routers.fact_exports.get_workflow_artifact_reference", reference)
    monkeypatch.setattr(
        "app.routers.fact_exports.iter_workflow_artifact_content",
        lambda *, artifact_id, **_: iter(
            [content["FACTS"] if artifact_id == artifact_ids["FACTS"] else content["EVIDENCE"]]
        ),
    )
    facts = client.get(url, headers=auth(ctx["token"]))
    assert facts.status_code == 200, facts.text
    assert facts.content == content["FACTS"]
    assert "facts-facts-as-of-20261015T000000Z.csv" in facts.headers["content-disposition"]
    evidence = client.get(url, headers=auth(ctx["token"]), params={"file": "evidence"})
    assert evidence.content == content["EVIDENCE"]

    monkeypatch.setattr(
        "app.routers.fact_exports.get_workflow_artifact_reference",
        lambda **kwargs: replace(reference(**kwargs), metadata={"export_job_id": job["id"], "file_kind": "OTHER"}),
    )
    assert client.get(url, headers=auth(ctx["token"])).status_code == 409

    with TestingSessionLocal() as db:
        state = db.get(MatterFactExportJob, uuid.UUID(job["id"]))
        assert state is not None
        state.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    assert client.get(url, headers=auth(ctx["token"])).status_code == 410
