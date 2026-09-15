import hashlib
import uuid
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from artifact_service.models import (
    Artifact,
    ArtifactLineage,
    CollectionItem,
    CollectionItemArtifact,
)
from artifact_service.service import create_artifact, get_or_create_blob, require_tenant_storage
from artifact_service.storage import BlobStorage


def find_derived_artifact(
    db: Session,
    *,
    collection_item_id: uuid.UUID,
    artifact_role: str,
    derivation_key: str,
) -> Artifact | None:
    return db.scalar(
        select(Artifact)
        .join(CollectionItemArtifact, CollectionItemArtifact.artifact_id == Artifact.id)
        .where(
            CollectionItemArtifact.collection_item_id == collection_item_id,
            CollectionItemArtifact.artifact_role == artifact_role,
            CollectionItemArtifact.derivation_key == derivation_key,
            Artifact.status == "FINALIZED",
        )
    )


def store_derived_artifact(
    db: Session,
    storage: BlobStorage,
    *,
    item: CollectionItem,
    content: bytes,
    media_type: str,
    original_filename: str,
    artifact_type: str,
    source_artifact_id: uuid.UUID,
    relationship: str,
    processing_run_id: uuid.UUID,
    derivation_key: str,
    artifact_metadata: dict,
    actor_user_id: uuid.UUID,
) -> tuple[Artifact, bool]:
    existing = find_derived_artifact(
        db,
        collection_item_id=item.id,
        artifact_role=artifact_type,
        derivation_key=derivation_key,
    )
    if existing is not None:
        return existing, False

    source = db.scalar(
        select(Artifact)
        .join(CollectionItemArtifact, CollectionItemArtifact.artifact_id == Artifact.id)
        .where(
            Artifact.id == source_artifact_id,
            Artifact.status == "FINALIZED",
            CollectionItemArtifact.collection_item_id == item.id,
        )
    )
    if source is None:
        raise ValueError("Derived artifact source is not a finalized artifact for this collection item")

    digest = hashlib.sha256(content).hexdigest()
    staged = BytesIO(content)
    blob = get_or_create_blob(
        db,
        storage,
        require_tenant_storage(db, item.tenant_id),
        item.tenant_id,
        staged,
        digest,
        len(content),
        media_type,
    )
    artifact = create_artifact(
        db,
        tenant_id=item.tenant_id,
        client_id=item.client_id,
        blob=blob,
        artifact_type=artifact_type,
        original_filename=original_filename,
        actor_user_id=actor_user_id,
        source_reference=f"processing-run:{processing_run_id}",
        artifact_metadata=artifact_metadata,
    )
    db.add(
        CollectionItemArtifact(
            artifact_id=artifact.id,
            collection_item_id=item.id,
            artifact_role=artifact_type,
            processing_run_id=processing_run_id,
            derivation_key=derivation_key,
        )
    )
    db.add(
        ArtifactLineage(
            artifact_id=artifact.id,
            source_artifact_id=source.id,
            relationship=relationship,
        )
    )
    db.flush()
    return artifact, True
