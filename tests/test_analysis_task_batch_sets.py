import uuid
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.analysis_task_batch_sets as batch_sets
from app.analysis_task_batch_set_runs import complete_batch_set_run
from app.analysis_task_skills import ensure_standard_analysis_task_skills
from app.artifact_gateway import CollectionItemGroupingSnapshot
from app.job_executions import ANALYSIS_TASK_BATCH_SET_RUN_JOB_TYPE, transition_job_execution
from app.models import (
    AnalysisInputManifest,
    AnalysisTaskBatchSet,
    AnalysisTaskBatchSetRun,
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
from app.search.client import OpenSearchError
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


def _ready_batch_set(
    client: TestClient,
    token: str,
    matter_id: str,
    task_id: str,
    *,
    target_batch_size: int = 2,
) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task_id}/batch-sets",
        headers=auth(token),
        json={
            "name": "Run-all partition",
            "population_source_type": "ALL_MATTER",
            "target_batch_size": target_batch_size,
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "READY"
    return response.json()


def test_task_batch_query_quotas_scale_to_requested_discovery_population() -> None:
    quotas = [150, 50, 150, 120, 120, 120, 100, 100, 90]
    queries = [
        batch_sets.TaskBatchSearchQuery(
            ordinal=index,
            key=f"issue_{index}",
            label=f"Issue {index}",
            reason=f"issue_{index}",
            search_definition={"query": f"issue {index}", "search_mode": "HYBRID"},
            quota=quota,
        )
        for index, quota in enumerate(quotas, start=1)
    ]

    allocations = batch_sets.allocate_task_batch_query_quotas(queries, 5_000)

    assert allocations == [750, 250, 750, 600, 600, 600, 500, 500, 450]
    assert sum(allocations) == 5_000


def test_task_batch_search_uses_scaled_query_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter_id = uuid.uuid4()
    calls: list[int] = []

    monkeypatch.setattr(batch_sets, "task_batch_document_ids", lambda *args, **kwargs: ([], set()))

    def fake_search_document_ids(*args, **kwargs):
        limit = kwargs["limit"]
        calls.append(limit)
        return [uuid.uuid5(uuid.NAMESPACE_URL, f"query:{len(calls)}:document:{index}") for index in range(limit)]

    monkeypatch.setattr(batch_sets, "search_document_ids", fake_search_document_ids)
    population = batch_sets.select_task_batch_search_population(
        SimpleNamespace(),
        matter=SimpleNamespace(id=matter_id),
        task=SimpleNamespace(id=uuid.uuid4(), matter_id=matter_id),
        generation=SimpleNamespace(),
        queries=[
            batch_sets.TaskBatchSearchQuery(
                ordinal=1,
                key="issue_1",
                label="Issue 1",
                reason="issue_1",
                search_definition={"query": "first", "search_mode": "KEYWORD"},
                quota=2,
            ),
            batch_sets.TaskBatchSearchQuery(
                ordinal=2,
                key="issue_2",
                label="Issue 2",
                reason="issue_2",
                search_definition={"query": "second", "search_mode": "KEYWORD"},
                quota=1,
            ),
        ],
        target_document_count=6,
        settings=SimpleNamespace(),
    )

    assert calls == [4, 2]
    assert len(population.document_ids) == 6
    assert [item["requested"] for item in population.query_yields] == [4, 2]


def test_task_batch_search_identifies_query_that_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter_id = uuid.uuid4()
    monkeypatch.setattr(batch_sets, "task_batch_document_ids", lambda *args, **kwargs: ([], set()))
    monkeypatch.setattr(
        batch_sets,
        "search_document_ids",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OpenSearchError("OpenSearch request failed: timed out")
        ),
    )

    with pytest.raises(
        batch_sets.AnalysisTaskBatchSetError,
        match='Retrieval query "Issue 3: Regulatory Actions" failed: .*timed out',
    ):
        batch_sets.select_task_batch_search_population(
            SimpleNamespace(),
            matter=SimpleNamespace(id=matter_id),
            task=SimpleNamespace(id=uuid.uuid4(), matter_id=matter_id),
            generation=SimpleNamespace(),
            queries=[
                batch_sets.TaskBatchSearchQuery(
                    ordinal=1,
                    key="issue_3",
                    label="Issue 3: Regulatory Actions",
                    reason="issue_3",
                    search_definition={"query": "rate filing", "search_mode": "HYBRID"},
                    quota=100,
                )
            ],
            target_document_count=100,
            settings=SimpleNamespace(),
        )


