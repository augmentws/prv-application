import asyncio
import json
import logging
import math
import random
import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from pydantic_ai.capabilities import Hooks
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import RunContext
from pydantic_core import to_jsonable_python

from app.config import Settings
from app.model_tracing import ModelCallTrace, current_model_call_trace
from app.provider_usage import external_model_identity

logger = logging.getLogger(__name__)
_SECONDS_DURATION = re.compile(r"^\s*(?P<seconds>\d+(?:\.\d+)?)s\s*$", re.IGNORECASE)
_RETRY_IN_SECONDS = re.compile(r"retry\s+in\s+(?P<seconds>\d+(?:\.\d+)?)s", re.IGNORECASE)


@dataclass(frozen=True)
class ModelRateLimitPolicy:
    requests_per_minute: int
    input_tokens_per_minute: int
    max_retries: int
    retry_base_seconds: float
    retry_max_seconds: float
    characters_per_token: float

    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelRateLimitPolicy":
        return cls(
            requests_per_minute=settings.model_rate_limit_requests_per_minute,
            input_tokens_per_minute=settings.model_rate_limit_input_tokens_per_minute,
            max_retries=settings.model_rate_limit_max_retries,
            retry_base_seconds=settings.model_rate_limit_retry_base_seconds,
            retry_max_seconds=settings.model_rate_limit_retry_max_seconds,
            characters_per_token=settings.model_rate_limit_characters_per_token,
        )


