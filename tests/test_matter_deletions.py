import hashlib
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from conftest import ArtifactTestingSessionLocal, TestingSessionLocal, create_client_tenant, memory_storage
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.matter_deletions import (
    complete_matter_deletion,
    delete_matter_database_rows,
    matter_deletion_blockers,
)
from app.models import Matter, MatterDeletionJob, SearchProjectionOperation
from app.workflows.matter_deletion import delete_search_indexes
from artifact_service.models import ContentBlob, WorkflowArtifact
from artifact_service.schemas import WorkflowArtifactScopeDelete
from artifact_service.workflow_artifacts import delete_workflow_artifact_scope_batch


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_matter(client: TestClient, token: str) -> tuple[str, str, str]:
    tenant_id = create_client_tenant(client, token)
    client_response = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(token),
        json={"name": "Deletion Client"},
    )
    assert client_response.status_code == 201, client_response.text
    client_id = client_response.json()["id"]
    matter_response = client.post(
        f"/v1/clients/{client_id}/matters",
        headers=auth(token),
        json={"name": "Delete Me"},
    )
    assert matter_response.status_code == 201, matter_response.text
    return tenant_id, client_id, matter_response.json()["id"]


def finish_initial_search_operation(matter_id: str) -> None:
    with TestingSessionLocal() as db:
        operations = db.scalars(
            select(SearchProjectionOperation).where(
                SearchProjectionOperation.matter_id == uuid.UUID(matter_id)
            )
        )
        for operation in operations:
            operation.status = "COMPLETED"
        db.commit()


def add_matter_workflow_artifact(
    *,
    tenant_id: str,
    client_id: str,
    matter_id: str,
    actor_user_id: uuid.UUID,
) -> uuid.UUID:
    content = b"matter export"
    digest = hashlib.sha256(content).hexdigest()
    bucket_name = "matter-deletion-test"
    storage_key = f"v1/blobs/{uuid.uuid4()}"
    memory_storage.ensure_bucket(bucket_name)
    memory_storage.buckets[bucket_name][storage_key] = content
    with ArtifactTestingSessionLocal() as db:
        blob = ContentBlob(
            tenant_id=uuid.UUID(tenant_id),
            sha256=digest,
            bucket_name=bucket_name,
            storage_key=storage_key,
            byte_length=len(content),
            media_type="text/csv",
        )
        db.add(blob)
        db.flush()
        artifact = WorkflowArtifact(
            tenant_id=uuid.UUID(tenant_id),
            client_id=uuid.UUID(client_id),
            core_scope_kind="MATTER",
            core_scope_id=uuid.UUID(matter_id),
            artifact_type="SEARCH_RESULT_EXPORT",
            schema_version=1,
            derivation_key=digest,
            content_blob_id=blob.id,
            content_hash=digest,
            media_type="text/csv",
            byte_length=len(content),
            producer_kind="CORE",
            producer_run_id=uuid.uuid4(),
            workflow_metadata={},
            created_by_user_id=actor_user_id,
        )
        db.add(artifact)
        db.commit()
        return artifact.id


def test_matter_deletion_reconciles_durable_search_failures(
    client: TestClient,
    root_token: str,
    monkeypatch,
) -> None:
    _tenant_id, _client_id, matter_id = create_matter(client, root_token)
    with TestingSessionLocal() as db:
        operation = db.scalar(
            select(SearchProjectionOperation).where(
                SearchProjectionOperation.matter_id == uuid.UUID(matter_id)
            )
        )
        assert operation is not None
        operation.status = "RUNNING"
        workflow_id = operation.workflow_id
        db.commit()

    completed_at = datetime.now(timezone.utc)
    durable = SimpleNamespace(
        workflow_id=workflow_id,
        status="ERROR",
        error="worker terminated",
        completed_at=int(completed_at.timestamp() * 1000),
    )
    dbos_client = SimpleNamespace(list_workflows=lambda **_kwargs: [durable])
    monkeypatch.setattr(
        "app.matter_deletions.get_settings",
        lambda: SimpleNamespace(dbos_enabled=True),
    )
    monkeypatch.setattr("app.matter_deletions.get_dbos_client", lambda: dbos_client)

    with TestingSessionLocal() as db:
        blockers = matter_deletion_blockers(db, uuid.UUID(matter_id))
        assert blockers.active_search_operation_count == 0
        operation = db.get(SearchProjectionOperation, operation.id)
        assert operation is not None
        assert operation.status == "FAILED"
        assert operation.error_message == "worker terminated"


