import json
from types import SimpleNamespace

import pytest

from app.model_batching import (
    BatchModelRequest,
    GoogleBatchProvider,
    OpenAIBatchProvider,
    resolve_batch_target,
)
from app.provider_schemas import provider_output_schema


def test_batch_provider_resolution_is_capability_based() -> None:
    resolved = resolve_batch_target("google:gemini-3.5-flash-lite")

    assert resolved is not None
    adapter, target = resolved
    assert isinstance(adapter, GoogleBatchProvider)
    assert target.provider == "google"
    assert target.model == "gemini-3.5-flash-lite"


def test_openai_models_resolve_to_the_openai_batch_adapter() -> None:
    resolved = resolve_batch_target("openai:gpt-6-luna")

    assert resolved is not None
    adapter, target = resolved
    assert isinstance(adapter, OpenAIBatchProvider)
    assert target.provider == "openai"
    assert target.model == "gpt-6-luna"


def test_models_without_a_batch_adapter_remain_eligible_for_realtime_execution() -> None:
    assert resolve_batch_target("anthropic:claude-sonnet-4-6") is None


def test_google_batch_schema_preserves_named_properties() -> None:
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string", "minLength": 1}},
        "required": ["answer"],
    }

    assert provider_output_schema(schema, provider="google") == {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }


def test_google_batch_schema_inlines_local_references() -> None:
    schema = {
        "$defs": {
            "cited_text": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "citation_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "citation_ids"],
                "additionalProperties": False,
            },
            "coverage": {
                "type": "object",
                "properties": {"status": {"type": "string", "enum": ["COMPLETE"]}},
                "required": ["status"],
                "additionalProperties": False,
            },
        },
        "type": "object",
        "properties": {
            "summary": {"type": "array", "items": {"$ref": "#/$defs/cited_text"}},
            "coverage": {"$ref": "#/$defs/coverage"},
        },
        "required": ["summary", "coverage"],
        "additionalProperties": False,
    }

    admitted = provider_output_schema(
        schema,
        provider="google",
        inline_references=True,
    )

    assert "$defs" not in admitted
    assert "$ref" not in str(admitted)
    assert admitted["properties"]["summary"]["items"] == schema["$defs"]["cited_text"]
    assert admitted["properties"]["coverage"] == schema["$defs"]["coverage"]
    assert "$defs" in schema


class _FakeTransport:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[tuple[str, object]] = []


class _FakeBatches:
    def __init__(self, transport: _FakeTransport, *, fail_create: bool = False) -> None:
        self._transport = transport
        self._fail_create = fail_create

    def create(self, **kwargs):
        assert not self._transport.closed
        self._transport.calls.append(("create", kwargs))
        if self._fail_create:
            raise RuntimeError("submission failed")
        return SimpleNamespace(
            name="batches/example",
            state=SimpleNamespace(value="JOB_STATE_PENDING"),
        )

    def get(self, *, name: str):
        assert not self._transport.closed
        self._transport.calls.append(("get", name))
        return SimpleNamespace(
            state=SimpleNamespace(value="JOB_STATE_SUCCEEDED"),
            error=None,
            dest=SimpleNamespace(inlined_responses=[]),
        )

    def cancel(self, *, name: str) -> None:
        assert not self._transport.closed
        self._transport.calls.append(("cancel", name))


class _FakeClient:
    def __init__(self, transport: _FakeTransport, *, fail_create: bool = False) -> None:
        self._transport = transport
        self.batches = _FakeBatches(transport, fail_create=fail_create)

    def close(self) -> None:
        self._transport.closed = True

    def __del__(self) -> None:
        self.close()


def _batch_request() -> BatchModelRequest:
    return BatchModelRequest(
        custom_id="document-1",
        prompt="Document text",
        instructions=("Assess the document",),
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
        model_settings={},
        limits={"max_output_tokens": 100},
    )


def test_google_batch_operations_keep_client_alive_and_close_it(monkeypatch) -> None:
    provider = GoogleBatchProvider()
    transports: list[_FakeTransport] = []

    def client_factory() -> _FakeClient:
        transport = _FakeTransport()
        transports.append(transport)
        return _FakeClient(transport)

    monkeypatch.setattr(provider, "_client", client_factory)

    submission = provider.submit(
        model="gemini-test",
        requests=[_batch_request()],
        display_name="assessment-test",
    )
    poll = provider.poll(submission.batch_id)
    state, results = provider.results(submission.batch_id)
    provider.cancel(submission.batch_id)

    assert submission.batch_id == "batches/example"
    assert poll.status == "JOB_STATE_SUCCEEDED"
    assert state == "JOB_STATE_SUCCEEDED"
    assert results == []
    assert [transport.calls[0][0] for transport in transports] == ["create", "get", "get", "cancel"]
    assert all(transport.closed for transport in transports)


