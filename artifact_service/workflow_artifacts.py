import codecs
import hashlib
import json
import tempfile
import uuid
import zipfile
from collections.abc import Iterable
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from artifact_service.models import (
    Artifact,
    WorkflowArtifact,
    WorkflowArtifactLineage,
)
from artifact_service.schemas import WorkflowArtifactLineageInput, WorkflowArtifactUploadMetadata
from artifact_service.service import get_or_create_blob, require_tenant_storage
from artifact_service.storage import BlobStorage
from artifact_service.workflow_artifact_types import ArtifactType

COLUMNAR_ARTIFACT_TYPES = {
    ArtifactType.ANALYSIS_INPUT_MANIFEST,
    ArtifactType.CLASSIFICATION_POPULATION,
    ArtifactType.CLASSIFICATION_FEATURES,
    ArtifactType.CLASSIFICATION_LABELS,
    ArtifactType.CLASSIFICATION_SPLITS,
    ArtifactType.CLASSIFICATION_PREDICTIONS,
    ArtifactType.JOB_ITEM_OUTCOME_FRAGMENT,
    ArtifactType.JOB_ITEM_OUTCOMES,
}
JSON_ARTIFACT_TYPES = {
    ArtifactType.CLASSIFICATION_EVALUATION,
    ArtifactType.CLASSIFICATION_RESULT_MANIFEST,
}
COLUMNAR_MEDIA_TYPES = {
    "application/vnd.apache.parquet",
    "application/vnd.apache.arrow.file",
    "application/vnd.apache.arrow.stream",
}
PACKAGE_MEDIA_TYPES = {
    "application/zip",
    "application/vnd.priv-view.classification-model+zip",
}
JSON_MEDIA_TYPES = {"application/json"}
CSV_MEDIA_TYPES = {"text/csv"}

WORKFLOW_ARTIFACT_MAX_BYTES = {
    ArtifactType.ANALYSIS_INPUT_MANIFEST: 20 * 1024**3,
    ArtifactType.CLASSIFICATION_POPULATION: 10 * 1024**3,
    ArtifactType.CLASSIFICATION_FEATURES: 50 * 1024**3,
    ArtifactType.CLASSIFICATION_LABELS: 10 * 1024**3,
    ArtifactType.CLASSIFICATION_SPLITS: 10 * 1024**3,
    ArtifactType.CLASSIFICATION_MODEL_PACKAGE: 2 * 1024**3,
    ArtifactType.CLASSIFICATION_PREDICTIONS: 20 * 1024**3,
    ArtifactType.CLASSIFICATION_EVALUATION: 50 * 1024**2,
    ArtifactType.CLASSIFICATION_RESULT_MANIFEST: 50 * 1024**2,
    ArtifactType.SEARCH_RESULT_EXPORT: 20 * 1024**3,
    ArtifactType.JOB_ITEM_OUTCOME_FRAGMENT: 5 * 1024**3,
    ArtifactType.JOB_ITEM_OUTCOMES: 20 * 1024**3,
    ArtifactType.FACT_EXPORT: 5 * 1024**3,
}


class WorkflowArtifactError(ValueError):
    pass


class WorkflowArtifactConflictError(WorkflowArtifactError):
    pass


def allowed_media_types(artifact_type: ArtifactType) -> set[str]:
    if artifact_type in COLUMNAR_ARTIFACT_TYPES:
        return COLUMNAR_MEDIA_TYPES
    if artifact_type == ArtifactType.CLASSIFICATION_MODEL_PACKAGE:
        return PACKAGE_MEDIA_TYPES
    if artifact_type in {ArtifactType.SEARCH_RESULT_EXPORT, ArtifactType.FACT_EXPORT}:
        return CSV_MEDIA_TYPES
    return JSON_MEDIA_TYPES


def workflow_artifact_max_bytes(artifact_type: ArtifactType) -> int:
    return WORKFLOW_ARTIFACT_MAX_BYTES[artifact_type]


def stage_workflow_upload(upload: BinaryIO, *, maximum_bytes: int) -> tuple[BinaryIO, str, int]:
    staged = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")  # noqa: SIM115
    digest = hashlib.sha256()
    byte_length = 0
    while chunk := upload.read(1024 * 1024):
        byte_length += len(chunk)
        if byte_length > maximum_bytes:
            staged.close()
            raise WorkflowArtifactError(f"Workflow artifact exceeds the {maximum_bytes}-byte size limit")
        digest.update(chunk)
        staged.write(chunk)
    staged.seek(0)
    return staged, digest.hexdigest(), byte_length


