from functools import lru_cache

import httpx

from embedding_service.backend import EmbeddingBackend, get_embedding_backend
from embedding_service.config import EmbeddingSettings, get_embedding_settings
from embedding_service.schemas import EmbeddingInputType, EmbeddingResponse


class EmbeddingGateway:
    def __init__(
        self,
        settings: EmbeddingSettings,
        embedded_backend: EmbeddingBackend | None = None,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.embedded_backend = embedded_backend
        self.http_transport = http_transport

    def warmup(self) -> None:
        if self.settings.mode == "embedded":
            backend = self.embedded_backend or get_embedding_backend()
            backend.warmup()
            return
        self.embed(["Priv-View embedding readiness probe"], "document")

    def embed(self, inputs: list[str], input_type: EmbeddingInputType = "document") -> EmbeddingResponse:
        self._validate_inputs(inputs)
        if self.settings.mode == "embedded":
            backend = self.embedded_backend or get_embedding_backend()
            return EmbeddingResponse(
                model=self.settings.model,
                model_revision=self.settings.model_revision,
                dimensions=self.settings.dimensions,
                normalized=self.settings.normalize,
                embeddings=backend.embed(inputs, input_type),
            )

        with httpx.Client(
            base_url=self.settings.base_url,
            timeout=self.settings.request_timeout_seconds,
            transport=self.http_transport,
        ) as client:
            response = client.post(
                "/v1/embeddings",
                headers={"authorization": f"Bearer {self.settings.service_token}"},
                json={"inputs": inputs, "input_type": input_type},
            )
        response.raise_for_status()
        result = EmbeddingResponse.model_validate(response.json())
        if (
            result.model != self.settings.model
            or result.model_revision != self.settings.model_revision
            or result.dimensions != self.settings.dimensions
            or result.normalized != self.settings.normalize
        ):
            raise RuntimeError("Remote embedding service configuration does not match the worker configuration")
        return result

    def _validate_inputs(self, inputs: list[str]) -> None:
        if not inputs:
            raise ValueError("At least one input is required")
        if len(inputs) > self.settings.max_inputs:
            raise ValueError(f"At most {self.settings.max_inputs} inputs are allowed per request")
        if any(not value for value in inputs):
            raise ValueError("Embedding inputs must not be empty")
        if any(len(value) > self.settings.max_input_characters for value in inputs):
            raise ValueError(
                f"Each input must be at most {self.settings.max_input_characters} characters"
            )


@lru_cache
def get_embedding_gateway() -> EmbeddingGateway:
    return EmbeddingGateway(get_embedding_settings())
