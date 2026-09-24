from dataclasses import dataclass
from typing import Any, Protocol

from google import genai


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

    def poll(self, batch_id: str) -> BatchPoll: ...

    def results(self, batch_id: str) -> tuple[str, list[BatchModelResult]]: ...

    def cancel(self, batch_id: str) -> None: ...


def _supported_gemini_schema(value: Any) -> Any:
    supported = {
        "$id",
        "$defs",
        "$ref",
        "$anchor",
        "type",
        "format",
        "title",
        "description",
        "enum",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "anyOf",
        "oneOf",
        "properties",
        "additionalProperties",
        "required",
        "propertyOrdering",
    }
    if isinstance(value, list):
        return [_supported_gemini_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: _supported_gemini_schema(item) for key, item in value.items() if key in supported}


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
                "response_json_schema": _supported_gemini_schema(request.output_schema),
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
        job = self._client().batches.create(
            model=model,
            src=source,
            config={"display_name": display_name},
        )
        if not job.name:
            raise ValueError("Gemini Batch API did not return a batch name")
        return BatchSubmission(batch_id=job.name, status=self._state(job))

    def poll(self, batch_id: str) -> BatchPoll:
        job = self._client().batches.get(name=batch_id)
        return BatchPoll(status=self._state(job), error=str(job.error) if job.error else None)

    def results(self, batch_id: str) -> tuple[str, list[BatchModelResult]]:
        job = self._client().batches.get(name=batch_id)
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
        return self._state(job), values

    def cancel(self, batch_id: str) -> None:
        self._client().batches.cancel(name=batch_id)


_ADAPTERS: dict[str, BatchProviderAdapter] = {"google": GoogleBatchProvider()}
_MODEL_PREFIXES = {"google": "google", "google-gla": "google"}


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
