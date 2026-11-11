from __future__ import annotations

import asyncio
import copy
import json
import math
import random
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from functools import lru_cache
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
from app.document_evidence import build_document_map_plan, segment_paragraphs
from app.model_rate_limits import ProviderModelRateLimiter
from app.openai_decision_engine import OpenAIDecisionsEngine


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

_DOCUMENT_WINDOW_HEADROOM_CHARACTERS = 256
_MINIMUM_DOCUMENT_WINDOW_CHARACTERS = 512


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


def _decision_payload(request: DecisionRequest, *, state: Any | None = None) -> dict[str, Any]:
    return {
        "state": request.state if state is None else state,
        "questions": {
            key: question.model_dump(
                mode="json",
                include={"type", "instructions", "criteria"},
                exclude_none=True,
            )
            for key, question in request.questions.items()
        },
    }


def _payload_characters(request: DecisionRequest, *, state: Any | None = None) -> int:
    return len(json.dumps(_decision_payload(request, state=state), ensure_ascii=False, default=str))


@lru_cache(maxsize=16)
def _token_encoding(model: str) -> Any:
    import tiktoken

    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding("o200k_base")


def _payload_tokens(request: DecisionRequest, *, state: Any | None = None) -> int:
    serialized = json.dumps(_decision_payload(request, state=state), ensure_ascii=False, default=str)
    try:
        return max(1, len(_token_encoding(request.model_key or "jev-latest").encode(serialized)))
    except (ImportError, ValueError):
        return max(1, math.ceil(len(serialized) / 3))


def _estimated_input_tokens(
    request: DecisionRequest,
    characters_per_token: float,
    *,
    state: Any | None = None,
) -> int:
    return max(1, math.ceil(_payload_characters(request, state=state) / characters_per_token))


def _document_text(state: Any) -> tuple[str, str] | None:
    if not isinstance(state, dict) or not isinstance(state.get("document"), dict):
        return None
    document = state["document"]
    paragraphs = document.get("paragraphs")
    if isinstance(paragraphs, list):
        texts = [item.get("text") for item in paragraphs if isinstance(item, dict)]
        usable = [value for value in texts if isinstance(value, str) and value]
        if usable:
            return "paragraphs", "\n\n".join(usable)
    text = document.get("text")
    if isinstance(text, str) and text:
        return "text", text
    return None


def _window_states(
    request: DecisionRequest,
    *,
    target_characters: int,
    target_tokens: int | None = None,
    maximum_windows: int,
) -> list[Any]:
    source = _document_text(request.state)
    if source is None:
        return []
    _, source_text = source
    empty_state = copy.deepcopy(request.state)
    empty_document = empty_state["document"]
    # Some state contracts request both text and paragraphs. They are two representations of
    # the same document, so leaving either full representation in the otherwise-empty state
    # makes it look like fixed prompt overhead and can collapse the other representation into
    # hundreds of tiny windows. Window every representation that is present together.
    has_text = isinstance(empty_document.get("text"), str)
    has_paragraphs = isinstance(empty_document.get("paragraphs"), list)
    if has_text:
        empty_document["text"] = ""
    if has_paragraphs:
        empty_document["paragraphs"] = []
    fixed_characters = _payload_characters(request, state=empty_state)
    representation_count = int(has_text) + int(has_paragraphs)
    document_budget = max(
        _MINIMUM_DOCUMENT_WINDOW_CHARACTERS,
        (
            target_characters
            - fixed_characters
            - _DOCUMENT_WINDOW_HEADROOM_CHARACTERS
        )
        // max(1, representation_count),
    )
    if target_tokens is not None and _payload_tokens(request, state=empty_state) >= target_tokens:
        raise DecisionEngineProviderError(
            "Jev request guidance and questions exceed the configured token target before document text is added",
            provider="typesafe",
            code="FIXED_REQUEST_TOKEN_LIMIT_EXCEEDED",
            retryable=False,
        )
    paragraph_map = segment_paragraphs(source_text)
    while True:
        plan = build_document_map_plan(
            paragraph_map,
            max_characters=document_budget,
            overlap_segments=1,
        )
        if len(plan.windows) > maximum_windows:
            raise DecisionEngineProviderError(
                f"Jev document requires {len(plan.windows)} windows, exceeding the configured maximum of {maximum_windows}",
                provider="typesafe",
                code="DOCUMENT_WINDOW_LIMIT_EXCEEDED",
                retryable=False,
            )
        states: list[Any] = []
        for window in plan.windows:
            state = copy.deepcopy(request.state)
            document = state["document"]
            if has_paragraphs:
                document["paragraphs"] = [
                    {
                        "paragraph_id": segment.paragraph_id,
                        "text": segment.text,
                        "part": segment.part,
                        "part_count": segment.part_count,
                        "char_start": segment.char_start,
                        "char_end": segment.char_end,
                    }
                    for segment in window.segments
                ]
            if has_text:
                document["text"] = window.text
            states.append(state)
        maximum_tokens = max((_payload_tokens(request, state=state) for state in states), default=0)
        if target_tokens is None or maximum_tokens <= target_tokens:
            return [] if len(states) <= 1 else states
        if document_budget <= _MINIMUM_DOCUMENT_WINDOW_CHARACTERS:
            raise DecisionEngineProviderError(
                f"Jev document cannot fit the configured {target_tokens}-token request target",
                provider="typesafe",
                code="DOCUMENT_TOKEN_LIMIT_EXCEEDED",
                retryable=False,
            )
        scaled_budget = int(document_budget * target_tokens / maximum_tokens * 0.9)
        document_budget = max(
            _MINIMUM_DOCUMENT_WINDOW_CHARACTERS,
            min(document_budget - 1, scaled_budget),
        )


