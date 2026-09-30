from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.decision_engine import DecisionEngineRegistry, DecisionEnvelope, DecisionRequest
from app.model_execution import InvocationTelemetry, content_hash
from app.model_tracing import start_model_call_trace


@dataclass(frozen=True)
class DecisionExecutionEnvelope:
    decision: DecisionEnvelope
    model_configuration_hash: str
    invocations: tuple[InvocationTelemetry, ...]


async def execute_decision_request(
    request: DecisionRequest,
    *,
    engine_key: str,
    registry: DecisionEngineRegistry,
    settings: Settings,
) -> DecisionExecutionEnvelope:
    """Evaluate one typed request and expose standard invocation telemetry for the shared ledger."""

    model_configuration_hash = content_hash(
        {
            "engine_key": engine_key,
            "model_key": request.model_key,
            "model_settings": request.model_settings,
            "limits": request.limits,
        }
    )
    trace = start_model_call_trace(
        settings,
        request_type=request.request_type,
        identifier=request.run_id or request.idempotency_key,
        request={
            "engine_key": engine_key,
            "model_key": request.model_key,
            "model_settings": request.model_settings,
            "limits": request.limits,
            "run_id": request.run_id,
            "idempotency_key": request.idempotency_key,
            "state": request.state,
            "questions": {
                key: question.model_dump(mode="json") for key, question in request.questions.items()
            },
            "model_configuration_hash": model_configuration_hash,
        },
    )
    try:
        decision = await registry.get(engine_key).evaluate(request)
    except Exception as exc:
        if trace is not None:
            trace.fail(exc)
        raise
    if trace is not None:
        trace.complete(decision.model_dump(mode="json"))
    invocation = InvocationTelemetry(
        request_sequence=1,
        provider_request_id=decision.provider_request_id,
        provider=decision.provider,
        model=decision.model,
        model_configuration_hash=model_configuration_hash,
        request_count=decision.usage.request_count,
        input_tokens=decision.usage.input_tokens,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=decision.usage.output_tokens,
        latency_ms=decision.latency_ms,
        started_at=decision.started_at,
        completed_at=decision.completed_at,
    )
    return DecisionExecutionEnvelope(
        decision=decision,
        model_configuration_hash=model_configuration_hash,
        invocations=(invocation,),
    )
