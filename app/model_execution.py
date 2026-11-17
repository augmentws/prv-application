import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic_ai import Agent, CachePoint, ModelRetry, StructuredDict, UsageLimits
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, ModelResponse

from app.agent_models import resolve_agent_model
from app.config import get_settings
from app.google_prompt_cache import google_prompt_cache
from app.model_rate_limits import ModelRateLimitPolicy, model_rate_limit_hooks
from app.model_tracing import ModelCallTrace, model_call_trace_context, start_model_call_trace
from app.provider_schemas import provider_output_schema
from app.provider_usage import external_model_identity

OutputT = TypeVar("OutputT")
ModelExecutionErrorCode = Literal[
    "INVALID_REQUEST",
    "MODEL_FAILURE",
    "INVALID_OUTPUT",
    "USAGE_LIMIT_EXCEEDED",
    "CANCELED",
]


class ModelExecutionError(RuntimeError):
    def __init__(
        self,
        code: ModelExecutionErrorCode,
        message: str,
        *,
        retryable: bool,
        invocations: tuple["InvocationTelemetry", ...] = (),
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.invocations = invocations
        self.details = details or {}


class StructuredOutputValidationError(ValueError):
    pass


@dataclass(frozen=True)
class InstructionLayer:
    key: str
    content: str


@dataclass(frozen=True)
class PromptAssembly:
    instructions: tuple[str, ...]
    prompt: str | list[Any]
    stable_prefix: str
    dynamic_payload: str
    cache_fingerprint: str
    model_configuration_hash: str
    model_settings: dict[str, Any]
    provider_output_schema: dict[str, Any]


@dataclass(frozen=True)
class StructuredModelRequest:
    instruction_layers: tuple[InstructionLayer, ...]
    stable_context: dict[str, Any]
    dynamic_input: dict[str, Any]
    output_schema: dict[str, Any]
    model_key: str
    model_settings: dict[str, Any]
    limits: dict[str, Any]
    cache_policy: dict[str, Any]
    cache_identity: dict[str, Any]
    provider_output_schema: dict[str, Any] | None = None
    request_type: str = "structured-model"
    output_normalizers: tuple[Callable[[dict[str, Any]], dict[str, Any]], ...] = ()
    output_validators: tuple[Callable[[dict[str, Any]], None], ...] = ()
    run_id: str | None = None
    conversation_id: str | None = None
    rate_limit_policy: ModelRateLimitPolicy | None = None


@dataclass(frozen=True)
class InvocationTelemetry:
    request_sequence: int
    provider_request_id: str | None
    provider: str
    model: str
    model_configuration_hash: str
    request_count: int
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    latency_ms: int
    started_at: datetime
    completed_at: datetime
    status: Literal["COMPLETED", "FAILED"] = "COMPLETED"
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class OutputValidationFailure:
    category: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"category": self.category, "message": self.message}


@dataclass(frozen=True)
class ModelRunEnvelope(Generic[OutputT]):
    output: OutputT
    message_history: list[dict[str, Any]]
    request_count: int
    tool_call_count: int
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    provider: str | None
    provider_model: str | None
    model_configuration_hash: str
    invocations: tuple[InvocationTelemetry, ...]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def usage_limits(limits: dict[str, Any]) -> UsageLimits:
    return UsageLimits(
        request_limit=limits.get("max_requests", 50),
        tool_calls_limit=limits.get("max_tool_calls"),
        input_tokens_limit=limits.get("max_input_tokens"),
        output_tokens_limit=limits.get("max_output_tokens"),
        total_tokens_limit=limits.get("max_total_tokens"),
    )


def _cache_model_settings(
    model_settings: dict[str, Any],
    *,
    provider: str | None,
    cache_fingerprint: str,
    cache_policy: dict[str, Any],
) -> dict[str, Any]:
    settings = dict(model_settings)
    if not cache_policy or cache_policy.get("enabled", True) is False:
        return settings
    ttl = cache_policy.get("ttl", "5m")
    if provider == "openai":
        settings.setdefault("openai_prompt_cache_key", cache_fingerprint)
        if cache_policy.get("explicit_boundary", True):
            settings.setdefault("openai_prompt_cache_options", {"mode": "explicit", "ttl": "30m"})
    elif provider == "anthropic":
        settings.setdefault("anthropic_cache_instructions", ttl)
    elif provider == "openrouter":
        settings.setdefault("openrouter_cache_instructions", ttl)
    elif provider == "mistral":
        settings.setdefault("mistral_prompt_cache_key", cache_fingerprint)
    return settings


