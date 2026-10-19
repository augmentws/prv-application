import hashlib
import io
import json
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from conftest import ArtifactTestingSessionLocal, create_client_tenant, memory_storage
from fastapi.testclient import TestClient

from app.artifact_gateway import (
    copy_workflow_artifact_content,
    find_workflow_artifact_reference,
    get_workflow_artifact_reference,
    store_classification_workflow_artifact,
)
from artifact_service.auth import ArtifactPrincipal, mint_artifact_delegation
from artifact_service.config import get_artifact_settings
from artifact_service.models import TenantStorage
from artifact_service.workflow_artifacts import WorkflowArtifactError, stage_workflow_upload
from classification_service.schemas import ArtifactType


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def parquet_bytes(label: str) -> bytes:
    return b"PAR1" + label.encode() + b"PAR1"


def package_bytes() -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", '{"package_schema_version":1}')
        archive.writestr("model.skops", b"safe-model-placeholder")
    return content.getvalue()


def content_for(artifact_type: ArtifactType) -> tuple[bytes, str]:
    if artifact_type == ArtifactType.SEARCH_RESULT_EXPORT:
        return b"Matter document ID,Original filename\r\n1,example.msg\r\n", "text/csv"
    if artifact_type == ArtifactType.FACT_EXPORT:
        return b"Fact ID,Summary\r\nf-1,Prices rose.\r\n", "text/csv"
    if artifact_type == ArtifactType.CLASSIFICATION_MODEL_PACKAGE:
        return package_bytes(), "application/zip"
    if artifact_type in {
        ArtifactType.CLASSIFICATION_EVALUATION,
        ArtifactType.CLASSIFICATION_RESULT_MANIFEST,
    }:
        return json.dumps({"artifact_type": artifact_type.value, "schema_version": 1}).encode(), "application/json"
    return parquet_bytes(artifact_type.value), "application/vnd.apache.parquet"


def workflow_metadata(
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    artifact_type: ArtifactType,
    content: bytes,
    derivation_key: str,
    scope_id: uuid.UUID | None = None,
    producer_run_id: uuid.UUID | None = None,
    lineage: list[dict] | None = None,
) -> dict:
    _, media_type = content_for(artifact_type)
    return {
        "tenant_id": str(tenant_id),
        "client_id": str(client_id),
        "core_scope_kind": "CLASSIFICATION_DATASET_SNAPSHOT",
        "core_scope_id": str(scope_id or uuid.uuid4()),
        "artifact_type": artifact_type.value,
        "schema_version": 1,
        "derivation_key": derivation_key,
        "content_hash": sha256(content),
        "byte_length": len(content),
        "media_type": media_type,
        "producer_kind": "CLASSIFICATION_WORKER",
        "producer_run_id": str(producer_run_id or uuid.uuid4()),
        "metadata": {"contract": "classification-v1"},
        "lineage": lineage or [],
    }


def provision_core_scope(client: TestClient, root_token: str, root_admin) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = create_client_tenant(client, root_token)
    client_response = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "Classification Artifact Client"},
    )
    assert client_response.status_code == 201, client_response.text
    client_id = uuid.UUID(client_response.json()["id"])
    storage = client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        headers=auth(root_token),
        json={"tenant_slug": "root"},
    )
    assert storage.status_code == 201, storage.text
    return tenant_id, client_id


def upload_workflow_artifact(
    client: TestClient,
    headers: dict[str, str],
    metadata: dict,
    content: bytes,
) -> object:
    return client.post(
        "/v1/workflow-artifacts:upload",
        headers=headers,
        data={"metadata": json.dumps(metadata)},
        files={"file": ("workflow-artifact.bin", content, metadata["media_type"])},
    )


def provision_artifact_storage(tenant_id: uuid.UUID) -> None:
    bucket_name = f"test-{tenant_id}"
    with ArtifactTestingSessionLocal() as db:
        db.add(
            TenantStorage(
                tenant_id=tenant_id,
                tenant_slug_snapshot="test",
                bucket_name=bucket_name,
                status="ACTIVE",
            )
        )
        db.commit()
    memory_storage.ensure_bucket(bucket_name)


