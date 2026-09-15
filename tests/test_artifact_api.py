import json
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient

from artifact_service.config import get_artifact_settings


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_individual_email_upload_with_source_container(client: TestClient, root_token: str) -> None:
    tenant_id = client.get("/v1/auth/me", headers=auth(root_token)).json()["tenant_id"]
    client_response = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "Dataset Client"},
    )
    assert client_response.status_code == 201, client_response.text
    client_id = client_response.json()["id"]

    custodian_response = client.post(
        f"/v1/clients/{client_id}/custodians",
        headers=auth(root_token),
        json={"display_name": "Phillip Allen", "email_addresses": ["phillip.allen@enron.com"]},
    )
    assert custodian_response.status_code == 201, custodian_response.text
    custodian_id = custodian_response.json()["id"]
    unused_custodian_response = client.post(
        f"/v1/clients/{client_id}/custodians",
        headers=auth(root_token),
        json={"display_name": "Unused Custodian", "email_addresses": ["unused@example.com"]},
    )
    assert unused_custodian_response.status_code == 201, unused_custodian_response.text

    storage_response = client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        headers=auth(root_token),
        json={"tenant_slug": "root"},
    )
    assert storage_response.status_code == 201, storage_response.text
    bucket_name = storage_response.json()["bucket_name"]
    assert bucket_name.startswith("pv-artifacts-root-")
    assert len(bucket_name.rsplit("-", 1)[1]) == 10

    collection_response = client.post(
        f"/v1/tenants/{tenant_id}/clients/{client_id}/collections",
        headers=auth(root_token),
        json={"name": "Enron Top CSV", "description": "Six-message sample"},
    )
    assert collection_response.status_code == 201, collection_response.text
    collection_id = collection_response.json()["id"]

    get_collection_response = client.get(
        f"/v1/collections/{collection_id}",
        headers=auth(root_token),
    )
    assert get_collection_response.status_code == 200, get_collection_response.text
    assert get_collection_response.json()["name"] == "Enron Top CSV"

    source_bytes = b'"file","message"\n"allen-p/_sent_mail/1.","Message-ID: <one@example>"\n'
    source_response = client.post(
        f"/v1/collections/{collection_id}/source-containers:upload",
        headers=auth(root_token),
        data={"original_source_path": "enron/top.csv"},
        files={"file": ("top.csv", source_bytes, "text/csv")},
    )
    assert source_response.status_code == 201, source_response.text
    source_artifact = source_response.json()["artifact"]
    assert source_artifact["role"] == "SOURCE_CONTAINER"

    email_bytes = (
        b"Message-ID: <one@example>\n"
        b"Date: Mon, 14 May 2001 16:39:00 -0700\n"
        b"From: phillip.allen@enron.com\n"
        b"To: tim.belden@enron.com\n"
        b"Subject: Test message\n\nHello\n"
    )
    metadata = {
        "source_item_id": "allen-p/_sent_mail/1.",
        "record_type": "EMAIL",
        "original_filename": "1.eml",
        "original_source_path": "allen-p/_sent_mail/1.",
        "custodian_ids": [custodian_id],
        "primary_custodian_id": custodian_id,
        "processing_status": "READY",
        "source_container_artifact_id": source_artifact["id"],
        "email": {
            "sender": "phillip.allen@enron.com",
            "subject": "Test message",
            "sent_at": "2001-05-14T23:39:00Z",
            "message_id": "<one@example>",
            "recipients": [
                {"recipient_type": "TO", "email_address": "tim.belden@enron.com"},
            ],
        },
        "raw_metadata": {"file": "allen-p/_sent_mail/1."},
        "unmapped_metadata": {},
    }
    item_response = client.post(
        f"/v1/collections/{collection_id}/items:upload",
        headers=auth(root_token),
        data={"metadata": json.dumps(metadata)},
        files={"file": ("1.eml", email_bytes, "message/rfc822")},
    )
    assert item_response.status_code == 201, item_response.text
    payload = item_response.json()
    assert payload["created"] is True
    assert payload["item"]["email"]["subject"] == "Test message"
    assert payload["item"]["custodian_ids"] == [custodian_id]
    artifact_id = payload["item"]["native_artifact"]["id"]
    sha256 = payload["item"]["native_artifact"]["sha256"]

    artifacts_response = client.get(
        f"/v1/collection-items/{payload['item']['id']}/artifacts",
        headers=auth(root_token),
    )
    assert artifacts_response.status_code == 200, artifacts_response.text
    assert [(artifact["id"], artifact["role"]) for artifact in artifacts_response.json()] == [
        (artifact_id, "NATIVE")
    ]

    selection_response = client.post(
        f"/v1/collections/{collection_id}/selections",
        headers=auth(root_token),
        json={
            "request_id": str(uuid.uuid4()),
            "mode": "QUERY",
            "q": None,
            "custodian_ids": [],
            "file_extensions": [],
            "record_types": [],
            "processing_statuses": [],
            "item_ids": [],
        },
    )
    assert selection_response.status_code == 201, selection_response.text
    batch_response = client.get(
        f"/v1/collection-selections/{selection_response.json()['id']}/items",
        headers=auth(root_token),
    )
    assert batch_response.status_code == 200, batch_response.text
    assert batch_response.json()["items"] == [
        {
            "item_id": payload["item"]["id"],
            "custodian_ids": [custodian_id],
            "custodians": [{"custodian_id": custodian_id, "relationship_type": "PRIMARY"}],
        }
    ]

    custodian_summary_response = client.get(
        f"/v1/collections/{collection_id}/custodians",
        headers=auth(root_token),
    )
    assert custodian_summary_response.status_code == 200, custodian_summary_response.text
    assert custodian_summary_response.json() == [{"custodian_id": custodian_id, "item_count": 1}]

    content_response = client.get(f"/v1/artifacts/{artifact_id}/content", headers=auth(root_token))
    assert content_response.status_code == 200, content_response.text
    assert content_response.content == email_bytes
    assert content_response.headers["etag"] == sha256

    lineage_response = client.get(f"/v1/artifacts/{artifact_id}/lineage", headers=auth(root_token))
    assert lineage_response.status_code == 200, lineage_response.text
    assert lineage_response.json() == [
        {
            "artifact_id": artifact_id,
            "source_artifact_id": source_artifact["id"],
            "relationship": "EXTRACTED_FROM_CONTAINER",
            "created_at": lineage_response.json()[0]["created_at"],
        }
    ]

    query_response = client.get(
        f"/v1/collections/{collection_id}/items",
        headers=auth(root_token),
        params={"custodian_id": custodian_id, "record_type": "EMAIL", "sha256": sha256},
    )
    assert query_response.status_code == 200, query_response.text
    assert [item["source_item_id"] for item in query_response.json()] == ["allen-p/_sent_mail/1."]

    retry_response = client.post(
        f"/v1/collections/{collection_id}/items:upload",
        headers=auth(root_token),
        data={"metadata": json.dumps(metadata)},
        files={"file": ("1.eml", email_bytes, "message/rfc822")},
    )
    assert retry_response.status_code == 201, retry_response.text
    assert retry_response.json()["created"] is False
    assert retry_response.json()["item"]["id"] == payload["item"]["id"]

    conflict_response = client.post(
        f"/v1/collections/{collection_id}/items:upload",
        headers=auth(root_token),
        data={"metadata": json.dumps(metadata)},
        files={"file": ("1.eml", b"different bytes", "message/rfc822")},
    )
    assert conflict_response.status_code == 409


