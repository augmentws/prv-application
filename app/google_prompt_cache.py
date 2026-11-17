import asyncio
import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_TTL_PATTERN = re.compile(r"^(?P<value>[1-9][0-9]*)(?P<unit>[smh])$")


@dataclass(frozen=True)
class GoogleCachedPrefix:
    name: str
    expires_at_monotonic: float


class GooglePromptCache:
    """Process-local index of provider-managed cached prefixes.

    Google owns the cached content; this index stores only its opaque resource
    name and expiration. A concurrent.futures.Future provides single-flight
    creation across async event loops without holding a database connection.
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client_factory = client_factory or genai.Client
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._entries: dict[str, GoogleCachedPrefix] = {}
        self._bypass_until: dict[str, float] = {}
        self._pending: dict[str, Future[GoogleCachedPrefix | None]] = {}

    async def get_or_create(
        self,
        *,
        fingerprint: str,
        model: str,
        instructions: tuple[str, ...],
        stable_prefix: str,
        ttl: str,
    ) -> GoogleCachedPrefix | None:
        ttl_seconds = parse_cache_ttl_seconds(ttl)
        now = self._monotonic()
        owner = False
        with self._lock:
            existing = self._entries.get(fingerprint)
            if existing is not None and existing.expires_at_monotonic > now + 5:
                return existing
            if self._bypass_until.get(fingerprint, 0) > now:
                return None
            pending = self._pending.get(fingerprint)
            if pending is None:
                pending = Future()
                self._pending[fingerprint] = pending
                owner = True
        if not owner:
            return await asyncio.wrap_future(pending)

        created: GoogleCachedPrefix | None = None
        try:
            client = self._client_factory()
            resource = await client.aio.caches.create(
                model=model,
                config=types.CreateCachedContentConfig(
                    display_name=f"priv-view-{fingerprint[:32]}",
                    system_instruction="\n\n".join(instructions),
                    contents=stable_prefix,
                    ttl=f"{ttl_seconds}s",
                ),
            )
            if resource.name:
                created = GoogleCachedPrefix(
                    name=resource.name,
                    expires_at_monotonic=self._monotonic() + ttl_seconds,
                )
                with self._lock:
                    self._entries[fingerprint] = created
        except Exception as exc:  # noqa: BLE001  Provider caching is never a correctness dependency.
            logger.warning(
                "Google explicit prompt cache unavailable; continuing without it: %s",
                exc,
            )
            with self._lock:
                self._bypass_until[fingerprint] = self._monotonic() + min(ttl_seconds, 300)
        finally:
            with self._lock:
                self._pending.pop(fingerprint, None)
            pending.set_result(created)
        return created


def parse_cache_ttl_seconds(value: str) -> int:
    match = _TTL_PATTERN.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError("Cache TTL must be a positive integer followed by s, m, or h")
    multiplier = {"s": 1, "m": 60, "h": 3600}[match.group("unit")]
    return int(match.group("value")) * multiplier


google_prompt_cache = GooglePromptCache()
