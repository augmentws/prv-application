from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

import httpx

LOGIN_OPERATION_ID = "login_v1_auth_login_post"


def _valid_json_unicode(value: Any) -> Any:
    """Replace Unicode surrogate code points that JSON receivers must reject."""

    if isinstance(value, str):
        return "".join("\ufffd" if 0xD800 <= ord(character) <= 0xDFFF else character for character in value)
    if isinstance(value, dict):
        return {
            _valid_json_unicode(key): _valid_json_unicode(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_valid_json_unicode(item) for item in value]
    return value


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status_code: int, detail: str) -> None:
        super().__init__(f"{method} {path} returned {status_code}: {detail}")
        self.status_code = status_code


@dataclass(frozen=True)
class OpenApiOperation:
    method: str
    path: str


class OpenApiClient:
    """Small HTTP client whose routes and methods are resolved from FastAPI OpenAPI operation IDs."""

    def __init__(
        self,
        base_url: str,
        openapi_path: Path,
        *,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        document = json.loads(openapi_path.read_text(encoding="utf-8"))
        self.operations: dict[str, OpenApiOperation] = {}
        for path, methods in document.get("paths", {}).items():
            for method, operation in methods.items():
                if not isinstance(operation, dict) or not operation.get("operationId"):
                    continue
                self.operations[operation["operationId"]] = OpenApiOperation(method.upper(), path)
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )
        self._reauthentication_credentials: tuple[str, str] | None = None
        self._reauthentication_lock = threading.Lock()

    def close(self) -> None:
        self.client.close()

    def set_access_token(self, token: str) -> None:
        self.client.headers["Authorization"] = f"Bearer {token}"

    def set_reauthentication_credentials(self, email: str, password: str) -> None:
        self._reauthentication_credentials = (email, password)

    @staticmethod
    def _file_positions(files: dict[str, Any] | None) -> list[tuple[BinaryIO, int]]:
        positions: list[tuple[BinaryIO, int]] = []
        for specification in (files or {}).values():
            content = specification[1] if isinstance(specification, tuple) else specification
            if not hasattr(content, "tell") or not hasattr(content, "seek"):
                continue
            try:
                positions.append((content, content.tell()))
            except (OSError, ValueError):
                continue
        return positions

    @staticmethod
    def _restore_file_positions(positions: list[tuple[BinaryIO, int]]) -> None:
        for content, position in positions:
            content.seek(position)

    def request(
        self,
        operation_id: str,
        *,
        path_params: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        try:
            operation = self.operations[operation_id]
        except KeyError as exc:
            raise RuntimeError(f"OpenAPI operation is missing: {operation_id}") from exc
        supplied = path_params or {}
        required = set(re.findall(r"\{([^}]+)\}", operation.path))
        if required != set(supplied):
            raise ValueError(
                f"Path parameters for {operation_id} must be {sorted(required)}, received {sorted(supplied)}"
            )
        path = operation.path
        for name, value in supplied.items():
            path = path.replace(f"{{{name}}}", quote(str(value), safe=""))
        request_kwargs = {
            "params": {key: value for key, value in (params or {}).items() if value is not None},
            "json": json_body,
            "data": data,
            "files": files,
        }
        file_positions = self._file_positions(files)
        authorization = self.client.headers.get("Authorization")
        response = self.client.request(operation.method, path, **request_kwargs)
        if (
            response.status_code == 401
            and operation_id != LOGIN_OPERATION_ID
            and self._reauthentication_credentials is not None
        ):
            with self._reauthentication_lock:
                if self.client.headers.get("Authorization") == authorization:
                    self.login(*self._reauthentication_credentials)
            self._restore_file_positions(file_positions)
            response = self.client.request(operation.method, path, **request_kwargs)
        if response.is_error:
            try:
                payload = response.json()
                detail = payload.get("error", {}).get("message") or payload.get("detail") or response.text
            except (ValueError, AttributeError):
                detail = response.text
            raise ApiError(operation.method, path, response.status_code, str(detail))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def login(self, email: str, password: str) -> dict[str, Any]:
        payload = self.request(
            LOGIN_OPERATION_ID,
            json_body={"email": email, "password": password},
        )
        self.set_access_token(payload["access_token"])
        self.set_reauthentication_credentials(email, password)
        return payload

    def ensure_storage(self, tenant_id: str, tenant_slug: str) -> dict[str, Any]:
        return self.request(
            "ensure_storage_v1_tenants__tenant_id__artifact_storage_ensure_post",
            path_params={"tenant_id": tenant_id},
            json_body={"tenant_slug": tenant_slug},
        )

    def list_collections(self, tenant_id: str, client_id: str) -> list[dict[str, Any]]:
        return self.request(
            "list_collections_v1_tenants__tenant_id__clients__client_id__collections_get",
            path_params={"tenant_id": tenant_id, "client_id": client_id},
        )

    def create_collection(
        self,
        tenant_id: str,
        client_id: str,
        name: str,
        description: str | None,
    ) -> dict[str, Any]:
        return self.request(
            "create_collection_v1_tenants__tenant_id__clients__client_id__collections_post",
            path_params={"tenant_id": tenant_id, "client_id": client_id},
            json_body={"name": name, "description": description},
        )

    def list_custodians(self, client_id: str) -> list[dict[str, Any]]:
        return self.request(
            "list_custodians_v1_clients__client_id__custodians_get",
            path_params={"client_id": client_id},
        )

    def create_custodian(
        self,
        client_id: str,
        display_name: str,
        email_addresses: tuple[str, ...],
        external_reference: str | None,
    ) -> dict[str, Any]:
        return self.request(
            "create_custodian_v1_clients__client_id__custodians_post",
            path_params={"client_id": client_id},
            json_body={
                "display_name": display_name,
                "email_addresses": list(email_addresses),
                "external_reference": external_reference,
            },
        )

    def upload_source_container(
        self,
        collection_id: str,
        filename: str,
        content: bytes | BinaryIO,
        media_type: str,
        original_source_path: str | None,
    ) -> dict[str, Any]:
        return self.request(
            "upload_source_container_v1_collections__collection_id__source_containers_upload_post",
            path_params={"collection_id": collection_id},
            data={"original_source_path": original_source_path or ""},
            files={"file": (filename, content, media_type)},
        )

    def upload_item(
        self,
        collection_id: str,
        filename: str,
        content: bytes,
        media_type: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return self.request(
            "upload_collection_item_v1_collections__collection_id__items_upload_post",
            path_params={"collection_id": collection_id},
            data={"metadata": json.dumps(_valid_json_unicode(metadata))},
            files={"file": (filename, content, media_type)},
        )
