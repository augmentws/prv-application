from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from typesafe_sdk import Choice, Noul, Score, TypeSafeAPIError, TypeSafeRateLimitError

from app.config import Settings
from app.decision_engine import DecisionEngineProviderError, DecisionRequest
from app.decision_specifications import DecisionSpecification
from app.jev_decision_engine import (
    TypeSafeJevDecisionEngine,
    _minimum_window_target_tokens,
    _payload_characters,
    _payload_tokens,
    _window_states,
    build_decision_engine_registry,
    get_shared_decision_engine_registry,
)


def _request() -> DecisionRequest:
    specification = DecisionSpecification.model_validate(
        {
            "questions": {
                "issue.responsiveness": {
                    "type": "choice",
                    "instructions": "Choose the best responsiveness classification.",
                    "criteria": {"responsive": "Responsive", "not_responsive": "Not responsive"},
                    "source_refs": [
                        {
                            "task_version_id": "56d6dd2c-41ad-4854-b696-a7b34c31becb",
                            "heading": "Responsiveness",
                            "excerpt_hash": "a" * 64,
                        }
                    ],
                    "aggregation": {"operator": "HIGHEST_CONFIDENCE_CHOICE"},
                },
                "issue.strength": {
                    "type": "score",
                    "instructions": "Score the strength of the evidence.",
                    "criteria": ["none", "weak", "strong"],
                    "source_refs": [
                        {
                            "task_version_id": "56d6dd2c-41ad-4854-b696-a7b34c31becb",
                            "heading": "Evidence",
                            "excerpt_hash": "b" * 64,
                        }
                    ],
                    "aggregation": {"operator": "MAX_PROBABILITY"},
                },
                "privilege.legal_advice": {
                    "type": "noul",
                    "instructions": "Does the document request or provide legal advice?",
                    "criteria": {"true": "Legal advice is present", "false": "It is absent"},
                    "source_refs": [
                        {
                            "task_version_id": "56d6dd2c-41ad-4854-b696-a7b34c31becb",
                            "heading": "Legal advice",
                            "excerpt_hash": "c" * 64,
                        }
                    ],
                    "aggregation": {"operator": "ANY_WINDOW"},
                },
            },
            "state_contract": {
                "builder_version": "document-review-state-v1",
                "required_paths": ["document.paragraphs"],
            },
        }
    )
    return DecisionRequest(
        state={
            "matter": {
                "decision_context": {
                    "summary": "Review documents concerning legal advice and the defined investigation issues."
                }
            },
            "document": {"paragraphs": [{"number": 1, "text": "Counsel advised the client."}]},
        },
        questions=specification.questions,
        model_key="jev-latest",
    )


class _FakeLimiter:
    def __init__(self) -> None:
        self.acquisitions: list[tuple[tuple[str, str], int]] = []
        self.deferrals: list[tuple[tuple[str, str], float]] = []

    async def acquire(self, key: tuple[str, str], **kwargs: Any) -> float:
        self.acquisitions.append((key, kwargs["estimated_input_tokens"]))
        return 0

    def defer(self, key: tuple[str, str], delay_seconds: float) -> None:
        self.deferrals.append((key, delay_seconds))


class _FakeClientContext(AbstractAsyncContextManager):
    def __init__(self, client: Any) -> None:
        self.client = client

    async def __aenter__(self) -> Any:
        return self.client

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def system_one(
        self,
        state: Any,
        questions: Mapping[str, Choice | Noul | Score],
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"state": state, "questions": questions, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _ConcurrentFakeClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.maximum_active = 0

    async def system_one(self, **_: Any) -> Any:
        with self._lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0.03)
            return _response()
        finally:
            with self._lock:
                self.active -= 1


def _response() -> SimpleNamespace:
    return SimpleNamespace(
        model="jev-2026-09-01",
        request_id="req_123",
        raw_http_response=SimpleNamespace(
            status_code=200,
            headers={"x-typesafe-request-id": "req_123", "authorization": "never-store"},
        ),
        usage=SimpleNamespace(input_tokens=120, output_tokens=18),
        answers={
            "issue.responsiveness": {
                "type": "choice",
                "choice": "responsive",
                "confidence": 0.88,
                "probabilities": {"responsive": 0.91, "not_responsive": 0.09},
            },
            "issue.strength": {
                "type": "score",
                "score": 1.8,
                "confidence": 0.72,
                "legend": {"0": "none", "1": "weak", "2": "strong"},
                "probabilities": {"0": 0.02, "1": 0.16, "2": 0.82},
            },
            "privilege.legal_advice": {"type": "noul", "noul": 0.94},
        },
    )


