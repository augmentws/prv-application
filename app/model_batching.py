import json
from dataclasses import dataclass
from io import BytesIO
from typing import Any, ClassVar, Protocol

from google import genai
from openai import OpenAI

from app.provider_schemas import provider_output_schema


@dataclass(frozen=True)
class BatchModelTarget:
    provider: str
    model: str


@dataclass(frozen=True)
class BatchModelRequest:
    custom_id: str
    prompt: str
    instructions: tuple[str, ...]
    output_schema: dict[str, Any]
    model_settings: dict[str, Any]
    limits: dict[str, Any]


@dataclass(frozen=True)
class BatchSubmission:
    batch_id: str
    status: str


@dataclass(frozen=True)
class BatchPoll:
    status: str
    error: str | None = None


@dataclass(frozen=True)
class BatchModelResult:
    custom_id: str
    output_text: str | None
    provider_request_id: str | None
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


class BatchProviderAdapter(Protocol):
    provider: str

    def submit(
        self,
        *,
        model: str,
        requests: list[BatchModelRequest],
        display_name: str,
    ) -> BatchSubmission: ...

    def find(self, display_name: str) -> BatchSubmission | None: ...

    def poll(self, batch_id: str) -> BatchPoll: ...

    def results(self, batch_id: str) -> tuple[str, list[BatchModelResult]]: ...

    def cancel(self, batch_id: str) -> None: ...


class GoogleBatchProvider:
    provider = "google"

    def _client(self) -> genai.Client:
        return genai.Client()

    @staticmethod
    def _state(job) -> str:
        state = job.state
        return str(getattr(state, "value", state or "JOB_STATE_UNSPECIFIED"))

    def submit(
        self,
        *,
        model: str,
        requests: list[BatchModelRequest],
        display_name: str,
    ) -> BatchSubmission:
        source: list[dict[str, Any]] = []
        for request in requests:
            provider_config: dict[str, Any] = {
                "system_instruction": "\n\n".join(request.instructions),
                "response_mime_type": "application/json",
                "response_schema": provider_output_schema(
                    request.output_schema,
                    provider="google",
                    inline_references=True,
                    include_additional_properties=False,
                ),
                "max_output_tokens": request.limits.get("max_output_tokens"),
            }
            for key in ("temperature", "top_p", "top_k", "seed"):
                if key in request.model_settings:
                    provider_config[key] = request.model_settings[key]
            source.append(
                {
                    "contents": request.prompt,
                    "metadata": {"custom_id": request.custom_id},
                    "config": provider_config,
                }
            )
        client = self._client()
        try:
            job = client.batches.create(
                model=model,
                src=source,
                config={"display_name": display_name},
            )
        finally:
            client.close()
        if not job.name:
            raise ValueError("Gemini Batch API did not return a batch name")
        return BatchSubmission(batch_id=job.name, status=self._state(job))

    def find(self, display_name: str, *, max_pages: int = 5) -> BatchSubmission | None:
        """The most recent batch with this display name, searched across a bounded number of list pages."""

        client = self._client()
        try:
            pager = client.batches.list(config={"page_size": 100})
            for index, job in enumerate(pager):
                if index >= max_pages * 100:
                    break
                if job.display_name == display_name and job.name:
                    return BatchSubmission(batch_id=job.name, status=self._state(job))
        finally:
            client.close()
        return None

    def poll(self, batch_id: str) -> BatchPoll:
        client = self._client()
        try:
            job = client.batches.get(name=batch_id)
        finally:
            client.close()
        return BatchPoll(status=self._state(job), error=str(job.error) if job.error else None)

    def results(self, batch_id: str) -> tuple[str, list[BatchModelResult]]:
        client = self._client()
        try:
            job = client.batches.get(name=batch_id)
            values: list[BatchModelResult] = []
            responses = list(job.dest.inlined_responses or []) if job.dest else []
            for item in responses:
                custom_id = str((item.metadata or {}).get("custom_id") or "")
                response = item.response
                usage = response.usage_metadata if response else None
                values.append(
                    BatchModelResult(
                        custom_id=custom_id,
                        output_text=response.text if response else None,
                        provider_request_id=response.response_id if response else None,
                        input_tokens=int(usage.prompt_token_count or 0) if usage else 0,
                        cached_input_tokens=int(usage.cached_content_token_count or 0) if usage else 0,
                        output_tokens=int(usage.candidates_token_count or 0) if usage else 0,
                        error=str(item.error) if item.error else None,
                    )
                )
        finally:
            client.close()
        return self._state(job), values

    def cancel(self, batch_id: str) -> None:
        client = self._client()
        try:
            client.batches.cancel(name=batch_id)
        finally:
            client.close()


