import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic_core import to_jsonable_python

from app.config import Settings

TRACE_VERSION = "model-call-v1"
_SAFE_FILENAME = re.compile(r"[^a-zA-Z0-9._-]+")
logger = logging.getLogger(__name__)
_ACTIVE_MODEL_CALL_TRACE: ContextVar["ModelCallTrace | None"] = ContextVar(
    "active_model_call_trace",
    default=None,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _safe_component(value: str, *, fallback: str) -> str:
    cleaned = _SAFE_FILENAME.sub("-", value.strip()).strip("-._")
    return cleaned[:120] or fallback


def _jsonable(value: Any) -> Any:
    return to_jsonable_python(value, fallback=lambda item: str(item))


class ModelCallTrace:
    def __init__(
        self,
        *,
        directory: Path,
        request_type: str,
        identifier: str | None,
        request: dict[str, Any],
    ) -> None:
        started_at = _utcnow()
        safe_type = _safe_component(request_type, fallback="model-request")
        safe_identifier = _safe_component(identifier or str(uuid.uuid4()), fallback="unidentified")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        self.path = directory / f"{safe_type}-{safe_identifier}-{_timestamp(started_at)}.json"
        self._lock = threading.RLock()
        self._record: dict[str, Any] = {
            "trace_version": TRACE_VERSION,
            "request_type": request_type,
            "identifier": identifier,
            "started_at": started_at.isoformat(),
            "completed_at": None,
            "status": "RUNNING",
            "request": _jsonable(request),
            "events": [],
            "response": None,
            "error": None,
        }
        self._write()

    def event(self, event_type: str, data: Any) -> None:
        with self._lock:
            self._record["events"].append({"at": _utcnow().isoformat(), "type": event_type, "data": _jsonable(data)})
            self._write_best_effort()

    def complete(self, response: Any) -> None:
        with self._lock:
            self._record["status"] = "COMPLETED"
            self._record["completed_at"] = _utcnow().isoformat()
            self._record["response"] = _jsonable(response)
            self._write_best_effort()

    def fail(self, exc: BaseException) -> None:
        with self._lock:
            self._record["status"] = "FAILED"
            self._record["completed_at"] = _utcnow().isoformat()
            self._record["error"] = {"type": type(exc).__name__, "message": str(exc)}
            self._write_best_effort()

    def _write_best_effort(self) -> None:
        try:
            self._write()
        except (OSError, TypeError, ValueError):
            logger.exception("Could not write model trace to %s", self.path)

    def _write(self) -> None:
        content = json.dumps(self._record, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        finally:
            if temporary.exists():
                temporary.unlink()


def current_model_call_trace() -> ModelCallTrace | None:
    return _ACTIVE_MODEL_CALL_TRACE.get()


@contextmanager
def model_call_trace_context(trace: ModelCallTrace | None) -> Iterator[None]:
    token = _ACTIVE_MODEL_CALL_TRACE.set(trace)
    try:
        yield
    finally:
        _ACTIVE_MODEL_CALL_TRACE.reset(token)


def start_model_call_trace(
    settings: Settings,
    *,
    request_type: str,
    identifier: str | None,
    request: dict[str, Any],
) -> ModelCallTrace | None:
    if not settings.model_trace_enabled:
        return None
    try:
        return ModelCallTrace(
            directory=Path(settings.model_trace_directory).expanduser().resolve(),
            request_type=request_type,
            identifier=identifier,
            request=request,
        )
    except (OSError, TypeError, ValueError):
        logger.exception("Could not start filesystem model trace")
        return None
