import httpx
import pytest
from fastapi.testclient import TestClient

from app.embedding_gateway import EmbeddingGateway
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
