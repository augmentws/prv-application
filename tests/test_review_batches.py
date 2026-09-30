import hashlib
import uuid
from datetime import datetime, timezone

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
    ReviewBatchDocument,
    ReviewBatchRun,
    ReviewBatchRunValue,
    ReviewBatchSearchCodingRun,
    SearchIndexGeneration,
    WorkflowRun,
)
from app.schemas import MatterFacetValuesResponse, MatterSearchResponse


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
    history = client.get(
        f"{base}/{batch['id']}/documents/{document_ids[0]}/coding-history",
        headers=auth(root_token),
    )
    assert history.status_code == 200, history.text
    assert history.json() == [
        {
            "review_batch_run_id": review_run.json()["id"],
            "run_type": "HUMAN",
            "purpose": "REVIEW",
            "metadata_definition_id": str(definition_id),
            "field_key": "key_document",
            "field_display_name": "Key document",
            "value": True,
            "value_label": "Yes",
            "recorded_at": history.json()[0]["recorded_at"],
            "source_kind": "HUMAN",
            "source_label": root_admin.email,
            "source_detail": root_admin.display_name,
            "score": None,
            "score_kind": None,
            "question_key": None,
            "review_decision_result_id": None,
        }
    ]
    document_history = client.get(
        f"/v1/matters/{matter_id}/documents/{document_ids[0]}/coding-history",
        headers=auth(root_token),
    )
    assert document_history.status_code == 200, document_history.text
    assert document_history.json()["direct"] == []
    assert document_history.json()["batches"][0]["review_batch_id"] == batch["id"]
    assert document_history.json()["batches"][0]["batch_name"] == "All documents"
    assert document_history.json()["batches"][0]["entries"] == history.json()
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
    coding_groups = client.post(
        f"{base}/{random_batch.json()['id']}/coding-groups",
        headers=auth(root_token),
        json={"coding_group_ids": [str(group_id)]},
    )
    assert coding_groups.status_code == 200, coding_groups.text
    assert [group["source_metadata_group_id"] for group in coding_groups.json()["coding_groups"]] == [
        str(group_id)
    ]
    repeated_assignment = client.post(
        f"{base}/{random_batch.json()['id']}/coding-groups",
        headers=auth(root_token),
        json={"coding_group_ids": [str(group_id)]},
    )
    assert repeated_assignment.status_code == 200, repeated_assignment.text
    assert len(repeated_assignment.json()["coding_groups"]) == 1
    removed_assignment = client.put(
        f"{base}/{random_batch.json()['id']}/coding-groups",
        headers=auth(root_token),
        json={"coding_group_ids": []},
    )
    assert removed_assignment.status_code == 200, removed_assignment.text
    assert removed_assignment.json()["coding_groups"] == []

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
    missing_saved_search = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(root_token),
        json={"name": "Missing saved search", "selection_type": "RANDOM_SAVED_SEARCH", "sample_size": 10},
    )
    assert missing_saved_search.status_code == 422
    missing_sample_size = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(root_token),
        json={
            "name": "Missing sample size",
            "selection_type": "RANDOM_SAVED_SEARCH",
            "saved_search_id": str(uuid.uuid4()),
        },
    )
    assert missing_sample_size.status_code == 422


