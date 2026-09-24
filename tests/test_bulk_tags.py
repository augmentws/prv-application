import uuid
from datetime import datetime, timezone

from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import bulk_tags
from app.models import (
    Client,
    DocumentMetadataCurrent,
    Matter,
    MatterBulkTagBatch,
    MatterBulkTagJob,
    MatterDocument,
    MatterDocumentImportJob,
    MetadataDefinition,
    MetadataEvent,
    SearchIndexGeneration,
    User,
)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_bulk_tag_job_requires_a_search_and_freezes_its_request(
    client: TestClient,
    root_token: str,
    root_admin: User,
    db: Session,
) -> None:
    account = Client(tenant_id=root_admin.tenant_id, name="Bulk Tag Client", status="ACTIVE")
    db.add(account)
    db.flush()
    matter = Matter(client_id=account.id, name="Bulk Tag Matter", status="ACTIVE")
    db.add(matter)
    db.flush()
    definition = MetadataDefinition(
        matter_id=matter.id,
        key="responsiveness",
        display_name="Responsiveness",
        type="ENUM",
        cardinality="SINGLE",
        allowed_values=[{"key": "responsive", "label": "Responsive", "active": True}],
        value_source="ASSERTED",
        assertion_policy="IMMEDIATE",
        resolution_policy="LATEST_VALID",
        searchable=True,
        facetable=True,
        reviewable=True,
        ai_assignable=True,
        status="ACTIVE",
    )
    db.add(definition)
    issue_definition = MetadataDefinition(
        matter_id=matter.id,
        key="issue",
        display_name="Issue",
        type="ENUM",
        cardinality="MULTIPLE",
        allowed_values=[
            {"key": "pricing", "label": "Pricing", "active": True},
            {"key": "competition", "label": "Competition", "active": True},
        ],
        value_source="ASSERTED",
        assertion_policy="IMMEDIATE",
        resolution_policy="LATEST_VALID",
        searchable=True,
        facetable=True,
        reviewable=True,
        ai_assignable=True,
        status="ACTIVE",
    )
    db.add(issue_definition)
    generation = SearchIndexGeneration(
        matter_id=matter.id,
        generation=1,
        index_name=f"matter-{matter.id}-000001",
        alias_name=f"matter-{matter.id}",
        schema_hash="a" * 64,
        status="ACTIVE",
        schema_snapshot={},
        activated_at=datetime.now(timezone.utc),
    )
    db.add(generation)
    db.commit()

    empty = client.post(
        f"/v1/matters/{matter.id}/bulk-tag-jobs",
        headers=auth(root_token),
        json={
            "search": {"query": None, "filters": [], "offset": 100, "size": 10},
            "assignments": [
                {"metadata_definition_id": str(definition.id), "value": "responsive"},
                {"metadata_definition_id": str(issue_definition.id), "value": ["pricing", "competition"]},
            ],
        },
    )
    assert empty.status_code == 422

    multiple_values_for_single_field = client.post(
        f"/v1/matters/{matter.id}/bulk-tag-jobs",
        headers=auth(root_token),
        json={
            "search": {"query": "price coordination", "filters": []},
            "metadata_definition_id": str(definition.id),
            "value": ["responsive", "responsive"],
        },
    )
    assert multiple_values_for_single_field.status_code == 422

    created = client.post(
        f"/v1/matters/{matter.id}/bulk-tag-jobs",
        headers=auth(root_token),
        json={
            "search": {
                "query": "price coordination",
                "filters": [],
                "facets": ["responsiveness"],
                "offset": 100,
                "size": 10,
            },
            "assignments": [
                {"metadata_definition_id": str(definition.id), "value": "responsive"},
                {"metadata_definition_id": str(issue_definition.id), "value": ["pricing", "competition"]},
            ],
        },
    )
    assert created.status_code == 202, created.text
    payload = created.json()
    assert payload["status"] == "QUEUED"
    assert payload["metadata_definition_id"] == str(definition.id)
    assert payload["assignments"] == [
        {"metadata_definition_id": str(definition.id), "value": "responsive"},
        {"metadata_definition_id": str(issue_definition.id), "value": ["pricing", "competition"]},
    ]
    assert payload["search_index_generation_id"] == str(generation.id)
    assert payload["search_index_snapshot"]["index_name"] == generation.index_name
    assert payload["search_definition"]["query"] == "price coordination"
    assert payload["search_definition"]["offset"] == 0
    assert payload["search_definition"]["size"] == 500
    assert payload["search_definition"]["facets"] == []

    listed = client.get(f"/v1/matters/{matter.id}/bulk-tag-jobs", headers=auth(root_token))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [payload["id"]]


def test_bulk_tag_rejects_non_reviewable_and_semantic_scopes(
    client: TestClient,
    root_token: str,
    root_admin: User,
    db: Session,
) -> None:
    account = Client(tenant_id=root_admin.tenant_id, name="Restricted Tag Client", status="ACTIVE")
    db.add(account)
    db.flush()
    matter = Matter(client_id=account.id, name="Restricted Tag Matter", status="ACTIVE")
    db.add(matter)
    db.flush()
    definition = MetadataDefinition(
        id=uuid.uuid4(),
        matter_id=matter.id,
        key="system_value",
        display_name="System value",
        type="TEXT",
        cardinality="SINGLE",
        value_source="SYSTEM",
        assertion_policy="IMMEDIATE",
        resolution_policy="EXPLICIT_ONLY",
        searchable=True,
        facetable=False,
        reviewable=False,
        ai_assignable=False,
        status="ACTIVE",
    )
    db.add(definition)
    db.add(
        SearchIndexGeneration(
            matter_id=matter.id,
            generation=1,
            index_name=f"matter-{matter.id}-000001",
            alias_name=f"matter-{matter.id}",
            schema_hash="b" * 64,
            status="ACTIVE",
            schema_snapshot={},
        )
    )
    db.commit()

    response = client.post(
        f"/v1/matters/{matter.id}/bulk-tag-jobs",
        headers=auth(root_token),
        json={
            "search": {"query": "concept", "search_mode": "SEMANTIC"},
            "metadata_definition_id": str(definition.id),
            "value": "blocked",
        },
    )
    assert response.status_code == 422

    response = client.post(
        f"/v1/matters/{matter.id}/bulk-tag-jobs",
        headers=auth(root_token),
        json={
            "search": {"query": "system record", "search_mode": "KEYWORD"},
            "metadata_definition_id": str(definition.id),
            "value": "blocked",
        },
    )
    assert response.status_code == 422
    assert "active, reviewable asserted field" in response.json()["error"]["message"]


