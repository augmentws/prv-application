"""Utilities for preparing and running Voyage asynchronous embedding batches."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx

from embedding_service.config import EmbeddingSettings

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_TERMINAL_BATCH_STATUSES = frozenset({"completed", "partially_completed", "failed", "cancelled"})


class VoyageBatchClient:
    """Small, retrying client for Voyage's file and asynchronous batch APIs."""

    def __init__(
        self,
        settings: EmbeddingSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if settings.provider != "voyage_api":
            raise ValueError("VoyageBatchClient requires EMBEDDING_PROVIDER=voyage_api")
        if settings.voyage_api_key is None:
            raise ValueError("VOYAGE_API_KEY is required")
        self.settings = settings
        self._transport = transport
        self._sleep = sleep

    @property
    def _headers(self) -> dict[str, str]:
        assert self.settings.voyage_api_key is not None
        return {
            "Authorization": f"Bearer {self.settings.voyage_api_key.get_secret_value()}",
            "Accept": "application/json",
        }

    def upload_input_file(self, path: Path) -> dict[str, Any]:
        """Upload a JSONL input file with ``purpose=batch``."""
        path = path.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)

        def send(client: httpx.Client) -> httpx.Response:
            with path.open("rb") as handle:
                return client.post(
                    "/v1/files",
                    headers=self._headers,
                    data={"purpose": "batch"},
                    files={"file": (path.name, handle, "application/jsonl")},
                )

        return self._request_json(send)

    def create_batch(
        self,
        input_file_id: str,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "input_file_id": input_file_id,
            "endpoint": "/v1/embeddings",
            "completion_window": "12h",
            "request_params": {
                "model": self.settings.model,
                "input_type": "document",
                "truncation": False,
                "output_dimension": self.settings.dimensions,
                "output_dtype": "float",
            },
        }
        if metadata:
            body["metadata"] = dict(metadata)
        return self._request_json(
            lambda client: client.post("/v1/batches", headers=self._headers, json=body),
            retry=False,
        )

    def submit(
        self,
        input_path: Path,
        *,
        metadata: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        uploaded = self.upload_input_file(input_path)
        file_id = _required_string(uploaded, "id", "uploaded file")
        batch = self.create_batch(file_id, metadata=metadata)
        return {"input_file": uploaded, "batch": batch}

    def submit_reconciled(
        self,
        input_path: Path,
        *,
        metadata: Mapping[str, str],
    ) -> dict[str, Any]:
        """Submit idempotently by reconciling deterministic metadata and filename."""
        application_batch_id = metadata.get("priv_view_batch_id")
        if not application_batch_id:
            raise ValueError("metadata must include priv_view_batch_id")
        existing_batch = self.find_batch(
            metadata_key="priv_view_batch_id",
            metadata_value=application_batch_id,
        )
        if existing_batch is not None:
            input_file_id = _required_string(existing_batch, "input_file_id", "existing Voyage batch")
            return {
                "input_file": {"id": input_file_id, "filename": input_path.name},
                "batch": existing_batch,
            }

        existing_file = self.find_file(filename=input_path.name, purpose="batch")
        uploaded = existing_file or self.upload_input_file(input_path)
        input_file_id = _required_string(uploaded, "id", "uploaded file")

        # A previous create request may have succeeded even if its response was
        # lost. Reconcile a second time immediately before making that side effect.
        existing_batch = self.find_batch(
            metadata_key="priv_view_batch_id",
            metadata_value=application_batch_id,
        )
        if existing_batch is not None:
            return {"input_file": uploaded, "batch": existing_batch}
        batch = self.create_batch(input_file_id, metadata=metadata)
        return {"input_file": uploaded, "batch": batch}

    def list_files(
        self,
        *,
        purpose: str | None = None,
        limit: int = 10_000,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "order": "desc"}
        if purpose:
            params["purpose"] = purpose
        return self._request_json(lambda client: client.get("/v1/files", headers=self._headers, params=params))

    def find_file(self, *, filename: str, purpose: str) -> dict[str, Any] | None:
        payload = self.list_files(purpose=purpose)
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise TypeError("Voyage Files API returned no data list")
        for row in rows:
            if isinstance(row, dict) and row.get("filename") == filename and row.get("purpose") == purpose:
                return row
        return None

    def list_batches(
        self,
        *,
        limit: int = 100,
        after: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        return self._request_json(lambda client: client.get("/v1/batches", headers=self._headers, params=params))

    def find_batch(
        self,
        *,
        metadata_key: str,
        metadata_value: str,
    ) -> dict[str, Any] | None:
        after: str | None = None
        while True:
            payload = self.list_batches(after=after)
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise TypeError("Voyage Batch API returned no data list")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                metadata = row.get("metadata")
                if isinstance(metadata, dict) and metadata.get(metadata_key) == metadata_value:
                    return row
            if not payload.get("has_more") or not rows:
                return None
            last = rows[-1]
            if not isinstance(last, dict):
                raise TypeError("Voyage Batch API returned an invalid pagination cursor")
            after = _required_string(last, "id", "Voyage batch page")

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        return self._request_json(lambda client: client.get(f"/v1/batches/{batch_id}", headers=self._headers))

    def cancel_batch(self, batch_id: str) -> dict[str, Any]:
        return self._request_json(lambda client: client.post(f"/v1/batches/{batch_id}/cancel", headers=self._headers))

    def wait_for_batch(
        self,
        batch_id: str,
        *,
        poll_seconds: float = 15.0,
        timeout_seconds: float = 43_200.0,
        on_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        deadline = time.monotonic() + timeout_seconds
        previous_status: str | None = None
        while True:
            batch = self.get_batch(batch_id)
            status = _required_string(batch, "status", "batch")
            if on_update is not None and status != previous_status:
                on_update(batch)
            if status in _TERMINAL_BATCH_STATUSES:
                return batch
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Voyage batch {batch_id} did not finish within {timeout_seconds}s")
            previous_status = status
            self._sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

    def download_file(self, file_id: str, output_path: Path, *, force: bool = False) -> int:
        output_path = output_path.expanduser().resolve()
        _ensure_writable_output(output_path, force=force)
        temp_path = output_path.with_name(f".{output_path.name}.tmp")
        if temp_path.exists():
            temp_path.unlink()

        last_error: Exception | None = None
        for attempt in range(self.settings.voyage_max_retries + 1):
            try:
                with (
                    self._client() as client,
                    client.stream(
                        "GET",
                        f"/v1/files/{file_id}/content",
                        headers=self._headers,
                    ) as response,
                ):
                    if response.status_code in _RETRYABLE_STATUS_CODES:
                        retry_after = response.headers.get("Retry-After")
                        response.read()
                        if attempt >= self.settings.voyage_max_retries:
                            response.raise_for_status()
                        self._wait_before_retry(attempt, retry_after)
                        continue
                    response.raise_for_status()
                    byte_count = 0
                    with temp_path.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            handle.write(chunk)
                            byte_count += len(chunk)
                    temp_path.replace(output_path)
                    return byte_count
            except httpx.TransportError as exc:
                last_error = exc
                if temp_path.exists():
                    temp_path.unlink()
                if attempt >= self.settings.voyage_max_retries:
                    raise
                self._wait_before_retry(attempt, None)
        assert last_error is not None
        raise last_error

    def _request_json(
        self,
        send: Callable[[httpx.Client], httpx.Response],
        *,
        retry: bool = True,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.settings.voyage_max_retries + 1):
            try:
                with self._client() as client:
                    response = send(client)
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    if not retry or attempt >= self.settings.voyage_max_retries:
                        response.raise_for_status()
                    self._wait_before_retry(attempt, response.headers.get("Retry-After"))
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TypeError("Voyage API returned a non-object JSON response")
                return payload
            except httpx.TransportError as exc:
                last_error = exc
                if not retry or attempt >= self.settings.voyage_max_retries:
                    raise
                self._wait_before_retry(attempt, None)
        assert last_error is not None
        raise last_error

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.settings.voyage_base_url.rstrip("/"),
            timeout=self.settings.request_timeout_seconds,
            transport=self._transport,
        )

    def _wait_before_retry(self, attempt: int, retry_after: str | None) -> None:
        delay = self.settings.voyage_retry_base_seconds * (2**attempt)
        if retry_after is not None:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        self._sleep(delay)


def prepare_batch_input(
    source_path: Path,
    request_path: Path,
    manifest_path: Path,
    *,
    max_inputs_per_request: int = 256,
    force: bool = False,
) -> dict[str, int]:
    """Convert ``{"id", "text"}`` JSONL into Voyage request and manifest JSONL."""
    if not 1 <= max_inputs_per_request <= 1000:
        raise ValueError("max_inputs_per_request must be between 1 and 1000")
    source_path = source_path.expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    request_path = request_path.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    if request_path == manifest_path:
        raise ValueError("request and manifest paths must be different")
    _ensure_writable_output(request_path, force=force)
    _ensure_writable_output(manifest_path, force=force)

    request_temp = request_path.with_name(f".{request_path.name}.tmp")
    manifest_temp = manifest_path.with_name(f".{manifest_path.name}.tmp")
    for temp_path in (request_temp, manifest_temp):
        if temp_path.exists():
            temp_path.unlink()

    seen_ids: set[str] = set()
    item_count = 0
    request_count = 0
    try:
        with (
            request_temp.open("w", encoding="utf-8") as requests,
            manifest_temp.open("w", encoding="utf-8") as manifest,
        ):
            for group in _groups(_read_source_items(source_path, seen_ids), max_inputs_per_request):
                custom_id = f"request-{request_count:08d}"
                request = {
                    "custom_id": custom_id,
                    "body": {"input": [item[1] for item in group]},
                }
                mapping = {
                    "custom_id": custom_id,
                    "item_ids": [item[0] for item in group],
                }
                requests.write(json.dumps(request, ensure_ascii=False) + "\n")
                manifest.write(json.dumps(mapping, ensure_ascii=False) + "\n")
                item_count += len(group)
                request_count += 1
                if request_count > 100_000:
                    raise ValueError("Voyage batch files may contain at most 100,000 requests")
        if item_count == 0:
            raise ValueError("source JSONL contains no items")
        request_temp.replace(request_path)
        manifest_temp.replace(manifest_path)
    except Exception:
        for temp_path in (request_temp, manifest_temp):
            if temp_path.exists():
                temp_path.unlink()
        raise
    return {"items": item_count, "requests": request_count}


def resolve_batch_output(
    output_path: Path,
    manifest_path: Path,
    resolved_path: Path,
    *,
    force: bool = False,
) -> dict[str, int]:
    """Join unordered Voyage batch responses to source IDs and emit flat JSONL."""
    output_path = output_path.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    resolved_path = resolved_path.expanduser().resolve()
    for input_path in (output_path, manifest_path):
        if not input_path.is_file():
            raise FileNotFoundError(input_path)
    _ensure_writable_output(resolved_path, force=force)

    manifest = _load_manifest(manifest_path)
    temp_path = resolved_path.with_name(f".{resolved_path.name}.tmp")
    if temp_path.exists():
        temp_path.unlink()
    item_count = 0
    request_count = 0
    total_tokens = 0
    try:
        with (
            output_path.open("r", encoding="utf-8") as source,
            temp_path.open("w", encoding="utf-8") as destination,
        ):
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue
                record = _parse_object(line, output_path, line_number)
                custom_id = _required_string(record, "custom_id", f"output line {line_number}")
                item_ids = manifest.pop(custom_id, None)
                if item_ids is None:
                    raise ValueError(f"Unknown or duplicate custom_id in output: {custom_id}")
                if record.get("error") is not None:
                    raise ValueError(f"Voyage batch request {custom_id} failed: {record['error']}")
                response = record.get("response")
                if not isinstance(response, dict) or response.get("status_code") != 200:
                    raise ValueError(f"Voyage batch request {custom_id} did not return HTTP 200")
                body = response.get("body")
                if not isinstance(body, dict):
                    raise TypeError(f"Voyage batch request {custom_id} has no response body")
                data = body.get("data")
                if not isinstance(data, list):
                    raise TypeError(f"Voyage batch request {custom_id} has no embedding data")
                ordered = sorted(data, key=_embedding_index)
                if len(ordered) != len(item_ids):
                    raise ValueError(
                        f"Voyage batch request {custom_id} returned {len(ordered)} embeddings "
                        f"for {len(item_ids)} inputs"
                    )
                for expected_index, (item_id, embedding_record) in enumerate(zip(item_ids, ordered, strict=True)):
                    if _embedding_index(embedding_record) != expected_index:
                        raise TypeError(f"Voyage batch request {custom_id} has a missing embedding index")
                    embedding = embedding_record.get("embedding")
                    if not isinstance(embedding, list):
                        raise TypeError(f"Voyage batch request {custom_id} returned an invalid embedding")
                    destination.write(
                        json.dumps(
                            {
                                "id": item_id,
                                "embedding": embedding,
                                "model": body.get("model"),
                            },
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    item_count += 1
                usage = body.get("usage")
                if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
                    total_tokens += usage["total_tokens"]
                request_count += 1
        if manifest:
            first_missing = next(iter(manifest))
            raise ValueError(f"Batch output is missing {len(manifest)} request(s); first missing: {first_missing}")
        temp_path.replace(resolved_path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise
    return {"items": item_count, "requests": request_count, "total_tokens": total_tokens}


def _read_source_items(path: Path, seen_ids: set[str]) -> Iterator[tuple[str, str]]:
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            record = _parse_object(line, path, line_number)
            item_id = _required_string(record, "id", f"source line {line_number}")
            text = _required_string(record, "text", f"source line {line_number}")
            if item_id in seen_ids:
                raise ValueError(f"Duplicate source id at line {line_number}: {item_id}")
            seen_ids.add(item_id)
            yield item_id, text


def _groups(values: Iterable[tuple[str, str]], size: int) -> Iterator[list[tuple[str, str]]]:
    group: list[tuple[str, str]] = []
    for value in values:
        group.append(value)
        if len(group) == size:
            yield group
            group = []
    if group:
        yield group


def _load_manifest(path: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            record = _parse_object(line, path, line_number)
            custom_id = _required_string(record, "custom_id", f"manifest line {line_number}")
            item_ids = record.get("item_ids")
            if (
                not isinstance(item_ids, list)
                or not item_ids
                or not all(isinstance(item_id, str) and item_id for item_id in item_ids)
            ):
                raise ValueError(f"Invalid item_ids in manifest line {line_number}")
            if custom_id in result:
                raise ValueError(f"Duplicate custom_id in manifest: {custom_id}")
            result[custom_id] = item_ids
    if not result:
        raise ValueError("manifest JSONL contains no requests")
    return result


def _parse_object(line: str, path: Path, line_number: int) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path} at line {line_number}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"Expected a JSON object in {path} at line {line_number}")
    return value


def _required_string(value: Mapping[str, Any], key: str, context: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ValueError(f"Missing {key!r} in {context}")
    return result


def _embedding_index(value: Any) -> int:
    if not isinstance(value, dict) or not isinstance(value.get("index"), int):
        raise TypeError("Voyage returned embedding data without an integer index")
    return value["index"]


def _ensure_writable_output(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"Output already exists (use --force): {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