def test_jev_adapter_translates_questions_and_preserves_provider_semantics() -> None:
    fake_client = _FakeClient([_response()])
    limiter = _FakeLimiter()
    times = iter([datetime(2026, 9, 26, tzinfo=UTC), datetime(2026, 9, 26, 0, 0, 1, tzinfo=UTC)])
    monotonic = iter([10.0, 10.25])
    engine = TypeSafeJevDecisionEngine(
        Settings(typesafe_api_key="secret"),
        client_factory=lambda **_: _FakeClientContext(fake_client),
        limiter=limiter,  # type: ignore[arg-type]
        now=lambda: next(times),
        monotonic=lambda: next(monotonic),
    )

    envelope = asyncio.run(engine.evaluate(_request()))

    assert isinstance(fake_client.calls[0]["questions"]["issue.responsiveness"], Choice)
    assert isinstance(fake_client.calls[0]["questions"]["issue.strength"], Score)
    assert isinstance(fake_client.calls[0]["questions"]["privilege.legal_advice"], Noul)
    assert not hasattr(fake_client.calls[0]["questions"]["issue.responsiveness"], "source_refs")
    assert fake_client.calls[0]["state"]["matter"]["decision_context"]["summary"].startswith(
        "Review documents"
    )
    assert envelope.provider == "typesafe"
    assert envelope.model == "jev-2026-09-01"
    assert envelope.provider_request_id == "req_123"
    assert envelope.answers["issue.responsiveness"].confidence == 0.88  # type: ignore[union-attr]
    assert envelope.answers["issue.strength"].probabilities["2"] == 0.82  # type: ignore[union-attr]
    assert envelope.answers["privilege.legal_advice"].model_dump() == {"type": "noul", "noul": 0.94}
    assert envelope.usage.input_tokens == 120
    assert envelope.usage.output_tokens == 18
    assert envelope.latency_ms == 250
    assert envelope.attempts == 1
    assert envelope.provider_metadata == {
        "http_status": 200,
        "typesafe_request_id": "req_123",
    }


def test_jev_adapter_partitions_only_the_document_and_aggregates_typed_answers() -> None:
    request = _request().model_copy(
        update={
            "state": {
                **_request().state,
                "document": {
                    "paragraphs": [
                        {"paragraph_id": f"¶{index}", "text": character * 7_000}
                        for index, character in enumerate(("A", "B", "C"), start=1)
                    ]
                },
            }
        }
    )
    states = _window_states(request, target_characters=10_000, maximum_windows=128)
    assert len(states) >= 3
    responses = []
    for index in range(len(states)):
        response = _response()
        response.request_id = f"req_window_{index + 1}"
        response.raw_http_response.headers["x-typesafe-request-id"] = response.request_id
        response.answers = {**response.answers}
        response.answers["privilege.legal_advice"] = {
            "type": "noul",
            "noul": (index + 1) / len(states),
        }
        response.answers["issue.responsiveness"] = {
            **response.answers["issue.responsiveness"],
            "confidence": (index + 1) / len(states),
        }
        response.answers["issue.strength"] = {
            **response.answers["issue.strength"],
            "score": float(index),
        }
        responses.append(response)
    fake_client = _FakeClient(responses)
    engine = TypeSafeJevDecisionEngine(
        Settings(
            typesafe_api_key="secret",
            typesafe_request_target_characters=10_000,
        ),
        client_factory=lambda **_: _FakeClientContext(fake_client),
        limiter=_FakeLimiter(),  # type: ignore[arg-type]
    )

    envelope = asyncio.run(engine.evaluate(request))

    assert len(fake_client.calls) == len(states)
    assert all(set(call["questions"]) == set(request.questions) for call in fake_client.calls)
    assert all(call["state"]["matter"] == request.state["matter"] for call in fake_client.calls)
    assert envelope.answers["privilege.legal_advice"].noul == 1.0  # type: ignore[union-attr]
    assert envelope.answers["issue.responsiveness"].confidence == 1.0  # type: ignore[union-attr]
    assert envelope.answers["issue.strength"].score == float(len(states) - 1)  # type: ignore[union-attr]
    assert envelope.usage.request_count == len(states)
    assert envelope.provider_metadata["windowed"] is True
    assert envelope.provider_metadata["window_count"] == len(states)


def test_jev_document_windows_keep_duplicate_representations_in_lockstep() -> None:
    source_text = "A" * 60_000
    request = _request().model_copy(
        update={
            "state": {
                **_request().state,
                "document": {
                    "text": source_text,
                    "paragraphs": [{"paragraph_id": "¶1", "text": source_text}],
                },
            }
        }
    )

    states = _window_states(request, target_characters=10_000, maximum_windows=128)

    assert 1 < len(states) < 128
    assert all(len(state["document"]["text"]) < len(source_text) for state in states)
    assert all(
        sum(len(paragraph["text"]) for paragraph in state["document"]["paragraphs"])
        < len(source_text)
        for state in states
    )
    assert all(_payload_characters(request, state=state) <= 11_000 for state in states)


def test_jev_document_windows_enforce_token_target_for_dense_text() -> None:
    request = _request().model_copy(
        update={
            "state": {
                **_request().state,
                "document": {"text": "\x01Ä\n" * 5_000},
            }
        }
    )

    states = _window_states(
        request,
        target_characters=60_000,
        target_tokens=1_000,
        maximum_windows=128,
    )

    assert len(states) > 1
    assert all(_payload_tokens(request, state=state) <= 1_000 for state in states)