def delegated_headers(
    *,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> dict[str, str]:
    principal = ArtifactPrincipal(
        actor_user_id=actor_user_id,
        allowed_tenant_ids=frozenset({tenant_id}),
        allowed_clients=frozenset({(tenant_id, client_id)}),
        allowed_custodians=frozenset(),
    )
    token = mint_artifact_delegation(principal, get_artifact_settings())
    return auth(token)


def test_all_classification_workflow_artifact_types_are_stored_and_resolved(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    tenant_id, client_id = provision_core_scope(client, root_token, root_admin)
    scope_id = uuid.uuid4()
    producer_run_id = uuid.uuid4()

    for index, artifact_type in enumerate(ArtifactType):
        content, _ = content_for(artifact_type)
        derivation_key = f"{index + 1:064x}"
        metadata = workflow_metadata(
            tenant_id=tenant_id,
            client_id=client_id,
            artifact_type=artifact_type,
            content=content,
            derivation_key=derivation_key,
            scope_id=scope_id,
            producer_run_id=producer_run_id,
        )
        response = upload_workflow_artifact(client, auth(root_token), metadata, content)
        assert response.status_code == 201, response.text
        result = response.json()
        assert result["created"] is True
        assert result["artifact"]["artifact_type"] == artifact_type.value
        assert result["artifact"]["content_hash"] == sha256(content)

        resolved = client.get(
            "/v1/workflow-artifacts/by-derivation",
            headers=auth(root_token),
            params={
                "tenant_id": str(tenant_id),
                "client_id": str(client_id),
                "artifact_type": artifact_type.value,
                "derivation_key": derivation_key,
            },
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["id"] == result["artifact"]["id"]

    listed = client.get(
        "/v1/workflow-artifacts",
        headers=auth(root_token),
        params={
            "tenant_id": str(tenant_id),
            "client_id": str(client_id),
            "core_scope_kind": "CLASSIFICATION_DATASET_SNAPSHOT",
            "core_scope_id": str(scope_id),
            "artifact_type": ArtifactType.SEARCH_RESULT_EXPORT.value,
        },
    )
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1
    assert listed.json()[0]["artifact_type"] == ArtifactType.SEARCH_RESULT_EXPORT.value


def test_workflow_artifact_retry_lineage_content_and_conflict(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    tenant_id, client_id = provision_core_scope(client, root_token, root_admin)
    scope_id = uuid.uuid4()
    producer_run_id = uuid.uuid4()
    population_content, _ = content_for(ArtifactType.CLASSIFICATION_POPULATION)
    population_metadata = workflow_metadata(
        tenant_id=tenant_id,
        client_id=client_id,
        artifact_type=ArtifactType.CLASSIFICATION_POPULATION,
        content=population_content,
        derivation_key="a" * 64,
        scope_id=scope_id,
        producer_run_id=producer_run_id,
    )
    first = upload_workflow_artifact(client, auth(root_token), population_metadata, population_content)
    assert first.status_code == 201, first.text
    first_id = first.json()["artifact"]["id"]

    retry = upload_workflow_artifact(client, auth(root_token), population_metadata, population_content)
    assert retry.status_code == 201, retry.text
    assert retry.json()["created"] is False
    assert retry.json()["artifact"]["id"] == first_id

    labels_content, _ = content_for(ArtifactType.CLASSIFICATION_LABELS)
    labels_metadata = workflow_metadata(
        tenant_id=tenant_id,
        client_id=client_id,
        artifact_type=ArtifactType.CLASSIFICATION_LABELS,
        content=labels_content,
        derivation_key="b" * 64,
        scope_id=scope_id,
        producer_run_id=producer_run_id,
        lineage=[
            {
                "source_kind": "WORKFLOW_ARTIFACT",
                "source_artifact_id": first_id,
                "relationship": "DERIVED_FROM",
            }
        ],
    )
    labels = upload_workflow_artifact(client, auth(root_token), labels_metadata, labels_content)
    assert labels.status_code == 201, labels.text
    labels_id = labels.json()["artifact"]["id"]

    lineage = client.get(
        f"/v1/workflow-artifacts/{labels_id}/lineage",
        headers=auth(root_token),
    )
    assert lineage.status_code == 200, lineage.text
    assert [(item["source_kind"], item["source_artifact_id"]) for item in lineage.json()] == [
        ("WORKFLOW_ARTIFACT", first_id)
    ]

    content = client.get(
        f"/v1/workflow-artifacts/{labels_id}/content",
        headers=auth(root_token),
    )
    assert content.status_code == 200, content.text
    assert content.content == labels_content
    assert content.headers["etag"] == sha256(labels_content)
    assert content.headers["content-length"] == str(len(labels_content))

    conflicting_content = parquet_bytes("different-population")
    conflict_metadata = {
        **population_metadata,
        "content_hash": sha256(conflicting_content),
        "byte_length": len(conflicting_content),
    }
    conflict = upload_workflow_artifact(
        client,
        auth(root_token),
        conflict_metadata,
        conflicting_content,
    )
    assert conflict.status_code == 409
    assert "different content hash" in conflict.json()["error"]["message"]


def test_workflow_artifact_validation_and_delegated_authorization(
    artifact_api_client: TestClient,
) -> None:
    tenant_id = uuid.uuid4()
    allowed_client_id = uuid.uuid4()
    denied_client_id = uuid.uuid4()
    actor_user_id = uuid.uuid4()
    provision_artifact_storage(tenant_id)
    headers = delegated_headers(
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=allowed_client_id,
    )
    content = parquet_bytes("population")
    metadata = workflow_metadata(
        tenant_id=tenant_id,
        client_id=allowed_client_id,
        artifact_type=ArtifactType.CLASSIFICATION_POPULATION,
        content=content,
        derivation_key="c" * 64,
    )

    created = upload_workflow_artifact(artifact_api_client, headers, metadata, content)
    assert created.status_code == 201, created.text

    wrong_hash = upload_workflow_artifact(
        artifact_api_client,
        headers,
        {**metadata, "derivation_key": "d" * 64, "content_hash": "0" * 64},
        content,
    )
    assert wrong_hash.status_code == 422
    assert "content_hash" in wrong_hash.json()["error"]["message"]

    wrong_media = upload_workflow_artifact(
        artifact_api_client,
        headers,
        {
            **metadata,
            "derivation_key": "e" * 64,
            "media_type": "application/json",
        },
        content,
    )
    assert wrong_media.status_code == 422

    denied = upload_workflow_artifact(
        artifact_api_client,
        headers,
        {
            **metadata,
            "client_id": str(denied_client_id),
            "derivation_key": "f" * 64,
        },
        content,
    )
    assert denied.status_code == 403


def test_workflow_artifact_gateway_embedded_remote_parity(
    artifact_api_client: TestClient,
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    client_id = uuid.uuid4()
    actor_user_id = uuid.uuid4()
    scope_id = uuid.uuid4()
    producer_run_id = uuid.uuid4()
    content = parquet_bytes("gateway-population")
    content_hash = sha256(content)
    provision_artifact_storage(tenant_id)

    monkeypatch.setattr("app.artifact_gateway.ArtifactSessionLocal", ArtifactTestingSessionLocal)
    monkeypatch.setattr("app.artifact_gateway.get_storage", lambda: memory_storage)
    monkeypatch.setattr(
        "app.artifact_gateway.get_settings",
        lambda: SimpleNamespace(artifact_mode="embedded", artifact_base_url="http://artifact.test"),
    )
    embedded, created = store_classification_workflow_artifact(
        content=io.BytesIO(content),
        artifact_type=ArtifactType.CLASSIFICATION_POPULATION,
        core_scope_kind="CLASSIFICATION_DATASET_SNAPSHOT",
        core_scope_id=scope_id,
        schema_version=1,
        derivation_key="1" * 64,
        content_hash=content_hash,
        byte_length=len(content),
        media_type="application/vnd.apache.parquet",
        producer_kind="CLASSIFICATION_WORKER",
        producer_run_id=producer_run_id,
        artifact_metadata={"gateway": "parity"},
        lineage=[],
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
        filename="population.parquet",
    )
    assert created is True
    assert find_workflow_artifact_reference(
        artifact_type=ArtifactType.CLASSIFICATION_POPULATION,
        derivation_key="1" * 64,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    ) == embedded
    assert get_workflow_artifact_reference(
        artifact_id=embedded.artifact_id,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    ) == embedded

    class TestClientAdapter:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            del args

        def get(self, path: str, **kwargs):
            return artifact_api_client.get(path, **kwargs)

        def post(self, path: str, **kwargs):
            return artifact_api_client.post(path, **kwargs)

        def stream(self, method: str, path: str, **kwargs):
            return artifact_api_client.stream(method, path, **kwargs)

    monkeypatch.setattr("app.artifact_gateway.httpx.Client", TestClientAdapter)
    monkeypatch.setattr(
        "app.artifact_gateway.get_settings",
        lambda: SimpleNamespace(artifact_mode="remote", artifact_base_url="http://artifact.test"),
    )
    remote, created = store_classification_workflow_artifact(
        content=io.BytesIO(content),
        artifact_type=ArtifactType.CLASSIFICATION_POPULATION,
        core_scope_kind="CLASSIFICATION_DATASET_SNAPSHOT",
        core_scope_id=scope_id,
        schema_version=1,
        derivation_key="1" * 64,
        content_hash=content_hash,
        byte_length=len(content),
        media_type="application/vnd.apache.parquet",
        producer_kind="CLASSIFICATION_WORKER",
        producer_run_id=producer_run_id,
        artifact_metadata={"gateway": "parity"},
        lineage=[],
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
        filename="population.parquet",
    )
    assert created is False
    assert remote == embedded
    assert get_workflow_artifact_reference(
        artifact_id=embedded.artifact_id,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    ) == embedded
    assert find_workflow_artifact_reference(
        artifact_type=ArtifactType.CLASSIFICATION_POPULATION,
        derivation_key="1" * 64,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    ) == embedded

    destination = io.BytesIO()
    copied = copy_workflow_artifact_content(
        artifact_id=embedded.artifact_id,
        destination=destination,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
        chunk_size=5,
    )
    assert copied == len(content)
    assert destination.getvalue() == content


def test_workflow_artifact_staging_enforces_streaming_size_limit() -> None:
    with pytest.raises(WorkflowArtifactError, match="exceeds"):
        stage_workflow_upload(io.BytesIO(b"12345"), maximum_bytes=4)