def assemble_structured_prompt(
    request: StructuredModelRequest,
    *,
    resolved_model: Any,
) -> PromptAssembly:
    if not request.instruction_layers:
        raise ModelExecutionError("INVALID_REQUEST", "At least one instruction layer is required", retryable=False)
    keys = [layer.key for layer in request.instruction_layers]
    if len(keys) != len(set(keys)):
        raise ModelExecutionError("INVALID_REQUEST", "Instruction layer keys must be unique", retryable=False)
    if any(not layer.content.strip() for layer in request.instruction_layers):
        raise ModelExecutionError("INVALID_REQUEST", "Instruction layers cannot be blank", retryable=False)
    try:
        Draft202012Validator.check_schema(request.output_schema)
    except SchemaError as exc:
        raise ModelExecutionError("INVALID_REQUEST", f"Invalid output schema: {exc.message}", retryable=False) from exc

    provider_identity = external_model_identity(resolved_model)
    provider = provider_identity[0] if provider_identity is not None else None
    admitted_output_schema = request.provider_output_schema or provider_output_schema(
        request.output_schema,
        provider=provider,
    )


    try:
        Draft202012Validator.check_schema(admitted_output_schema)
    except SchemaError as exc:
        raise ModelExecutionError(
            "INVALID_REQUEST",
            f"Invalid provider output schema: {exc.message}",
            retryable=False,
        ) from exc

    output_contract = "Return only JSON matching this output schema:\n" + canonical_json(admitted_output_schema)
    instructions = tuple(layer.content for layer in request.instruction_layers) + (output_contract,)
    stable_prefix = canonical_json({"stable_context": request.stable_context})
    dynamic_payload = canonical_json(request.dynamic_input)
    cache_fingerprint = content_hash(
        {
            "cache_identity": request.cache_identity,
            "instruction_layers": [
                {"key": layer.key, "content": layer.content} for layer in request.instruction_layers
            ],
            "stable_prefix": stable_prefix,
            "output_schema": admitted_output_schema,
            "model_key": request.model_key,
            "model_settings": request.model_settings,
        }
    )
    cache_enabled = bool(request.cache_policy) and request.cache_policy.get("enabled", True) is not False
    prompt: str | list[Any]
    if cache_enabled and stable_prefix and dynamic_payload:
        inline_ttl = "1h" if request.cache_policy.get("ttl") == "1h" else "5m"
        prompt = [stable_prefix, CachePoint(ttl=inline_ttl), dynamic_payload]
    else:
        prompt = f"{stable_prefix}\n{dynamic_payload}"
    settings = _cache_model_settings(
        request.model_settings,
        provider=provider,
        cache_fingerprint=cache_fingerprint,
        cache_policy=request.cache_policy,
    )
    return PromptAssembly(
        instructions=instructions,
        prompt=prompt,
        stable_prefix=stable_prefix,
        dynamic_payload=dynamic_payload,
        cache_fingerprint=cache_fingerprint,
        model_configuration_hash=content_hash(
            {"model_key": request.model_key, "resolved_model": str(resolved_model), "settings": settings}
        ),
        model_settings=settings,
        provider_output_schema=admitted_output_schema,
    )


async def apply_explicit_provider_cache(
    assembly: PromptAssembly,
    request: StructuredModelRequest,
    *,
    resolved_model: Any,
) -> PromptAssembly:
    """Replace a Google stable prefix with an explicit cached-content reference.

    Cache creation is deliberately best-effort. If the prefix is too small, the
    provider rejects the model, or cache creation otherwise fails, the original
    complete prompt is returned and model execution proceeds normally.
    """

    settings = get_settings()
    provider_identity = external_model_identity(resolved_model)
    if (
        provider_identity is None
        or provider_identity[0] != "google"
        or not settings.google_explicit_prompt_cache_enabled
        or request.cache_policy.get("explicit_provider_cache") is not True
    ):
        return assembly
    cacheable_character_count = len(assembly.stable_prefix) + sum(
        len(value) for value in assembly.instructions
    )
    if cacheable_character_count < settings.google_explicit_prompt_cache_min_characters:
        return assembly
    cached = await google_prompt_cache.get_or_create(
        fingerprint=assembly.cache_fingerprint,
        model=provider_identity[1],
        instructions=assembly.instructions,
        stable_prefix=assembly.stable_prefix,
        ttl=request.cache_policy.get("ttl", "10m"),
    )
    if cached is None:
        return assembly
    model_settings = {
        **assembly.model_settings,
        "google_cached_content": cached.name,
    }
    return replace(
        assembly,
        instructions=(),
        prompt=assembly.dynamic_payload,
        model_settings=model_settings,
        model_configuration_hash=content_hash(
            {
                "model_key": request.model_key,
                "resolved_model": str(resolved_model),
                "settings": model_settings,
            }
        ),
    )


