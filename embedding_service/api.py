from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status

from embedding_service.backend import EmbeddingBackend, get_embedding_backend
from embedding_service.config import EmbeddingSettings, get_embedding_settings
from embedding_service.schemas import EmbeddingModelRead, EmbeddingRequest, EmbeddingResponse

AuthorizationDependency = Callable[..., None]


def build_router(authorize: AuthorizationDependency) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["embeddings"], dependencies=[Depends(authorize)])

    @router.get("/models/current", response_model=EmbeddingModelRead)
    def current_model(
        settings: EmbeddingSettings = Depends(get_embedding_settings),
        backend: EmbeddingBackend = Depends(get_embedding_backend),
    ) -> EmbeddingModelRead:
        return EmbeddingModelRead(
            provider=settings.provider,
            model=settings.model,
            model_revision=settings.model_revision,
            dimensions=settings.dimensions,
            normalized=settings.normalize,
            device=settings.device,
            loaded=backend.loaded,
        )

    @router.post("/embeddings", response_model=EmbeddingResponse)
    def create_embeddings(
        payload: EmbeddingRequest,
        settings: EmbeddingSettings = Depends(get_embedding_settings),
        backend: EmbeddingBackend = Depends(get_embedding_backend),
    ) -> EmbeddingResponse:
        if len(payload.inputs) > settings.max_inputs:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"At most {settings.max_inputs} inputs are allowed per request",
            )
        if any(len(value) > settings.max_input_characters for value in payload.inputs):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Each input must be at most {settings.max_input_characters} characters",
            )
        return EmbeddingResponse(
            model=settings.model,
            model_revision=settings.model_revision,
            dimensions=settings.dimensions,
            normalized=settings.normalize,
            embeddings=backend.embed(payload.inputs, payload.input_type),
        )

    return router
