from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.routers import (
    agent_conversations,
    agents,
    auth,
    clients,
    collection_deletions,
    document_metadata,
    managed_skills,
    matter_definitions,
    matter_definition_assessments,
    matter_embeddings,
    matter_imports,
    matter_templates,
    matter_topics,
    matters,
    metadata,
    metadata_groups,
    provider_usage,
    review_batches,
    saved_searches,
    search,
    tenants,
)
from artifact_service.api import build_router as build_artifact_router
from artifact_service.auth import get_embedded_artifact_principal

app = FastAPI(
    title="Priv-View Core API",
    version="0.1.0",
    description="Phase-one Core API for authentication, hierarchical tenants, clients, matters, and matter metadata definitions.",
)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": f"http_{exc.status_code}", "message": str(exc.detail)}},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder(
            {"error": {"code": "validation_error", "message": "Request validation failed", "details": exc.errors()}}
        ),
    )


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(agents.router)
app.include_router(agent_conversations.router)
app.include_router(tenants.router)
app.include_router(clients.router)
app.include_router(collection_deletions.router)
app.include_router(matters.router)
app.include_router(matter_definitions.router)
app.include_router(matter_imports.router)
app.include_router(matter_embeddings.router)
app.include_router(matter_topics.router)
app.include_router(document_metadata.router)
app.include_router(metadata.router)
app.include_router(metadata_groups.router)
app.include_router(provider_usage.router)
app.include_router(matter_templates.router)
app.include_router(saved_searches.router)
app.include_router(review_batches.router)
app.include_router(search.router)
app.include_router(managed_skills.router)
app.include_router(matter_definition_assessments.router)
if get_settings().artifact_mode == "embedded":
    app.include_router(build_artifact_router(get_embedded_artifact_principal))
elif get_settings().artifact_mode == "remote":
    from app.routers import artifact_proxy

    app.include_router(artifact_proxy.router)
else:
    raise RuntimeError("ARTIFACT_MODE must be 'embedded' or 'remote'")