def _validate_content_format(
    staged: BinaryIO,
    *,
    artifact_type: ArtifactType,
    media_type: str,
    byte_length: int,
) -> None:
    try:
        if media_type == "application/vnd.apache.parquet":
            if byte_length < 8:
                raise WorkflowArtifactError("Parquet workflow artifacts are too small")
            if staged.read(4) != b"PAR1":
                raise WorkflowArtifactError("Parquet workflow artifacts must start with PAR1")
            staged.seek(-4, 2)
            if staged.read(4) != b"PAR1":
                raise WorkflowArtifactError("Parquet workflow artifacts must end with PAR1")
        elif media_type == "application/vnd.apache.arrow.file":
            if byte_length < 12:
                raise WorkflowArtifactError("Arrow file workflow artifacts are too small")
            if staged.read(6) != b"ARROW1":
                raise WorkflowArtifactError("Arrow file workflow artifacts must start with ARROW1")
            staged.seek(-6, 2)
            if staged.read(6) != b"ARROW1":
                raise WorkflowArtifactError("Arrow file workflow artifacts must end with ARROW1")
        elif artifact_type == ArtifactType.CLASSIFICATION_MODEL_PACKAGE:
            if not zipfile.is_zipfile(staged):
                raise WorkflowArtifactError("Classification model packages must be valid ZIP archives")
        elif artifact_type in JSON_ARTIFACT_TYPES:
            try:
                value = json.load(staged)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkflowArtifactError("JSON workflow artifacts must contain valid UTF-8 JSON") from exc
            if not isinstance(value, dict):
                raise WorkflowArtifactError("JSON workflow artifacts must contain a top-level object")
        elif artifact_type in {ArtifactType.SEARCH_RESULT_EXPORT, ArtifactType.FACT_EXPORT}:
            if byte_length < 2:
                raise WorkflowArtifactError("CSV exports must contain a header row")
            decoder = codecs.getincrementaldecoder("utf-8")()
            try:
                while chunk := staged.read(1024 * 1024):
                    decoder.decode(chunk)
                decoder.decode(b"", final=True)
            except UnicodeDecodeError as exc:
                raise WorkflowArtifactError("CSV exports must use UTF-8") from exc
    finally:
        staged.seek(0)


def validate_workflow_artifact_upload(
    staged: BinaryIO,
    *,
    payload: WorkflowArtifactUploadMetadata,
    actual_content_hash: str,
    actual_byte_length: int,
) -> None:
    if payload.media_type not in allowed_media_types(payload.artifact_type):
        allowed = ", ".join(sorted(allowed_media_types(payload.artifact_type)))
        raise WorkflowArtifactError(f"{payload.artifact_type.value} requires one of these media types: {allowed}")
    if actual_byte_length <= 0:
        raise WorkflowArtifactError("Workflow artifacts must not be empty")
    if actual_byte_length > workflow_artifact_max_bytes(payload.artifact_type):
        raise WorkflowArtifactError("Workflow artifact exceeds the size limit for its type")
    if payload.content_hash != actual_content_hash:
        raise WorkflowArtifactError("Declared content_hash does not match the uploaded bytes")
    if payload.byte_length != actual_byte_length:
        raise WorkflowArtifactError("Declared byte_length does not match the uploaded bytes")
    _validate_content_format(
        staged,
        artifact_type=payload.artifact_type,
        media_type=payload.media_type,
        byte_length=actual_byte_length,
    )


def find_workflow_artifact(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    artifact_type: ArtifactType | str,
    derivation_key: str,
) -> WorkflowArtifact | None:
    return db.scalar(
        select(WorkflowArtifact).where(
            WorkflowArtifact.tenant_id == tenant_id,
            WorkflowArtifact.client_id == client_id,
            WorkflowArtifact.artifact_type == str(artifact_type),
            WorkflowArtifact.derivation_key == derivation_key,
        )
    )


def _lineage_signature(values: Iterable[WorkflowArtifactLineageInput]) -> set[tuple[str, uuid.UUID, str]]:
    return {
        (value.source_kind, value.source_artifact_id, value.relationship)
        for value in values
    }


def _stored_lineage_signature(
    db: Session,
    workflow_artifact_id: uuid.UUID,
) -> set[tuple[str, uuid.UUID, str]]:
    rows = db.scalars(
        select(WorkflowArtifactLineage).where(
            WorkflowArtifactLineage.workflow_artifact_id == workflow_artifact_id
        )
    )
    return {
        (
            "WORKFLOW_ARTIFACT" if row.source_workflow_artifact_id is not None else "ARTIFACT",
            row.source_workflow_artifact_id or row.source_artifact_id,
            row.relationship,
        )
        for row in rows
    }


