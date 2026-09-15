import uuid

from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import matter_imports
from app.artifact_gateway import SelectionBatchItem
from app.document_metadata import current_metadata_values
from app.models import DocumentMetadataCurrent, MetadataDefinition, MetadataEvent, SearchProjectionOperation


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_matter_document(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> tuple[str, str, dict[str, dict]]:
    monkeypatch.setattr(matter_imports, "SessionLocal", TestingSessionLocal)
    client_record = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "Document Metadata Client"},
    ).json()
    matter = client.post(
        f"/v1/clients/{client_record['id']}/matters",
        headers=auth(root_token),
        json={"name": "Document Metadata Matter"},
    ).json()
    client.post(
        f"/v1/tenants/{root_admin.tenant_id}/artifact-storage/ensure",
        headers=auth(root_token),
        json={"tenant_slug": "root"},
    )
    collection = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/clients/{client_record['id']}/collections",
        headers=auth(root_token),
        json={"name": "Metadata source"},
    ).json()
    job_response = client.post(
        f"/v1/matters/{matter['id']}/document-imports",
        headers=auth(root_token),
        json={
            "source_collection_id": collection["id"],
            "selection": {
                "mode": "QUERY",
                "q": None,
                "custodian_ids": [],
                "file_extensions": [],
                "record_types": [],
                "processing_statuses": [],
                "item_ids": [],
            },
            "selection_summary": "Metadata test document",
        },
    )
    assert job_response.status_code == 202, job_response.text
    item_id = uuid.uuid4()
    result = matter_imports.process_batch(
        uuid.UUID(job_response.json()["id"]),
        0,
        [SelectionBatchItem(item_id=item_id, custodians=[])],
    )
    assert result.added_count == 1
    documents = client.get(
        f"/v1/matters/{matter['id']}/documents",
        headers=auth(root_token),
    ).json()
    definitions = client.get(
        f"/v1/matters/{matter['id']}/metadata-definitions",
        headers=auth(root_token),
    ).json()
    return matter["id"], documents[0]["id"], {definition["key"]: definition for definition in definitions}


def event_url(matter_id: str, document_id: str, definition_id: str) -> str:
    return f"/v1/matters/{matter_id}/documents/{document_id}/metadata-values/{definition_id}/events"


def test_single_value_events_update_projection_and_preserve_history(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter_id, document_id, definitions = create_matter_document(client, root_token, root_admin, monkeypatch)
    responsiveness = definitions["responsiveness"]
    url = event_url(matter_id, document_id, responsiveness["id"])

    first = client.post(url, headers=auth(root_token), json={"operation": "SET", "value": "responsive"})
    assert first.status_code == 201, first.text
    assert first.json()["current"]["resolution_state"] == "VALUE"
    assert [value["value"] for value in first.json()["current"]["values"]] == ["responsive"]

    second = client.post(
        url,
        headers=auth(root_token),
        json={"operation": "SET", "value": "not_responsive"},
    )
    assert second.status_code == 201, second.text
    assert second.json()["current"]["resolution_state"] == "CONFLICTED"
    assert {value["value"] for value in second.json()["current"]["values"]} == {
        "responsive",
        "not_responsive",
    }
    rejected = client.post(
        url,
        headers=auth(root_token),
        json={"operation": "REJECT", "target_event_id": first.json()["event"]["id"]},
    )
    assert rejected.status_code == 201, rejected.text
    assert [value["value"] for value in rejected.json()["current"]["values"]] == ["not_responsive"]

    third = client.post(
        url,
        headers=auth(root_token),
        json={
            "operation": "SET",
            "value": "needs_review",
            "supersedes_id": second.json()["event"]["id"],
        },
    )
    assert third.status_code == 201, third.text
    assert [value["value"] for value in third.json()["current"]["values"]] == ["needs_review"]
    with TestingSessionLocal() as db:
        definition = db.get(MetadataDefinition, uuid.UUID(responsiveness["id"]))
        assert definition is not None
        assert current_metadata_values(db, uuid.UUID(document_id), [definition]) == {
            "responsiveness": "needs_review"
        }
    history = client.get(url, headers=auth(root_token)).json()
    assert history[0]["effective_status"] == "REJECTED"
    assert history[1]["effective_status"] == "SUPERSEDED"
    assert history[3]["effective_status"] == "ACTIVE"

    cleared = client.post(url, headers=auth(root_token), json={"operation": "CLEAR"})
    assert cleared.status_code == 201, cleared.text
    assert cleared.json()["current"]["resolution_state"] == "EMPTY"
    assert cleared.json()["current"]["values"] == []

    invalid_enum = client.post(url, headers=auth(root_token), json={"operation": "SET", "value": "other"})
    assert invalid_enum.status_code == 422
    system_definition = definitions["custodian"]
    read_only = client.post(
        event_url(matter_id, document_id, system_definition["id"]),
        headers=auth(root_token),
        json={"operation": "ADD", "value": str(uuid.uuid4())},
    )
    assert read_only.status_code == 422

    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(MetadataEvent)) == 5
        assert db.scalar(select(func.count()).select_from(DocumentMetadataCurrent)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(SearchProjectionOperation)
                .where(SearchProjectionOperation.kind == "DOCUMENT_UPSERT")
            )
            == 6
        )


