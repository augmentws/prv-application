import json
import uuid

from conftest import ArtifactTestingSessionLocal, TestingSessionLocal, memory_storage
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import MatterDocument, MatterDocumentImportJob
from artifact_service.deletion import (
    complete_deletion,
    delete_blob_batch,
    delete_collection_record,
    delete_item_batch,
    prepare_deletion,
)
from artifact_service.models import Artifact, ClientCollection, CollectionItem, ContentBlob


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_collection_with_item(
    client: TestClient,
    root_token: str,
    *,
    client_id: str,
    tenant_id: str,
    custodian_id: str,
    name: str,
    content: bytes = b"shared evidence",
) -> tuple[str, str]:
    collection = client.post(
        f"/v1/tenants/{tenant_id}/clients/{client_id}/collections",
        headers=auth(root_token),
        json={"name": name, "description": None},
    )
    assert collection.status_code == 201, collection.text
    collection_id = collection.json()["id"]
    metadata = {
        "source_item_id": f"{name}/document.txt",
        "record_type": "FILE",
        "original_filename": "document.txt",
        "custodian_ids": [custodian_id],
        "processing_status": "READY",
    }
    item = client.post(
        f"/v1/collections/{collection_id}/items:upload",
        headers=auth(root_token),
        data={"metadata": json.dumps(metadata)},
        files={"file": ("document.txt", content, "text/plain")},
    )
    assert item.status_code == 201, item.text
    return collection_id, item.json()["item"]["id"]


def run_deletion(job_id: str) -> None:
    job_uuid = uuid.UUID(job_id)
    with ArtifactTestingSessionLocal() as db:
        prepare_deletion(db, job_uuid)
    while True:
        with ArtifactTestingSessionLocal() as db:
            result = delete_item_batch(db, job_uuid, batch_size=1)
        if not result["has_more"]:
            break
    with ArtifactTestingSessionLocal() as db:
        delete_collection_record(db, job_uuid)
    while True:
        with ArtifactTestingSessionLocal() as db:
            result = delete_blob_batch(db, memory_storage, job_uuid, batch_size=1)
        if not result["has_more"]:
            break
    with ArtifactTestingSessionLocal() as db:
        complete_deletion(db, job_uuid)


def setup_client_scope(client: TestClient, root_token: str, tenant_id: str) -> tuple[str, str]:
    created_client = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "Deletion Client"},
    )
    assert created_client.status_code == 201, created_client.text
    client_id = created_client.json()["id"]
    custodian = client.post(
        f"/v1/clients/{client_id}/custodians",
        headers=auth(root_token),
        json={"display_name": "Deletion Custodian", "email_addresses": []},
    )
    assert custodian.status_code == 201, custodian.text
    storage = client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        headers=auth(root_token),
        json={"tenant_slug": "root"},
    )
    assert storage.status_code == 201, storage.text
    return client_id, custodian.json()["id"]


