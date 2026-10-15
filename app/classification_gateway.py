import io
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from pydantic import ValidationError

from app.artifact_gateway import (
    WorkflowArtifactReference,
    copy_workflow_artifact_content,
    store_classification_workflow_artifact,
)
from app.config import Settings, get_settings
from artifact_service.auth import ArtifactPrincipal, mint_artifact_delegation
from artifact_service.config import get_artifact_settings
from artifact_service.schemas import WorkflowArtifactLineageInput
from classification_service.artifacts import ClassificationArtifactStore, content_hash
from classification_service.backend import ClassificationBackend
from classification_service.config import ClassificationSettings
from classification_service.registry import capabilities
from classification_service.schemas import (
    ArtifactReference,
    ArtifactType,
    ClassificationCapabilities,
    ClassificationScoringRequest,
    ClassificationScoringResult,
    ClassificationTrainingRequest,
    ClassificationTrainingResult,
    PackageValidationRequest,
    PackageValidationResult,
)


class ClassificationGatewayError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClassificationArtifactAccess:
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    actor_user_id: uuid.UUID

    def delegation_token(self, *, lifetime_seconds: float = 300) -> str:
        return mint_artifact_delegation(
            ArtifactPrincipal(
                actor_user_id=self.actor_user_id,
                allowed_tenant_ids=frozenset({self.tenant_id}),
                allowed_clients=frozenset({(self.tenant_id, self.client_id)}),
                allowed_custodians=frozenset(),
            ),
            get_artifact_settings(),
            lifetime=timedelta(seconds=max(lifetime_seconds, 300)),
        )


def _artifact_reference(reference: WorkflowArtifactReference) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=reference.artifact_id,
        artifact_type=reference.artifact_type,
        schema_version=reference.schema_version,
        content_hash=reference.content_hash,
        media_type=reference.media_type,
    )


class EmbeddedClassificationArtifactStore(ClassificationArtifactStore):
    def __init__(self, access: ClassificationArtifactAccess) -> None:
        self.access = access

    def read(self, reference: ArtifactReference) -> bytes:
        destination = io.BytesIO()
        copy_workflow_artifact_content(
            artifact_id=reference.artifact_id,
            destination=destination,
            actor_user_id=self.access.actor_user_id,
            tenant_id=self.access.tenant_id,
            client_id=self.access.client_id,
        )
        value = destination.getvalue()
        if content_hash(value) != reference.content_hash:
            raise ClassificationGatewayError("Workflow artifact content hash mismatch")
        return value

    def write(
        self,
        *,
        artifact_type: ArtifactType,
        core_scope_kind: str,
        core_scope_id: uuid.UUID,
        schema_version: int,
        derivation_key: str,
        content: bytes,
        media_type: str,
        producer_run_id: uuid.UUID,
        metadata: dict,
        lineage: list[ArtifactReference],
        filename: str,
    ) -> ArtifactReference:
        reference, _ = store_classification_workflow_artifact(
            content=io.BytesIO(content),
            artifact_type=artifact_type,
            core_scope_kind=core_scope_kind,
            core_scope_id=core_scope_id,
            schema_version=schema_version,
            derivation_key=derivation_key,
            content_hash=content_hash(content),
            byte_length=len(content),
            media_type=media_type,
            producer_kind="CLASSIFICATION_RUNTIME",
            producer_run_id=producer_run_id,
            artifact_metadata=metadata,
            lineage=[
                WorkflowArtifactLineageInput(
                    source_kind="WORKFLOW_ARTIFACT",
                    source_artifact_id=item.artifact_id,
                )
                for item in lineage
            ],
            actor_user_id=self.access.actor_user_id,
            tenant_id=self.access.tenant_id,
            client_id=self.access.client_id,
            filename=filename,
        )
        return _artifact_reference(reference)


