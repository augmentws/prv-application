import json
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app.artifact_gateway import DerivedArtifactReference, EmbeddingTextSource
from app.embeddings.chunking import semantic_chunks
from app.embeddings.parquet import read_chunk_set, read_vector_set, write_chunk_set, write_vector_set
from app.matter_embeddings import process_document
from embedding_service.schemas import EmbeddingResponse


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_sentence_aware_chunks_and_parquet_round_trip() -> None:
    text = (
        "First sentence explains the agreement. Second sentence adds important detail.\n\n"
        "A separate paragraph discusses damages. Final sentence covers the requested remedy."
    )
    chunks = semantic_chunks(
        text,
        source_hash="a" * 64,
        target_characters=70,
        max_characters=100,
        overlap_characters=45,
    )

    assert len(chunks) >= 2
    assert chunks[0].text.startswith("First sentence")
    assert all(chunk.char_end - chunk.char_start <= 100 for chunk in chunks)
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))

    chunk_content = write_chunk_set(chunks, {"source_content_hash": "a" * 64})
    assert read_chunk_set(chunk_content) == chunks

    vectors = [[float(index)] * 32 for index in range(len(chunks))]
    vector_content = write_vector_set(chunks, vectors, dimensions=32, metadata={"model": "test"})
    loaded_vectors = read_vector_set(vector_content, dimensions=32)
    assert loaded_vectors[chunks[-1].chunk_id] == vectors[-1]


def test_create_list_and_cancel_embedding_job(client: TestClient, root_token: str) -> None:
    headers = auth(root_token)
    tenant_id = client.get("/v1/auth/me", headers=headers).json()["tenant_id"]
    client_record = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=headers,
        json={"name": "Embedding Client"},
    ).json()
    matter = client.post(
        f"/v1/clients/{client_record['id']}/matters",
        headers=headers,
        json={"name": "Embedding Matter"},
    ).json()

    created = client.post(f"/v1/matters/{matter['id']}/embedding-jobs", headers=headers)
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "QUEUED"
    assert created.json()["embedding_model"] == "voyageai/voyage-4-nano"
    assert created.json()["embedding_dimensions"] == 1024

    duplicate = client.post(f"/v1/matters/{matter['id']}/embedding-jobs", headers=headers)
    assert duplicate.status_code == 409

    listed = client.get(f"/v1/matters/{matter['id']}/embedding-jobs", headers=headers)
    assert listed.status_code == 200
    assert [job["id"] for job in listed.json()] == [created.json()["id"]]

    canceled = client.post(
        f"/v1/matters/{matter['id']}/embedding-jobs/{created.json()['id']}/cancel",
        headers=headers,
    )
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "CANCELED"


def test_derived_artifact_upload_is_idempotent(client: TestClient, root_token: str) -> None:
    headers = auth(root_token)
    tenant_id = client.get("/v1/auth/me", headers=headers).json()["tenant_id"]
    client_id = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=headers,
        json={"name": "Derived Artifact Client"},
    ).json()["id"]
    custodian_id = client.post(
        f"/v1/clients/{client_id}/custodians",
        headers=headers,
        json={"display_name": "Document Owner"},
    ).json()["id"]
    client.post(
        f"/v1/tenants/{tenant_id}/artifact-storage/ensure",
        headers=headers,
        json={"tenant_slug": "root"},
    )
    collection_id = client.post(
        f"/v1/tenants/{tenant_id}/clients/{client_id}/collections",
        headers=headers,
        json={"name": "Derived collection"},
    ).json()["id"]
    item = client.post(
        f"/v1/collections/{collection_id}/items:upload",
        headers=headers,
        data={
            "metadata": json.dumps(
                {
                    "source_item_id": "memo-1",
                    "record_type": "FILE",
                    "original_filename": "memo.txt",
                    "custodian_ids": [custodian_id],
                    "processing_status": "READY",
                }
            )
        },
        files={"file": ("memo.txt", b"A short legal memorandum.", "text/plain")},
    ).json()["item"]
    run_id = uuid.uuid4()
    chunk_metadata = {
        "artifact_type": "CHUNK_SET",
        "source_artifact_id": item["native_artifact"]["id"],
        "relationship": "CHUNKED_FROM",
        "processing_run_id": str(run_id),
        "derivation_key": "b" * 64,
        "metadata": {"schema_version": 1, "chunk_count": 1},
    }
    first = client.post(
        f"/v1/collection-items/{item['id']}/derived-artifacts:upload",
        headers=headers,
        data={"metadata": json.dumps(chunk_metadata)},
        files={"file": ("chunks.parquet", b"parquet-chunks", "application/vnd.apache.parquet")},
    )
    retry = client.post(
        f"/v1/collection-items/{item['id']}/derived-artifacts:upload",
        headers=headers,
        data={"metadata": json.dumps(chunk_metadata)},
        files={"file": ("chunks.parquet", b"different-retry-content", "application/vnd.apache.parquet")},
    )

    assert first.status_code == 201, first.text
    assert first.json()["created"] is True
    assert retry.status_code == 201, retry.text
    assert retry.json()["created"] is False
    assert retry.json()["artifact"]["id"] == first.json()["artifact"]["id"]
    assert first.json()["artifact"]["derivation_key"] == "b" * 64
    assert first.json()["artifact"]["metadata"]["chunk_count"] == 1