def test_collection_deletion_removes_database_rows_and_unshared_blob(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    tenant_id = str(root_admin.tenant_id)
    client_id, custodian_id = setup_client_scope(client, root_token, tenant_id)
    collection_id, item_id = create_collection_with_item(
        client,
        root_token,
        client_id=client_id,
        tenant_id=tenant_id,
        custodian_id=custodian_id,
        name="Delete me",
    )
    assert sum(len(objects) for objects in memory_storage.buckets.values()) == 1

    response = client.delete(f"/v1/collections/{collection_id}", headers=auth(root_token))
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status"] == "QUEUED"
    assert job["item_count"] == 1
    assert job["artifact_count"] == 1
    assert client.get(f"/v1/collections/{collection_id}", headers=auth(root_token)).json()["status"] == "DELETING"

    run_deletion(job["id"])

    assert client.get(f"/v1/collections/{collection_id}", headers=auth(root_token)).status_code == 404
    status_response = client.get(f"/v1/collection-deletions/{job['id']}", headers=auth(root_token))
    assert status_response.status_code == 200, status_response.text
    completed = status_response.json()
    assert completed["status"] == "COMPLETED"
    assert completed["deleted_item_count"] == 1
    assert completed["deleted_artifact_count"] == 1
    assert completed["deleted_blob_count"] == 1
    assert sum(len(objects) for objects in memory_storage.buckets.values()) == 0
    with ArtifactTestingSessionLocal() as db:
        assert db.get(ClientCollection, uuid.UUID(collection_id)) is None
        assert db.get(CollectionItem, uuid.UUID(item_id)) is None
        assert db.scalar(select(func.count()).select_from(Artifact)) == 0
        assert db.scalar(select(func.count()).select_from(ContentBlob)) == 0


def test_collection_deletion_preserves_blob_shared_by_another_collection(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    tenant_id = str(root_admin.tenant_id)
    client_id, custodian_id = setup_client_scope(client, root_token, tenant_id)
    first_id, _ = create_collection_with_item(
        client,
        root_token,
        client_id=client_id,
        tenant_id=tenant_id,
        custodian_id=custodian_id,
        name="First collection",
    )
    second_id, _ = create_collection_with_item(
        client,
        root_token,
        client_id=client_id,
        tenant_id=tenant_id,
        custodian_id=custodian_id,
        name="Second collection",
    )
    assert sum(len(objects) for objects in memory_storage.buckets.values()) == 1

    first_job = client.delete(f"/v1/collections/{first_id}", headers=auth(root_token)).json()
    run_deletion(first_job["id"])
    assert sum(len(objects) for objects in memory_storage.buckets.values()) == 1
    with ArtifactTestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ContentBlob)) == 1

    second_job = client.delete(f"/v1/collections/{second_id}", headers=auth(root_token)).json()
    run_deletion(second_job["id"])
    assert sum(len(objects) for objects in memory_storage.buckets.values()) == 0


def test_collection_deletion_is_blocked_by_matter_documents_and_active_imports(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    tenant_id = str(root_admin.tenant_id)
    client_id, custodian_id = setup_client_scope(client, root_token, tenant_id)
    collection_id, item_id = create_collection_with_item(
        client,
        root_token,
        client_id=client_id,
        tenant_id=tenant_id,
        custodian_id=custodian_id,
        name="Referenced collection",
    )
    matter = client.post(
        f"/v1/clients/{client_id}/matters",
        headers=auth(root_token),
        json={"name": "Referenced matter"},
    )
    assert matter.status_code == 201, matter.text
    import_response = client.post(
        f"/v1/matters/{matter.json()['id']}/document-imports",
        headers=auth(root_token),
        json={
            "source_collection_id": collection_id,
            "selection": {
                "mode": "EXPLICIT",
                "q": None,
                "custodian_ids": [],
                "file_extensions": [],
                "record_types": [],
                "processing_statuses": [],
                "item_ids": [item_id],
            },
            "selection_summary": "Referenced document",
        },
    )
    assert import_response.status_code == 202, import_response.text
    blocked = client.delete(f"/v1/collections/{collection_id}", headers=auth(root_token))
    assert blocked.status_code == 409
    assert "active matter import" in blocked.json()["error"]["message"]

    with TestingSessionLocal() as db:
        import_job = db.get(MatterDocumentImportJob, uuid.UUID(import_response.json()["id"]))
        import_job.status = "COMPLETED"
        db.add(
            MatterDocument(
                matter_id=uuid.UUID(matter.json()["id"]),
                source_collection_id=uuid.UUID(collection_id),
                collection_item_id=uuid.UUID(item_id),
                added_by_import_job_id=import_job.id,
            )
        )
        db.commit()

    blocked = client.delete(f"/v1/collections/{collection_id}", headers=auth(root_token))
    assert blocked.status_code == 409
    assert "matter document" in blocked.json()["error"]["message"]