def test_google_batch_submit_inlines_schema_references(monkeypatch) -> None:
    provider = GoogleBatchProvider()
    transport = _FakeTransport()
    monkeypatch.setattr(provider, "_client", lambda: _FakeClient(transport))
    request = _batch_request()
    request = BatchModelRequest(
        custom_id=request.custom_id,
        prompt=request.prompt,
        instructions=request.instructions,
        output_schema={
            "$defs": {
                "answer": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                }
            },
            "type": "object",
            "properties": {"answer": {"$ref": "#/$defs/answer"}},
            "required": ["answer"],
        },
        model_settings=request.model_settings,
        limits=request.limits,
    )

    provider.submit(
        model="gemini-test",
        requests=[request],
        display_name="assessment-test",
    )

    _, call = transport.calls[0]
    submitted_config = call["src"][0]["config"]
    submitted_schema = submitted_config["response_schema"]
    assert "response_json_schema" not in submitted_config
    assert "$defs" not in submitted_schema
    assert "$ref" not in str(submitted_schema)
    assert "additionalProperties" not in str(submitted_schema)
    assert submitted_schema["properties"]["answer"]["type"] == "object"


def test_google_batch_submit_closes_client_when_submission_fails(monkeypatch) -> None:
    provider = GoogleBatchProvider()
    transport = _FakeTransport()
    monkeypatch.setattr(provider, "_client", lambda: _FakeClient(transport, fail_create=True))

    with pytest.raises(RuntimeError, match="submission failed"):
        provider.submit(
            model="gemini-test",
            requests=[_batch_request()],
            display_name="assessment-test",
        )


class _FakeOpenAIFiles:
    def __init__(self, transport: _FakeTransport) -> None:
        self._transport = transport

    def create(self, **kwargs):
        filename, stream, media_type = kwargs["file"]
        self._transport.calls.append(
            (
                "file_create",
                {
                    "filename": filename,
                    "content": stream.read(),
                    "media_type": media_type,
                    "purpose": kwargs["purpose"],
                },
            )
        )
        return SimpleNamespace(id="file-input")

    def content(self, file_id: str):
        self._transport.calls.append(("file_content", file_id))
        record = {
            "custom_id": "document-1",
            "response": {
                "status_code": 200,
                "request_id": "request-1",
                "body": {
                    "id": "response-1",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": '{"answer":"privileged"}'}
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 21,
                        "input_tokens_details": {"cached_tokens": 8},
                        "output_tokens": 4,
                    },
                },
            },
            "error": None,
        }
        return SimpleNamespace(read=lambda: (json.dumps(record) + "\n").encode())


class _FakeOpenAIBatches:
    def __init__(self, transport: _FakeTransport) -> None:
        self._transport = transport

    def create(self, **kwargs):
        self._transport.calls.append(("batch_create", kwargs))
        return SimpleNamespace(id="batch-1", status="validating")

    def retrieve(self, batch_id: str):
        self._transport.calls.append(("batch_retrieve", batch_id))
        return SimpleNamespace(
            id=batch_id,
            status="completed",
            output_file_id="file-output",
            error_file_id=None,
            errors=None,
        )

    def cancel(self, batch_id: str):
        self._transport.calls.append(("batch_cancel", batch_id))


class _FakeOpenAIClient:
    def __init__(self, transport: _FakeTransport) -> None:
        self._transport = transport
        self.files = _FakeOpenAIFiles(transport)
        self.batches = _FakeOpenAIBatches(transport)

    def close(self) -> None:
        self._transport.closed = True


def test_openai_batch_submit_uses_responses_api_and_structured_output(monkeypatch) -> None:
    provider = OpenAIBatchProvider()
    transport = _FakeTransport()
    monkeypatch.setattr(provider, "_client", lambda: _FakeOpenAIClient(transport))
    request = _batch_request()
    request = BatchModelRequest(
        **{
            **request.__dict__,
            "model_settings": {"openai_reasoning_effort": "none"},
        }
    )

    submission = provider.submit(
        model="gpt-6-luna",
        requests=[request],
        display_name="privilege-evidence-test",
    )

    assert submission.batch_id == "batch-1"
    assert submission.status == "JOB_STATE_PENDING"
    upload = transport.calls[0][1]
    record = json.loads(upload["content"])
    assert record["url"] == "/v1/responses"
    assert record["body"]["model"] == "gpt-6-luna"
    assert record["body"]["reasoning"] == {"effort": "none"}
    assert record["body"]["text"]["format"]["strict"] is True
    assert record["body"]["text"]["format"]["schema"]["required"] == ["answer"]
    assert record["body"]["text"]["format"]["schema"]["additionalProperties"] is False
    assert "temperature" not in record["body"]
    assert transport.calls[1] == (
        "batch_create",
        {
            "input_file_id": "file-input",
            "endpoint": "/v1/responses",
            "completion_window": "24h",
            "metadata": {"display_name": "privilege-evidence-test"},
        },
    )
    assert transport.closed


def test_openai_batch_results_parse_output_and_usage(monkeypatch) -> None:
    provider = OpenAIBatchProvider()
    transport = _FakeTransport()
    monkeypatch.setattr(provider, "_client", lambda: _FakeOpenAIClient(transport))

    state, results = provider.results("batch-1")

    assert state == "JOB_STATE_SUCCEEDED"
    assert len(results) == 1
    result = results[0]
    assert result.custom_id == "document-1"
    assert result.output_text == '{"answer":"privileged"}'
    assert result.provider_request_id == "request-1"
    assert result.input_tokens == 21
    assert result.cached_input_tokens == 8
    assert result.output_tokens == 4
    assert result.error is None
    assert transport.closed
