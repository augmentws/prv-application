import json
from pathlib import Path

import httpx

from app.voyage_batch import VoyageBatchClient, prepare_batch_input, resolve_batch_output
from embedding_service.config import EmbeddingSettings


def settings() -> EmbeddingSettings:
    return EmbeddingSettings(
        _env_file=None,
        mode="remote",
        provider="voyage_api",
        model="voyage-4-lite",
        dimensions=256,
        VOYAGE_API_KEY="test-voyage-key",
    )


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_prepare_and_resolve_batch_files_with_unordered_results(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    requests = tmp_path / "requests.jsonl"
    manifest = tmp_path / "manifest.jsonl"
    output = tmp_path / "output.jsonl"
    resolved = tmp_path / "resolved.jsonl"
    write_jsonl(
        source,
        [
            {"id": "chunk-a", "text": "alpha"},
            {"id": "chunk-b", "text": "bravo"},
            {"id": "chunk-c", "text": "charlie"},
        ],
    )

    counts = prepare_batch_input(
        source,
        requests,
        manifest,
        max_inputs_per_request=2,
    )

    assert counts == {"items": 3, "requests": 2}
    request_rows = read_jsonl(requests)
    assert request_rows == [
        {"custom_id": "request-00000000", "body": {"input": ["alpha", "bravo"]}},
        {"custom_id": "request-00000001", "body": {"input": ["charlie"]}},
    ]
    write_jsonl(
        output,
        [
            {
                "custom_id": "request-00000001",
                "response": {
                    "status_code": 200,
                    "body": {
                        "model": "voyage-4-lite",
                        "data": [{"index": 0, "embedding": [3.0]}],
                        "usage": {"total_tokens": 1},
                    },
                },
                "error": None,
            },
            {
                "custom_id": "request-00000000",
                "response": {
                    "status_code": 200,
                    "body": {
                        "model": "voyage-4-lite",
                        "data": [
                            {"index": 1, "embedding": [2.0]},
                            {"index": 0, "embedding": [1.0]},
                        ],
                        "usage": {"total_tokens": 2},
                    },
                },
                "error": None,
            },
        ],
    )

    resolved_counts = resolve_batch_output(output, manifest, resolved)

    assert resolved_counts == {"items": 3, "requests": 2, "total_tokens": 3}
    assert {row["id"]: row["embedding"] for row in read_jsonl(resolved)} == {
        "chunk-a": [1.0],
        "chunk-b": [2.0],
        "chunk-c": [3.0],
    }


def test_reconciled_submission_reuses_existing_provider_batch(tmp_path: Path) -> None:
    input_path = tmp_path / "priv-view-application-batch.jsonl"
    input_path.write_text('{"custom_id":"one","body":{"input":["text"]}}\n')
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer test-voyage-key"
        if request.url.path == "/v1/batches":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "batch-existing",
                            "input_file_id": "file-existing",
                            "status": "in_progress",
                            "metadata": {"priv_view_batch_id": "application-batch"},
                        }
                    ],
                    "has_more": False,
                },
            )
        raise AssertionError(f"Unexpected request {request.method} {request.url}")

    client = VoyageBatchClient(settings(), transport=httpx.MockTransport(handler))

    result = client.submit_reconciled(
        input_path,
        metadata={"priv_view_batch_id": "application-batch"},
    )

    assert result["batch"]["id"] == "batch-existing"
    assert result["input_file"]["id"] == "file-existing"
    assert requests == [("GET", "/v1/batches")]