def test_random_saved_search_batch_snapshots_and_samples_matching_documents(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    document_ids = add_documents(matter_id, root_admin.id, count=5)
    eligible_ids = document_ids[:4]
    with TestingSessionLocal() as db:
        generation = SearchIndexGeneration(
            matter_id=uuid.UUID(matter_id),
            generation=1,
            index_name=f"matter-{matter_id}-000001",
            alias_name=f"matter-{matter_id}",
            schema_hash="a" * 64,
            status="ACTIVE",
            document_count=len(document_ids),
            schema_snapshot={},
            activated_at=datetime.now(timezone.utc),
        )
        db.add(generation)
        db.commit()
        generation_id = str(generation.id)

    class FakeOpenSearchClient:
        def __init__(self, _settings):
            pass

        def search(self, index_name, body):
            assert index_name == f"matter-{matter_id}-000001"
            assert body["size"] == 500
            return {
                "hits": {
                    "hits": [
                        {"_source": {"document_id": document_id}, "sort": [document_id]}
                        for document_id in eligible_ids
                    ]
                }
            }

        def close(self):
            pass

    monkeypatch.setattr("app.review_batches.OpenSearchClient", FakeOpenSearchClient)
    saved = client.post(
        f"/v1/matters/{matter_id}/saved-searches",
        headers=auth(root_token),
        json={
            "name": "Insurance correspondence",
            "visibility": "PRIVATE",
            "search": {"query": "insurance", "search_mode": "KEYWORD"},
        },
    )
    assert saved.status_code == 201, saved.text
    seed = "saved-search-sample"
    created = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(root_token),
        json={
            "name": "Saved search sample",
            "selection_type": "RANDOM_SAVED_SEARCH",
            "saved_search_id": saved.json()["id"],
            "sample_size": 2,
            "random_seed": seed,
        },
    )
    assert created.status_code == 202, created.text
    batch = created.json()
    assert batch["status"] == "READY"
    assert batch["document_count"] == 2
    assert batch["sample_size"] == 2
    assert batch["random_seed"] == seed
    assert batch["search_index_generation_id"] == generation_id
    assert batch["selection_definition"]["saved_search_id"] == saved.json()["id"]
    assert batch["selection_definition"]["saved_search_name"] == "Insurance correspondence"
    assert batch["selection_definition"]["search"]["query"] == "insurance"
    assert batch["selection_definition"]["search_index_generation"]["generation"] == 1

    expected = sorted(
        eligible_ids,
        key=lambda document_id: hashlib.sha256(f"{seed}:{document_id}".encode()).digest(),
    )[:2]
    with TestingSessionLocal() as db:
        actual = [
            str(document_id)
            for document_id in db.scalars(
                select(ReviewBatchDocument.matter_document_id)
                .where(ReviewBatchDocument.review_batch_id == uuid.UUID(batch["id"]))
                .order_by(ReviewBatchDocument.sequence_number)
            )
        ]
    assert actual == expected