def test_jev_adapter_refines_document_after_provider_max_tokens_error() -> None:
    request = _request().model_copy(
        update={
            "state": {
                **_request().state,
                "document": {
                    "paragraphs": [
                        {"paragraph_id": f"¶{index}", "text": character * 7_000}
                        for index, character in enumerate(("A", "B", "C"), start=1)
                    ]
                },
            }
        }
    )
    refined_target = max(10_000, _payload_characters(request) // 2)
    refined_target_tokens = max(_minimum_window_target_tokens(request), _payload_tokens(request) // 2)
    refined_states = _window_states(
        request,
        target_characters=refined_target,
        target_tokens=refined_target_tokens,
        maximum_windows=128,
    )
    provider_error = TypeSafeAPIError(
        400,
        {"detail": {"error_type": "max_tokens_exceeded"}},
        httpx2.Headers({"x-typesafe-request-id": "req_oversized"}),
    )
    fake_client = _FakeClient([provider_error, *[_response() for _ in refined_states]])
    engine = TypeSafeJevDecisionEngine(
        Settings(
            typesafe_api_key="secret",
            typesafe_request_target_characters=100_000,
            typesafe_request_target_tokens=100_000,
        ),
        client_factory=lambda **_: _FakeClientContext(fake_client),
        limiter=_FakeLimiter(),  # type: ignore[arg-type]
    )

    envelope = asyncio.run(engine.evaluate(request))

    assert len(fake_client.calls) == 1 + len(refined_states)
    assert len(fake_client.calls[0]["state"]["document"]["paragraphs"]) == 3
    assert all(set(call["questions"]) == set(request.questions) for call in fake_client.calls[1:])
    assert envelope.provider_metadata["windowed"] is True


def test_jev_adapter_retries_429_and_reports_attempt_count() -> None:
    rate_limit = TypeSafeRateLimitError(
        429,
        {"message": "slow down"},
        httpx2.Headers({"retry-after": "0"}),
    )
    fake_client = _FakeClient([rate_limit, _response()])
    limiter = _FakeLimiter()
    sleeps: list[float] = []
    engine = TypeSafeJevDecisionEngine(
        Settings(
            typesafe_api_key="secret",
            typesafe_max_retries=2,
            typesafe_retry_base_seconds=0.1,
            typesafe_retry_max_seconds=1,
        ),
        client_factory=lambda **_: _FakeClientContext(fake_client),
        limiter=limiter,  # type: ignore[arg-type]
        sleep=lambda delay: _record_sleep(sleeps, delay),
        jitter=lambda: 0,
    )

    envelope = asyncio.run(engine.evaluate(_request()))

    assert envelope.attempts == 2
    assert envelope.usage.request_count == 2
    assert len(limiter.acquisitions) == 2
    assert limiter.deferrals == [(('typesafe', 'jev-latest'), 0.1)]
    assert sleeps == [0.1]


async def _record_sleep(sleeps: list[float], delay: float) -> None:
    sleeps.append(delay)


def test_jev_adapter_rejects_missing_configuration_and_mismatched_answers() -> None:
    with pytest.raises(DecisionEngineProviderError, match="TYPESAFE_API_KEY") as missing:
        asyncio.run(TypeSafeJevDecisionEngine(Settings(typesafe_api_key=None)).evaluate(_request()))
    assert missing.value.code == "NOT_CONFIGURED"

    response = _response()
    del response.answers["privilege.legal_advice"]
    engine = TypeSafeJevDecisionEngine(
        Settings(typesafe_api_key="secret"),
        client_factory=lambda **_: _FakeClientContext(_FakeClient([response])),
        limiter=_FakeLimiter(),  # type: ignore[arg-type]
    )
    with pytest.raises(DecisionEngineProviderError, match="answer keys") as mismatch:
        asyncio.run(engine.evaluate(_request()))
    assert mismatch.value.code == "ANSWER_KEY_MISMATCH"


def test_jev_is_registered_in_a_code_owned_adapter_allowlist() -> None:
    registry = build_decision_engine_registry(Settings(typesafe_api_key="secret"))
    assert registry.keys() == ("jev", "openai_decisions")
    assert isinstance(registry.get("jev"), TypeSafeJevDecisionEngine)


def test_shared_jev_registry_reuses_one_engine_for_matching_configuration() -> None:
    settings = Settings(typesafe_api_key="shared-secret", typesafe_concurrency=2)

    first = get_shared_decision_engine_registry(settings)
    second = get_shared_decision_engine_registry(settings)

    assert first is second
    assert first.get("jev") is second.get("jev")


def test_shared_jev_engine_caps_concurrency_across_threads_and_event_loops() -> None:
    fake_client = _ConcurrentFakeClient()
    engine = TypeSafeJevDecisionEngine(
        Settings(typesafe_api_key="secret", typesafe_concurrency=2),
        client_factory=lambda **_: _FakeClientContext(fake_client),
        limiter=_FakeLimiter(),  # type: ignore[arg-type]
    )

    with ThreadPoolExecutor(max_workers=6) as executor:
        envelopes = list(executor.map(lambda _: asyncio.run(engine.evaluate(_request())), range(6)))

    assert len(envelopes) == 6
    assert fake_client.maximum_active == 2
