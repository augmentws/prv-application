import threading
import uuid
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from classification_service.artifacts import (
    ClassificationArtifactError,
    HttpClassificationArtifactStore,
    RuntimeArtifactContext,
)
from classification_service.auth import require_service_token
from classification_service.backend import ClassificationBackend, ClassificationRuntimeError
from classification_service.config import ClassificationSettings, get_classification_settings
from classification_service.packaging import ModelPackageError
from classification_service.registry import capabilities
from classification_service.schemas import (
    ClassificationCapabilities,
    ClassificationScoringRequest,
    ClassificationScoringResult,
    ClassificationTrainingRequest,
    ClassificationTrainingResult,
    PackageValidationRequest,
    PackageValidationResult,
)

router = APIRouter()
_semaphores: dict[int, threading.BoundedSemaphore] = {}
_semaphore_lock = threading.Lock()


def _semaphore(limit: int) -> threading.BoundedSemaphore:
    with _semaphore_lock:
        return _semaphores.setdefault(limit, threading.BoundedSemaphore(limit))


@contextmanager
def _execution_slot(settings: ClassificationSettings):
    semaphore = _semaphore(settings.max_concurrency)
    if not semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Classification runtime concurrency limit reached",
        )
    try:
        yield
    finally:
        semaphore.release()


def _store(
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    artifact_delegation_token: str,
    settings: ClassificationSettings,
) -> HttpClassificationArtifactStore:
    return HttpClassificationArtifactStore(
        settings=settings,
        context=RuntimeArtifactContext(
            client_id=client_id,
            artifact_delegation_token=artifact_delegation_token,
        ),
        tenant_id=tenant_id,
    )


def _runtime_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, (ClassificationRuntimeError, ModelPackageError)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, ClassificationArtifactError):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Classification runtime failed",
    )


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/v1/capabilities",
    response_model=ClassificationCapabilities,
    dependencies=[Depends(require_service_token)],
)
def get_capabilities() -> ClassificationCapabilities:
    return capabilities()


@router.post(
    "/v1/packages/validate",
    response_model=PackageValidationResult,
    dependencies=[Depends(require_service_token)],
)
def validate_package(
    payload: PackageValidationRequest,
    x_artifact_tenant_id: Annotated[uuid.UUID, Header()],
    x_artifact_client_id: Annotated[uuid.UUID, Header()],
    x_artifact_delegation_token: Annotated[str, Header(min_length=1)],
    settings: ClassificationSettings = Depends(get_classification_settings),
) -> PackageValidationResult:
    try:
        with _execution_slot(settings):
            return ClassificationBackend(settings).validate_package(
                _store(
                    tenant_id=x_artifact_tenant_id,
                    client_id=x_artifact_client_id,
                    artifact_delegation_token=x_artifact_delegation_token,
                    settings=settings,
                ),
                payload.package_artifact,
            )
    except Exception as exc:
        raise _runtime_error(exc) from exc


@router.post(
    "/v1/train",
    response_model=ClassificationTrainingResult,
    dependencies=[Depends(require_service_token)],
)
def train(
    payload: ClassificationTrainingRequest,
    x_artifact_tenant_id: Annotated[uuid.UUID, Header()],
    x_artifact_client_id: Annotated[uuid.UUID, Header()],
    x_artifact_delegation_token: Annotated[str, Header(min_length=1)],
    settings: ClassificationSettings = Depends(get_classification_settings),
) -> ClassificationTrainingResult:
    if x_artifact_tenant_id != payload.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Artifact tenant scope mismatch")
    try:
        with _execution_slot(settings):
            return ClassificationBackend(settings).train(
                payload,
                _store(
                    tenant_id=payload.tenant_id,
                    client_id=x_artifact_client_id,
                    artifact_delegation_token=x_artifact_delegation_token,
                    settings=settings,
                ),
            )
    except Exception as exc:
        raise _runtime_error(exc) from exc


@router.post(
    "/v1/score",
    response_model=ClassificationScoringResult,
    dependencies=[Depends(require_service_token)],
)
def score(
    payload: ClassificationScoringRequest,
    x_artifact_tenant_id: Annotated[uuid.UUID, Header()],
    x_artifact_client_id: Annotated[uuid.UUID, Header()],
    x_artifact_delegation_token: Annotated[str, Header(min_length=1)],
    settings: ClassificationSettings = Depends(get_classification_settings),
) -> ClassificationScoringResult:
    if x_artifact_tenant_id != payload.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Artifact tenant scope mismatch")
    try:
        with _execution_slot(settings):
            return ClassificationBackend(settings).score(
                payload,
                _store(
                    tenant_id=payload.tenant_id,
                    client_id=x_artifact_client_id,
                    artifact_delegation_token=x_artifact_delegation_token,
                    settings=settings,
                ),
            )
    except Exception as exc:
        raise _runtime_error(exc) from exc
