from __future__ import annotations

import logging
from functools import lru_cache
from threading import Lock
from time import monotonic
from typing import Protocol

from embedding_service.config import EmbeddingSettings, get_embedding_settings
from embedding_service.schemas import EmbeddingInputType

logger = logging.getLogger(__name__)


class EmbeddingBackend(Protocol):
    @property
    def loaded(self) -> bool: ...

    def warmup(self) -> None: ...

    def embed(self, inputs: list[str], input_type: EmbeddingInputType) -> list[list[float]]: ...


class SentenceTransformerBackend:
    def __init__(self, settings: EmbeddingSettings) -> None:
        self.settings = settings
        self._model = None
        self._load_lock = Lock()
        self._encode_lock = Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def warmup(self) -> None:
        self._get_model()

    def embed(self, inputs: list[str], input_type: EmbeddingInputType) -> list[list[float]]:
        model = self._get_model()
        method_name = "encode_query" if input_type == "query" else "encode_document"
        encoder = getattr(model, method_name, model.encode)
        with self._encode_lock:
            vectors = encoder(
                inputs,
                batch_size=self.settings.batch_size,
                normalize_embeddings=self.settings.normalize,
                show_progress_bar=False,
            )
        result = vectors.tolist()
        if len(result) != len(inputs) or any(len(vector) != self.settings.dimensions for vector in result):
            raise RuntimeError(
                f"Embedding model returned an unexpected shape; expected {len(inputs)}x{self.settings.dimensions}"
            )
        return result

    def _get_model(self):
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            from sentence_transformers import SentenceTransformer

            options = {
                "trust_remote_code": self.settings.trust_remote_code,
                "truncate_dim": self.settings.dimensions,
            }
            if self.settings.model_revision:
                options["revision"] = self.settings.model_revision
            if self.settings.device != "auto":
                options["device"] = self.settings.device
            started_at = monotonic()
            logger.info(
                "Loading embedding model model=%s revision=%s dimensions=%s device=%s",
                self.settings.model,
                self.settings.model_revision or "default",
                self.settings.dimensions,
                self.settings.device,
            )
            self._model = SentenceTransformer(self.settings.model, **options)
            logger.info(
                "Embedding model loaded model=%s elapsed_seconds=%.2f",
                self.settings.model,
                monotonic() - started_at,
            )
            return self._model


@lru_cache
def get_embedding_backend() -> EmbeddingBackend:
    settings = get_embedding_settings()
    if settings.provider == "sentence_transformers":
        return SentenceTransformerBackend(settings)
    raise RuntimeError(f"Unsupported embedding provider: {settings.provider}")