def _assert_existing_matches(
    db: Session,
    existing: WorkflowArtifact,
    payload: WorkflowArtifactUploadMetadata,
) -> None:
    comparisons = {
        "core scope kind": (existing.core_scope_kind, payload.core_scope_kind),
        "core scope ID": (existing.core_scope_id, payload.core_scope_id),
        "schema version": (existing.schema_version, payload.schema_version),
        "content hash": (existing.content_hash, payload.content_hash),
        "byte length": (existing.byte_length, payload.byte_length),
        "media type": (existing.media_type, payload.media_type),
        "producer kind": (existing.producer_kind, payload.producer_kind),
        "producer run ID": (existing.producer_run_id, payload.producer_run_id),
        "metadata": (existing.workflow_metadata or {}, payload.metadata),
    }
    for field_name, (stored, requested) in comparisons.items():
        if stored != requested:
            raise WorkflowArtifactConflictError(
                f"Derivation key already exists with different {field_name}"
            )
    if _stored_lineage_signature(db, existing.id) != _lineage_signature(payload.lineage):
        raise WorkflowArtifactConflictError("Derivation key already exists with different lineage")


def find_matching_workflow_artifact(
    db: Session,
    *,
    payload: WorkflowArtifactUploadMetadata,
) -> WorkflowArtifact | None:
    """Resolve an idempotent retry, rejecting reuse with different immutable inputs."""
    existing = find_workflow_artifact(
        db,
        tenant_id=payload.tenant_id,
        client_id=payload.client_id,
        artifact_type=payload.artifact_type,
        derivation_key=payload.derivation_key,
    )
    if existing is not None:
        _assert_existing_matches(db, existing, payload)
    return existing


def _validate_lineage_sources(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    lineage: list[WorkflowArtifactLineageInput],
) -> None:
    for source in lineage:
        if source.source_kind == "WORKFLOW_ARTIFACT":
            record = db.get(WorkflowArtifact, source.source_artifact_id)
        else:
            record = db.get(Artifact, source.source_artifact_id)
        if record is None:
            raise WorkflowArtifactError(f"Lineage source {source.source_artifact_id} does not exist")
        if record.tenant_id != tenant_id or record.client_id != client_id:
            raise WorkflowArtifactError("Workflow artifact lineage cannot cross tenant or client scope")
        if isinstance(record, Artifact) and record.status != "FINALIZED":
            raise WorkflowArtifactError("Workflow artifact lineage requires finalized source artifacts")


def store_workflow_artifact(
    db: Session,
    storage: BlobStorage,
    *,
    staged: BinaryIO,
    payload: WorkflowArtifactUploadMetadata,
    actual_content_hash: str,
    actual_byte_length: int,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowArtifact, bool]:
    validate_workflow_artifact_upload(
        staged,
        payload=payload,
        actual_content_hash=actual_content_hash,
        actual_byte_length=actual_byte_length,
    )
    existing = find_matching_workflow_artifact(db, payload=payload)
    if existing is not None:
        return existing, False

    _validate_lineage_sources(
        db,
        tenant_id=payload.tenant_id,
        client_id=payload.client_id,
        lineage=payload.lineage,
    )
    blob = get_or_create_blob(
        db,
        storage,
        require_tenant_storage(db, payload.tenant_id),
        payload.tenant_id,
        staged,
        actual_content_hash,
        actual_byte_length,
        payload.media_type,
    )
    artifact = WorkflowArtifact(
        tenant_id=payload.tenant_id,
        client_id=payload.client_id,
        core_scope_kind=payload.core_scope_kind,
        core_scope_id=payload.core_scope_id,
        artifact_type=payload.artifact_type.value,
        schema_version=payload.schema_version,
        derivation_key=payload.derivation_key,
        content_blob_id=blob.id,
        content_hash=actual_content_hash,
        media_type=payload.media_type,
        byte_length=actual_byte_length,
        producer_kind=payload.producer_kind,
        producer_run_id=payload.producer_run_id,
        workflow_metadata=payload.metadata,
        created_by_user_id=actor_user_id,
    )
    db.add(artifact)
    db.flush()
    for source in payload.lineage:
        db.add(
            WorkflowArtifactLineage(
                workflow_artifact_id=artifact.id,
                source_workflow_artifact_id=(
                    source.source_artifact_id if source.source_kind == "WORKFLOW_ARTIFACT" else None
                ),
                source_artifact_id=(source.source_artifact_id if source.source_kind == "ARTIFACT" else None),
                relationship=source.relationship,
            )
        )
    db.flush()
    return artifact, True
