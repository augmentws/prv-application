import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart

from app.model_rate_limits import (
    ModelRateLimitPolicy,
    ProviderModelRateLimiter,
    ProviderModelRequestController,
    provider_retry_delay_seconds,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def policy(**overrides: Any) -> ModelRateLimitPolicy:
    values = {
        "requests_per_minute": 12,
        "input_tokens_per_minute": 200_000,
        "max_retries": 2,
        "retry_base_seconds": 1.0,
        "retry_max_seconds": 120.0,
        "characters_per_token": 3.0,
    }
    values.update(overrides)
    return ModelRateLimitPolicy(**values)


def request_context(model_id: str = "google:gemini-test") -> Any:
    return SimpleNamespace(
        model_id=model_id,
        model=SimpleNamespace(system="google", model_name="gemini-test"),
        messages=[],
        model_request_parameters={},
    )


def test_rate_limiter_enforces_independent_provider_model_request_windows() -> None:
    clock = FakeClock()
    limiter = ProviderModelRateLimiter(monotonic=clock.monotonic, sleep=clock.sleep)

    async def exercise() -> None:
        options = {
            "estimated_input_tokens": 10,
            "requests_per_minute": 2,
            "input_tokens_per_minute": 1_000,
        }
        assert await limiter.acquire(("google", "model-a"), **options) == 0
        assert await limiter.acquire(("google", "model-a"), **options) == 0
        assert await limiter.acquire(("google", "model-b"), **options) == 0
        assert await limiter.acquire(("google", "model-a"), **options) == 60

    asyncio.run(exercise())
    assert clock.sleeps == [60]


def test_rate_limiter_enforces_estimated_input_token_window() -> None:
    clock = FakeClock()
    limiter = ProviderModelRateLimiter(monotonic=clock.monotonic, sleep=clock.sleep)

    async def exercise() -> None:
        options = {"requests_per_minute": 10, "input_tokens_per_minute": 100}
        assert await limiter.acquire(("google", "model"), estimated_input_tokens=60, **options) == 0
        assert await limiter.acquire(("google", "model"), estimated_input_tokens=60, **options) == 60

    asyncio.run(exercise())


def test_provider_retry_delay_reads_google_retry_info() -> None:
    error = ModelHTTPError(
        429,
        "gemini-test",
        body={
            "error": {
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "43.908441307s"}
                ]
            }
        },
    )

    assert provider_retry_delay_seconds(error) == pytest.approx(43.908441307)


def test_controller_shares_provider_cooldown_and_retries_429() -> None:
    clock = FakeClock()
    limiter = ProviderModelRateLimiter(monotonic=clock.monotonic, sleep=clock.sleep)
    controller = ProviderModelRequestController(limiter=limiter, policy=policy(), jitter=lambda: 0.0)
    calls = 0

    async def handler(context: Any) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModelHTTPError(
                429,
                "gemini-test",
                body={"error": {"details": [{"retryDelay": "43s"}]}},
            )
        return ModelResponse(parts=[TextPart("ok")], model_name="gemini-test")

    result = asyncio.run(controller.execute(request_context(), handler))

    assert result.parts == [TextPart("ok")]
    assert calls == 2
    assert clock.sleeps == [43]


def test_controller_does_not_retry_non_rate_limit_errors() -> None:
    clock = FakeClock()
    controller = ProviderModelRequestController(
        limiter=ProviderModelRateLimiter(monotonic=clock.monotonic, sleep=clock.sleep),
        policy=policy(),
        jitter=lambda: 0.0,
    )

    async def handler(context: Any) -> ModelResponse:
        raise ModelHTTPError(500, "gemini-test", body={"error": "unavailable"})

    with pytest.raises(ModelHTTPError) as exc_info:
        asyncio.run(controller.execute(request_context(), handler))

    assert exc_info.value.status_code == 500
    assert clock.sleeps == []


def test_controller_does_not_throttle_local_test_models() -> None:
    clock = FakeClock()
    controller = ProviderModelRequestController(
        limiter=ProviderModelRateLimiter(monotonic=clock.monotonic, sleep=clock.sleep),
        policy=policy(requests_per_minute=1),
        jitter=lambda: 0.0,
    )
    context = request_context("test:test")

    async def handler(request: Any) -> ModelResponse:
        return ModelResponse(parts=[TextPart("ok")], model_name="test")

    async def exercise() -> None:
        await controller.execute(context, handler)
        await controller.execute(context, handler)

    asyncio.run(exercise())
    assert clock.sleeps == []
