from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.config import Settings, get_settings
from artifact_service.auth import ArtifactPrincipal, get_embedded_artifact_principal, mint_artifact_delegation
from artifact_service.config import ArtifactSettings, get_artifact_settings

router = APIRouter(tags=["artifacts"])

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


async def _proxy_artifact_request(
    request: Request,
    principal: ArtifactPrincipal,
    core_settings: Settings,
    artifact_settings: ArtifactSettings,
) -> StreamingResponse:
    client = httpx.AsyncClient(base_url=core_settings.artifact_base_url, timeout=None)
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS | {"host", "authorization", "content-length"}
    }
    headers["authorization"] = f"Bearer {mint_artifact_delegation(principal, artifact_settings)}"
    upstream_request = client.build_request(
        request.method,
        request.url.path,
        params=request.query_params,
        headers=headers,
        content=request.stream(),
    )
    upstream = await client.send(upstream_request, stream=True)
    response_headers = {
        key: value for key, value in upstream.headers.items() if key.lower() not in HOP_BY_HOP_HEADERS
    }

    async def close_upstream() -> None:
        await upstream.aclose()
        await client.aclose()

    async def body() -> AsyncIterator[bytes]:
        async for chunk in upstream.aiter_raw():
            yield chunk

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=response_headers,
        background=BackgroundTask(close_upstream),
    )


def _proxy_route(path: str, methods: list[str]) -> None:
    async def endpoint(
        request: Request,
        principal: ArtifactPrincipal = Depends(get_embedded_artifact_principal),
        core_settings: Settings = Depends(get_settings),
        artifact_settings: ArtifactSettings = Depends(get_artifact_settings),
    ) -> StreamingResponse:
        return await _proxy_artifact_request(request, principal, core_settings, artifact_settings)

    router.add_api_route(path, endpoint, methods=methods, include_in_schema=False)


_proxy_route("/v1/tenants/{tenant_id}/artifact-storage/ensure", ["POST"])
_proxy_route("/v1/tenants/{tenant_id}/clients/{client_id}/collections", ["GET", "POST"])
_proxy_route("/v1/collections/{collection_id}", ["GET"])
_proxy_route("/v1/collections/{collection_id}/text-processing/profile", ["GET", "PUT"])
_proxy_route("/v1/collections/{collection_id}/text-processing:test", ["POST"])
_proxy_route("/v1/collections/{collection_id}/text-processing/runs", ["GET", "POST"])
_proxy_route("/v1/collections/{collection_id}/selections", ["POST"])
_proxy_route("/v1/collection-selections/{selection_id}/items", ["GET"])
_proxy_route("/v1/collection-selections/{selection_id}", ["DELETE"])
_proxy_route("/v1/collections/{collection_id}/custodians", ["GET"])
_proxy_route("/v1/collections/{collection_id}/source-containers:upload", ["POST"])
_proxy_route("/v1/collections/{collection_id}/items:upload", ["POST"])
_proxy_route("/v1/collections/{collection_id}/search", ["GET"])
_proxy_route("/v1/collections/{collection_id}/search/facets/{facet}", ["GET"])
_proxy_route("/v1/collections/{collection_id}/date-histogram", ["GET"])
_proxy_route("/v1/collections/{collection_id}/items", ["GET"])
_proxy_route("/v1/collection-items/{collection_item_id}", ["GET"])
_proxy_route("/v1/collection-items/{collection_item_id}/artifacts", ["GET"])
_proxy_route("/v1/collection-items/{collection_item_id}/derived-artifacts:upload", ["POST"])
_proxy_route("/v1/artifacts/{artifact_id}", ["GET"])
_proxy_route("/v1/artifacts/{artifact_id}/lineage", ["GET"])
_proxy_route("/v1/artifacts/{artifact_id}/content", ["GET"])