def test_bulk_tag_batch_uses_metadata_ledger_and_is_idempotent(
    root_admin: User,
    db: Session,
    monkeypatch,
) -> None:
    account = Client(tenant_id=root_admin.tenant_id, name="Ledger Tag Client", status="ACTIVE")
    db.add(account)
    db.flush()
    matter = Matter(client_id=account.id, name="Ledger Tag Matter", status="ACTIVE")
    db.add(matter)
    db.flush()
    definition = MetadataDefinition(
        matter_id=matter.id,
        key="issue",
        display_name="Issue",
        type="ENUM",
        cardinality="MULTIPLE",
        allowed_values=[
            {"key": "pricing", "label": "Pricing", "active": True},
            {"key": "competition", "label": "Competition", "active": True},
        ],
        value_source="ASSERTED",
        assertion_policy="IMMEDIATE",
        resolution_policy="LATEST_VALID",
        searchable=True,
        facetable=True,
        reviewable=True,
        ai_assignable=True,
        status="ACTIVE",
    )
    db.add(definition)
    responsiveness_definition = MetadataDefinition(
        matter_id=matter.id,
        key="responsiveness",
        display_name="Responsiveness",
        type="ENUM",
        cardinality="SINGLE",
        allowed_values=[{"key": "responsive", "label": "Responsive", "active": True}],
        value_source="ASSERTED",
        assertion_policy="IMMEDIATE",
        resolution_policy="LATEST_VALID",
        searchable=True,
        facetable=True,
        reviewable=True,
        ai_assignable=True,
        status="ACTIVE",
    )
    db.add(responsiveness_definition)
    generation = SearchIndexGeneration(
        matter_id=matter.id,
        generation=1,
        index_name=f"matter-{matter.id}-000001",
        alias_name=f"matter-{matter.id}",
        schema_hash="c" * 64,
        status="ACTIVE",
        schema_snapshot={},
    )
    db.add(generation)
    import_job = MatterDocumentImportJob(
        matter_id=matter.id,
        source_collection_id=uuid.uuid4(),
        selection_type="EXPLICIT",
        selection={"item_ids": []},
        selection_summary="Test import",
        status="COMPLETED",
        workflow_id=f"test-import:{uuid.uuid4()}",
        created_by_user_id=root_admin.id,
    )
    db.add(import_job)
    db.flush()
    documents = [
        MatterDocument(
            matter_id=matter.id,
            source_collection_id=import_job.source_collection_id,
            collection_item_id=uuid.uuid4(),
            added_by_import_job_id=import_job.id,
        )
        for _ in range(2)
    ]
    db.add_all(documents)
    db.flush()
    job = MatterBulkTagJob(
        matter_id=matter.id,
        metadata_definition_id=definition.id,
        search_index_generation_id=generation.id,
        search_index_snapshot={"generation": 1, "index_name": generation.index_name},
        search_definition={"query": "pricing", "search_mode": "KEYWORD"},
        value=["pricing", "competition"],
        assignments=[
            {"metadata_definition_id": str(definition.id), "value": ["pricing", "competition"]},
            {"metadata_definition_id": str(responsiveness_definition.id), "value": "responsive"},
        ],
        status="RUNNING",
        workflow_id=f"bulk-tag-test:{uuid.uuid4()}",
        matched_count=2,
        batch_count=1,
        created_by_user_id=root_admin.id,
    )
    db.add(job)
    db.flush()
    batch = MatterBulkTagBatch(
        job_id=job.id,
        batch_number=1,
        document_ids=[str(document.id) for document in documents],
        item_count=2,
    )
    db.add(batch)
    db.commit()

    monkeypatch.setattr(bulk_tags, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(bulk_tags, "_ensure_batch_search_operation", lambda *_: uuid.uuid4())
    monkeypatch.setattr(bulk_tags, "process_search_operation", lambda *_: None)

    first = bulk_tags.process_bulk_tag_batch(batch.id)
    second = bulk_tags.process_bulk_tag_batch(batch.id)
    assert first == second == {"processed_count": 2, "tagged_count": 2, "failed_count": 0}

    with TestingSessionLocal() as verification:
        assert verification.scalar(select(func.count(MetadataEvent.id))) == 6
        rows = list(
            verification.scalars(
                select(DocumentMetadataCurrent).where(
                    DocumentMetadataCurrent.metadata_definition_id == definition.id
                )
            )
        )
        assert len(rows) == 4
        assert {row.value_text for row in rows} == {"pricing", "competition"}
        responsiveness_rows = list(
            verification.scalars(
                select(DocumentMetadataCurrent).where(
                    DocumentMetadataCurrent.metadata_definition_id == responsiveness_definition.id
                )
            )
        )
        assert len(responsiveness_rows) == 2
        assert {row.value_text for row in responsiveness_rows} == {"responsive"}
