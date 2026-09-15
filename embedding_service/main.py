from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from embedding_service.api import build_router
from embedding_service.auth import require_embedding_service_token
from embedding_service.backend import get_embedding_backend
from embedding_service.config import get_embedding_settings

app = FastAPI(
    title="Priv-View Embedding API",
    version="0.1.0",
    description="Private, configurable text embedding inference for Priv-View processing workflows.",
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
def health() -> dict[str, str | bool | int | None]:
    settings = get_embedding_settings()
    return {
        "status": "ok",
        "provider": settings.provider,
        "model": settings.model,
        "model_revision": settings.model_revision,
        "dimensions": settings.dimensions,
        "loaded": get_embedding_backend().loaded,
    }


app.include_router(build_router(require_embedding_service_token))