def _minimum_window_target_characters(request: DecisionRequest) -> int:
    empty_state = copy.deepcopy(request.state)
    document = empty_state.get("document") if isinstance(empty_state, dict) else None
    if isinstance(document, dict):
        has_text = isinstance(document.get("text"), str)
        has_paragraphs = isinstance(document.get("paragraphs"), list)
        if has_text:
            document["text"] = ""
        if has_paragraphs:
            document["paragraphs"] = []
    else:
        has_text = False
        has_paragraphs = False
    return (
        _payload_characters(request, state=empty_state)
        + _DOCUMENT_WINDOW_HEADROOM_CHARACTERS
        + _MINIMUM_DOCUMENT_WINDOW_CHARACTERS * max(1, int(has_text) + int(has_paragraphs))
    )


def _minimum_window_target_tokens(request: DecisionRequest) -> int:
    empty_state = copy.deepcopy(request.state)
    document = empty_state.get("document") if isinstance(empty_state, dict) else None
    if isinstance(document, dict):
        if isinstance(document.get("text"), str):
            document["text"] = ""
        if isinstance(document.get("paragraphs"), list):
            document["paragraphs"] = []
    return _payload_tokens(request, state=empty_state) + 256


def _is_max_tokens_error(exc: DecisionEngineProviderError) -> bool:
    return exc.code == "HTTP_400" and "max_tokens_exceeded" in str(exc).lower()


