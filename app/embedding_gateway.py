import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from functools import lru_cache

import httpx

from embedding_service.backend import EmbeddingBackend, SentenceTransformerBackend, get_embedding_backend
from embedding_service.config import EmbeddingSettings, get_embedding_settings
from embedding_service.schemas import EmbeddingInputType, EmbeddingResponse

logger = logging.getLogger(__name__)
VOYAGE_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class VoyageRateLimiter:
    """Process-local sliding-window limiter for Voyage real-time quotas."""

    def __init__(self, tokens_per_minute: int, requests_per_minute: int) -> None:
        self.tokens_per_minute = tokens_per_minute
        self.requests_per_minute = requests_per_minute
        self._events: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def acquire(self, estimated_tokens: int) -> None:
        estimated_tokens = max(1, min(estimated_tokens, self.tokens_per_minute))
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - 60.0
                while self._events and self._events[0][0] <= cutoff:
                    self._events.popleft()
                used_tokens = sum(tokens for _, tokens in self._events)
                if (
                    len(self._events) < self.requests_per_minute
                    and used_tokens + estimated_tokens <= self.tokens_per_minute
                ):
                    self._events.append((now, estimated_tokens))
                    return
                wait_seconds = max(0.01, 60.0 - (now - self._events[0][0]))
            time.sleep(wait_seconds)


class EmbeddingGateway:
    def __init__(
        self,
        settings: EmbeddingSettings,
        embedded_backend: EmbeddingBackend | None = None,
        http_transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.embedded_backend = embedded_backend
        self.http_transport = http_transport
        self.sleep = sleep
        self._voyage_client = (
            httpx.Client(
                base_url=self.settings.voyage_base_url,
                timeout=self.settings.request_timeout_seconds,
                transport=self.http_transport,
            )
            if self.settings.provider == "voyage_api"
            else None
        )
        self._voyage_rate_limiter = VoyageRateLimiter(
            self.settings.voyage_tokens_per_minute,
            self.settings.voyage_requests_per_minute,
        )

    def warmup(self) -> None:
        if self.settings.provider == "voyage_api":
            return
        if self.settings.mode == "embedded":
            backend = self.embedded_backend or get_embedding_backend()
            backend.warmup()
            return
        self.embed(["Priv-View embedding readiness probe"], "document")

    def embed(self, inputs: list[str], input_type: EmbeddingInputType = "document") -> EmbeddingResponse:
        self._validate_inputs(inputs)
        if self.settings.provider == "voyage_api":
            return self._embed_voyage(inputs, input_type)
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

    def _embed_voyage(
        self,
        inputs: list[str],
        input_type: EmbeddingInputType,
    ) -> EmbeddingResponse:
        api_key = self.settings.voyage_api_key
        if api_key is None:
            raise RuntimeError("Voyage API key is not configured")
        payload = {
            "input": inputs,
            "model": self.settings.model,
            "input_type": input_type,
            "truncation": False,
            "output_dimension": self.settings.dimensions,
            "output_dtype": "float",
        }
        estimated_tokens = sum(max(1, math.ceil(len(value) / 4)) for value in inputs)
        client = self._voyage_client
        if client is None:  # pragma: no cover - protected by provider validation
            raise RuntimeError("Voyage HTTP client is not configured")
        for attempt in range(self.settings.voyage_max_retries + 1):
            self._voyage_rate_limiter.acquire(estimated_tokens)
            try:
                response = client.post(
                    "/v1/embeddings",
                    headers={"authorization": f"Bearer {api_key.get_secret_value()}"},
                    json=payload,
                )
            except httpx.TransportError:
                if attempt >= self.settings.voyage_max_retries:
                    raise
                self._wait_before_retry(attempt, None)
                continue
            if response.status_code not in VOYAGE_RETRYABLE_STATUS_CODES:
                response.raise_for_status()
                break
            if attempt >= self.settings.voyage_max_retries:
                response.raise_for_status()
            self._wait_before_retry(attempt, response.headers.get("retry-after"))
        else:  # pragma: no cover - the bounded loop always returns or raises
            raise RuntimeError("Voyage API retry loop exited unexpectedly")

        data = response.json()
        response_model = data.get("model", self.settings.model)
        rows = data.get("data")
        if response_model != self.settings.model or not isinstance(rows, list):
            raise RuntimeError("Voyage API returned an unexpected embedding response")
        try:
            ordered = sorted(rows, key=lambda row: int(row["index"]))
            embeddings = [row["embedding"] for row in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Voyage API returned malformed embedding rows") from exc
        if len(embeddings) != len(inputs):
            raise RuntimeError("Voyage API response count does not match the submitted input count")
        if any(len(vector) != self.settings.dimensions for vector in embeddings):
            raise RuntimeError("Voyage API returned an unexpected embedding dimension")
        total_tokens = (data.get("usage") or {}).get("total_tokens")
        logger.info(
            "Completed Voyage embedding request model=%s input_type=%s inputs=%s tokens=%s",
            self.settings.model,
            input_type,
            len(inputs),
            total_tokens if total_tokens is not None else "unknown",
        )
        return EmbeddingResponse(
            model=response_model,
            model_revision=None,
            dimensions=self.settings.dimensions,
            normalized=True,
            embeddings=embeddings,
            input_tokens=total_tokens if isinstance(total_tokens, int) else 0,
            output_tokens=0,
            request_count=1,
        )

    def _wait_before_retry(self, attempt: int, retry_after: str | None) -> None:
        delay = self.settings.voyage_retry_base_seconds * (2**attempt)
        if retry_after is not None:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        logger.warning(
            "Retrying Voyage embedding request attempt=%s delay_seconds=%.2f",
            attempt + 1,
            delay,
        )
        self.sleep(delay)

    def _validate_inputs(self, inputs: list[str]) -> None:
        if not inputs:
            raise ValueError("At least one input is required")
        if len(inputs) > self.settings.max_inputs:
            raise ValueError(f"At most {self.settings.max_inputs} inputs are allowed per request")
        if any(not value for value in inputs):
            raise ValueError("Embedding inputs must not be empty")
        if any(len(value) > self.settings.max_input_characters for value in inputs):
            raise ValueError(f"Each input must be at most {self.settings.max_input_characters} characters")


@lru_cache
def get_embedding_gateway() -> EmbeddingGateway:
    return EmbeddingGateway(get_embedding_settings())


def query_embedding_settings(settings: EmbeddingSettings) -> EmbeddingSettings:
    if settings.query_model is None:
        return settings
    return settings.model_copy(
        update={
            "mode": settings.query_mode,
            "provider": "sentence_transformers",
            "model": settings.query_model,
            "model_revision": settings.query_model_revision,
            "device": settings.query_device,
            "base_url": settings.query_base_url or settings.base_url,
            "voyage_batch_enabled": False,
        }
    )


@lru_cache
def get_query_embedding_gateway() -> EmbeddingGateway:
    settings = get_embedding_settings()
    query_settings = query_embedding_settings(settings)
    if query_settings is settings:
        return get_embedding_gateway()
    if (
        query_settings.mode == settings.mode
        and query_settings.provider == settings.provider
        and query_settings.model == settings.model
        and query_settings.model_revision == settings.model_revision
        and query_settings.device == settings.device
        and query_settings.base_url == settings.base_url
    ):
        return get_embedding_gateway()
    backend = SentenceTransformerBackend(query_settings) if query_settings.mode == "embedded" else None
    return EmbeddingGateway(query_settings, embedded_backend=backend)
