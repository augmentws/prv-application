import uuid
from datetime import datetime, timezone

from conftest import ArtifactTestingSessionLocal, TestingSessionLocal
from sqlalchemy import delete, func, select

from app import matter_imports
from app.artifact_gateway import SelectionBatchItem, SelectionCustodian
from app.models import (
    DocumentMetadataCurrent,
    MatterDocumentCustodian,
    MatterDocumentImportBatch,
    MatterDocumentImportJob,
    MetadataEvent,
)
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
                "file_date_from": "2020-01-01T00:00:00Z",
                "file_date_to": "2020-12-31T23:59:59.999Z",
                "item_ids": [],
            },
            "selection_summary": "PDF contracts",
        },
        headers=headers,
    )
    assert created_job.status_code == 202, created_job.text
    assert created_job.json()["status"] == "QUEUED"
    assert created_job.json()["selection_summary"] == "PDF contracts"
    assert created_job.json()["selection"]["file_date_from"] == "2020-01-01T00:00:00Z"
    assert created_job.json()["selection"]["file_date_to"] == "2020-12-31T23:59:59.999000Z"
    assert created_job.json()["execution"]["job_type"] == "MATTER_DOCUMENT_IMPORT"
    assert created_job.json()["execution"]["attempt_number"] == 1
    assert created_job.json()["execution"]["current_stage"] == "QUEUED"
    assert created_job.json()["execution"]["available_actions"] == ["CANCEL"]
    jobs = client.get(f"/v1/matters/{matter_id}/document-imports", headers=headers)
    assert jobs.status_code == 200
    assert [job["id"] for job in jobs.json()] == [created_job.json()["id"]]

    canceled = client.post(
        f"/v1/matters/{matter_id}/document-imports/{created_job.json()['id']}/cancel",
        headers=headers,
    )
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "CANCELED"
    assert canceled.json()["execution"]["status"] == "CANCELED"

    history = client.get(
        f"/v1/jobs/MATTER_DOCUMENT_IMPORT/{created_job.json()['id']}/execution",
        headers=headers,
    )
    assert history.status_code == 200, history.text
    assert history.json()["current_execution"]["status"] == "CANCELED"
    assert len(history.json()["attempts"]) == 1

    second_job = client.post(
        f"/v1/matters/{matter_id}/document-imports",
        json={
            "source_collection_id": collection.json()["id"],
            "selection": {
                "mode": "QUERY",
                "q": None,
                "custodian_ids": [],
                "file_extensions": [],
                "record_types": [],
                "processing_statuses": [],
                "file_date_from": None,
                "file_date_to": None,
                "item_ids": [],
            },
            "selection_summary": "All collection documents",
        },
        headers=headers,
    )
    assert second_job.status_code == 202, second_job.text
    common_cancel = client.post(
        f"/v1/jobs/MATTER_DOCUMENT_IMPORT/{second_job.json()['id']}/cancel",
        headers=headers,
    )
    assert common_cancel.status_code == 200, common_cancel.text
    assert common_cancel.json()["status"] == "CANCELED"