def test_document_processing_creates_chunk_and_vector_sets(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    client_id = uuid.uuid4()
    job_id = uuid.uuid4()
    document = SimpleNamespace(collection_item_id=uuid.uuid4())
    configuration = {
        "chunking": {
            "name": "sentence-aware",
            "version": 1,
            "target_characters": 40,
            "max_characters": 80,
            "overlap_characters": 10,
        },
        "embedding": {
            "provider": "sentence_transformers",
            "model": "test-model",
            "model_revision": "revision-1",
            "dimensions": 32,
            "normalized": True,
        },
    }
    job = SimpleNamespace(
        id=job_id,
        matter=SimpleNamespace(client=SimpleNamespace(tenant_id=tenant_id), client_id=client_id),
        created_by_user_id=uuid.uuid4(),
        configuration=configuration,
        configuration_hash="f" * 64,
        embedding_model="test-model",
        embedding_model_revision="revision-1",
        embedding_dimensions=32,
        embedding_normalized=True,
    )
    source = EmbeddingTextSource(
        artifact_id=uuid.uuid4(),
        content_hash="a" * 64,
        text="First sentence has substance. Second sentence adds detail. Final sentence closes.",
    )
    monkeypatch.setattr("app.matter_embeddings.get_embedding_text_source", lambda **_: source)
    monkeypatch.setattr("app.matter_embeddings.find_derived_artifact_reference", lambda **_: None)
    stored: list[dict] = []
    stored_refs: list[DerivedArtifactReference] = []

    def fake_store(**kwargs):
        stored.append(kwargs)
        reference = DerivedArtifactReference(
            artifact_id=uuid.uuid4(),
            content_hash=("b" if kwargs["artifact_type"] == "CHUNK_SET" else "c") * 64,
            derivation_key=kwargs["derivation_key"],
            metadata=kwargs["artifact_metadata"],
        )
        stored_refs.append(reference)
        return reference, True

    monkeypatch.setattr("app.matter_embeddings.store_derived_artifact", fake_store)
    gateway = Mock()
    gateway.settings.max_inputs = 256
    gateway.embed.side_effect = lambda inputs, _: EmbeddingResponse(
        model="test-model",
        model_revision="revision-1",
        dimensions=32,
        normalized=True,
        embeddings=[[0.25] * 32 for _ in inputs],
    )
    monkeypatch.setattr("app.matter_embeddings.get_embedding_gateway", lambda: gateway)

    result = process_document(job, document)

    assert result.outcome == "EMBEDDED"
    assert result.chunk_count >= 1
    assert [value["artifact_type"] for value in stored] == ["CHUNK_SET", "CHUNK_VECTOR_SET"]
    chunk_rows = read_chunk_set(stored[0]["content"])
    vector_rows = read_vector_set(stored[1]["content"], dimensions=32)
    assert len(chunk_rows) == len(vector_rows) == result.chunk_count
    assert stored[1]["source_artifact_id"] == stored_refs[0].artifact_id
