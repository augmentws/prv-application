import hashlib
import json
from typing import Any

from app.config import Settings
from embedding_service.config import EmbeddingSettings

CHUNKER_NAME = "sentence-aware"
CHUNKER_VERSION = 1
CHUNK_SET_SCHEMA_VERSION = 1
CHUNK_VECTOR_SET_SCHEMA_VERSION = 1


def processing_configuration(settings: Settings, embeddings: EmbeddingSettings) -> dict[str, Any]:
    return {
        "chunking": {
            "name": CHUNKER_NAME,
            "version": CHUNKER_VERSION,
            "target_characters": settings.chunk_target_characters,
            "max_characters": settings.chunk_max_characters,
            "overlap_characters": settings.chunk_overlap_characters,
        },
        "embedding": {
            "provider": embeddings.provider,
            "model": embeddings.model,
            "model_revision": embeddings.model_revision,
            "dimensions": embeddings.dimensions,
            "normalized": embeddings.normalize,
        },
    }


def canonical_hash(value: dict[str, Any]) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def chunk_configuration_hash(configuration: dict[str, Any]) -> str:
    return canonical_hash(configuration["chunking"])


def derivation_key(*, source_hash: str, configuration: dict[str, Any]) -> str:
    return canonical_hash({"source_hash": source_hash, "configuration": configuration})