def test_document_import_read_reconciles_durable_failure_and_batch_totals(
    client,
    root_admin,
    db,
    monkeypatch,
) -> None:
    login = client.post(
        "/v1/auth/login",
        json={"email": "root@example.com", "password": "correct-horse-battery-staple"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    created_client = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/clients",
        json={"name": "Import Reconciliation Client"},
        headers=headers,
    ).json()
    matter = client.post(
        f"/v1/clients/{created_client['id']}/matters",
        json={"name": "Import Reconciliation Matter"},
        headers=headers,
    ).json()
    client.post(
        f"/v1/tenants/{root_admin.tenant_id}/artifact-storage/ensure",
        json={"tenant_slug": "root"},
        headers=headers,
    )
    collection = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/clients/{created_client['id']}/collections",
        json={"name": "Import Reconciliation Collection", "description": None},
        headers=headers,
    ).json()
    created = client.post(
        f"/v1/matters/{matter['id']}/document-imports",
        json={
            "source_collection_id": collection["id"],
            "selection": {"mode": "EXPLICIT", "item_ids": [str(uuid.uuid4())]},
            "selection_summary": "Reconciliation fixture",
        },
        headers=headers,
    ).json()
    job = db.get(MatterDocumentImportJob, uuid.UUID(created["id"]))
    assert job is not None
    job.status = "RUNNING"
    job.matched_count = 10
    job.batch_count = 1
    db.add(
        MatterDocumentImportBatch(
            job_id=job.id,
            batch_number=0,
            status="COMPLETED",
            item_count=10,
            added_count=8,
            duplicate_count=2,
            imported_label_document_count=4,
            imported_label_event_count=6,
            imported_label_warning_count=1,
        )
    )
    db.commit()
    completed_at = datetime(2026, 10, 9, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "app.routers.matter_imports._durable_terminal_imports",
        lambda _settings, workflow_ids: {
            workflow_id: ("ERROR", "Import worker exhausted retries", completed_at)
            for workflow_id in workflow_ids
        },
    )

    response = client.get(
        f"/v1/matters/{matter['id']}/document-imports/{job.id}",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "FAILED"
    assert response.json()["processed_count"] == 10
    assert response.json()["added_count"] == 8
    assert response.json()["duplicate_count"] == 2
    assert response.json()["imported_label_event_count"] == 6
    assert response.json()["error_message"] == "Import worker exhausted retries"
    assert response.json()["execution"]["status"] == "FAILED"
    assert response.json()["execution"]["progress_completed"] == 10


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
    matter_custodians = client.get(f"/v1/matters/{matter['id']}/custodians", headers=headers)
    assert matter_custodians.status_code == 200, matter_custodians.text
    assert [custodian["display_name"] for custodian in matter_custodians.json()] == [
        "Alex Primary",
        "Casey Common",
    ]
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


def test_matter_publication_creates_idempotent_imported_metadata_labels(
    client,
    root_admin,
    monkeypatch,
) -> None:
    monkeypatch.setattr(matter_imports, "SessionLocal", TestingSessionLocal)
    login = client.post(
        "/v1/auth/login",
        json={"email": "root@example.com", "password": "correct-horse-battery-staple"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    created_client = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/clients",
        json={"name": "Imported Labels Client"},
        headers=headers,
    ).json()
    matter = client.post(
        f"/v1/clients/{created_client['id']}/matters",
        json={"name": "Imported Labels Matter"},
        headers=headers,
    ).json()
    definition_response = client.post(
        f"/v1/matters/{matter['id']}/metadata-definitions",
        headers=headers,
        json={
            "key": "external_topics",
            "display_name": "External topics",
            "type": "TEXT",
            "cardinality": "MULTIPLE",
            "facetable": True,
        },
    )
    assert definition_response.status_code == 201, definition_response.text
    definition = definition_response.json()
    single_definition_response = client.post(
        f"/v1/matters/{matter['id']}/metadata-definitions",
        headers=headers,
        json={
            "key": "external_flag",
            "display_name": "External flag",
            "type": "BOOLEAN",
            "cardinality": "SINGLE",
        },
    )
    assert single_definition_response.status_code == 201, single_definition_response.text
    client.post(
        f"/v1/tenants/{root_admin.tenant_id}/artifact-storage/ensure",
        json={"tenant_slug": "root"},
        headers=headers,
    )
    collection = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/clients/{created_client['id']}/collections",
        json={"name": "Imported label source"},
        headers=headers,
    ).json()

    def create_job() -> dict:
        response = client.post(
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
                "selection_summary": "Imported label document",
            },
            headers=headers,
        )
        assert response.status_code == 202, response.text
        return response.json()

    item_id = uuid.uuid4()
    first_job = create_job()
    first = matter_imports.process_batch(
        uuid.UUID(first_job["id"]),
        0,
        [
            SelectionBatchItem(
                item_id=item_id,
                custodians=[],
                unmapped_metadata={
                    "External_Topics": ["george w. bush", "marketing", "marketing"],
                    "External_Flag": [True],
                },
            )
        ],
    )
    assert first.added_count == 1
    assert first.imported_label_document_count == 1
    assert first.imported_label_event_count == 3
    assert first.imported_label_warning_count == 0

    documents = client.get(
        f"/v1/matters/{matter['id']}/documents",
        headers=headers,
    ).json()
    document_id = documents[0]["id"]
    history = client.get(
        f"/v1/matters/{matter['id']}/documents/{document_id}/coding-history",
        headers=headers,
    )
    assert history.status_code == 200, history.text
    assert {entry["source_kind"] for entry in history.json()["direct"]} == {"IMPORT"}
    assert {entry["value"] for entry in history.json()["direct"]} == {
        "george w. bush",
        "marketing",
        True,
    }

    with TestingSessionLocal() as db:
        assert db.scalar(
            select(func.count()).select_from(MetadataEvent).where(
                MetadataEvent.matter_document_id == uuid.UUID(document_id),
                MetadataEvent.metadata_definition_id == uuid.UUID(definition["id"]),
                MetadataEvent.source_type == "IMPORT",
            )
        ) == 2
        current = list(
            db.scalars(
                select(DocumentMetadataCurrent).where(
                    DocumentMetadataCurrent.matter_document_id == uuid.UUID(document_id),
                    DocumentMetadataCurrent.metadata_definition_id == uuid.UUID(definition["id"]),
                )
            )
        )
        assert {row.value_text for row in current} == {"george w. bush", "marketing"}

    second_job = create_job()
    second = matter_imports.process_batch(
        uuid.UUID(second_job["id"]),
        0,
        [
            SelectionBatchItem(
                item_id=item_id,
                custodians=[],
                unmapped_metadata={
                    "external_topics": ["george w. bush", "marketing"],
                    "external_flag": [True],
                },
            )
        ],
    )
    assert second.added_count == 0
    assert second.duplicate_count == 1
    assert second.imported_label_event_count == 0
    with TestingSessionLocal() as db:
        assert db.scalar(
            select(func.count()).select_from(MetadataEvent).where(
                MetadataEvent.matter_document_id == uuid.UUID(document_id),
                MetadataEvent.metadata_definition_id == uuid.UUID(definition["id"]),
                MetadataEvent.source_type == "IMPORT",
            )
        ) == 2