def validate_structured_output(output: dict[str, Any], schema: dict[str, Any]) -> None:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(output), key=lambda error: tuple(str(part) for part in error.absolute_path))
    if not errors:
        return
    details = []
    for error in errors[:10]:
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        details.append(f"{location}: {error.message}")
    raise StructuredOutputValidationError("Structured output failed validation: " + "; ".join(details))


def _invocation_telemetry(
    messages: Sequence[Any],
    *,
    provider_identity: tuple[str, str] | None,
    model_configuration_hash: str,
) -> tuple[InvocationTelemetry, ...]:
    invocations: list[InvocationTelemetry] = []
    request_started_at: datetime | None = None
    for message in messages:
        if isinstance(message, ModelRequest):
            request_started_at = message.timestamp
            continue
        if not isinstance(message, ModelResponse):
            continue
        usage = message.usage
        completed_at = message.timestamp
        started_at = request_started_at or completed_at
        latency_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))
        provider = provider_identity[0] if provider_identity is not None else (message.provider_name or "unknown")
        model = provider_identity[1] if provider_identity is not None else (message.model_name or "unknown")
        invocations.append(
            InvocationTelemetry(
                request_sequence=len(invocations) + 1,
                provider_request_id=message.provider_response_id,
                provider=provider,
                model=model,
                model_configuration_hash=model_configuration_hash,
                request_count=max(1, usage.requests),
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=latency_ms,
                started_at=started_at,
                completed_at=completed_at,
            )
        )
        request_started_at = None
    return tuple(invocations)