class ClassificationGateway:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        artifact_store: ClassificationArtifactStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.transport = transport
        self.artifact_store = artifact_store

    def _runtime_settings(self) -> ClassificationSettings:
        return ClassificationSettings(
            CLASSIFICATION_SERVICE_TOKEN=self.settings.classification_service_token,
            ARTIFACT_BASE_URL=self.settings.artifact_base_url,
            CLASSIFICATION_REQUEST_TIMEOUT_SECONDS=self.settings.classification_request_timeout_seconds,
            CLASSIFICATION_MAX_CONCURRENCY=self.settings.classification_max_concurrency,
            CLASSIFICATION_RUNTIME_IMAGE_DIGEST=self.settings.classification_runtime_image_digest,
            CLASSIFICATION_SOURCE_REVISION=self.settings.classification_source_revision,
            CLASSIFICATION_PACKAGE_MAX_BYTES=self.settings.classification_package_max_bytes,
        )

    def _store(self, access: ClassificationArtifactAccess) -> ClassificationArtifactStore:
        return self.artifact_store or EmbeddedClassificationArtifactStore(access)

    def _headers(self, access: ClassificationArtifactAccess | None = None) -> dict[str, str]:
        headers = {"x-classification-service-token": self.settings.classification_service_token}
        if access is not None:
            headers.update(
                {
                    "x-artifact-tenant-id": str(access.tenant_id),
                    "x-artifact-client-id": str(access.client_id),
                    "x-artifact-delegation-token": access.delegation_token(
                        lifetime_seconds=self.settings.classification_request_timeout_seconds + 60
                    ),
                }
            )
        return headers

    def _remote_request(
        self,
        method: str,
        path: str,
        *,
        response_model: type[Any],
        access: ClassificationArtifactAccess | None = None,
        payload: Any = None,
    ) -> Any:
        try:
            with httpx.Client(
                base_url=self.settings.classification_base_url,
                timeout=self.settings.classification_request_timeout_seconds,
                transport=self.transport,
            ) as client:
                response = client.request(
                    method,
                    path,
                    headers=self._headers(access),
                    json=payload.model_dump(mode="json") if payload is not None else None,
                )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ClassificationGatewayError("Classification runtime request timed out") from exc
        except httpx.HTTPStatusError as exc:
            try:
                error_payload = exc.response.json()
                detail = (
                    error_payload.get("detail") or error_payload.get("error", {}).get("message")
                    if isinstance(error_payload, dict)
                    else None
                )
            except ValueError:
                detail = None
            raise ClassificationGatewayError(detail or "Classification runtime request failed") from exc
        except httpx.HTTPError as exc:
            raise ClassificationGatewayError("Classification runtime is unavailable") from exc
        try:
            return response_model.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise ClassificationGatewayError("Classification runtime returned an invalid response") from exc

    def get_capabilities(self) -> ClassificationCapabilities:
        if self.settings.classification_mode == "embedded":
            return capabilities()
        return self._remote_request("GET", "/v1/capabilities", response_model=ClassificationCapabilities)

    def validate_package(
        self,
        package_artifact: ArtifactReference,
        access: ClassificationArtifactAccess,
    ) -> PackageValidationResult:
        if self.settings.classification_mode == "embedded":
            return ClassificationBackend(self._runtime_settings()).validate_package(
                self._store(access),
                package_artifact,
            )
        return self._remote_request(
            "POST",
            "/v1/packages/validate",
            response_model=PackageValidationResult,
            access=access,
            payload=PackageValidationRequest(package_artifact=package_artifact),
        )

    def train(
        self,
        request: ClassificationTrainingRequest,
        access: ClassificationArtifactAccess,
    ) -> ClassificationTrainingResult:
        if request.tenant_id != access.tenant_id:
            raise ClassificationGatewayError("Training request tenant does not match artifact access")
        if self.settings.classification_mode == "embedded":
            return ClassificationBackend(self._runtime_settings()).train(request, self._store(access))
        return self._remote_request(
            "POST",
            "/v1/train",
            response_model=ClassificationTrainingResult,
            access=access,
            payload=request,
        )

    def score(
        self,
        request: ClassificationScoringRequest,
        access: ClassificationArtifactAccess,
    ) -> ClassificationScoringResult:
        if request.tenant_id != access.tenant_id:
            raise ClassificationGatewayError("Scoring request tenant does not match artifact access")
        if self.settings.classification_mode == "embedded":
            return ClassificationBackend(self._runtime_settings()).score(request, self._store(access))
        return self._remote_request(
            "POST",
            "/v1/score",
            response_model=ClassificationScoringResult,
            access=access,
            payload=request,
        )
