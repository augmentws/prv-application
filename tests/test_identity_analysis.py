import json
import uuid
from types import SimpleNamespace

import pytest
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.artifact_gateway import IdentityMetadataSnapshot, IdentityParticipantSnapshot
from app.identity_analysis import IdentityInventoryBuilder, run_identity_analysis
from app.models import MatterDocument, MatterIdentityAnalysis
from tests.test_agents_and_matter_definitions import auth, create_tenant_context
from tests.test_review_batches import add_documents


def test_identity_inventory_uses_only_structured_participants() -> None:
    builder = IdentityInventoryBuilder()
    document_id = uuid.uuid4()
    builder.add_document(
        document_id,
        IdentityMetadataSnapshot(
            item_id=uuid.uuid4(),
            sender=IdentityParticipantSnapshot(email_address="Alice Counsel <ALICE@LawFirm.example>", display_name=None),
            recipients={
                "TO": [IdentityParticipantSnapshot(email_address="bob@client.example", display_name="Bob Client")],
                "CC": [
                    IdentityParticipantSnapshot(email_address=None, display_name="Unresolved Recipient"),
                    IdentityParticipantSnapshot(email_address=None, display_name="unresolved recipient"),
                ],
                "BCC": [],
            },
        ),
    )

    result = builder.result(matter_id=uuid.uuid4(), analysis_id=uuid.uuid4(), population_hash="a" * 64)

    assert result["source_contract"] == "structured-email-participants-only-v1"
    assert result["coverage"] == {
        "documents_scanned": 1,
        "documents_with_usable_participants": 1,
        "documents_without_usable_participants": 0,
        "unresolved_participant_observations": 2,
        "unique_normalized_unresolved_values": 1,
        "unique_email_addresses": 2,
        "unique_domains": 2,
    }
    assert [row["email_address"] for row in result["identities"]] == [
        "alice@lawfirm.example",
        "bob@client.example",
    ]
    assert result["identities"][0]["display_names"] == [
        {"value": "Alice Counsel", "observation_count": 1}
    ]
    assert result["unresolved_participants"] == [
        {
            "normalized_value": "unresolved recipient",
            "observation_count": 2,
            "document_count": 1,
            "variants": [
                {"value": "Unresolved Recipient", "observation_count": 1},
                {"value": "unresolved recipient", "observation_count": 1},
            ],
            "reason_counts": {"NO_EMAIL_ADDRESS": 2},
            "role_counts": {"CC": 2},
        }
    ]
    assert set(result) == {
        "schema_version",
        "analysis_id",
        "matter_id",
        "population_hash",
        "source_contract",
        "coverage",
        "identities",
        "domains",
        "unresolved_participants",
    }


def _start_analysis(client: TestClient, root_token: str, monkeypatch: pytest.MonkeyPatch):
    _tenant_id, token, matter_id = create_tenant_context(client, root_token)
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    add_documents(matter_id, user_id, count=2)
    enqueued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.routers.identity_analysis.enqueue_identity_analysis",
        lambda _db, workflow_id, analysis_id: enqueued.append((workflow_id, analysis_id)),
    )
    response = client.post(f"/v1/matters/{matter_id}/identity-analysis-jobs", headers=auth(token))
    assert response.status_code == 202, response.text
    return token, uuid.UUID(matter_id), response.json(), enqueued


def test_manual_identity_analysis_trigger_is_matter_scoped_and_single_active(
    client: TestClient,
    root_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, matter_id, payload, enqueued = _start_analysis(client, root_token, monkeypatch)

    assert payload["status"] == "QUEUED"
    assert payload["total_document_count"] == 2
    assert payload["processed_document_count"] == 0
    assert enqueued == [(payload["execution"]["dbos_workflow_id"], payload["id"])]

    duplicate = client.post(f"/v1/matters/{matter_id}/identity-analysis-jobs", headers=auth(token))
    assert duplicate.status_code == 409
    listed = client.get(f"/v1/matters/{matter_id}/identity-analysis-jobs", headers=auth(token))
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [payload["id"]]


def test_identity_analysis_run_publishes_artifact_without_document_content(
    client: TestClient,
    root_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _token, matter_id, payload, _enqueued = _start_analysis(client, root_token, monkeypatch)
    with TestingSessionLocal() as db:
        rows = list(
            db.execute(
                select(MatterDocument.id, MatterDocument.collection_item_id)
                .where(MatterDocument.matter_id == matter_id)
                .order_by(MatterDocument.id)
            )
        )

    def snapshots(**kwargs):
        return {
            item_id: IdentityMetadataSnapshot(
                item_id=item_id,
                sender=IdentityParticipantSnapshot(email_address=f"sender-{index}@example.com", display_name=None),
                recipients={"TO": [], "CC": [], "BCC": []},
            )
            for index, item_id in enumerate(kwargs["collection_item_ids"])
        }

    artifact_content: dict = {}

    def store(**kwargs):
        artifact_content.update(json.loads(kwargs["content"].read()))
        return (
            SimpleNamespace(
                artifact_id=uuid.uuid4(),
                content_hash=kwargs["content_hash"],
                byte_length=kwargs["byte_length"],
            ),
            True,
        )

    monkeypatch.setattr("app.identity_analysis.get_identity_metadata_snapshots", snapshots)
    monkeypatch.setattr("app.identity_analysis.store_workflow_artifact", store)

    run_identity_analysis(uuid.UUID(payload["id"]), session_factory=TestingSessionLocal)

    with TestingSessionLocal() as db:
        analysis = db.get(MatterIdentityAnalysis, uuid.UUID(payload["id"]))
        assert analysis is not None
        assert analysis.status == "COMPLETED"
        assert analysis.processed_document_count == len(rows) == 2
        assert analysis.documents_with_participants_count == 2
        assert analysis.identity_count == 2
        assert analysis.domain_count == 1
        assert analysis.result_artifact_id is not None
    assert artifact_content["source_contract"] == "structured-email-participants-only-v1"
    assert "text" not in json.dumps(artifact_content).casefold()
    assert "subject" not in json.dumps(artifact_content).casefold()