async def run_model(
    agent: Agent[Any, OutputT],
    *,
    prompt: Any,
    instructions: Sequence[str],
    model: Any,
    model_settings: dict[str, Any],
    limits: dict[str, Any],
    model_configuration_hash: str,
    deps: Any = None,
    message_history: Any = None,
    deferred_tool_results: Any = None,
    run_id: str | None = None,
    conversation_id: str | None = None,
    request_type: str = "model-request",
    trace_identifier: str | None = None,
    trace: ModelCallTrace | None = None,
    rate_limit_capability_registered: bool = False,
    rate_limit_policy: ModelRateLimitPolicy | None = None,
    invocation_sink: list[InvocationTelemetry] | None = None,
) -> ModelRunEnvelope[OutputT]:
    settings = get_settings()
    provider_identity = external_model_identity(model)
    active_trace = trace or start_model_call_trace(
        settings,
        request_type=request_type,
        identifier=trace_identifier or run_id,
        request={
            "instructions": list(instructions),
            "prompt": prompt,
            "message_history": message_history,
            "deferred_tool_results": deferred_tool_results,
            "conversation_id": conversation_id,
            "run_id": run_id,
            "model": str(model),
            "provider": provider_identity[0] if provider_identity is not None else None,
            "model_settings": model_settings,
            "limits": limits,
            "model_configuration_hash": model_configuration_hash,
        },
    )
    captured_invocations = invocation_sink if invocation_sink is not None else []

    def capture_response(
        _request_context: Any, response: ModelResponse, started_at: datetime, completed_at: datetime
    ) -> None:
        usage = response.usage
        provider = provider_identity[0] if provider_identity is not None else (response.provider_name or "unknown")
        provider_model = provider_identity[1] if provider_identity is not None else (response.model_name or "unknown")
        captured_invocations.append(
            InvocationTelemetry(
                request_sequence=len(captured_invocations) + 1,
                provider_request_id=response.provider_response_id,
                provider=provider,
                model=provider_model,
                model_configuration_hash=model_configuration_hash,
                request_count=max(1, usage.requests),
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=max(0, int((completed_at - started_at).total_seconds() * 1000)),
                started_at=started_at,
                completed_at=completed_at,
            )
        )

    try:
        with model_call_trace_context(active_trace):
            result = await agent.run(
                prompt,
                message_history=message_history,
                deferred_tool_results=deferred_tool_results,
                conversation_id=conversation_id,
                run_id=run_id,
                model=model,
                instructions=list(instructions),
                deps=deps,
                model_settings=model_settings,
                usage_limits=usage_limits(limits),
                capabilities=(
                    None
                    if rate_limit_capability_registered
                    else [
                        model_rate_limit_hooks(
                            settings,
                            policy=rate_limit_policy,
                            trace=active_trace,
                            on_response=capture_response,
                        )
                    ]
                ),
            )
    except ModelExecutionError as exc:
        if active_trace is not None:
            active_trace.fail(exc)
        raise
    except ModelHTTPError as exc:
        if active_trace is not None:
            active_trace.fail(exc)
        retryable = exc.status_code in {408, 409, 425, 429} or exc.status_code >= 500
        raise ModelExecutionError(
            "MODEL_FAILURE" if retryable else "INVALID_REQUEST",
            str(exc),
            retryable=retryable,
            invocations=tuple(captured_invocations),
            details={"exception_type": type(exc).__name__},
        ) from exc
    except Exception as exc:
        if active_trace is not None:
            active_trace.fail(exc)
        name = type(exc).__name__
        if "UsageLimit" in name:
            raise ModelExecutionError(
                "USAGE_LIMIT_EXCEEDED",
                str(exc),
                retryable=False,
                invocations=tuple(captured_invocations),
                details={"exception_type": name},
            ) from exc
        if "UnexpectedModelBehavior" in name or "Validation" in name:
            raise ModelExecutionError(
                "INVALID_OUTPUT",
                str(exc),
                retryable=True,
                invocations=tuple(captured_invocations),
                details={"exception_type": name},
            ) from exc
        raise ModelExecutionError(
            "MODEL_FAILURE",
            str(exc),
            retryable=True,
            invocations=tuple(captured_invocations),
            details={"exception_type": name},
        ) from exc
    usage = result.usage
    messages = json.loads(result.all_messages_json())
    if active_trace is not None:
        active_trace.complete(
            {
                "output": result.output,
                "messages": messages,
                "usage": {
                    "requests": usage.requests,
                    "tool_calls": usage.tool_calls,
                    "input_tokens": usage.input_tokens,
                    "cached_input_tokens": usage.cache_read_tokens,
                    "cache_write_tokens": usage.cache_write_tokens,
                    "output_tokens": usage.output_tokens,
                },
            }
        )
    return ModelRunEnvelope(
        output=result.output,
        message_history=messages,
        request_count=usage.requests,
        tool_call_count=usage.tool_calls,
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
        provider=provider_identity[0] if provider_identity is not None else None,
        provider_model=provider_identity[1] if provider_identity is not None else None,
        model_configuration_hash=model_configuration_hash,
        invocations=(
            tuple(captured_invocations)
            or _invocation_telemetry(
                result.new_messages(),
                provider_identity=provider_identity,
                model_configuration_hash=model_configuration_hash,
            )
        ),
    )