class ProviderModelRateLimiter:
    """Process-wide sliding-window RPM/TPM limiter keyed by provider and model."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._monotonic = monotonic
        self._sleep = sleep
        self._events: dict[tuple[str, str], deque[tuple[float, int]]] = defaultdict(deque)
        self._blocked_until: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    async def acquire(
        self,
        key: tuple[str, str],
        *,
        estimated_input_tokens: int,
        requests_per_minute: int,
        input_tokens_per_minute: int,
    ) -> float:
        estimated_input_tokens = max(1, min(estimated_input_tokens, input_tokens_per_minute))
        total_wait = 0.0
        while True:
            with self._lock:
                now = self._monotonic()
                events = self._events[key]
                cutoff = now - 60.0
                while events and events[0][0] <= cutoff:
                    events.popleft()

                blocked_wait = max(0.0, self._blocked_until.get(key, 0.0) - now)
                used_tokens = sum(tokens for _, tokens in events)
                within_limits = (
                    len(events) < requests_per_minute
                    and used_tokens + estimated_input_tokens <= input_tokens_per_minute
                )
                if blocked_wait <= 0 and within_limits:
                    events.append((now, estimated_input_tokens))
                    return total_wait

                window_wait = 0.0 if within_limits or not events else max(0.01, 60.0 - (now - events[0][0]))
                wait_seconds = max(0.01, blocked_wait, window_wait)
            total_wait += wait_seconds
            await self._sleep(wait_seconds)

    def defer(self, key: tuple[str, str], delay_seconds: float) -> None:
        with self._lock:
            blocked_until = self._monotonic() + max(0.0, delay_seconds)
            self._blocked_until[key] = max(self._blocked_until.get(key, 0.0), blocked_until)


def _provider_model_key(request_context: ModelRequestContext) -> tuple[str, str]:
    identity = external_model_identity(request_context.model_id)
    if identity is not None:
        return identity
    model = request_context.model
    provider = str(getattr(model, "system", None) or "unknown").strip().lower()
    model_name = str(getattr(model, "model_name", None) or type(model).__name__).strip()
    return provider or "unknown", model_name or "unknown"


def estimate_model_request_input_tokens(
    request_context: ModelRequestContext,
    *,
    characters_per_token: float,
) -> int:
    payload = to_jsonable_python(
        {
            "messages": request_context.messages,
            "model_request_parameters": request_context.model_request_parameters,
        },
        fallback=lambda item: str(item),
    )
    character_count = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return max(1, math.ceil(character_count / characters_per_token))


def _duration_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if not isinstance(value, str):
        return None
    if match := _SECONDS_DURATION.match(value):
        return float(match.group("seconds"))
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _body_retry_delay(value: Any) -> float | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower().replace("_", "") == "retrydelay" and (delay := _duration_seconds(child)):
                return delay
            if delay := _body_retry_delay(child):
                return delay
    elif isinstance(value, list):
        for child in value:
            if delay := _body_retry_delay(child):
                return delay
    return None


def provider_retry_delay_seconds(error: ModelHTTPError, *, now: datetime | None = None) -> float | None:
    retry_after = (error.headers or {}).get("retry-after")
    if retry_after:
        if delay := _duration_seconds(retry_after):
            return delay
        try:
            retry_at = parsedate_to_datetime(retry_after)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - (now or datetime.now(timezone.utc))).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    if delay := _body_retry_delay(error.body):
        return delay
    if match := _RETRY_IN_SECONDS.search(str(error)):
        return float(match.group("seconds"))
    return None


class ProviderModelRequestController:
    def __init__(
        self,
        *,
        limiter: ProviderModelRateLimiter,
        policy: ModelRateLimitPolicy,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._limiter = limiter
        self._policy = policy
        self._jitter = jitter

    async def execute(
        self,
        request_context: ModelRequestContext,
        handler: Callable[[ModelRequestContext], Awaitable[ModelResponse]],
        *,
        trace: ModelCallTrace | None = None,
    ) -> ModelResponse:
        key = _provider_model_key(request_context)
        if key[0] in {"test", "unknown"}:
            return await handler(request_context)
        estimated_tokens = estimate_model_request_input_tokens(
            request_context,
            characters_per_token=self._policy.characters_per_token,
        )
        for attempt in range(self._policy.max_retries + 1):
            waited = await self._limiter.acquire(
                key,
                estimated_input_tokens=estimated_tokens,
                requests_per_minute=self._policy.requests_per_minute,
                input_tokens_per_minute=self._policy.input_tokens_per_minute,
            )
            if waited >= 0.01:
                logger.info(
                    "Model request rate limited provider=%s model=%s wait_seconds=%.2f estimated_input_tokens=%s",
                    key[0],
                    key[1],
                    waited,
                    estimated_tokens,
                )
                if trace is not None:
                    trace.event(
                        "provider_model_rate_limit_wait",
                        {
                            "provider": key[0],
                            "model": key[1],
                            "wait_seconds": waited,
                            "estimated_input_tokens": estimated_tokens,
                        },
                    )
            try:
                return await handler(request_context)
            except ModelHTTPError as exc:
                if exc.status_code != 429 or attempt >= self._policy.max_retries:
                    raise
                provider_delay = provider_retry_delay_seconds(exc)
                exponential_delay = self._policy.retry_base_seconds * (2**attempt)
                delay = max(
                    provider_delay or 0.0,
                    min(
                        self._policy.retry_max_seconds,
                        exponential_delay + self._jitter(),
                    ),
                )
                self._limiter.defer(key, delay)
                logger.warning(
                    "Retrying provider model request after 429 provider=%s model=%s attempt=%s delay_seconds=%.2f",
                    key[0],
                    key[1],
                    attempt + 1,
                    delay,
                )
                if trace is not None:
                    trace.event(
                        "provider_rate_limit_retry",
                        {
                            "provider": key[0],
                            "model": key[1],
                            "attempt": attempt + 1,
                            "delay_seconds": delay,
                        },
                    )
        raise RuntimeError("Model request retry loop exited unexpectedly")  # pragma: no cover


_MODEL_RATE_LIMITER = ProviderModelRateLimiter()


def model_rate_limit_hooks(settings: Settings, *, trace: ModelCallTrace | None = None) -> Hooks[Any]:
    controller = ProviderModelRequestController(
        limiter=_MODEL_RATE_LIMITER,
        policy=ModelRateLimitPolicy.from_settings(settings),
    )

    async def limited_model_request(
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        handler: Callable[[ModelRequestContext], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        del ctx
        return await controller.execute(
            request_context,
            handler,
            trace=trace or current_model_call_trace(),
        )

    return Hooks(model_request=limited_model_request, id="provider_model_rate_limit")
