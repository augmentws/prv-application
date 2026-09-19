import httpx
import pytest
from fastapi.testclient import TestClient

from app.embedding_gateway import EmbeddingGateway, query_embedding_settings
from embedding_service.backend import get_embedding_backend
from embedding_service.config import EmbeddingSettings, get_embedding_settings
from embedding_service.main import app as embedding_app


class FakeBackend:
    loaded = True
    warmed = False

    def warmup(self) -> None:
        self.warmed = True

    def embed(self, inputs: list[str], input_type: str) -> list[list[float]]:
        marker = 1.0 if input_type == "query" else 2.0
        return [[marker, float(len(value)), *([0.0] * 30)] for value in inputs]


def test_standalone_embedding_api_requires_token_and_returns_configured_vectors() -> None:
    settings = EmbeddingSettings(dimensions=32)
    embedding_app.dependency_overrides[get_embedding_backend] = lambda: FakeBackend()
    embedding_app.dependency_overrides[get_embedding_settings] = lambda: settings
    try:
        with TestClient(embedding_app) as client:
            unauthorized = client.post("/v1/embeddings", json={"inputs": ["hello"], "input_type": "query"})
            assert unauthorized.status_code == 401

            response = client.post(
                "/v1/embeddings",
                headers={"Authorization": "Bearer development-embedding-service-token"},
                json={"inputs": ["hello", "world!"], "input_type": "query"},
            )
            assert response.status_code == 200, response.text
            assert response.json() == {
                "model": "voyageai/voyage-4-nano",
                "model_revision": None,
                "dimensions": 32,
                "normalized": True,
                "embeddings": [
                    [1.0, 5.0, *([0.0] * 30)],
                    [1.0, 6.0, *([0.0] * 30)],
                ],
                "input_tokens": 0,
                "output_tokens": 0,
                "request_count": 0,
            }
    finally:
        embedding_app.dependency_overrides.clear()


def test_embedded_gateway_uses_in_process_backend() -> None:
    settings = EmbeddingSettings(dimensions=32)
    gateway = EmbeddingGateway(settings, embedded_backend=FakeBackend())

    response = gateway.embed(["evidence"], "document")

    assert response.embeddings == [[2.0, 8.0, *([0.0] * 30)]]
    assert response.model == "voyageai/voyage-4-nano"


def test_embedded_gateway_warmup_loads_backend_without_inference() -> None:
    settings = EmbeddingSettings(dimensions=32)
    backend = FakeBackend()
    gateway = EmbeddingGateway(settings, embedded_backend=backend)

    gateway.warmup()

    assert backend.warmed is True


def test_gateway_applies_batch_limits_before_invoking_either_mode() -> None:
    settings = EmbeddingSettings(dimensions=32, max_inputs=1)
    gateway = EmbeddingGateway(settings, embedded_backend=FakeBackend())

    with pytest.raises(ValueError, match="At most 1 input"):
        gateway.embed(["one", "two"])


def test_remote_gateway_uses_private_service_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer development-embedding-service-token"
        assert request.url.path == "/v1/embeddings"
        assert request.read() == b'{"inputs":["evidence"],"input_type":"document"}'
        return httpx.Response(
            200,
            json={
                "model": "voyageai/voyage-4-nano",
                "model_revision": None,
                "dimensions": 32,
                "normalized": True,
                "embeddings": [[0.25, 0.75, *([0.0] * 30)]],
            },
        )

    settings = EmbeddingSettings(mode="remote", dimensions=32)
    gateway = EmbeddingGateway(settings, http_transport=httpx.MockTransport(handler))

    response = gateway.embed(["evidence"])

    assert response.embeddings == [[0.25, 0.75, *([0.0] * 30)]]


def voyage_settings(**overrides) -> EmbeddingSettings:
    values = {
        "mode": "remote",
        "provider": "voyage_api",
        "model": "voyage-4-lite",
        "dimensions": 256,
        "VOYAGE_API_KEY": "test-voyage-key",
    }
    values.update(overrides)
    return EmbeddingSettings(_env_file=None, **values)


def test_voyage_gateway_uses_hosted_contract_and_restores_index_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-voyage-key"
        assert request.url.path == "/v1/embeddings"
        assert request.read() == (
            b'{"input":["first","second"],"model":"voyage-4-lite",'
            b'"input_type":"document","truncation":false,"output_dimension":256,'
            b'"output_dtype":"float"}'
        )
        return httpx.Response(
            200,
            json={
                "model": "voyage-4-lite",
                "data": [
                    {"index": 1, "embedding": [2.0] * 256},
                    {"index": 0, "embedding": [1.0] * 256},
                ],
                "usage": {"total_tokens": 4},
            },
        )

    gateway = EmbeddingGateway(
        voyage_settings(),
        http_transport=httpx.MockTransport(handler),
    )

    response = gateway.embed(["first", "second"])

    assert response.model == "voyage-4-lite"
    assert response.model_revision is None
    assert response.dimensions == 256
    assert response.normalized is True
    assert response.embeddings == [[1.0] * 256, [2.0] * 256]
    assert response.input_tokens == 4
    assert response.output_tokens == 0
    assert response.request_count == 1


def test_voyage_gateway_retries_rate_limits_with_retry_after() -> None:
    requests = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(
            200,
            json={
                "model": "voyage-4-lite",
                "data": [{"index": 0, "embedding": [1.0] * 256}],
                "usage": {"total_tokens": 1},
            },
        )

    gateway = EmbeddingGateway(
        voyage_settings(),
        http_transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )

    gateway.embed(["evidence"])

    assert requests == 2
    assert sleeps == [2.0]


def test_voyage_settings_require_supported_hosted_configuration() -> None:
    with pytest.raises(ValueError, match="VOYAGE_API_KEY"):
        EmbeddingSettings(
            _env_file=None,
            mode="remote",
            provider="voyage_api",
            model="voyage-4-lite",
            dimensions=256,
            VOYAGE_API_KEY=None,
        )
    with pytest.raises(ValueError, match="dimensions"):
        voyage_settings(dimensions=300)


def test_query_embedding_settings_use_local_nano_without_changing_document_provider() -> None:
    document_settings = voyage_settings(
        query_mode="embedded",
        query_model="voyageai/voyage-4-nano",
        query_device="cpu",
    )

    query_settings = query_embedding_settings(document_settings)

    assert document_settings.provider == "voyage_api"
    assert document_settings.model == "voyage-4-lite"
    assert query_settings.provider == "sentence_transformers"
    assert query_settings.mode == "embedded"
    assert query_settings.model == "voyageai/voyage-4-nano"
    assert query_settings.device == "cpu"
    assert query_settings.dimensions == document_settings.dimensions
    assert query_settings.normalize == document_settings.normalize
