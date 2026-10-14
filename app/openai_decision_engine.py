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

import httpx

from app.config import Settings
from app.decision_engine import (
    ChoiceDecisionAnswer,
    DecisionAnswer,
    DecisionEngineProviderError,
    DecisionEnvelope,
    DecisionRequest,
    DecisionUsage,
    NoulDecisionAnswer,
    ScoreDecisionAnswer,
)
from app.decision_specifications import ChoiceDecisionQuestion, NoulDecisionQuestion, ScoreDecisionQuestion
from app.model_rate_limits import ProviderModelRateLimiter


class _AsyncHttpClient(Protocol):
    async def post(self, url: str, **kwargs: Any) -> httpx.Response: ...


HttpClientFactory = Callable[..., AbstractAsyncContextManager[_AsyncHttpClient]]


def _default_client_factory(**kwargs: Any) -> AbstractAsyncContextManager[_AsyncHttpClient]:
    return cast(AbstractAsyncContextManager[_AsyncHttpClient], httpx.AsyncClient(**kwargs))


def _description(value: Any, *, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if value is not None:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if rendered and rendered != "null":
            return rendered
    return fallback


def _instructions(question: ChoiceDecisionQuestion | NoulDecisionQuestion | ScoreDecisionQuestion) -> str:
    instructions = _description(question.instructions, fallback="Evaluate this question from the supplied input.")
    if isinstance(question, NoulDecisionQuestion) and question.criteria is not None:
        instructions += (
            "\n\nReturn true when: "
            + _description(question.criteria.true, fallback="the condition is satisfied")
            + "\nReturn false when: "
            + _description(question.criteria.false, fallback="the condition is not satisfied")
        )
    return instructions


def _question_payload(
    provider_name: str,
    question: ChoiceDecisionQuestion | NoulDecisionQuestion | ScoreDecisionQuestion,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": provider_name,
        "instructions": _instructions(question),
    }
    if isinstance(question, ChoiceDecisionQuestion):
        payload.update(
            {
                "type": "choice",
                "choices": [
                    {
                        "value": value,
                        "description": _description(description, fallback=f"Select {value}."),
                    }
                    for value, description in question.criteria.items()
                ],
            }
        )
    elif isinstance(question, ScoreDecisionQuestion):
        payload.update(
            {
                "type": "score",
                "levels": [
                    {
                        "label": str(index),
                        "description": _description(level, fallback=f"Score level {index}."),
                    }
                    for index, level in enumerate(question.criteria)
                ],
            }
        )
    else:
        payload["type"] = "predicate"
    return payload


def _probability_map(raw: Any) -> dict[str, float]:
    if not isinstance(raw, list):
        raise TypeError("probabilities must be an array")
    values: dict[str, float] = {}
    for item in raw:
        if not isinstance(item, Mapping) or "value" not in item or "probability" not in item:
            raise ValueError("probability entries require value and probability")
        values[str(item["value"])] = float(item["probability"])
    return values


def _normalize_answers(request: DecisionRequest, payload: Any) -> dict[str, DecisionAnswer]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("answers"), list):
        raise TypeError("OpenAI Decisions response does not contain an answers array")
    raw_answers = payload["answers"]
    if len(raw_answers) != len(request.questions):
        raise ValueError("OpenAI Decisions answer count does not match the request")

    normalized: dict[str, DecisionAnswer] = {}
    for index, ((question_key, question), raw_answer) in enumerate(zip(request.questions.items(), raw_answers)):
        provider_name = f"question_{index}"
        if not isinstance(raw_answer, Mapping):
            raise TypeError(f"OpenAI Decisions answer {index} is not an object")
        if raw_answer.get("name") != provider_name:
            raise ValueError(f"OpenAI Decisions answer {index} has an unexpected name")
        answer_type = raw_answer.get("type")
        if answer_type == "refusal":
            raise DecisionEngineProviderError(
                f"OpenAI Decisions refused question {question_key}",
                provider="openai",
                code="REFUSAL",
                retryable=False,
            )
        if isinstance(question, NoulDecisionQuestion):
            if answer_type != "predicate":
                raise ValueError(f"OpenAI Decisions answer type for {question_key} must be predicate")
            normalized[question_key] = NoulDecisionAnswer(type="noul", noul=float(raw_answer["probability"]))
        elif isinstance(question, ChoiceDecisionQuestion):
            if answer_type != "choice":
                raise ValueError(f"OpenAI Decisions answer type for {question_key} must be choice")
            probabilities = _probability_map(raw_answer.get("probabilities"))
            if set(probabilities) != set(question.criteria):
                raise ValueError(f"OpenAI Decisions probabilities for {question_key} do not match its choices")
            normalized[question_key] = ChoiceDecisionAnswer(
                type="choice",
                choice=str(raw_answer["choice"]),
                probabilities=probabilities,
                confidence=float(raw_answer["confidence"]),
            )
        else:
            if answer_type != "score":
                raise ValueError(f"OpenAI Decisions answer type for {question_key} must be score")
            probabilities = _probability_map(raw_answer.get("probabilities"))
            expected_levels = {str(level) for level in range(len(question.criteria))}
            if set(probabilities) != expected_levels:
                raise ValueError(f"OpenAI Decisions probabilities for {question_key} do not match its levels")
            normalized[question_key] = ScoreDecisionAnswer(
                type="score",
                score=float(raw_answer["score"]),
                legend={str(level): level for level in range(len(question.criteria))},
                probabilities=probabilities,
                confidence=float(raw_answer["confidence"]),
            )
    return normalized


