import hashlib
import io
import uuid
from dataclasses import dataclass
from typing import Protocol

import httpx

from artifact_service.schemas import WorkflowArtifactLineageInput, WorkflowArtifactUploadMetadata
from classification_service.config import ClassificationSettings
from classification_service.schemas import ArtifactReference, ArtifactType


class ClassificationArtifactError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeArtifactContext:
    client_id: uuid.UUID
    actor_user_id: uuid.UUID | None = None
    artifact_delegation_token: str | None = None


class ClassificationArtifactStore(Protocol):
    def read(self, reference: ArtifactReference) -> bytes: ...

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
    ) -> ArtifactReference: ...


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class HttpClassificationArtifactStore:
    def __init__(
        self,
        *,
        settings: ClassificationSettings,
        context: RuntimeArtifactContext,
        tenant_id: uuid.UUID,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if context.artifact_delegation_token is None:
            raise ClassificationArtifactError("Artifact delegation token is required in remote mode")
        self.settings = settings
        self.context = context
        self.tenant_id = tenant_id
        self.transport = transport

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.context.artifact_delegation_token}"}

    def read(self, reference: ArtifactReference) -> bytes:
        with httpx.Client(
            base_url=self.settings.artifact_base_url,
            timeout=self.settings.request_timeout_seconds,
            transport=self.transport,
        ) as client:
            response = client.get(
                f"/v1/workflow-artifacts/{reference.artifact_id}/content",
                headers=self._headers,
            )
        if response.status_code in {401, 403}:
            raise ClassificationArtifactError("Artifact access denied")
        if response.status_code == 404:
            raise ClassificationArtifactError("Workflow artifact not found")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ClassificationArtifactError("Artifact content read failed") from exc
        content = response.content
        if content_hash(content) != reference.content_hash:
            raise ClassificationArtifactError("Workflow artifact content hash mismatch")
        return content

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
        digest = content_hash(content)
        payload = WorkflowArtifactUploadMetadata(
            tenant_id=self.tenant_id,
            client_id=self.context.client_id,
            core_scope_kind=core_scope_kind,
            core_scope_id=core_scope_id,
            artifact_type=artifact_type,
            schema_version=schema_version,
            derivation_key=derivation_key,
            content_hash=digest,
            byte_length=len(content),
            media_type=media_type,
            producer_kind="CLASSIFICATION_RUNTIME",
            producer_run_id=producer_run_id,
            metadata=metadata,
            lineage=[
                WorkflowArtifactLineageInput(
                    source_kind="WORKFLOW_ARTIFACT",
                    source_artifact_id=reference.artifact_id,
                )
                for reference in lineage
            ],
        )
        with httpx.Client(
            base_url=self.settings.artifact_base_url,
            timeout=self.settings.request_timeout_seconds,
            transport=self.transport,
        ) as client:
            response = client.post(
                "/v1/workflow-artifacts:upload",
                headers=self._headers,
                data={"metadata": payload.model_dump_json()},
                files={"file": (filename, io.BytesIO(content), media_type)},
            )
        if response.status_code in {401, 403}:
            raise ClassificationArtifactError("Artifact access denied")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ClassificationArtifactError("Workflow artifact upload failed") from exc
        record = response.json()["artifact"]
        return ArtifactReference(
            artifact_id=record["id"],
            artifact_type=record["artifact_type"],
            schema_version=record["schema_version"],
            content_hash=record["content_hash"],
            media_type=record["media_type"],
        )
