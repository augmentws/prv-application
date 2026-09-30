from __future__ import annotations

import asyncio
import json
import math
import random
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    Noul,
    RetryPolicy,
    Score,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPITimeoutError,
    TypeSafeRateLimitError,
)

from app.config import Settings
from app.decision_engine import (
    ChoiceDecisionAnswer,
    DecisionAnswer,
    DecisionEngineProviderError,
    DecisionEngineRegistry,
    DecisionEnvelope,
    DecisionRequest,
    DecisionUsage,
    NoulDecisionAnswer,
    ScoreDecisionAnswer,
)
from app.decision_specifications import ChoiceDecisionQuestion, NoulDecisionQuestion, ScoreDecisionQuestion
from app.model_rate_limits import ProviderModelRateLimiter


class _SystemOneClient(Protocol):
    async def system_one(
        self,
        state: Any,
        questions: Mapping[str, Choice | Noul | Score],
        *,
        model: str | None = None,
        retry: RetryPolicy | None = None,
        timeout: float | None = None,
    ) -> Any: ...


ClientFactory = Callable[..., AbstractAsyncContextManager[_SystemOneClient]]


def _default_client_factory(**kwargs: Any) -> AbstractAsyncContextManager[_SystemOneClient]:
    return cast(AbstractAsyncContextManager[_SystemOneClient], AsyncTypeSafeClient(**kwargs))


def _question_payload(
    question: ChoiceDecisionQuestion | NoulDecisionQuestion | ScoreDecisionQuestion,
) -> Choice | Noul | Score:
    if isinstance(question, ChoiceDecisionQuestion):
        return Choice(instructions=question.instructions, criteria=question.criteria)
    if isinstance(question, ScoreDecisionQuestion):
        return Score(instructions=question.instructions, criteria=question.criteria)
    return Noul(
        instructions=question.instructions,
        criteria=question.criteria.model_dump(mode="json") if question.criteria is not None else None,
    )


def _answer_payload(answer: Any) -> dict[str, Any]:
    if hasattr(answer, "model_dump"):
        return cast(dict[str, Any], answer.model_dump(mode="json"))
    if isinstance(answer, Mapping):
        return dict(answer)
    raise DecisionEngineProviderError(
        "Jev returned an unsupported answer object",
        provider="typesafe",
        code="INVALID_RESPONSE",
        retryable=False,
    )


def _normalize_answers(request: DecisionRequest, response: Any) -> dict[str, DecisionAnswer]:
    response_answers = getattr(response, "answers", None)
    if not isinstance(response_answers, Mapping):
        raise DecisionEngineProviderError(
            "Jev response does not contain an answer mapping",
            provider="typesafe",
            code="INVALID_RESPONSE",
            retryable=False,
        )
    expected_keys = set(request.questions)
    actual_keys = set(response_answers)
    if expected_keys != actual_keys:
        missing = sorted(expected_keys - actual_keys)
        unexpected = sorted(actual_keys - expected_keys)
        raise DecisionEngineProviderError(
            f"Jev answer keys do not match the request (missing={missing}, unexpected={unexpected})",
            provider="typesafe",
            code="ANSWER_KEY_MISMATCH",
            retryable=False,
        )

    normalized: dict[str, DecisionAnswer] = {}
    for key, question in request.questions.items():
        payload = _answer_payload(response_answers[key])
        if payload.get("type") != question.type:
            raise DecisionEngineProviderError(
                f"Jev answer type for {key} does not match question type {question.type}",
                provider="typesafe",
                code="ANSWER_TYPE_MISMATCH",
                retryable=False,
            )
        if question.type == "choice":
            expected_options = set(question.criteria)
            if set(payload.get("probabilities", {})) != expected_options:
                raise DecisionEngineProviderError(
                    f"Jev Choice probabilities for {key} do not match its criteria",
                    provider="typesafe",
                    code="ANSWER_OPTION_MISMATCH",
                    retryable=False,
                )
            normalized[key] = ChoiceDecisionAnswer.model_validate(payload)
        elif question.type == "score":
            expected_levels = {str(index) for index in range(len(question.criteria))}
            if (
                set(payload.get("legend", {})) != expected_levels
                or set(payload.get("probabilities", {})) != expected_levels
            ):
                raise DecisionEngineProviderError(
                    f"Jev Score levels for {key} do not match its criteria",
                    provider="typesafe",
                    code="ANSWER_OPTION_MISMATCH",
                    retryable=False,
                )
            normalized[key] = ScoreDecisionAnswer.model_validate(payload)
        else:
            normalized[key] = NoulDecisionAnswer.model_validate(payload)
    return normalized


