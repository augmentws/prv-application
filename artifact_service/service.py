import hashlib
import secrets
import string
import tempfile
import uuid
from pathlib import Path
from typing import BinaryIO

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from artifact_service.config import ArtifactSettings
from artifact_service.models import Artifact, ContentBlob, TenantStorage
from artifact_service.storage import BlobStorage


def build_bucket_name(tenant_slug: str, settings: ArtifactSettings) -> str:
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(settings.bucket_suffix_length))
    prefix = settings.bucket_prefix.strip("-").lower()
    available_slug_length = 63 - len(prefix) - len(suffix) - 2
    safe_slug = tenant_slug.strip("-").lower()[:available_slug_length].rstrip("-")
    return f"{prefix}-{safe_slug}-{suffix}"


def ensure_tenant_storage(
    db: Session,
    storage: BlobStorage,
    settings: ArtifactSettings,
    tenant_id: uuid.UUID,
    tenant_slug: str,
) -> TenantStorage:
    existing = db.get(TenantStorage, tenant_id)
    if existing is not None:
        storage.ensure_bucket(existing.bucket_name)
        return existing

    record = TenantStorage(
        tenant_id=tenant_id,
        tenant_slug_snapshot=tenant_slug,
        bucket_name=build_bucket_name(tenant_slug, settings),
        status="ACTIVE",
    )
    storage.ensure_bucket(record.bucket_name)
    db.add(record)
    db.flush()
    return record


def stage_upload(upload: UploadFile) -> tuple[BinaryIO, str, int]:
    staged = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")  # noqa: SIM115
    digest = hashlib.sha256()
    byte_length = 0
    while chunk := upload.file.read(1024 * 1024):
        digest.update(chunk)
        staged.write(chunk)
        byte_length += len(chunk)
    staged.seek(0)
    return staged, digest.hexdigest(), byte_length


def get_or_create_blob(
    db: Session,
    storage: BlobStorage,
    tenant_storage: TenantStorage,
    tenant_id: uuid.UUID,
    staged: BinaryIO,
    sha256: str,
    byte_length: int,
    media_type: str,
) -> ContentBlob:
    existing = db.scalar(
        select(ContentBlob).where(ContentBlob.tenant_id == tenant_id, ContentBlob.sha256 == sha256)
    )
    if existing is not None:
        return existing

    blob_id = uuid.uuid4()
    storage_key = f"v1/blobs/{blob_id}"
    storage.put_fileobj(tenant_storage.bucket_name, storage_key, staged, media_type)
    blob = ContentBlob(
        id=blob_id,
        tenant_id=tenant_id,
        sha256=sha256,
        bucket_name=tenant_storage.bucket_name,
        storage_key=storage_key,
        byte_length=byte_length,
        media_type=media_type,
    )
    db.add(blob)
    db.flush()
    return blob


def create_artifact(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    blob: ContentBlob,
    artifact_type: str,
    original_filename: str,
    actor_user_id: uuid.UUID,
    source_reference: str | None = None,
    artifact_metadata: dict | None = None,
) -> Artifact:
    artifact = Artifact(
        tenant_id=tenant_id,
        client_id=client_id,
        artifact_class="EVIDENCE" if artifact_type in {"SOURCE_CONTAINER", "NATIVE_FILE"} else "DERIVED",
        artifact_type=artifact_type,
        content_blob_id=blob.id,
        content_hash=blob.sha256,
        media_type=blob.media_type,
        byte_length=blob.byte_length,
        original_filename=Path(original_filename).name,
        source_reference=source_reference,
        artifact_metadata=artifact_metadata or {},
        created_by_user_id=actor_user_id,
        status="FINALIZED",
    )
    db.add(artifact)
    db.flush()
    return artifact


def require_tenant_storage(db: Session, tenant_id: uuid.UUID) -> TenantStorage:
    tenant_storage = db.get(TenantStorage, tenant_id)
    if tenant_storage is None or tenant_storage.status != "ACTIVE":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant artifact storage has not been provisioned",
        )
    return tenant_storage
