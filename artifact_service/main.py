from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from artifact_service.api import build_router
from artifact_service.auth import get_delegated_artifact_principal

app = FastAPI(
    title="Priv-View Artifact API",
    version="0.1.0",
    description="Client evidence collections and immutable native or derived artifacts.",
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


app.include_router(
    build_router(
        get_delegated_artifact_principal,
        expose_internal_deletion_control=True,
    )
)
