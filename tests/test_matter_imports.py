import uuid

from conftest import ArtifactTestingSessionLocal, TestingSessionLocal
from sqlalchemy import delete, func, select

from app import matter_imports
from app.artifact_gateway import SelectionBatchItem, SelectionCustodian
from app.models import MatterDocumentCustodian
from artifact_service.models import CollectionItem, CollectionItemCustodian
from scripts.backfill_matter_document_custodians import backfill_matter_document_custodians


def test_create_list_and_cancel_matter_document_import(client, root_admin) -> None:
    login = client.post(
        "/v1/auth/login",
        json={"email": "root@example.com", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    created_client = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/clients",
        json={"name": "Import Client"},
        headers=headers,
    )
    assert created_client.status_code == 201, created_client.text
    client_id = created_client.json()["id"]
    matter = client.post(
        f"/v1/clients/{client_id}/matters",
        json={"name": "Import Matter"},
        headers=headers,
    )
    assert matter.status_code == 201, matter.text
    matter_id = matter.json()["id"]
    overview = client.get(f"/v1/matters/{matter_id}/overview-counts", headers=headers)
    assert overview.status_code == 200
    assert overview.json() == {"document_count": 0, "custodian_count": 0}
    storage = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/artifact-storage/ensure",
        json={"tenant_slug": "root"},
        headers=headers,
    )
    assert storage.status_code == 201, storage.text
    collection = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/clients/{client_id}/collections",
        json={"name": "Source collection", "description": None},
        headers=headers,
    )
    assert collection.status_code == 201, collection.text

    created_job = client.post(
        f"/v1/matters/{matter_id}/document-imports",
        json={
            "source_collection_id": collection.json()["id"],
            "selection": {
                "mode": "QUERY",
                "q": "contract",
                "custodian_ids": [],
                "file_extensions": ["pdf"],
                "record_types": [],
                "processing_statuses": [],
                "item_ids": [],
            },
            "selection_summary": "PDF contracts",
        },
        headers=headers,
    )
    assert created_job.status_code == 202, created_job.text
    assert created_job.json()["status"] == "QUEUED"
    assert created_job.json()["selection_summary"] == "PDF contracts"

    jobs = client.get(f"/v1/matters/{matter_id}/document-imports", headers=headers)
    assert jobs.status_code == 200
    assert [job["id"] for job in jobs.json()] == [created_job.json()["id"]]

    canceled = client.post(
        f"/v1/matters/{matter_id}/document-imports/{created_job.json()['id']}/cancel",
        headers=headers,
    )
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "CANCELED"