def test_multiple_values_and_confirmation_policy(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter_id, document_id, _ = create_matter_document(client, root_token, root_admin, monkeypatch)
    multi_definition = client.post(
        f"/v1/matters/{matter_id}/metadata-definitions",
        headers=auth(root_token),
        json={
            "key": "issue_tags",
            "display_name": "Issue tags",
            "type": "ENUM",
            "cardinality": "MULTIPLE",
            "allowed_values": [
                {"key": "contract", "label": "Contract"},
                {"key": "conduct", "label": "Conduct"},
            ],
            "facetable": True,
        },
    ).json()
    multi_url = event_url(matter_id, document_id, multi_definition["id"])
    contract = client.post(multi_url, headers=auth(root_token), json={"operation": "ADD", "value": "contract"})
    conduct = client.post(multi_url, headers=auth(root_token), json={"operation": "ADD", "value": "conduct"})
    assert contract.status_code == conduct.status_code == 201
    assert {value["value"] for value in conduct.json()["current"]["values"]} == {"contract", "conduct"}
    removed = client.post(
        multi_url,
        headers=auth(root_token),
        json={"operation": "REMOVE", "target_event_id": contract.json()["event"]["id"]},
    )
    assert removed.status_code == 201, removed.text
    assert [value["value"] for value in removed.json()["current"]["values"]] == ["conduct"]

    confirmed_definition = client.post(
        f"/v1/matters/{matter_id}/metadata-definitions",
        headers=auth(root_token),
        json={
            "key": "quality_checked",
            "display_name": "Quality checked",
            "type": "BOOLEAN",
            "assertion_policy": "REQUIRES_CONFIRMATION",
        },
    ).json()
    confirmed_url = event_url(matter_id, document_id, confirmed_definition["id"])
    proposed = client.post(confirmed_url, headers=auth(root_token), json={"operation": "SET", "value": True})
    assert proposed.status_code == 201, proposed.text
    assert proposed.json()["current"]["resolution_state"] == "PENDING"
    assert proposed.json()["current"]["pending_event_ids"] == [proposed.json()["event"]["id"]]
    confirmed = client.post(
        confirmed_url,
        headers=auth(root_token),
        json={"operation": "CONFIRM", "target_event_id": proposed.json()["event"]["id"]},
    )
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["current"]["resolution_state"] == "VALUE"
    assert confirmed.json()["current"]["values"][0]["value"] is True
    rejected = client.post(
        confirmed_url,
        headers=auth(root_token),
        json={"operation": "REJECT", "target_event_id": proposed.json()["event"]["id"]},
    )
    assert rejected.status_code == 201, rejected.text
    assert rejected.json()["current"]["resolution_state"] == "EMPTY"