async def execute_structured_model(
    request: StructuredModelRequest,
    *,
    model: Any | None = None,
) -> tuple[ModelRunEnvelope[dict[str, Any]], PromptAssembly]:
    selected_model = model if model is not None else resolve_agent_model(request.model_key, get_settings())
    assembly = assemble_structured_prompt(request, resolved_model=selected_model)
    assembly = await apply_explicit_provider_cache(
        assembly,
        request,
        resolved_model=selected_model,
    )
    admitted_output_schema = assembly.provider_output_schema
    trace = start_model_call_trace(
        get_settings(),
        request_type=request.request_type,
        identifier=request.run_id,
        request={
            "instruction_layers": [
                {"key": layer.key, "content": layer.content} for layer in request.instruction_layers
            ],
            "stable_context": request.stable_context,
            "dynamic_input": request.dynamic_input,
            "output_schema": request.output_schema,
            "provider_output_schema": admitted_output_schema,
            "model_key": request.model_key,
            "resolved_model": str(selected_model),
            "model_settings": assembly.model_settings,
            "limits": request.limits,
            "cache_policy": request.cache_policy,
            "cache_identity": request.cache_identity,
            "cache_fingerprint": assembly.cache_fingerprint,
            "run_id": request.run_id,
            "conversation_id": request.conversation_id,
        },
    )
    agent: Agent[None, dict[str, Any]] = Agent(
        selected_model,
        name="priv_view_structured_model_executor",
        output_type=StructuredDict(
            admitted_output_schema,
            name=f"{request.request_type.replace('-', '_')}_result",
            description="Return a result that exactly matches the supplied JSON schema.",
        ),
        retries={"output": request.limits.get("max_output_retries", 2), "tools": 0},
        defer_model_check=True,
    )
    invocation_sink: list[InvocationTelemetry] = []
    validation_failures: list[OutputValidationFailure] = []

    def record_validation_failure(category: str, exc: BaseException) -> None:
        message = str(exc)
        validation_failures.append(OutputValidationFailure(category=category, message=message))
        if invocation_sink:
            invocation_sink[-1] = replace(
                invocation_sink[-1],
                error_code=category,
                error_message=message[:4000],
            )
        if trace is not None:
            trace.event(
                "output_validation_failed",
                {"category": category, "message": message},
            )

    def request_retry(category: str, exc: BaseException) -> None:
        record_validation_failure(category, exc)
        raise ModelRetry(str(exc)) from exc

    @agent.output_validator
    def validate_output(output: dict[str, Any]) -> dict[str, Any]:
        raw_output = output
        try:
            for normalizer in request.output_normalizers:
                output = normalizer(output)
                if not isinstance(output, dict):
                    raise StructuredOutputValidationError("An output normalizer returned a non-object result")
        except (StructuredOutputValidationError, KeyError, TypeError, ValueError) as exc:
            request_retry("ANALYSIS_OUTPUT_NORMALIZATION_INVALID", exc)
        if trace is not None and output != raw_output:
            trace.event(
                "output_normalized",
                {"raw_output": raw_output, "normalized_output": output},
            )
        try:
            validate_structured_output(output, request.output_schema)
        except StructuredOutputValidationError as exc:
            request_retry("ANALYSIS_OUTPUT_SCHEMA_INVALID", exc)
        try:
            for validator in request.output_validators:
                validator(output)
        except ValueError as exc:
            category = (
                "ANALYSIS_OUTPUT_CITATION_INVALID"
                if type(exc).__name__ == "CitationValidationError"
                else "ANALYSIS_OUTPUT_SEMANTIC_INVALID"
            )
            request_retry(category, exc)
        return output

    envelope: ModelRunEnvelope[dict[str, Any]]
    try:
        envelope = await run_model(
            agent,
            prompt=assembly.prompt,
            instructions=assembly.instructions,
            model=selected_model,
            model_settings=assembly.model_settings,
            limits=request.limits,
            model_configuration_hash=assembly.model_configuration_hash,
            run_id=request.run_id,
            conversation_id=request.conversation_id,
            request_type=request.request_type,
            trace_identifier=request.run_id,
            trace=trace,
            invocation_sink=invocation_sink,
            rate_limit_policy=request.rate_limit_policy,
        )
    except ModelExecutionError as exc:
        if exc.code == "INVALID_OUTPUT":
            for index, invocation in enumerate(invocation_sink):
                if invocation.error_code is None:
                    invocation_sink[index] = replace(
                        invocation,
                        error_code="ANALYSIS_PROVIDER_OUTPUT_UNPARSEABLE",
                        error_message=str(exc)[:4000],
                    )
        elif exc.code == "USAGE_LIMIT_EXCEEDED" and invocation_sink:
            final_index = len(invocation_sink) - 1
            if invocation_sink[final_index].error_code is None:
                invocation_sink[final_index] = replace(
                    invocation_sink[final_index],
                    error_code="ANALYSIS_OUTPUT_USAGE_LIMIT_EXCEEDED",
                    error_message=str(exc)[:4000],
                )
        failure_details = {
            **exc.details,
            "validation_failures": [failure.as_dict() for failure in validation_failures],
        }
        message = str(exc)
        if validation_failures:
            messages = "; ".join(dict.fromkeys(failure.message for failure in validation_failures))
            message = f"{message}. Output validation failures: {messages}"[:4000]
        raise ModelExecutionError(
            exc.code,
            message,
            retryable=exc.retryable,
            invocations=tuple(invocation_sink) or exc.invocations,
            details=failure_details,
        ) from exc

    for index, invocation in enumerate(invocation_sink[:-1]):
        if invocation.error_code is None:
            invocation_sink[index] = replace(
                invocation,
                error_code="ANALYSIS_PROVIDER_OUTPUT_UNPARSEABLE",
                error_message="Provider output did not satisfy the admitted structured-output contract",
            )
    if invocation_sink:
        envelope = replace(envelope, invocations=tuple(invocation_sink))
    return envelope, assembly