class OpenAIBatchProvider:
    provider = "openai"

    _STATUS_MAP: ClassVar[dict[str, str]] = {
        "validating": "JOB_STATE_PENDING",
        "in_progress": "JOB_STATE_RUNNING",
        "finalizing": "JOB_STATE_RUNNING",
        "completed": "JOB_STATE_SUCCEEDED",
        "failed": "JOB_STATE_FAILED",
        "expired": "JOB_STATE_EXPIRED",
        "cancelling": "JOB_STATE_RUNNING",
        "cancelled": "JOB_STATE_CANCELLED",
        "canceled": "JOB_STATE_CANCELLED",
    }

    def _client(self) -> OpenAI:
        return OpenAI()

    @classmethod
    def _state(cls, batch: Any) -> str:
        return cls._STATUS_MAP.get(str(getattr(batch, "status", "")), "JOB_STATE_UNSPECIFIED")

    @staticmethod
    def _error(batch: Any) -> str | None:
        errors = getattr(batch, "errors", None)
        if errors is None:
            return None
        if hasattr(errors, "model_dump"):
            return json.dumps(errors.model_dump(mode="json"), sort_keys=True)
        return str(errors)

    @staticmethod
    def _response_text(body: dict[str, Any]) -> str | None:
        values: list[str] = []
        for item in body.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if isinstance(content, dict) and content.get("type") == "output_text":
                    text = content.get("text")
                    if isinstance(text, str):
                        values.append(text)
        return "".join(values) or None

    @staticmethod
    def _jsonl_records(client: OpenAI, file_id: str | None) -> list[dict[str, Any]]:
        if not file_id:
            return []
        response = client.files.content(file_id)
        content = response.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        return [json.loads(line) for line in content.splitlines() if line.strip()]

    def submit(
        self,
        *,
        model: str,
        requests: list[BatchModelRequest],
        display_name: str,
    ) -> BatchSubmission:
        lines: list[bytes] = []
        for request in requests:
            body: dict[str, Any] = {
                "model": model,
                "instructions": "\n\n".join(request.instructions),
                "input": request.prompt,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "batch_result",
                        "schema": provider_output_schema(request.output_schema, provider="openai"),
                        "strict": True,
                    }
                },
            }
            max_output_tokens = request.limits.get("max_output_tokens")
            if max_output_tokens is not None:
                body["max_output_tokens"] = max_output_tokens
            for key in ("temperature", "top_p"):
                if key in request.model_settings:
                    body[key] = request.model_settings[key]
            effort = request.model_settings.get("openai_reasoning_effort")
            if effort is not None:
                body["reasoning"] = {"effort": effort}
            lines.append(
                json.dumps(
                    {
                        "custom_id": request.custom_id,
                        "method": "POST",
                        "url": "/v1/responses",
                        "body": body,
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
        client = self._client()
        try:
            uploaded = client.files.create(
                file=("batch-requests.jsonl", BytesIO(b"".join(lines)), "application/jsonl"),
                purpose="batch",
            )
            batch = client.batches.create(
                input_file_id=uploaded.id,
                endpoint="/v1/responses",
                completion_window="24h",
                metadata={"display_name": display_name},
            )
        finally:
            client.close()
        return BatchSubmission(batch_id=batch.id, status=self._state(batch))

    def find(self, display_name: str, *, max_pages: int = 5) -> BatchSubmission | None:
        client = self._client()
        try:
            after: str | None = None
            for _ in range(max_pages):
                page = client.batches.list(limit=100, **({"after": after} if after else {}))
                values = list(page.data)
                for batch in values:
                    metadata = getattr(batch, "metadata", None) or {}
                    if metadata.get("display_name") == display_name:
                        return BatchSubmission(batch_id=batch.id, status=self._state(batch))
                if not values or not page.has_next_page():
                    break
                after = values[-1].id
        finally:
            client.close()
        return None

    def poll(self, batch_id: str) -> BatchPoll:
        client = self._client()
        try:
            batch = client.batches.retrieve(batch_id)
        finally:
            client.close()
        return BatchPoll(status=self._state(batch), error=self._error(batch))

    def results(self, batch_id: str) -> tuple[str, list[BatchModelResult]]:
        client = self._client()
        try:
            batch = client.batches.retrieve(batch_id)
            records = self._jsonl_records(client, getattr(batch, "output_file_id", None))
            records.extend(self._jsonl_records(client, getattr(batch, "error_file_id", None)))
        finally:
            client.close()
        values: list[BatchModelResult] = []
        for item in records:
            response = item.get("response") if isinstance(item.get("response"), dict) else {}
            body = response.get("body") if isinstance(response.get("body"), dict) else {}
            error = item.get("error")
            if error is None and response.get("status_code") not in {None, 200}:
                error = body.get("error") or f"HTTP {response.get('status_code')}"
            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            input_details = (
                usage.get("input_tokens_details")
                if isinstance(usage.get("input_tokens_details"), dict)
                else {}
            )
            values.append(
                BatchModelResult(
                    custom_id=str(item.get("custom_id") or ""),
                    output_text=self._response_text(body),
                    provider_request_id=str(response.get("request_id") or body.get("id") or "") or None,
                    input_tokens=int(usage.get("input_tokens") or 0),
                    cached_input_tokens=int(input_details.get("cached_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                    error=(json.dumps(error, sort_keys=True) if isinstance(error, dict) else str(error))
                    if error
                    else None,
                )
            )
        return self._state(batch), values

    def cancel(self, batch_id: str) -> None:
        client = self._client()
        try:
            client.batches.cancel(batch_id)
        finally:
            client.close()


_ADAPTERS: dict[str, BatchProviderAdapter] = {
    "google": GoogleBatchProvider(),
    "openai": OpenAIBatchProvider(),
}
_MODEL_PREFIXES = {"google": "google", "google-gla": "google", "openai": "openai"}


def resolve_batch_target(model_key: str) -> tuple[BatchProviderAdapter, BatchModelTarget] | None:
    prefix, separator, model = model_key.partition(":")
    provider = _MODEL_PREFIXES.get(prefix)
    if not separator or not model or provider is None:
        return None
    adapter = _ADAPTERS.get(provider)
    return (adapter, BatchModelTarget(provider=provider, model=model)) if adapter else None


def get_batch_adapter(provider: str) -> BatchProviderAdapter:
    try:
        return _ADAPTERS[provider]
    except KeyError as exc:
        raise ValueError(f"No Batch API adapter is registered for provider {provider}") from exc
