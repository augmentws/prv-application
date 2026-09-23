import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic_ai import Agent, CachePoint, ModelRetry, StructuredDict, UsageLimits
from pydantic_ai.messages import ModelRequest, ModelResponse

from app.agent_models import resolve_agent_model
from app.config import get_settings
from app.model_rate_limits import model_rate_limit_hooks
from app.model_tracing import ModelCallTrace, model_call_trace_context, start_model_call_trace
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
    def __init__(self, code: ModelExecutionErrorCode, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


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
    request_type: str = "structured-model"
    output_validators: tuple[Callable[[dict[str, Any]], None], ...] = ()
    run_id: str | None = None
    conversation_id: str | None = None


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

    output_contract = "Return only JSON matching this output schema:\n" + canonical_json(request.output_schema)
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
            "output_schema": request.output_schema,
            "model_key": request.model_key,
            "model_settings": request.model_settings,
        }
    )
    provider_identity = external_model_identity(resolved_model)
    provider = provider_identity[0] if provider_identity is not None else None
    cache_enabled = bool(request.cache_policy) and request.cache_policy.get("enabled", True) is not False
    prompt: str | list[Any]
    if cache_enabled and stable_prefix and dynamic_payload:
        prompt = [stable_prefix, CachePoint(ttl=request.cache_policy.get("ttl", "5m")), dynamic_payload]
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
                    else [model_rate_limit_hooks(settings, trace=active_trace)]
                ),
            )
    except ModelExecutionError as exc:
        if active_trace is not None:
            active_trace.fail(exc)
        raise
    except Exception as exc:
        if active_trace is not None:
            active_trace.fail(exc)
        name = type(exc).__name__
        if "UsageLimit" in name:
            raise ModelExecutionError("USAGE_LIMIT_EXCEEDED", str(exc), retryable=False) from exc
        if "UnexpectedModelBehavior" in name or "Validation" in name:
            raise ModelExecutionError("INVALID_OUTPUT", str(exc), retryable=True) from exc
        raise ModelExecutionError("MODEL_FAILURE", str(exc), retryable=True) from exc
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
        invocations=_invocation_telemetry(
            result.new_messages(),
            provider_identity=provider_identity,
            model_configuration_hash=model_configuration_hash,
        ),
    )


async def execute_structured_model(
    request: StructuredModelRequest,
    *,
    model: Any | None = None,
) -> tuple[ModelRunEnvelope[dict[str, Any]], PromptAssembly]:
    selected_model = model if model is not None else resolve_agent_model(request.model_key, get_settings())
    assembly = assemble_structured_prompt(request, resolved_model=selected_model)
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
            request.output_schema,
            name=f"{request.request_type.replace('-', '_')}_result",
            description="Return a result that exactly matches the supplied JSON schema.",
        ),
        retries={"output": request.limits.get("max_output_retries", 2), "tools": 0},
        defer_model_check=True,
    )
    validation_failures: list[str] = []

    @agent.output_validator
    def validate_output(output: dict[str, Any]) -> dict[str, Any]:
        try:
            validate_structured_output(output, request.output_schema)
            for validator in request.output_validators:
                validator(output)
        except (StructuredOutputValidationError, ValueError) as exc:
            message = str(exc)
            validation_failures.append(message)
            if trace is not None:
                trace.event("output_validation_failed", {"message": message, "output": output})
            raise ModelRetry(message) from exc
        return output

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
        )
    except ModelExecutionError as exc:
        if exc.code == "INVALID_OUTPUT" and validation_failures:
            details = "; ".join(dict.fromkeys(validation_failures))
            raise ModelExecutionError(
                "INVALID_OUTPUT",
                f"{exc}. Output validation failures: {details}"[:4000],
                retryable=exc.retryable,
            ) from exc
        raise
    return envelope, assembly