def test_matter_deletion_does_not_block_on_search_confirmation(
    client: TestClient,
    root_token: str,
) -> None:
    _tenant_id, _client_id, matter_id = create_matter(client, root_token)
    with TestingSessionLocal() as db:
        operation = db.scalar(
            select(SearchProjectionOperation).where(
                SearchProjectionOperation.matter_id == uuid.UUID(matter_id)
            )
        )
        assert operation is not None
        operation.status = "AWAITING_USER"
        db.commit()

        blockers = matter_deletion_blockers(db, uuid.UUID(matter_id))
        assert blockers.active_search_operation_count == 0


def test_matter_deletion_blocks_active_work_then_purges_core_and_artifact_data(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch,
) -> None:
    tenant_id, client_id, matter_id = create_matter(client, root_token)
    artifact_id = add_matter_workflow_artifact(
        tenant_id=tenant_id,
        client_id=client_id,
        matter_id=matter_id,
        actor_user_id=root_admin.id,
    )

    blocked = client.delete(f"/v1/matters/{matter_id}", headers=auth(root_token))
    assert blocked.status_code == 409
    assert "active search operation" in blocked.json()["error"]["message"]

    finish_initial_search_operation(matter_id)
    queued = client.delete(f"/v1/matters/{matter_id}", headers=auth(root_token))
    assert queued.status_code == 202, queued.text
    job = queued.json()
    assert job["status"] == "QUEUED"
    assert job["artifact_count"] == 1
    assert client.get(f"/v1/matters/{matter_id}", headers=auth(root_token)).json()["status"] == "DELETING"

    deleted_indexes: list[str] = []

    class FakeOpenSearchClient:
        def __init__(self, _settings) -> None:
            pass

        def resolve_indices(self, pattern: str) -> list[str]:
            assert uuid.UUID(matter_id).hex in pattern
            return [f"matter-index-v{value:06d}" for value in (1, 2)]

        def delete_index(self, name: str) -> None:
            deleted_indexes.append(name)

        def close(self) -> None:
            pass

    monkeypatch.setattr("app.workflows.matter_deletion.SessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.workflows.matter_deletion.OpenSearchClient", FakeOpenSearchClient)
    assert delete_search_indexes(job["id"]) == 2
    assert deleted_indexes == ["matter-index-v000001", "matter-index-v000002"]

    scopes = [WorkflowArtifactScopeDelete(core_scope_kind="MATTER", core_scope_id=uuid.UUID(matter_id))]
    with ArtifactTestingSessionLocal() as artifact_db:
        result = delete_workflow_artifact_scope_batch(
            artifact_db,
            memory_storage,
            tenant_id=uuid.UUID(tenant_id),
            client_id=uuid.UUID(client_id),
            scopes=scopes,
        )
    assert result == {
        "matched_artifact_count": 1,
        "deleted_artifact_count": 1,
        "deleted_blob_count": 1,
        "has_more": False,
    }
    with TestingSessionLocal() as db:
        deletion = db.get(MatterDeletionJob, uuid.UUID(job["id"]))
        deletion.deleted_artifact_count = 1
        deletion.deleted_blob_count = 1
        db.commit()
        delete_matter_database_rows(db, deletion.id)
        complete_matter_deletion(db, deletion.id)

    assert client.get(f"/v1/matters/{matter_id}", headers=auth(root_token)).status_code == 404
    status_response = client.get(f"/v1/matter-deletions/{job['id']}", headers=auth(root_token))
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "COMPLETED"
    assert status_response.json()["deleted_index_count"] == 2
    with TestingSessionLocal() as db:
        assert db.get(Matter, uuid.UUID(matter_id)) is None
    with ArtifactTestingSessionLocal() as artifact_db:
        assert artifact_db.get(WorkflowArtifact, artifact_id) is None
        assert artifact_db.scalar(select(ContentBlob.id)) is None
    assert sum(len(objects) for objects in memory_storage.buckets.values()) == 0
