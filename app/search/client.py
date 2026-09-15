import json
from collections.abc import Iterable
from typing import Any

import httpx

from app.config import Settings


class OpenSearchError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenSearchClient:
    def __init__(self, settings: Settings) -> None:
        auth = None
        if settings.opensearch_username:
            auth = (settings.opensearch_username, settings.opensearch_password or "")
        self._client = httpx.Client(
            base_url=settings.opensearch_url.rstrip("/"),
            auth=auth,
            verify=settings.opensearch_verify_tls,
            timeout=settings.search_request_timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise OpenSearchError(f"OpenSearch request failed: {exc}") from exc
        if response.status_code >= 400:
            detail = response.text[:2000]
            raise OpenSearchError(
                f"OpenSearch returned {response.status_code}: {detail}",
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        return response.json()

    def create_index(self, name: str, body: dict[str, Any]) -> None:
        self._request("PUT", f"/{name}", json=body)

    def update_mapping(self, name: str, body: dict[str, Any]) -> None:
        self._request("PUT", f"/{name}/_mapping", json=body)

    def delete_index(self, name: str) -> None:
        try:
            self._request("DELETE", f"/{name}")
        except OpenSearchError as exc:
            if exc.status_code != 404:
                raise

    def resolve_indices(self, pattern: str) -> list[str]:
        try:
            result = self._request("GET", f"/_resolve/index/{pattern}", params={"expand_wildcards": "all"})
        except OpenSearchError as exc:
            if exc.status_code == 404:
                return []
            raise
        return sorted(item["name"] for item in result.get("indices", []) if item.get("name"))

    def index_exists(self, name: str) -> bool:
        try:
            self._request("HEAD", f"/{name}")
        except OpenSearchError as exc:
            if exc.status_code == 404:
                return False
            raise
        return True

    def update_aliases(self, actions: list[dict[str, Any]]) -> None:
        self._request("POST", "/_aliases", json={"actions": actions})

    def alias_indices(self, alias: str) -> list[str]:
        try:
            result = self._request("GET", f"/_alias/{alias}")
        except OpenSearchError as exc:
            if exc.status_code == 404:
                return []
            raise
        return sorted(result)

    def bulk(self, index: str, operations: Iterable[tuple[str, str, dict[str, Any] | None]]) -> None:
        lines: list[str] = []
        for action, document_id, source in operations:
            lines.append(json.dumps({action: {"_id": document_id}}))
            if source is not None:
                lines.append(json.dumps(source, default=str))
        if not lines:
            return
        result = self._request(
            "POST",
            f"/{index}/_bulk",
            content="\n".join(lines) + "\n",
            headers={"content-type": "application/x-ndjson"},
        )
        if result.get("errors"):
            failures = [item for item in result.get("items", []) if next(iter(item.values())).get("error")]
            details = []
            for item in failures[:3]:
                action, outcome = next(iter(item.items()))
                error = outcome.get("error") or {}
                details.append(f"{action} {outcome.get('_id')}: {error.get('type')}: {error.get('reason')}")
            suffix = f": {'; '.join(details)}" if details else ""
            raise OpenSearchError(
                f"OpenSearch bulk request contained {len(failures)} failed item(s){suffix}"[:4000]
            )

    def refresh(self, index: str) -> None:
        self._request("POST", f"/{index}/_refresh")

    def ensure_rrf_search_pipeline(self, pipeline_id: str) -> None:
        self._request(
            "PUT",
            f"/_search/pipeline/{pipeline_id}",
            json={
                "description": "Priv-View keyword and semantic reciprocal-rank fusion",
                "phase_results_processors": [
                    {
                        "score-ranker-processor": {
                            "combination": {"technique": "rrf", "rank_constant": 60}
                        }
                    }
                ],
            },
        )

    def search(
        self,
        index: str,
        body: dict[str, Any],
        *,
        search_pipeline: str | None = None,
    ) -> dict[str, Any]:
        params = {"search_pipeline": search_pipeline} if search_pipeline else None
        return self._request("POST", f"/{index}/_search", json=body, params=params)
