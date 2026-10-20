import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.analysis_task_batch_sets as batch_sets
from app.analysis_task_skills import ensure_standard_analysis_task_skills
from app.artifact_gateway import CollectionItemGroupingSnapshot
from app.models import (
    AnalysisTaskBatchSet,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    MatterDocument,
    ReviewBatch,
    ReviewBatchDocument,
    ReviewBatchRun,
    SearchIndexGeneration,
    Tenant,
    WorkflowRun,
)
from tests.test_agents_and_matter_definitions import auth, create_tenant_context
from tests.test_analysis_tasks import _specification
from tests.test_review_batches import add_documents


def _published_partitioned_task(client: TestClient, token: str, matter_id: str) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "partitioned_review",
            "name": "Partitioned review",
            "task_type": "QUESTION_ANSWERING",
            "definition_markdown": "# Issue\nReview the issue.",
            "scope_policy": {
                "allowed_modes": ["AD_HOC_BATCH", "PARTITIONED_SET"],
                "default_mode": "PARTITIONED_SET",
            },
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    response = client.put(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/specification",
        headers=auth(token),
        json={"decision_specification": _specification(task["version"]["id"])},
    )
    assert response.status_code == 200, response.text
    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/publish",
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_partitioned_batch_set_materializes_exact_non_overlapping_children(
    client: TestClient,
    root_token: str,
    db: Session,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _published_partitioned_task(client, token, matter_id)
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    document_ids = add_documents(matter_id, user_id, count=5)

    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets",
        headers=auth(token),
        json={
            "name": "First partition",
            "population_source_type": "ALL_MATTER",
            "target_batch_size": 2,
        },
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == "READY"
    assert payload["population_count"] == 5
    assert payload["batch_count"] == 3
    assert [child["document_count"] for child in payload["children"]] == [2, 2, 1]
    assert task["version"]["scope_policy"]["default_mode"] == "PARTITIONED_SET"

    batch_set_id = uuid.UUID(payload["id"])
    memberships = list(
        db.execute(
            select(
                ReviewBatchDocument.matter_document_id,
                func.count(ReviewBatchDocument.review_batch_id),
            )
            .where(ReviewBatchDocument.analysis_task_batch_set_id == batch_set_id)
            .group_by(ReviewBatchDocument.matter_document_id)
        )
    )
    assert {str(document_id) for document_id, _ in memberships} == set(document_ids)
    assert {count for _, count in memberships} == {1}
    children = list(
        db.scalars(
            select(ReviewBatch)
            .where(ReviewBatch.analysis_task_batch_set_id == batch_set_id)
            .order_by(ReviewBatch.batch_set_ordinal)
        )
    )
    assert [child.selection_type for child in children] == ["TASK_BATCH_PARTITION"] * 3
    first_document_id = uuid.UUID(document_ids[0])
    owning_child_id = db.scalar(
        select(ReviewBatchDocument.review_batch_id).where(
            ReviewBatchDocument.analysis_task_batch_set_id == batch_set_id,
            ReviewBatchDocument.matter_document_id == first_document_id,
        )
    )
    other_child = next(child for child in children if child.id != owning_child_id)
    db.add(
        ReviewBatchDocument(
            review_batch_id=other_child.id,
            matter_document_id=first_document_id,
            analysis_task_batch_set_id=batch_set_id,
            sequence_number=99,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_partitioned_batch_set_requires_opted_in_published_version(
    client: TestClient,
    root_token: str,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "ordinary_review",
            "name": "Ordinary review",
            "task_type": "QUESTION_ANSWERING",
            "definition_markdown": "# Issue\nReview the issue.",
        },
    )
    assert response.status_code == 201
    task = response.json()
    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets",
        headers=auth(token),
        json={
            "name": "Not allowed",
            "population_source_type": "ALL_MATTER",
            "target_batch_size": 100,
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "Analysis Task has no published version"


def test_batch_set_preview_is_read_only(
    client: TestClient,
    root_token: str,
    db: Session,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _published_partitioned_task(client, token, matter_id)
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    add_documents(matter_id, user_id, count=5)

    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets/preview",
        headers=auth(token),
        json={
            "population_source_type": "ALL_MATTER",
            "target_batch_size": 2,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["population_count"] == 5
    assert response.json()["estimated_batch_count"] == 3
    assert response.json()["estimated_batch_sizes"] == [2, 2, 1]
    assert response.json()["warnings"] == []
    assert db.scalar(select(func.count()).select_from(AnalysisTaskBatchSet)) == 0


def test_failed_batch_set_can_retry_with_a_new_execution(
    client: TestClient,
    root_token: str,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _published_partitioned_task(client, token, matter_id)
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    add_documents(matter_id, user_id, count=3)
    def fail_grouping_snapshots(**_: object) -> dict[uuid.UUID, CollectionItemGroupingSnapshot]:
        raise batch_sets.AnalysisTaskBatchSetError("Temporary family lookup failure")

    monkeypatch.setattr(batch_sets, "get_collection_item_grouping_snapshots", fail_grouping_snapshots)

    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets",
        headers=auth(token),
        json={
            "name": "Retry partition",
            "population_source_type": "ALL_MATTER",
            "target_batch_size": 2,
            "keep_together": ["FAMILY"],
        },
    )
    assert response.status_code == 202, response.text
    failed = response.json()
    assert failed["status"] == "FAILED"

    monkeypatch.setattr(
        batch_sets,
        "get_collection_item_grouping_snapshots",
        lambda **kwargs: {
            item_id: CollectionItemGroupingSnapshot(
                item_id=item_id,
                family_id=None,
                record_type="file",
                native_content_hash=None,
            )
            for item_id in kwargs["item_ids"]
        },
    )
    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets/{failed['id']}/retry",
        headers=auth(token),
    )

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "READY"
    assert response.json()["population_count"] == 3
    executions = list(
        db.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.job_id == uuid.UUID(failed["id"]))
            .order_by(WorkflowRun.attempt_number)
        )
    )
    assert [execution.attempt_number for execution in executions] == [1, 2]
    assert executions[1].retry_of_workflow_run_id == executions[0].id


def test_queued_batch_set_can_be_canceled(
    client: TestClient,
    root_token: str,
    db: Session,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task_payload = _published_partitioned_task(client, token, matter_id)
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    matter = db.get(Matter, uuid.UUID(matter_id))
    task = db.get(MatterAnalysisTask, uuid.UUID(task_payload["id"]))
    task_version = db.get(MatterAnalysisTaskVersion, uuid.UUID(task_payload["version"]["id"]))
    assert matter is not None and task is not None and task_version is not None
    batch_set = batch_sets.create_batch_set(
        db,
        matter=matter,
        task=task,
        task_version=task_version,
        name="Cancelable partition",
        description=None,
        population_source_type="ALL_MATTER",
        source_review_batch_id=None,
        search=None,
        saved_search_id=None,
        saved_search_name=None,
        saved_search_updated_at=None,
        target_batch_size=2,
        keep_together=[],
        random_seed=None,
        actor_user_id=user_id,
    )
    db.commit()

    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task.id}/batch-sets/{batch_set.id}/cancel",
        headers=auth(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELED"
    db.expire_all()
    execution = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.job_id == batch_set.id,
            WorkflowRun.attempt_number == 1,
        )
    )
    assert execution is not None
    assert execution.status == "CANCELED"
    assert execution.cancel_requested_by_user_id == user_id


def test_family_keep_together_and_oversized_warning(
    client: TestClient,
    root_token: str,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _published_partitioned_task(client, token, matter_id)
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    document_ids = add_documents(matter_id, user_id, count=5)
    documents = list(
        db.scalars(
            select(MatterDocument)
            .where(MatterDocument.id.in_([uuid.UUID(value) for value in document_ids]))
            .order_by(MatterDocument.created_at, MatterDocument.id)
        )
    )
    family_id = uuid.uuid4()

    def grouping_snapshots(**kwargs: object) -> dict[uuid.UUID, CollectionItemGroupingSnapshot]:
        item_ids = kwargs["item_ids"]
        assert isinstance(item_ids, list)
        return {
            item_id: CollectionItemGroupingSnapshot(
                item_id=item_id,
                family_id=family_id if index < 3 else None,
                record_type="file",
                native_content_hash=None,
            )
            for index, item_id in enumerate(item_ids)
        }

    monkeypatch.setattr(batch_sets, "get_collection_item_grouping_snapshots", grouping_snapshots)

    preview = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets/preview",
        headers=auth(token),
        json={
            "population_source_type": "ALL_MATTER",
            "target_batch_size": 2,
            "keep_together": ["FAMILY"],
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["estimated_batch_sizes"] == [3, 1, 1]
    assert preview.json()["oversized_groups"] == [
        {
            "code": "OVERSIZED_FAMILY",
            "family_id": str(family_id),
            "document_count": 3,
            "target_batch_size": 2,
        }
    ]

    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets",
        headers=auth(token),
        json={
            "name": "Family partition",
            "population_source_type": "ALL_MATTER",
            "target_batch_size": 2,
            "keep_together": ["FAMILY"],
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "READY"
    assert [child["document_count"] for child in response.json()["children"]] == [3, 1, 1]
    family_child_ids = set(
        db.scalars(
            select(ReviewBatchDocument.review_batch_id).where(
                ReviewBatchDocument.analysis_task_batch_set_id == uuid.UUID(response.json()["id"]),
                ReviewBatchDocument.matter_document_id.in_([document.id for document in documents[:3]]),
            )
        )
    )
    assert len(family_child_ids) == 1


def test_search_and_saved_search_populations_pin_generation_and_query(
    client: TestClient,
    root_token: str,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _published_partitioned_task(client, token, matter_id)
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    document_ids = add_documents(matter_id, user_id, count=4)
    generation = SearchIndexGeneration(
        matter_id=uuid.UUID(matter_id),
        generation=7,
        index_name=f"matter-{matter_id}-000007",
        alias_name=f"matter-{matter_id}",
        schema_hash="a" * 64,
        status="ACTIVE",
        document_count=4,
        schema_snapshot={},
        activated_at=datetime.now(timezone.utc),
    )
    db.add(generation)
    db.commit()

    class FakeOpenSearchClient:
        def __init__(self, _settings: object) -> None:
            pass

        def search(self, index_name: str, body: dict) -> dict:
            assert index_name == generation.index_name
            assert body["sort"] == [{"document_id": "asc"}]
            return {
                "hits": {
                    "hits": [
                        {"_source": {"document_id": document_id}, "sort": [document_id]}
                        for document_id in document_ids[:3]
                    ]
                }
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr(batch_sets, "OpenSearchClient", FakeOpenSearchClient)
    direct = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets",
        headers=auth(token),
        json={
            "name": "Keyword partition",
            "population_source_type": "SEARCH_QUERY",
            "search": {"query": "contract", "search_mode": "KEYWORD"},
            "target_batch_size": 2,
        },
    )
    assert direct.status_code == 202, direct.text
    assert direct.json()["status"] == "READY"
    assert direct.json()["population_count"] == 3
    assert direct.json()["search_index_generation_id"] == str(generation.id)
    assert direct.json()["selection_definition"]["search"]["query"] == "contract"
    assert direct.json()["selection_definition"]["search_index_generation"]["generation"] == 7

    saved = client.post(
        f"/v1/matters/{matter_id}/saved-searches",
        headers=auth(token),
        json={
            "name": "Contract documents",
            "visibility": "PRIVATE",
            "search": {"query": "contract", "search_mode": "KEYWORD"},
        },
    )
    assert saved.status_code == 201, saved.text
    preview = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets/preview",
        headers=auth(token),
        json={
            "population_source_type": "SAVED_SEARCH",
            "saved_search_id": saved.json()["id"],
            "target_batch_size": 2,
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["estimated_batch_sizes"] == [2, 1]

    created = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets",
        headers=auth(token),
        json={
            "name": "Saved partition",
            "population_source_type": "SAVED_SEARCH",
            "saved_search_id": saved.json()["id"],
            "target_batch_size": 2,
        },
    )
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "READY"
    assert created.json()["selection_definition"]["saved_search_id"] == saved.json()["id"]
    assert created.json()["selection_definition"]["saved_search_name"] == "Contract documents"


def test_child_search_projection_failure_completes_set_with_warning(
    client: TestClient,
    root_token: str,
    db: Session,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task_payload = _published_partitioned_task(client, token, matter_id)
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    add_documents(matter_id, user_id, count=2)
    matter = db.get(Matter, uuid.UUID(matter_id))
    task = db.get(MatterAnalysisTask, uuid.UUID(task_payload["id"]))
    task_version = db.get(MatterAnalysisTaskVersion, uuid.UUID(task_payload["version"]["id"]))
    assert matter is not None and task is not None and task_version is not None
    batch_set = batch_sets.create_batch_set(
        db,
        matter=matter,
        task=task,
        task_version=task_version,
        name="Projection warning",
        description=None,
        population_source_type="ALL_MATTER",
        source_review_batch_id=None,
        search=None,
        saved_search_id=None,
        saved_search_name=None,
        saved_search_updated_at=None,
        target_batch_size=2,
        keep_together=[],
        random_seed=None,
        actor_user_id=user_id,
    )
    db.commit()
    settings = batch_sets.get_settings().model_copy(update={"search_enabled": False})

    batch_sets.materialize_batch_set(db, batch_set.id, settings)
    child = db.scalar(select(ReviewBatch).where(ReviewBatch.analysis_task_batch_set_id == batch_set.id))
    assert child is not None
    child.search_status = "FAILED"
    child.search_error_message = "OpenSearch unavailable"
    db.commit()
    completed = batch_sets.complete_batch_set_materialization(db, batch_set.id)
    db.commit()

    assert completed.status == "READY"
    assert completed.warning_details == [
        {
            "code": "SEARCH_PROJECTION_FAILED",
            "child_batch_ids": [str(child.id)],
            "child_count": 1,
        }
    ]
    execution = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.job_id == batch_set.id,
            WorkflowRun.attempt_number == 1,
        )
    )
    assert execution is not None
    assert execution.status == "COMPLETED_WITH_ERRORS"


def test_managed_child_run_uses_pinned_retired_version_and_records_set_provenance(
    client: TestClient,
    root_token: str,
    root_admin,
    db: Session,
) -> None:
    root_tenant = db.get(Tenant, root_admin.tenant_id)
    assert root_tenant is not None
    ensure_standard_analysis_task_skills(db, root_tenant, root_admin)
    db.commit()
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _published_partitioned_task(client, token, matter_id)
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    add_documents(matter_id, user_id, count=2)
    created = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets",
        headers=auth(token),
        json={
            "name": "Pinned run partition",
            "population_source_type": "ALL_MATTER",
            "target_batch_size": 2,
        },
    )
    assert created.status_code == 202, created.text
    batch_set = created.json()
    child = batch_set["children"][0]
    task_version = db.get(MatterAnalysisTaskVersion, uuid.UUID(task["version"]["id"]))
    assert task_version is not None
    task_version.status = "RETIRED"
    db.commit()

    response = client.post(
        f"/v1/matters/{matter_id}/review-batches/{child['id']}/analysis-runs",
        headers=auth(token),
        json={
            "matter_analysis_task_id": task["id"],
            "matter_analysis_task_version_id": task["version"]["id"],
        },
    )

    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["analysis_task_batch_set_id"] == batch_set["id"]
    assert payload["batch_set_ordinal"] == 1
    assert payload["configuration_snapshot"]["analysis_task_batch_set_id"] == batch_set["id"]
    run = db.get(ReviewBatchRun, uuid.UUID(payload["id"]))
    assert run is not None
    assert run.analysis_task_batch_set_id == uuid.UUID(batch_set["id"])
    assert run.batch_set_ordinal == 1
    workflow = db.get(WorkflowRun, run.workflow_run_record_id)
    assert workflow is not None
    assert workflow.input_snapshot["task_version_id"] == task["version"]["id"]
    assert workflow.input_snapshot["analysis_task_batch_set_id"] == batch_set["id"]
