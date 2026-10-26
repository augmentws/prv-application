import hashlib
import uuid
from io import BytesIO
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app import artifact_gateway
from artifact_service.models import ClientCollection, ContentBlob


def test_chunk_vector_selection_prefers_exact_configuration_then_compatible_space() -> None:
    embedding = {
        "provider": "voyage",
        "model": "voyage-4-lite",
        "model_revision": "voyage-4-lite",
        "dimensions": 1024,
        "normalized": True,
    }
    compatible = SimpleNamespace(
        name="compatible",
        artifact_metadata={"configuration_hash": "legacy", "embedding": embedding},
    )
    exact = SimpleNamespace(
        name="exact",
        artifact_metadata={"configuration_hash": "current", "embedding": embedding},
    )
    incompatible = SimpleNamespace(
        name="incompatible",
        artifact_metadata={
            "configuration_hash": "other",
            "embedding": {**embedding, "dimensions": 512},
        },
    )

    selected = artifact_gateway._select_compatible_chunk_vector(
        [compatible, exact],
        configuration_hash="current",
        compatible_embedding=embedding,
        metadata=lambda artifact: artifact.artifact_metadata,
    )
    assert selected is exact

    selected = artifact_gateway._select_compatible_chunk_vector(
        [incompatible, compatible],
        configuration_hash="current",
        compatible_embedding=embedding,
        metadata=lambda artifact: artifact.artifact_metadata,
    )
    assert selected is compatible


def test_embedded_grouping_snapshots_use_one_array_join(monkeypatch) -> None:
    item_ids = [uuid.uuid4() for _ in range(5)]
    statements: list[str] = []
    parameter_sets: list[dict] = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def execute(self, statement, parameters):
            statements.append(str(statement.compile(dialect=postgresql.dialect())))
            parameter_sets.append(parameters)
            return [
                (item_id, None, "FILE", f"hash-{item_id}")
                for item_id in parameters["grouping_item_ids"]
            ]

    monkeypatch.setattr(
        artifact_gateway,
        "get_settings",
        lambda: SimpleNamespace(artifact_mode="embedded"),
    )
    monkeypatch.setattr(artifact_gateway, "ArtifactSessionLocal", FakeSession)

    snapshots = artifact_gateway.get_collection_item_grouping_snapshots(
        item_ids=[*item_ids, item_ids[0]],
        actor_user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        client_id=uuid.uuid4(),
    )

    assert len(statements) == 1
    assert "JOIN collection_item" in statements[0]
    assert "unnest(" in statements[0]
    assert " IN " not in statements[0]
    assert parameter_sets == [{"grouping_item_ids": item_ids}]
    assert set(snapshots) == set(item_ids)


def test_pinned_text_source_is_verified_against_its_frozen_hash(monkeypatch) -> None:
    content = b"Plain text body"
    monkeypatch.setattr(artifact_gateway, "read_artifact_bytes", lambda **_: content)
    reference = artifact_gateway.TextSourceReference(
        artifact_id=uuid.uuid4(),
        role="EXTRACTED_TEXT",
        media_type="text/plain",
        original_filename="body.txt",
        content_hash=hashlib.sha256(content).hexdigest(),
        byte_length=len(content),
    )
    scope = {"actor_user_id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "client_id": uuid.uuid4()}

    assert artifact_gateway.read_text_source(reference, record_type="FILE", **scope) == "Plain text body"
    tampered = artifact_gateway.TextSourceReference(**{**reference.__dict__, "content_hash": "0" * 64})
    with pytest.raises(ValueError, match="frozen content hash"):
        artifact_gateway.read_text_source(tampered, record_type="FILE", **scope)


def test_embedded_search_text_falls_back_when_preferred_blob_is_missing(monkeypatch) -> None:
    item = SimpleNamespace(id=uuid.uuid4(), collection_id=uuid.uuid4(), record_type="EMAIL")
    normalized_blob_id = uuid.uuid4()
    native_blob_id = uuid.uuid4()
    normalized_artifact = SimpleNamespace(
        id=uuid.uuid4(),
        original_filename="message.txt",
        media_type="text/plain",
        content_blob_id=normalized_blob_id,
        content_hash="a" * 64,
        created_at=None,
    )
    native_artifact = SimpleNamespace(
        id=uuid.uuid4(),
        original_filename="message.eml",
        media_type="message/rfc822",
        content_blob_id=native_blob_id,
        content_hash="b" * 64,
        created_at=None,
    )
    normalized_blob = SimpleNamespace(id=normalized_blob_id, bucket_name="bucket", storage_key="missing")
    native_blob = SimpleNamespace(id=native_blob_id, bucket_name="bucket", storage_key="native")

    class Result:
        def all(self):
            return [("NORMALIZED_TEXT", normalized_artifact), ("NATIVE", native_artifact)]

    class FakeDb:
        def execute(self, _statement):
            return Result()

        def get(self, model, identifier):
            if model is ClientCollection:
                return SimpleNamespace(active_text_processing_run_id=uuid.uuid4())
            if model is ContentBlob and identifier == normalized_blob_id:
                return normalized_blob
            if model is ContentBlob and identifier == native_blob_id:
                return native_blob
            return None

    class Storage:
        def open(self, _bucket, key):
            if key == "missing":
                raise FileNotFoundError(key)
            return BytesIO(b"From: sender@example.com\r\nTo: recipient@example.com\r\n\r\nNative fallback body")

    monkeypatch.setattr(artifact_gateway, "get_storage", lambda: Storage())

    text = artifact_gateway._embedded_body_text(FakeDb(), item, 100_000)

    assert text == "Native fallback body"