def test_workflow_review_run_has_workflow_owner(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    add_documents(matter_id, root_admin.id, count=1)
    base = f"/v1/matters/{matter_id}/review-batches"
    created = client.post(
        base,
        headers=auth(root_token),
        json={"name": "Assessment batch", "selection_type": "ALL_MATTER"},
    )
    assert created.status_code == 202, created.text
    batch_id = uuid.UUID(created.json()["id"])

    with TestingSessionLocal() as db:
        workflow_run = WorkflowRun(
            tenant_id=root_admin.tenant_id,
            matter_id=uuid.UUID(matter_id),
            workflow_key="matter_definition_assessment_v1",
            code_version="1",
            dbos_workflow_id=f"test-assessment:{uuid.uuid4()}",
            status="QUEUED",
            input_snapshot={"matter_id": matter_id},
            binding_snapshot={},
            configuration_snapshot={},
            progress={},
            initiated_by_user_id=root_admin.id,
        )
        db.add(workflow_run)
        db.flush()
        run = ReviewBatchRun(
            review_batch_id=batch_id,
            run_type="WORKFLOW",
            purpose="ASSESSMENT",
            status="RUNNING",
            result_policy="ISOLATED",
            actor_user_id=None,
            agent_definition_version_id=None,
            workflow_run_record_id=workflow_run.id,
            configuration_snapshot={},
            initiated_by_user_id=root_admin.id,
        )
        db.add(run)
        db.commit()
        run_id = run.id
        workflow_run_id = workflow_run.id

    listed = client.get(f"{base}/{batch_id}/runs", headers=auth(root_token))
    assert listed.status_code == 200, listed.text
    payload = next(item for item in listed.json() if item["id"] == str(run_id))
    assert payload["run_type"] == "WORKFLOW"
    assert payload["purpose"] == "ASSESSMENT"
    assert payload["workflow_run_record_id"] == str(workflow_run_id)
    assert payload["actor_user_id"] is None
    assert payload["agent_definition_version_id"] is None

    interactive = client.post(
        f"{base}/{batch_id}/runs",
        headers=auth(root_token),
        json={"run_type": "WORKFLOW", "purpose": "ASSESSMENT"},
    )
    assert interactive.status_code == 422


def test_batch_search_injects_authoritative_membership_filter(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    matter_id = create_matter(client, root_token, str(root_admin.tenant_id))
    document_id = add_documents(matter_id, root_admin.id, count=1)[0]
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

    with TestingSessionLocal() as db:
        definition = db.scalar(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == uuid.UUID(matter_id),
                MetadataDefinition.key == "key_document",
            )
        )
        assert definition is not None
        responsiveness_definition = db.scalar(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == uuid.UUID(matter_id),
                MetadataDefinition.key == "responsiveness",
            )
        )
        assert responsiveness_definition is not None
        run = ReviewBatchRun(
            review_batch_id=uuid.UUID(batch_id),
            run_type="HUMAN",
            purpose="REFERENCE",
            status="COMPLETED",
            result_policy="ISOLATED",
            actor_user_id=root_admin.id,
            configuration_snapshot={},
            initiated_by_user_id=root_admin.id,
            processed_document_count=1,
        )
        db.add(run)
        db.flush()
        db.add(
            ReviewBatchRunValue(
                review_batch_run_id=run.id,
                matter_document_id=uuid.UUID(document_id),
                metadata_definition_id=definition.id,
                value_ordinal=0,
                value_boolean=True,
                confidence=0.91,
                confidence_kind="SELECTED_PROBABILITY",
            )
        )
        db.add(
            ReviewBatchRunValue(
                review_batch_run_id=run.id,
                matter_document_id=uuid.UUID(document_id),
                metadata_definition_id=responsiveness_definition.id,
                value_ordinal=0,
                value_text="responsive",
                confidence=0.86,
                confidence_kind="SELECTED_PROBABILITY",
            )
        )
        db.add(
            ReviewBatchSearchCodingRun(
                review_batch_id=uuid.UUID(batch_id),
                review_batch_run_id=run.id,
                status="READY",
                selected_by_user_id=root_admin.id,
            )
        )
        db.commit()
        run_id = str(run.id)
        definition_id = str(definition.id)
        responsiveness_definition_id = str(responsiveness_definition.id)

    response = client.post(
        f"{base}/{batch_id}/search",
        headers=auth(root_token),
        json={
            "search_mode": "KEYWORD",
            "coding_filters": [
                {"field": "key_document", "values": [True], "minimum_confidence": 0.8}
            ],
        },
    )
    assert response.status_code == 200, response.text
    coding_filter = captured["required_filters"][1]["nested"]
    assert coding_filter["path"] == "batch_coding"
    assert coding_filter["query"]["bool"]["filter"] == [
        {"term": {"batch_coding.batch_id": batch_id}},
        {"term": {"batch_coding.run_id": run_id}},
        {"term": {"batch_coding.field_id": definition_id}},
        {"terms": {"batch_coding.value_boolean": [True]}},
        {"range": {"batch_coding.confidence": {"gte": 0.8}}},
    ]

    facet_capture: dict = {}

    def fake_coding_facets(_matter, _payload, **kwargs):
        facet_capture.update(kwargs)
        return MatterFacetValuesResponse(
            field="key_document",
            values=[{"value": True, "count": 1}],
        )

    monkeypatch.setattr(
        "app.routers.review_batches.execute_matter_batch_coding_facets",
        fake_coding_facets,
    )
    response = client.post(
        f"{base}/{batch_id}/coding-facets/key_document/values",
        headers=auth(root_token),
        json={
            "search_mode": "KEYWORD",
            "coding_filters": [{"field": "responsiveness", "values": ["responsive"]}],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["values"] == [{"value": True, "count": 1}]
    assert facet_capture["required_filters"][1]["nested"]["query"]["bool"]["filter"][:3] == [
        {"term": {"batch_coding.batch_id": batch_id}},
        {"term": {"batch_coding.run_id": run_id}},
        {"term": {"batch_coding.field_id": responsiveness_definition_id}},
    ]
