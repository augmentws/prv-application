import uuid

from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import (
    MatterDocument,
    MatterDocumentImportJob,
    MetadataDefinition,
    MetadataGroup,
    MetadataGroupField,
    ReviewBatch,
)
from app.schemas import MatterSearchResponse


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_matter(client: TestClient, root_token: str, tenant_id: str) -> str:
    created_client = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "Batch Client"},
    )
    assert created_client.status_code == 201, created_client.text
    matter = client.post(
        f"/v1/clients/{created_client.json()['id']}/matters",
        headers=auth(root_token),
        json={"name": "Batch Matter"},
    )
    assert matter.status_code == 201, matter.text
    return matter.json()["id"]


def add_documents(matter_id: str, user_id: uuid.UUID, count: int = 5) -> list[str]:
    with TestingSessionLocal() as db:
        job = MatterDocumentImportJob(
            matter_id=uuid.UUID(matter_id),
            source_collection_id=uuid.uuid4(),
            selection_type="EXPLICIT",
            selection={},
            selection_summary="Test documents",
            status="COMPLETED",
            workflow_id=f"test:{uuid.uuid4()}",
            created_by_user_id=user_id,
        )
        db.add(job)
        db.flush()
        documents = [
            MatterDocument(
                matter_id=uuid.UUID(matter_id),
                source_collection_id=job.source_collection_id,
                collection_item_id=uuid.uuid4(),
                added_by_import_job_id=job.id,
            )
            for _ in range(count)
        ]
        db.add_all(documents)
        db.commit()
        return [str(document.id) for document in documents]


def test_batch_membership_group_snapshot_runs_and_comparison(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    document_ids = add_documents(matter_id, root_admin.id)
    with TestingSessionLocal() as db:
        definition_id = db.scalar(
            select(MetadataDefinition.id).where(
                MetadataDefinition.matter_id == uuid.UUID(matter_id),
                MetadataDefinition.key == "key_document",
            )
        )
        group_id = db.scalar(
            select(MetadataGroup.id)
            .join(MetadataGroupField, MetadataGroupField.metadata_group_id == MetadataGroup.id)
            .where(
                MetadataGroup.matter_id == uuid.UUID(matter_id),
                MetadataGroupField.metadata_definition_id == definition_id,
            )
        )
    assert group_id is not None
    assert definition_id is not None

    base = f"/v1/matters/{matter_id}/review-batches"
    created = client.post(
        base,
        headers=auth(root_token),
        json={
            "name": "All documents",
            "description": "Human reference and agent evaluation corpus",
            "selection_type": "ALL_MATTER",
            "reviewer_value_visibility": "OWN_VALUES",
            "coding_group_ids": [str(group_id)],
            "note": "Frozen before the first agent run.",
        },
    )
    assert created.status_code == 202, created.text
    batch = created.json()
    assert batch["status"] == "READY"
    assert batch["search_status"] == "NOT_CONFIGURED"
    assert batch["document_count"] == len(document_ids)
    assert batch["coding_groups"]

    review_run = client.post(f"{base}/{batch['id']}/review-run", headers=auth(root_token))
    assert review_run.status_code == 200, review_run.text
    resumed = client.post(f"{base}/{batch['id']}/review-run", headers=auth(root_token))
    assert resumed.status_code == 200
    assert resumed.json()["id"] == review_run.json()["id"]
    documents = client.get(
        f"{base}/{batch['id']}/documents",
        headers=auth(root_token),
        params={"run_id": review_run.json()["id"]},
    )
    assert documents.status_code == 200, documents.text
    assert documents.json()[0]["collection_item_id"]
    review_value_payload = {
        "matter_document_id": document_ids[0],
        "fields": [{"metadata_definition_id": str(definition_id), "values": [True]}],
    }
    saved_review = client.put(
        f"{base}/{batch['id']}/runs/{review_run.json()['id']}/documents/{document_ids[0]}/values",
        headers=auth(root_token),
        json=review_value_payload,
    )
    assert saved_review.status_code == 200, saved_review.text
    coding = client.get(
        f"{base}/{batch['id']}/runs/{review_run.json()['id']}/documents/{document_ids[0]}",
        headers=auth(root_token),
    )
    assert coding.status_code == 200
    assert coding.json()["review_status"] == "COMPLETED"
    assert coding.json()["values"][0]["value"] is True
    progress = client.get(
        f"{base}/{batch['id']}/runs/{review_run.json()['id']}/progress",
        headers=auth(root_token),
    )
    assert progress.status_code == 200
    assert progress.json()["completed_count"] == 1
    assert progress.json()["not_started_count"] == len(document_ids) - 1

    random_batch = client.post(
        base,
        headers=auth(root_token),
        json={
            "name": "Random three",
            "selection_type": "RANDOM_BATCH",
            "source_batch_id": batch["id"],
            "sample_size": 3,
            "random_seed": "repeatable-seed",
        },
    )
    assert random_batch.status_code == 202, random_batch.text
    assert random_batch.json()["document_count"] == 3

    run_payload = {"run_type": "HUMAN", "purpose": "REFERENCE"}
    left = client.post(f"{base}/{batch['id']}/runs", headers=auth(root_token), json=run_payload)
    right = client.post(f"{base}/{batch['id']}/runs", headers=auth(root_token), json=run_payload)
    assert left.status_code == right.status_code == 201
    value_payload = {
        "matter_document_id": document_ids[0],
        "fields": [{"metadata_definition_id": str(definition_id), "values": [True]}],
    }
    for run in (left.json(), right.json()):
        response = client.put(
            f"{base}/{batch['id']}/runs/{run['id']}/documents/{document_ids[0]}/values",
            headers=auth(root_token),
            json=value_payload,
        )
        assert response.status_code == 200, response.text
        assert response.json()[0]["value"] is True
        completed = client.post(
            f"{base}/{batch['id']}/runs/{run['id']}/complete",
            headers=auth(root_token),
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "COMPLETED"

    comparison = client.get(
        f"{base}/{batch['id']}/comparisons",
        headers=auth(root_token),
        params={"left_run_id": left.json()["id"], "right_run_id": right.json()["id"]},
    )
    assert comparison.status_code == 200, comparison.text
    assert comparison.json()["fields"][0]["agreement"] == 1.0


def test_batch_selection_validation(client: TestClient, root_token: str, root_admin) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    response = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(root_token),
        json={"name": "Bad search batch", "selection_type": "SEARCH_QUERY"},
    )
    assert response.status_code == 422


def test_batch_search_injects_authoritative_membership_filter(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    add_documents(matter_id, root_admin.id, count=1)
    base = f"/v1/matters/{matter_id}/review-batches"
    created = client.post(
        base,
        headers=auth(root_token),
        json={"name": "Searchable batch", "selection_type": "ALL_MATTER"},
    )
    assert created.status_code == 202, created.text
    batch_id = created.json()["id"]
    with TestingSessionLocal() as db:
        batch = db.get(ReviewBatch, uuid.UUID(batch_id))
        assert batch is not None
        batch.search_status = "READY"
        db.commit()

    captured: dict = {}

    def fake_search(_matter, _payload, **kwargs):
        captured.update(kwargs)
        return MatterSearchResponse(total=0, took_ms=1, timed_out=False, hits=[], facets={})

    monkeypatch.setattr("app.routers.review_batches.execute_matter_search", fake_search)
    response = client.post(
        f"{base}/{batch_id}/search",
        headers=auth(root_token),
        json={"query": "agreement", "search_mode": "KEYWORD"},
    )
    assert response.status_code == 200, response.text
    assert captured["required_filters"] == [{"term": {"batch_ids": batch_id}}]