def test_upload_rejects_unknown_custodian(client: TestClient, root_token: str) -> None:
    tenant_id = client.get("/v1/auth/me", headers=auth(root_token)).json()["tenant_id"]
    client_id = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "No Custodian Client"},
    ).json()["id"]
    client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        headers=auth(root_token),
        json={"tenant_slug": "root"},
    )
    collection_id = client.post(
        f"/v1/tenants/{tenant_id}/clients/{client_id}/collections",
        headers=auth(root_token),
        json={"name": "Collection"},
    ).json()["id"]
    metadata = {
        "source_item_id": "file-1",
        "record_type": "FILE",
        "original_filename": "file.txt",
        "custodian_ids": ["00000000-0000-0000-0000-000000000001"],
    }
    response = client.post(
        f"/v1/collections/{collection_id}/items:upload",
        headers=auth(root_token),
        data={"metadata": json.dumps(metadata)},
        files={"file": ("file.txt", b"content", "text/plain")},
    )
    assert response.status_code == 422


def test_collection_search_returns_disjunctive_facets_and_path_matches(
    client: TestClient,
    root_token: str,
) -> None:
    tenant_id = client.get("/v1/auth/me", headers=auth(root_token)).json()["tenant_id"]
    client_id = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "Search Client"},
    ).json()["id"]
    custodian_ids = []
    for name in ["Alice Adams", "Bob Baker"]:
        response = client.post(
            f"/v1/clients/{client_id}/custodians",
            headers=auth(root_token),
            json={"display_name": name},
        )
        assert response.status_code == 201, response.text
        custodian_ids.append(response.json()["id"])
    client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        headers=auth(root_token),
        json={"tenant_slug": "root"},
    )
    collection_id = client.post(
        f"/v1/tenants/{tenant_id}/clients/{client_id}/collections",
        headers=auth(root_token),
        json={"name": "Search Collection"},
    ).json()["id"]

    items = [
        ("budget-pdf", "budget.pdf", "/finance/annual", [custodian_ids[0]], "READY"),
        ("legal-notes", "notes.txt", "/legal/notes", [custodian_ids[1]], "NOT_PROCESSED"),
        ("budget-data", "data.csv", "/finance/budget-data", custodian_ids, "READY"),
    ]
    for source_id, filename, source_path, item_custodians, processing_status in items:
        metadata = {
            "source_item_id": source_id,
            "record_type": "FILE",
            "original_filename": filename,
            "original_source_path": source_path,
            "custodian_ids": item_custodians,
            "processing_status": processing_status,
        }
        response = client.post(
            f"/v1/collections/{collection_id}/items:upload",
            headers=auth(root_token),
            data={"metadata": json.dumps(metadata)},
            files={"file": (filename, source_id.encode(), "application/octet-stream")},
        )
        assert response.status_code == 201, response.text

    extension_response = client.get(
        f"/v1/collections/{collection_id}/search",
        headers=auth(root_token),
        params=[("q", "budget"), ("extension", "pdf")],
    )
    assert extension_response.status_code == 200, extension_response.text
    extension_payload = extension_response.json()
    assert extension_payload["total"] == 1
    assert [item["source_item_id"] for item in extension_payload["items"]] == ["budget-pdf"]
    assert {entry["value"]: entry["count"] for entry in extension_payload["facets"]["file_extensions"]} == {
        "csv": 1,
        "pdf": 1,
    }
    assert extension_payload["facets"]["custodians"] == [
        {"value": custodian_ids[0], "count": 1}
    ]

    custodian_response = client.get(
        f"/v1/collections/{collection_id}/search",
        headers=auth(root_token),
        params=[("q", "budget"), ("custodian_id", custodian_ids[1])],
    )
    assert custodian_response.status_code == 200, custodian_response.text
    custodian_payload = custodian_response.json()
    assert custodian_payload["total"] == 1
    assert [item["source_item_id"] for item in custodian_payload["items"]] == ["budget-data"]
    assert {entry["value"]: entry["count"] for entry in custodian_payload["facets"]["custodians"]} == {
        custodian_ids[0]: 2,
        custodian_ids[1]: 1,
    }

    path_response = client.get(
        f"/v1/collections/{collection_id}/search",
        headers=auth(root_token),
        params={"q": "/legal/notes"},
    )
    assert path_response.status_code == 200, path_response.text
    assert [item["source_item_id"] for item in path_response.json()["items"]] == ["legal-notes"]


def test_standalone_artifact_api_uses_scoped_delegation(artifact_api_client: TestClient) -> None:
    tenant_id = uuid.uuid4()
    client_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    settings = get_artifact_settings()
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": settings.delegation_issuer,
            "aud": settings.delegation_audience,
            "sub": str(actor_id),
            "tenants": [str(tenant_id)],
            "clients": [[str(tenant_id), str(client_id)]],
            "custodians": [],
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        settings.delegation_secret,
        algorithm="HS256",
    )
    headers = auth(token)

    unauthorized = artifact_api_client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        json={"tenant_slug": "standalone"},
    )
    assert unauthorized.status_code == 401

    storage_response = artifact_api_client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        headers=headers,
        json={"tenant_slug": "standalone"},
    )
    assert storage_response.status_code == 201, storage_response.text

    collection_response = artifact_api_client.post(
        f"/v1/tenants/{tenant_id}/clients/{client_id}/collections",
        headers=headers,
        json={"name": "Standalone Collection"},
    )
    assert collection_response.status_code == 201, collection_response.text