def test_collection_selection_is_idempotent_and_removable(client, root_admin) -> None:
    login = client.post(
        "/v1/auth/login",
        json={"email": "root@example.com", "password": "correct-horse-battery-staple"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    created_client = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/clients",
        json={"name": "Selection Client"},
        headers=headers,
    )
    client_id = created_client.json()["id"]
    client.post(
        f"/v1/tenants/{root_admin.tenant_id}/artifact-storage/ensure",
        json={"tenant_slug": "root"},
        headers=headers,
    )
    collection = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/clients/{client_id}/collections",
        json={"name": "Selection source", "description": None},
        headers=headers,
    ).json()
    request_id = "102db148-5c28-4745-aaec-a4a6ff933ee0"
    payload = {
        "request_id": request_id,
        "mode": "QUERY",
        "q": None,
        "custodian_ids": [],
        "file_extensions": [],
        "record_types": [],
        "processing_statuses": [],
        "item_ids": [],
    }
    first = client.post(f"/v1/collections/{collection['id']}/selections", json=payload, headers=headers)
    second = client.post(f"/v1/collections/{collection['id']}/selections", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["total_count"] == 0

    removed = client.delete(f"/v1/collection-selections/{first.json()['id']}", headers=headers)
    assert removed.status_code == 204


def test_imported_document_and_custodian_counts_are_core_aggregates(client, root_admin, monkeypatch) -> None:
    monkeypatch.setattr(matter_imports, "SessionLocal", TestingSessionLocal)
    login = client.post(
        "/v1/auth/login",
        json={"email": "root@example.com", "password": "correct-horse-battery-staple"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    created_client = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/clients",
        json={"name": "Overview Count Client"},
        headers=headers,
    ).json()
    matter = client.post(
        f"/v1/clients/{created_client['id']}/matters",
        json={"name": "Overview Count Matter"},
        headers=headers,
    ).json()
    custodians = [
        client.post(
            f"/v1/clients/{created_client['id']}/custodians",
            json={"display_name": name, "email_addresses": [], "external_reference": None},
            headers=headers,
        ).json()
        for name in ("Alex Primary", "Casey Common")
    ]
    client.post(
        f"/v1/tenants/{root_admin.tenant_id}/artifact-storage/ensure",
        json={"tenant_slug": "root"},
        headers=headers,
    )
    collection = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/clients/{created_client['id']}/collections",
        json={"name": "Count source", "description": None},
        headers=headers,
    ).json()
    job_response = client.post(
        f"/v1/matters/{matter['id']}/document-imports",
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
            "selection_summary": "Two selected documents",
        },
        headers=headers,
    )
    assert job_response.status_code == 202, job_response.text
    job = job_response.json()
    item_ids = [uuid.uuid4(), uuid.uuid4()]
    result = matter_imports.process_batch(
        uuid.UUID(job["id"]),
        0,
        [
            SelectionBatchItem(
                item_id=item_ids[0],
                custodians=[
                    SelectionCustodian(
                        custodian_id=uuid.UUID(custodians[0]["id"]),
                        relationship_type="PRIMARY",
                    )
                ],
            ),
            SelectionBatchItem(
                item_id=item_ids[1],
                custodians=[
                    SelectionCustodian(
                        custodian_id=uuid.UUID(custodians[0]["id"]),
                        relationship_type="PRIMARY",
                    ),
                    SelectionCustodian(
                        custodian_id=uuid.UUID(custodians[1]["id"]),
                        relationship_type="COMMON",
                    ),
                ],
            ),
        ],
    )
    assert result.added_count == 2
    assert result.duplicate_count == 0

    overview = client.get(f"/v1/matters/{matter['id']}/overview-counts", headers=headers)
    assert overview.status_code == 200
    assert overview.json() == {"document_count": 2, "custodian_count": 2}
    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(MatterDocumentCustodian)) == 3

    with ArtifactTestingSessionLocal() as artifact_db:
        for index, item_id in enumerate(item_ids):
            artifact_db.add(
                CollectionItem(
                    id=item_id,
                    tenant_id=root_admin.tenant_id,
                    client_id=uuid.UUID(created_client["id"]),
                    collection_id=uuid.UUID(collection["id"]),
                    source_item_id=f"backfill-item-{index}",
                    record_type="FILE",
                    original_filename=f"item-{index}.pdf",
                    processing_status="READY",
                    raw_metadata={},
                    unmapped_metadata={},
                )
            )
        artifact_db.flush()
        artifact_db.add_all(
            [
                CollectionItemCustodian(
                    collection_item_id=item_ids[0],
                    custodian_id=uuid.UUID(custodians[0]["id"]),
                    relationship_type="PRIMARY",
                ),
                CollectionItemCustodian(
                    collection_item_id=item_ids[1],
                    custodian_id=uuid.UUID(custodians[0]["id"]),
                    relationship_type="PRIMARY",
                ),
                CollectionItemCustodian(
                    collection_item_id=item_ids[1],
                    custodian_id=uuid.UUID(custodians[1]["id"]),
                    relationship_type="COMMON",
                ),
            ]
        )
        artifact_db.commit()
    with TestingSessionLocal() as db:
        db.execute(delete(MatterDocumentCustodian))
        db.commit()

    backfill = backfill_matter_document_custodians(
        core_session_factory=TestingSessionLocal,
        artifact_session_factory=ArtifactTestingSessionLocal,
        batch_size=1,
    )
    assert backfill.documents_examined == 2
    assert backfill.relationships_added == 3
    rerun = backfill_matter_document_custodians(
        core_session_factory=TestingSessionLocal,
        artifact_session_factory=ArtifactTestingSessionLocal,
        batch_size=1,
    )
    assert rerun.relationships_added == 0