def _aggregate_window_answers(
    request: DecisionRequest,
    envelopes: list[DecisionEnvelope],
) -> dict[str, DecisionAnswer]:
    combined: dict[str, DecisionAnswer] = {}
    for key, question in request.questions.items():
        answers = [envelope.answers[key] for envelope in envelopes]
        operator = question.aggregation.operator
        if isinstance(answers[0], NoulDecisionAnswer):
            nouls = cast(list[NoulDecisionAnswer], answers)
            value = (
                min(answer.noul for answer in nouls)
                if operator == "ALL_WINDOWS"
                else max(answer.noul for answer in nouls)
            )
            combined[key] = NoulDecisionAnswer(type="noul", noul=value)
        elif isinstance(answers[0], ChoiceDecisionAnswer):
            choices = cast(list[ChoiceDecisionAnswer], answers)
            if operator == "ALL_WINDOWS":
                selected = min(choices, key=lambda answer: (answer.confidence, answer.probabilities[answer.choice]))
            else:
                selected = max(choices, key=lambda answer: (answer.confidence, answer.probabilities[answer.choice]))
            combined[key] = selected
        else:
            scores = cast(list[ScoreDecisionAnswer], answers)
            if operator == "ALL_WINDOWS":
                selected_score = min(scores, key=lambda answer: (answer.score, answer.confidence))
            elif operator == "HIGHEST_CONFIDENCE_CHOICE":
                selected_score = max(scores, key=lambda answer: answer.confidence)
            else:
                selected_score = max(scores, key=lambda answer: (answer.score, answer.confidence))
            combined[key] = selected_score
    return combined


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
        target_characters = self._settings.typesafe_request_target_characters
        target_tokens = self._settings.typesafe_request_target_tokens
        states = (
            _window_states(
                request,
                target_characters=target_characters,
                target_tokens=target_tokens,
                maximum_windows=self._settings.typesafe_max_document_windows,
            )
            if (
                _payload_characters(request) > target_characters
                or _payload_tokens(request) > target_tokens
            )
            else []
        )
        if not states:
            try:
                return await self._evaluate_once(request)
            except DecisionEngineProviderError as exc:
                if not _is_max_tokens_error(exc):
                    raise
                target_characters = max(
                    _minimum_window_target_characters(request),
                    _payload_characters(request) // 2,
                )
                target_tokens = max(
                    _minimum_window_target_tokens(request),
                    _payload_tokens(request) // 2,
                )
                states = _window_states(
                    request,
                    target_characters=target_characters,
                    target_tokens=target_tokens,
                    maximum_windows=self._settings.typesafe_max_document_windows,
                )
                if not states:
                    raise

        started_at = self._now()
        started_monotonic = self._monotonic()
        envelopes: list[DecisionEnvelope] = []
        for state in states:
            envelopes.extend(
                await self._evaluate_window_state(
                    request,
                    state=state,
                    target_characters=target_characters,
                    target_tokens=target_tokens,
                )
            )
            if len(envelopes) > self._settings.typesafe_max_document_windows:
                raise DecisionEngineProviderError(
                    "Jev document refinement exceeded the configured document-window limit",
                    provider=self.provider,
                    code="DOCUMENT_WINDOW_LIMIT_EXCEEDED",
                    retryable=False,
                )

        completed_at = self._now()
        request_ids = [envelope.provider_request_id for envelope in envelopes if envelope.provider_request_id]
        return DecisionEnvelope(
            answers=_aggregate_window_answers(request, envelopes),
            provider=self.provider,
            model=envelopes[0].model,
            provider_request_id=request_ids[-1] if request_ids else None,
            usage=DecisionUsage(
                request_count=sum(envelope.usage.request_count for envelope in envelopes),
                input_tokens=sum(envelope.usage.input_tokens for envelope in envelopes),
                output_tokens=sum(envelope.usage.output_tokens for envelope in envelopes),
            ),
            latency_ms=max(0, round((self._monotonic() - started_monotonic) * 1000)),
            started_at=started_at,
            completed_at=completed_at,
            attempts=sum(envelope.attempts for envelope in envelopes),
            provider_metadata={
                "windowed": True,
                "window_count": len(envelopes),
                "document_window_target_characters": target_characters,
                "document_window_target_tokens": target_tokens,
                "provider_request_ids": request_ids,
                "aggregation_version": "document-window-v1",
            },
        )

    async def _evaluate_window_state(
        self,
        request: DecisionRequest,
        *,
        state: Any,
        target_characters: int,
        target_tokens: int,
    ) -> list[DecisionEnvelope]:
        window_request = request.model_copy(update={"state": state})
        try:
            return [await self._evaluate_once(window_request)]
        except DecisionEngineProviderError as exc:
            if not _is_max_tokens_error(exc):
                raise
            refined_target = max(
                _minimum_window_target_characters(window_request),
                target_characters // 2,
            )
            refined_target_tokens = max(
                _minimum_window_target_tokens(window_request),
                target_tokens // 2,
            )
            if refined_target >= target_characters and refined_target_tokens >= target_tokens:
                raise
            refined_states = _window_states(
                window_request,
                target_characters=refined_target,
                target_tokens=refined_target_tokens,
                maximum_windows=self._settings.typesafe_max_document_windows,
            )
            if not refined_states:
                raise
            envelopes: list[DecisionEnvelope] = []
            for refined_state in refined_states:
                envelopes.extend(
                    await self._evaluate_window_state(
                        request,
                        state=refined_state,
                        target_characters=refined_target,
                        target_tokens=refined_target_tokens,
                    )
                )
            return envelopes

    async def _evaluate_once(self, request: DecisionRequest) -> DecisionEnvelope:
        model = request.model_key or self._settings.typesafe_default_model
        timeout = float(request.limits.get("timeout_seconds", self._settings.typesafe_timeout_seconds))
        timeout = min(max(timeout, 0.1), self._settings.typesafe_timeout_seconds)
        requested_retries = int(request.limits.get("max_retries", self._settings.typesafe_max_retries))
        max_retries = min(max(requested_retries, 0), self._settings.typesafe_max_retries)
        questions = {key: _question_payload(question) for key, question in request.questions.items()}
        estimated_tokens = _payload_tokens(request)
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
    registry.register("openai_decisions", OpenAIDecisionsEngine(settings))
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
        settings.typesafe_request_target_characters,
        settings.typesafe_request_target_tokens,
        settings.typesafe_max_document_windows,
        settings.openai_api_key,
        settings.openai_base_url,
        settings.openai_decisions_timeout_seconds,
        settings.openai_decisions_concurrency,
        settings.openai_decisions_requests_per_minute,
        settings.openai_decisions_input_tokens_per_minute,
        settings.openai_decisions_max_retries,
        settings.openai_decisions_retry_base_seconds,
        settings.openai_decisions_retry_max_seconds,
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