def _estimated_input_tokens(request: DecisionRequest, characters_per_token: float) -> int:
    payload = {
        "input": request.state,
        "questions": [
            _question_payload(f"question_{index}", question)
            for index, question in enumerate(request.questions.values())
        ],
    }
    return max(1, math.ceil(len(json.dumps(payload, ensure_ascii=False, default=str)) / characters_per_token))


class OpenAIDecisionsEngine:
    """Adapter for the public-beta OpenAI Decisions API."""

    provider = "openai"

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: HttpClientFactory = _default_client_factory,
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
        self._concurrency_gate = _CrossLoopConcurrencyGate(settings.openai_decisions_concurrency)

    async def evaluate(self, request: DecisionRequest) -> DecisionEnvelope:
        if not self._settings.openai_api_key:
            raise DecisionEngineProviderError(
                "OPENAI_API_KEY is not configured",
                provider=self.provider,
                code="NOT_CONFIGURED",
                retryable=False,
            )
        model = request.model_key
        if model != "gpt-6-luna":
            raise DecisionEngineProviderError(
                f"OpenAI Decisions does not support configured model {model}",
                provider=self.provider,
                code="UNSUPPORTED_MODEL",
                retryable=False,
            )
        timeout = min(
            max(float(request.limits.get("timeout_seconds", self._settings.openai_decisions_timeout_seconds)), 0.1),
            self._settings.openai_decisions_timeout_seconds,
        )
        requested_retries = int(request.limits.get("max_retries", self._settings.openai_decisions_max_retries))
        max_retries = min(max(requested_retries, 0), self._settings.openai_decisions_max_retries)
        body = {
            "model": model,
            "input": json.dumps(request.state, ensure_ascii=False, separators=(",", ":"), default=str),
            "questions": [
                _question_payload(f"question_{index}", question)
                for index, question in enumerate(request.questions.values())
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        if request.idempotency_key:
            headers["Idempotency-Key"] = request.idempotency_key
        estimated_tokens = _estimated_input_tokens(
            request,
            self._settings.model_rate_limit_characters_per_token,
        )
        started_at = self._now()
        started_monotonic = self._monotonic()
        response: httpx.Response | None = None
        attempts = 0
        async with self._concurrency_gate:
            for attempt in range(max_retries + 1):
                attempts = attempt + 1
                await self._limiter.acquire(
                    (self.provider, model),
                    estimated_input_tokens=estimated_tokens,
                    requests_per_minute=self._settings.openai_decisions_requests_per_minute,
                    input_tokens_per_minute=self._settings.openai_decisions_input_tokens_per_minute,
                )
                try:
                    async with self._client_factory(timeout=timeout) as client:
                        response = await client.post(
                            f"{self._settings.openai_base_url.rstrip('/')}/decisions",
                            headers=headers,
                            json=body,
                        )
                except httpx.RequestError as exc:
                    if attempt >= max_retries:
                        raise self._transport_error(exc) from exc
                    delay = self._retry_delay(None, attempt)
                    self._limiter.defer((self.provider, model), delay)
                    await self._sleep(delay)
                    continue
                if response.status_code < 400:
                    break
                retryable = response.status_code in {408, 409, 429} or response.status_code >= 500
                if not retryable or attempt >= max_retries:
                    raise self._http_error(response, retryable=retryable)
                delay = self._retry_delay(response, attempt)
                self._limiter.defer((self.provider, model), delay)
                await self._sleep(delay)
        if response is None:  # pragma: no cover - loop returns or raises
            raise RuntimeError("OpenAI Decisions retry loop exited without a response")
        try:
            response_payload = response.json()
            answers = _normalize_answers(request, response_payload)
        except DecisionEngineProviderError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DecisionEngineProviderError(
                f"Invalid OpenAI Decisions response: {exc}",
                provider=self.provider,
                code="INVALID_RESPONSE",
                retryable=False,
                provider_request_id=response.headers.get("x-request-id"),
            ) from exc
        usage = response_payload.get("usage") if isinstance(response_payload, Mapping) else None
        usage = usage if isinstance(usage, Mapping) else {}
        completed_at = self._now()
        return DecisionEnvelope(
            answers=answers,
            provider=self.provider,
            model=str(response_payload.get("model") or model),
            provider_request_id=response.headers.get("x-request-id"),
            usage=DecisionUsage(
                request_count=attempts,
                input_tokens=max(0, int(usage.get("input_tokens", 0) or 0)),
                output_tokens=max(0, int(usage.get("output_tokens", 0) or 0)),
            ),
            latency_ms=max(0, round((self._monotonic() - started_monotonic) * 1000)),
            started_at=started_at,
            completed_at=completed_at,
            attempts=attempts,
            provider_metadata={
                "api": "decisions",
                "http_status": response.status_code,
            },
        )

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        retry_after = self._retry_after_seconds(response) or 0.0
        exponential = min(
            self._settings.openai_decisions_retry_max_seconds,
            self._settings.openai_decisions_retry_base_seconds * (2**attempt) + self._jitter(),
        )
        return min(self._settings.openai_decisions_retry_max_seconds, max(retry_after, exponential))

    @staticmethod
    def _retry_after_seconds(response: httpx.Response | None) -> float | None:
        if response is None or "retry-after" not in response.headers:
            return None
        try:
            return max(0.0, float(response.headers["retry-after"]))
        except (TypeError, ValueError):
            return None

    def _transport_error(self, exc: BaseException) -> DecisionEngineProviderError:
        return DecisionEngineProviderError(
            str(exc),
            provider=self.provider,
            code=type(exc).__name__.upper(),
            retryable=True,
        )

    def _http_error(self, response: httpx.Response, *, retryable: bool) -> DecisionEngineProviderError:
        message = f"OpenAI Decisions returned HTTP {response.status_code}"
        try:
            payload = response.json()
            error = payload.get("error") if isinstance(payload, Mapping) else None
            if isinstance(error, Mapping) and isinstance(error.get("message"), str):
                message = error["message"]
        except (ValueError, json.JSONDecodeError):
            pass
        return DecisionEngineProviderError(
            message,
            provider=self.provider,
            code=f"HTTP_{response.status_code}",
            retryable=retryable,
            provider_request_id=response.headers.get("x-request-id"),
            retry_after_seconds=self._retry_after_seconds(response),
        )


class _CrossLoopConcurrencyGate:
    def __init__(self, limit: int, *, poll_seconds: float = 0.01) -> None:
        self._semaphore = threading.BoundedSemaphore(limit)
        self._poll_seconds = poll_seconds

    async def __aenter__(self) -> None:
        while not self._semaphore.acquire(blocking=False):
            await asyncio.sleep(self._poll_seconds)

    async def __aexit__(self, *_: object) -> None:
        self._semaphore.release()