def _safe_request_id(response: Any) -> str | None:
    try:
        value = getattr(response, "request_id", None)
    except (AttributeError, RuntimeError):
        return None
    return str(value) if value else None


def _provider_metadata(response: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    try:
        raw_response = getattr(response, "raw_http_response", None)
    except (AttributeError, RuntimeError):
        raw_response = None
    if raw_response is None:
        return metadata
    status_code = getattr(raw_response, "status_code", None)
    if isinstance(status_code, int):
        metadata["http_status"] = status_code
    headers = getattr(raw_response, "headers", {})
    for name in ("x-typesafe-request-id", "x-ratelimit-limit", "x-ratelimit-remaining"):
        value = headers.get(name) if hasattr(headers, "get") else None
        if value is not None:
            metadata[name.replace("x-", "").replace("-", "_")] = str(value)
    return metadata


def _estimated_input_tokens(request: DecisionRequest, characters_per_token: float) -> int:
    payload = {
        "state": request.state,
        "questions": {
            key: question.model_dump(
                mode="json",
                include={"type", "instructions", "criteria"},
                exclude_none=True,
            )
            for key, question in request.questions.items()
        },
    }
    return max(1, math.ceil(len(json.dumps(payload, ensure_ascii=False, default=str)) / characters_per_token))


class TypeSafeJevDecisionEngine:
    """Provider adapter for TypeSafe System One/Jev with bounded retry and local admission control."""

    provider = "typesafe"

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: ClientFactory = _default_client_factory,
        limiter: ProviderModelRateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory
        self._limiter = limiter or ProviderModelRateLimiter()
        self._sleep = sleep
        self._jitter = jitter
        self._monotonic = monotonic
        self._now = now
        # Batch documents execute in separate DBOS threads, each with its own asyncio event
        # loop. asyncio.Semaphore is bound to one loop once contended, so it cannot enforce a
        # process-wide cap across those document workflows. This gate uses a thread-safe
        # semaphore with non-blocking async polling and is safe to share across event loops.
        self._concurrency_gate = _CrossLoopConcurrencyGate(settings.typesafe_concurrency)

    async def evaluate(self, request: DecisionRequest) -> DecisionEnvelope:
        if not self._settings.typesafe_api_key:
            raise DecisionEngineProviderError(
                "TYPESAFE_API_KEY is not configured",
                provider=self.provider,
                code="NOT_CONFIGURED",
                retryable=False,
            )
        model = request.model_key or self._settings.typesafe_default_model
        timeout = float(request.limits.get("timeout_seconds", self._settings.typesafe_timeout_seconds))
        timeout = min(max(timeout, 0.1), self._settings.typesafe_timeout_seconds)
        requested_retries = int(request.limits.get("max_retries", self._settings.typesafe_max_retries))
        max_retries = min(max(requested_retries, 0), self._settings.typesafe_max_retries)
        questions = {key: _question_payload(question) for key, question in request.questions.items()}
        estimated_tokens = _estimated_input_tokens(
            request,
            self._settings.model_rate_limit_characters_per_token,
        )
        started_at = self._now()
        started_monotonic = self._monotonic()
        response: Any = None
        attempts = 0
        async with self._concurrency_gate:
            for attempt in range(max_retries + 1):
                attempts = attempt + 1
                await self._limiter.acquire(
                    (self.provider, model),
                    estimated_input_tokens=estimated_tokens,
                    requests_per_minute=self._settings.typesafe_requests_per_minute,
                    input_tokens_per_minute=self._settings.typesafe_input_tokens_per_minute,
                )
                try:
                    client_kwargs: dict[str, Any] = {
                        "api_key": self._settings.typesafe_api_key,
                        "model": model,
                        "retry": RetryPolicy(max_retries=0),
                        "timeout": timeout,
                    }
                    if self._settings.typesafe_base_url:
                        client_kwargs["base_url"] = self._settings.typesafe_base_url
                    async with self._client_factory(**client_kwargs) as client:
                        response = await client.system_one(
                            state=request.state,
                            questions=questions,
                            model=model,
                            retry=RetryPolicy(max_retries=0),
                            timeout=timeout,
                        )
                    break
                except (TypeSafeRateLimitError, TypeSafeAPIConnectionError, TypeSafeAPITimeoutError) as exc:
                    if attempt >= max_retries:
                        raise self._provider_error(exc, retryable=True) from exc
                    delay = self._retry_delay(exc, attempt)
                    self._limiter.defer((self.provider, model), delay)
                    await self._sleep(delay)
                except TypeSafeAPIError as exc:
                    retryable = exc.status >= 500
                    if not retryable or attempt >= max_retries:
                        raise self._provider_error(exc, retryable=retryable) from exc
                    delay = self._retry_delay(exc, attempt)
                    self._limiter.defer((self.provider, model), delay)
                    await self._sleep(delay)
        if response is None:  # pragma: no cover - the loop returns or raises
            raise RuntimeError("Jev retry loop exited without a response")

        usage = getattr(response, "usage", None)
        completed_at = self._now()
        return DecisionEnvelope(
            answers=_normalize_answers(request, response),
            provider=self.provider,
            model=str(getattr(response, "model", model)),
            provider_request_id=_safe_request_id(response),
            usage=DecisionUsage(
                request_count=attempts,
                input_tokens=max(0, int(getattr(usage, "input_tokens", 0) or 0)),
                output_tokens=max(0, int(getattr(usage, "output_tokens", 0) or 0)),
            ),
            latency_ms=max(0, round((self._monotonic() - started_monotonic) * 1000)),
            started_at=started_at,
            completed_at=completed_at,
            attempts=attempts,
            provider_metadata=_provider_metadata(response),
        )

    def _retry_delay(self, exc: BaseException, attempt: int) -> float:
        provider_delay = getattr(exc, "retry_after_ms", None)
        retry_after = max(0.0, float(provider_delay) / 1000) if provider_delay is not None else 0.0
        exponential = min(
            self._settings.typesafe_retry_max_seconds,
            self._settings.typesafe_retry_base_seconds * (2**attempt) + self._jitter(),
        )
        return min(self._settings.typesafe_retry_max_seconds, max(retry_after, exponential))

    def _provider_error(self, exc: BaseException, *, retryable: bool) -> DecisionEngineProviderError:
        status = getattr(exc, "status", None)
        request_id = getattr(exc, "request_id", None)
        retry_after_ms = getattr(exc, "retry_after_ms", None)
        return DecisionEngineProviderError(
            str(exc),
            provider=self.provider,
            code=f"HTTP_{status}" if status is not None else type(exc).__name__.upper(),
            retryable=retryable,
            provider_request_id=str(request_id) if request_id else None,
            retry_after_seconds=float(retry_after_ms) / 1000 if retry_after_ms is not None else None,
        )


def build_decision_engine_registry(settings: Settings) -> DecisionEngineRegistry:
    """Build the code-owned adapter allowlist for a worker or API process."""

    registry = DecisionEngineRegistry()
    registry.register("jev", TypeSafeJevDecisionEngine(settings))
    return registry


class _CrossLoopConcurrencyGate:
    """Async context manager backed by a process-wide, thread-safe semaphore."""

    def __init__(self, limit: int, *, poll_seconds: float = 0.01) -> None:
        self._semaphore = threading.BoundedSemaphore(limit)
        self._poll_seconds = poll_seconds

    async def __aenter__(self) -> None:
        while not self._semaphore.acquire(blocking=False):
            await asyncio.sleep(self._poll_seconds)

    async def __aexit__(self, *_: object) -> None:
        self._semaphore.release()


_SHARED_REGISTRY_LOCK = threading.Lock()
_SHARED_REGISTRIES: dict[tuple[object, ...], DecisionEngineRegistry] = {}


def _shared_registry_key(settings: Settings) -> tuple[object, ...]:
    return (
        settings.typesafe_api_key,
        settings.typesafe_base_url,
        settings.typesafe_default_model,
        settings.typesafe_timeout_seconds,
        settings.typesafe_concurrency,
        settings.typesafe_requests_per_minute,
        settings.typesafe_input_tokens_per_minute,
        settings.typesafe_max_retries,
        settings.typesafe_retry_base_seconds,
        settings.typesafe_retry_max_seconds,
        settings.model_rate_limit_characters_per_token,
    )


def get_shared_decision_engine_registry(settings: Settings) -> DecisionEngineRegistry:
    """Return one code-owned decision-engine registry per process and Jev configuration."""

    key = _shared_registry_key(settings)
    with _SHARED_REGISTRY_LOCK:
        registry = _SHARED_REGISTRIES.get(key)
        if registry is None:
            registry = build_decision_engine_registry(settings)
            _SHARED_REGISTRIES[key] = registry
        return registry