def test_task_batch_selection_warns_when_retrieval_misses_requested_target() -> None:
    warnings = batch_sets._selection_warnings(
        {
            "review_program": {
                "target_document_count": 5_000,
                "selected_document_count": 4_250,
            }
        }
    )

    assert warnings == [
        {
            "code": "RETRIEVAL_TARGET_SHORTFALL",
            "message": "Retrieval selected 4250 of the requested 5000 documents.",
            "target_document_count": 5_000,
            "selected_document_count": 4_250,
            "shortfall_count": 750,
        }
    ]


def test_batch_set_run_creates_one_parent_with_ordered_child_references(
    client: TestClient,
    root_token: str,
    db: Session,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _published_partitioned_task(client, token, matter_id)
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    add_documents(matter_id, user_id, count=5)
    batch_set = _ready_batch_set(client, token, matter_id, task["id"])
    path = f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets/{batch_set['id']}/runs"
    payload = {"idempotency_key": "run-all-1", "max_concurrency": 2}

    response = client.post(path, headers=auth(token), json=payload)
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["status"] == "QUEUED"
    assert run["child_count"] == 3
    assert [child["ordinal"] for child in run["children"]] == [1, 2, 3]
    assert [child["review_batch_id"] for child in run["children"]] == [
        child["id"] for child in batch_set["children"]
    ]
    assert run["execution"]["status"] == "QUEUED"

    duplicate = client.post(path, headers=auth(token), json=payload)
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["id"] == run["id"]
    assert db.scalar(select(func.count()).select_from(AnalysisTaskBatchSetRun)) == 1


def test_input_manifest_population_preserves_frozen_order(
    client: TestClient,
    root_token: str,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task_payload = _published_partitioned_task(client, token, matter_id)
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    document_ids = [uuid.UUID(value) for value in add_documents(matter_id, user_id, count=3)]
    matter = db.get(Matter, uuid.UUID(matter_id))
    task = db.get(MatterAnalysisTask, uuid.UUID(task_payload["id"]))
    task_version = db.get(MatterAnalysisTaskVersion, uuid.UUID(task_payload["version"]["id"]))
    assert matter is not None and task is not None and task_version is not None
    frozen_order = [document_ids[2], document_ids[0], document_ids[1]]
    manifest = AnalysisInputManifest(
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        matter_id=matter.id,
        owner_type="TEST_BATCH_SET",
        owner_id=uuid.uuid4(),
        scope_type="TEST",
        scope_id=uuid.uuid4(),
        manifest_schema="analysis-input-manifest",
        manifest_schema_version=1,
        resolver_version="test-v1",
        input_contract={"key": "test", "version": 1},
        input_contract_hash="a" * 64,
        derivation_key="b" * 64,
        artifact_id=uuid.uuid4(),
        artifact_content_hash="c" * 64,
        artifact_byte_length=100,
        item_count=len(frozen_order),
        manifest_status="FINALIZED",
        producer_kind="TEST",
        producer_run_id=uuid.uuid4(),
        created_by_user_id=user_id,
        finalized_at=datetime.now(timezone.utc),
    )
    db.add(manifest)
    db.flush()

    class FakeReader:
        def __init__(self, _manifest, *, actor_user_id):
            assert actor_user_id == user_id

        def rows(self, start, count):
            assert (start, count) == (0, len(frozen_order))
            return [
                {"matter_document_id": str(document_id), "input_status": "READY"}
                for document_id in frozen_order
            ]

    monkeypatch.setattr(batch_sets, "AnalysisInputManifestReader", FakeReader)
    batch_set = batch_sets.create_batch_set(
        db,
        matter=matter,
        task=task,
        task_version=task_version,
        name="Frozen manifest population",
        description=None,
        population_source_type="INPUT_MANIFEST",
        source_review_batch_id=None,
        input_manifest_id=manifest.id,
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
    batch_sets.complete_batch_set_materialization(db, batch_set.id)
    db.commit()

    memberships = list(
        db.execute(
            select(ReviewBatch.batch_set_ordinal, ReviewBatchDocument.matter_document_id)
            .join(ReviewBatchDocument, ReviewBatchDocument.review_batch_id == ReviewBatch.id)
            .where(ReviewBatch.analysis_task_batch_set_id == batch_set.id)
            .order_by(ReviewBatch.batch_set_ordinal, ReviewBatchDocument.sequence_number)
        )
    )
    assert [row.matter_document_id for row in memberships] == frozen_order
    assert [row.batch_set_ordinal for row in memberships] == [1, 1, 2]


def test_batch_set_run_freezes_selected_execution_profile(
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
    profile_response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/execution-profiles",
        headers=auth(token),
        json={"key": "jev", "name": "Jev review model", "decision_provider": "JEV"},
    )
    assert profile_response.status_code == 201, profile_response.text
    profile = profile_response.json()
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    add_documents(matter_id, user_id, count=2)
    batch_set = _ready_batch_set(client, token, matter_id, task["id"])
    path = f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets/{batch_set['id']}/runs"

    response = client.post(
        path,
        headers=auth(token),
        json={"idempotency_key": "profile-run", "execution_profile_id": profile["id"]},
    )

    assert response.status_code == 202, response.text
    run = response.json()
    assert run["execution_profile_id"] == profile["id"]
    assert run["configuration_snapshot"]["execution_profile_id"] == profile["id"]
    assert run["configuration_snapshot"]["execution_profile_content_hash"] == profile["content_hash"]


def test_batch_set_run_partial_completion_retries_only_failed_children(
    client: TestClient,
    root_token: str,
    db: Session,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _published_partitioned_task(client, token, matter_id)
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    add_documents(matter_id, user_id, count=3)
    batch_set = _ready_batch_set(client, token, matter_id, task["id"])
    path = f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets/{batch_set['id']}/runs"
    response = client.post(path, headers=auth(token), json={"idempotency_key": "partial-1"})
    assert response.status_code == 202, response.text
    run = db.get(AnalysisTaskBatchSetRun, uuid.UUID(response.json()["id"]))
    assert run is not None
    completed_id = uuid.uuid4()
    failed_id = uuid.uuid4()
    run.children[0].status = "COMPLETED"
    run.children[0].run_type = "QUESTION_ANSWERING"
    run.children[0].child_run_id = completed_id
    run.children[1].status = "FAILED"
    run.children[1].run_type = "QUESTION_ANSWERING"
    run.children[1].child_run_id = failed_id
    run.children[1].error_message = "provider failed"
    transition_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_RUN_JOB_TYPE,
        job_id=run.id,
        status="RUNNING",
    )
    complete_batch_set_run(db, run.id)
    db.commit()

    retry = client.post(f"{path}/{run.id}/retry", headers=auth(token))
    assert retry.status_code == 202, retry.text
    retried = retry.json()
    assert retried["status"] == "QUEUED"
    assert retried["children"][0]["status"] == "COMPLETED"
    assert retried["children"][0]["child_run_id"] == str(completed_id)
    assert retried["children"][1]["status"] == "PENDING"
    assert retried["children"][1]["child_run_id"] is None


def test_cancel_batch_set_run_prevents_pending_child_launches(
    client: TestClient,
    root_token: str,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _published_partitioned_task(client, token, matter_id)
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    add_documents(matter_id, user_id, count=3)
    batch_set = _ready_batch_set(client, token, matter_id, task["id"])
    path = f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets/{batch_set['id']}/runs"
    created = client.post(path, headers=auth(token), json={"idempotency_key": "cancel-1"}).json()

    canceled = client.post(f"{path}/{created['id']}/cancel", headers=auth(token))
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "CANCELED"
    assert {child["status"] for child in canceled.json()["children"]} == {"CANCELED"}


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
        input_manifest_id=None,
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

    query_vector = [0.25] * 32
    embedded: list[tuple[list[str], str]] = []
    search_calls: list[tuple[dict, str | None]] = []
    ensured_pipelines: list[str] = []
    hybrid_relevance_ids = [uuid.uuid4() for _ in range(501)]
    transient_failures = [0]

    class FakeQueryEmbeddingGateway:
        def embed(self, inputs: list[str], input_type: str) -> SimpleNamespace:
            embedded.append((inputs, input_type))
            return SimpleNamespace(embeddings=[query_vector])

    class FakeOpenSearchClient:
        def __init__(self, _settings: object) -> None:
            pass

        def ensure_rrf_search_pipeline(self, pipeline_id: str) -> None:
            ensured_pipelines.append(pipeline_id)

        def search(self, index_name: str, body: dict, *, search_pipeline: str | None = None) -> dict:
            assert index_name == generation.index_name
            search_calls.append((deepcopy(body), search_pipeline))
            if transient_failures[0]:
                transient_failures[0] -= 1
                raise OpenSearchError("OpenSearch request failed: timed out")
            if search_pipeline and "sort" not in body:
                offset = int(body.get("from", 0))
                page = hybrid_relevance_ids[offset : offset + int(body["size"])]
                return {
                    "hits": {
                        "hits": [
                            {"_source": {"document_id": str(document_id)}}
                            for document_id in page
                        ]
                    }
                }
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
    monkeypatch.setattr(batch_sets, "get_query_embedding_gateway", FakeQueryEmbeddingGateway)
    matter = db.get(Matter, uuid.UUID(matter_id))
    assert matter is not None
    for search_mode in ("SEMANTIC", "HYBRID"):
        assert batch_sets.search_document_ids(
            db,
            matter=matter,
            generation=generation,
            search_definition={"query": "concealed pricing", "search_mode": search_mode},
            settings=batch_sets.get_settings(),
        ) == [uuid.UUID(document_id) for document_id in document_ids[:3]]
    assert embedded == [
        (["concealed pricing"], "query"),
        (["concealed pricing"], "query"),
    ]
    semantic_body, semantic_pipeline = search_calls[-2]
    assert semantic_body["sort"] == [{"document_id": "asc"}]
    assert semantic_body["query"]["nested"]["query"]["knn"]["chunks.embedding"]["vector"] == query_vector
    assert semantic_pipeline is None
    hybrid_body, hybrid_pipeline = search_calls[-1]
    assert hybrid_body["sort"] == [{"document_id": "asc"}]
    assert hybrid_body["query"]["hybrid"]["queries"][1]["nested"]["query"]["knn"]["chunks.embedding"]["vector"] == query_vector
    assert ensured_pipelines == [batch_sets.RRF_SEARCH_PIPELINE]
    assert hybrid_pipeline == batch_sets.RRF_SEARCH_PIPELINE

    assert batch_sets.search_document_ids(
        db,
        matter=matter,
        generation=generation,
        search_definition={"query": "concealed pricing", "search_mode": "HYBRID", "size": 150},
        settings=batch_sets.get_settings(),
        limit=501,
        relevance_order=True,
        excluded_document_ids={uuid.UUID(document_ids[3])},
    ) == hybrid_relevance_ids
    relevance_calls = search_calls[-4:]
    first_relevance_body, first_relevance_pipeline = relevance_calls[0]
    second_relevance_body, second_relevance_pipeline = relevance_calls[1]
    final_relevance_body, final_relevance_pipeline = relevance_calls[-1]
    assert "sort" not in first_relevance_body
    assert first_relevance_body["from"] == 0
    assert first_relevance_body["size"] == 150
    assert first_relevance_body["query"]["hybrid"]["pagination_depth"] == 150
    keyword_exclusions = first_relevance_body["query"]["hybrid"]["queries"][0]["bool"]["filter"][-1]
    semantic_exclusions = first_relevance_body["query"]["hybrid"]["queries"][1]["nested"]["query"]["knn"]["chunks.embedding"]["filter"]["bool"]["filter"][-1]
    expected_exclusions = {"bool": {"must_not": [{"terms": {"document_id": [document_ids[3]]}}]}}
    assert keyword_exclusions == expected_exclusions
    assert semantic_exclusions == expected_exclusions
    assert first_relevance_pipeline == batch_sets.RRF_SEARCH_PIPELINE
    assert "sort" not in second_relevance_body
    assert second_relevance_body["from"] == 150
    assert second_relevance_body["query"]["hybrid"]["pagination_depth"] == 300
    assert second_relevance_pipeline == batch_sets.RRF_SEARCH_PIPELINE
    assert final_relevance_body["from"] == 450
    assert final_relevance_body["query"]["hybrid"]["pagination_depth"] == 600
    assert final_relevance_pipeline == batch_sets.RRF_SEARCH_PIPELINE

    assert batch_sets.search_document_ids(
        db,
        matter=matter,
        generation=generation,
        search_definition={"query": "concealed pricing", "search_mode": "HYBRID", "size": 150},
        settings=batch_sets.get_settings(),
        limit=150,
        relevance_order=True,
    ) == hybrid_relevance_ids[:150]
    bounded_relevance_body, _ = search_calls[-1]
    assert bounded_relevance_body["size"] == 150
    assert bounded_relevance_body["query"]["hybrid"]["pagination_depth"] == 150

    assert len(
        batch_sets.search_document_ids(
            db,
            matter=matter,
            generation=generation,
            search_definition={"query": "concealed pricing", "search_mode": "SEMANTIC"},
            settings=batch_sets.get_settings(),
            limit=250,
            relevance_order=True,
        )
    ) == 3
    semantic_population_body, _ = search_calls[-1]
    assert semantic_population_body["size"] == 250
    assert semantic_population_body["query"]["nested"]["query"]["knn"]["chunks.embedding"]["k"] == 250

    retry_sleeps: list[float] = []
    transient_failures[0] = 2
    monkeypatch.setattr(batch_sets.time, "sleep", retry_sleeps.append)
    assert len(
        batch_sets.search_document_ids(
            db,
            matter=matter,
            generation=generation,
            search_definition={"query": "concealed pricing", "search_mode": "SEMANTIC"},
            settings=batch_sets.get_settings(),
            limit=1,
            relevance_order=True,
        )
    ) == 1
    assert retry_sleeps == [0.25, 0.5]

    transient_failures[0] = 3
    with pytest.raises(OpenSearchError, match="after 3 attempts"):
        batch_sets.search_document_ids(
            db,
            matter=matter,
            generation=generation,
            search_definition={"query": "concealed pricing", "search_mode": "SEMANTIC"},
            settings=batch_sets.get_settings(),
            limit=1,
            relevance_order=True,
        )

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
    assert preview.status_code == 409, preview.text
    assert "does not allow a document to belong to more than one Batch Set" in preview.text

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
    assert created.json()["status"] == "FAILED"
    assert "does not allow a document to belong to more than one Batch Set" in created.json()["error_message"]
    assert created.json()["selection_definition"]["saved_search_id"] == saved.json()["id"]
    assert created.json()["selection_definition"]["saved_search_name"] == "Contract documents"

    direct_batch_set = db.get(AnalysisTaskBatchSet, uuid.UUID(direct.json()["id"]))
    assert direct_batch_set is not None
    direct_batch_set.status = "ARCHIVED"
    for child_batch in db.scalars(
        select(ReviewBatch).where(ReviewBatch.analysis_task_batch_set_id == direct_batch_set.id)
    ):
        child_batch.status = "ARCHIVED"
    db.commit()
    active_set_ids, active_document_ids = batch_sets.task_batch_document_ids(
        db,
        task_id=uuid.UUID(task["id"]),
    )
    assert direct_batch_set.id not in active_set_ids
    assert not active_document_ids
    visible_sets = client.get(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets",
        headers=auth(token),
    )
    assert visible_sets.status_code == 200, visible_sets.text
    assert str(direct_batch_set.id) not in {item["id"] for item in visible_sets.json()}
    archived_detail = client.get(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets/{direct_batch_set.id}",
        headers=auth(token),
    )
    assert archived_detail.status_code == 200, archived_detail.text
    assert archived_detail.json()["status"] == "ARCHIVED"

    released_preview = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets/preview",
        headers=auth(token),
        json={
            "population_source_type": "SAVED_SEARCH",
            "saved_search_id": saved.json()["id"],
            "target_batch_size": 2,
        },
    )
    assert released_preview.status_code == 200, released_preview.text


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
        input_manifest_id=None,
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
