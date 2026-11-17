import asyncio
from types import SimpleNamespace

from app.google_prompt_cache import GooglePromptCache, parse_cache_ttl_seconds


def test_parse_cache_ttl_seconds() -> None:
    assert parse_cache_ttl_seconds("30s") == 30
    assert parse_cache_ttl_seconds("10m") == 600
    assert parse_cache_ttl_seconds("2h") == 7200


def test_google_prompt_cache_reuses_provider_resource() -> None:
    calls: list[dict[str, object]] = []

    class Caches:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(name="cachedContents/one")

    client = SimpleNamespace(aio=SimpleNamespace(caches=Caches()))
    cache = GooglePromptCache(client_factory=lambda: client, monotonic=lambda: 100.0)

    async def run():
        first = await cache.get_or_create(
            fingerprint="fingerprint",
            model="gemini-test",
            instructions=("instruction",),
            stable_prefix="stable",
            ttl="10m",
        )
        second = await cache.get_or_create(
            fingerprint="fingerprint",
            model="gemini-test",
            instructions=("instruction",),
            stable_prefix="stable",
            ttl="10m",
        )
        return first, second

    first, second = asyncio.run(run())

    assert first == second
    assert first is not None and first.name == "cachedContents/one"
    assert len(calls) == 1


def test_google_prompt_cache_failure_is_non_blocking() -> None:
    calls = 0

    class Caches:
        async def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("cache unavailable")

    client = SimpleNamespace(aio=SimpleNamespace(caches=Caches()))
    cache = GooglePromptCache(client_factory=lambda: client)

    result = asyncio.run(
        cache.get_or_create(
            fingerprint="fingerprint",
            model="gemini-test",
            instructions=("instruction",),
            stable_prefix="stable",
            ttl="10m",
        )
    )
    second = asyncio.run(
        cache.get_or_create(
            fingerprint="fingerprint",
            model="gemini-test",
            instructions=("instruction",),
            stable_prefix="stable",
            ttl="10m",
        )
    )

    assert result is None
    assert second is None
    assert calls == 1
