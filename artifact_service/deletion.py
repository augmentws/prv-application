from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from artifact_service.models import (
    Artifact,
    ArtifactLineage,
    ClientCollection,
    CollectionArtifact,
    CollectionDeletionBlob,
    CollectionDeletionJob,
    CollectionItem,
    CollectionItemArtifact,
    CollectionItemCustodian,
    CollectionItemEmail,
    CollectionItemEmailRecipient,
    CollectionSelection,
    CollectionSelectionItem,
    CollectionTextProcessingProfile,
    CollectionTextProcessingRun,
    ContentBlob,
)
from artifact_service.storage import BlobStorage

DATABASE_BATCH_SIZE = 1_000
BLOB_BATCH_SIZE = 250
ACTIVE_DELETION_STATUSES = (
    "QUEUED",
    "VALIDATING",
    "DELETING_DATABASE_ROWS",
    "DELETING_BLOBS",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_deletion_job(
    db: Session,
    collection_id: uuid.UUID,
    requested_by_user_id: uuid.UUID,
) -> CollectionDeletionJob:
    collection = db.scalar(
        select(ClientCollection).where(ClientCollection.id == collection_id).with_for_update()
    )
    if collection is None:
        raise ValueError("Collection not found")
    existing = db.scalar(
        select(CollectionDeletionJob)
        .where(
            CollectionDeletionJob.collection_id == collection.id,
            CollectionDeletionJob.status.in_(ACTIVE_DELETION_STATUSES),
        )
        .order_by(CollectionDeletionJob.created_at.desc())
    )
    if existing is not None:
        return existing
    if collection.status == "DELETING":
        failed = db.scalar(
            select(CollectionDeletionJob)
            .where(CollectionDeletionJob.collection_id == collection.id)
            .order_by(CollectionDeletionJob.created_at.desc())
        )
        if failed is not None:
            return failed
        raise ValueError("Collection is already being deleted")
    active_processing = db.scalar(
        select(func.count())
        .select_from(CollectionTextProcessingRun)
        .where(
            CollectionTextProcessingRun.collection_id == collection.id,
            CollectionTextProcessingRun.status.in_(("QUEUED", "RUNNING")),
        )
    )
    if active_processing:
        raise ValueError("Collection has an active text processing run")

    item_count = int(
        db.scalar(
            select(func.count()).select_from(CollectionItem).where(CollectionItem.collection_id == collection.id)
        )
        or 0
    )
    item_artifact_count = int(
        db.scalar(
            select(func.count())
            .select_from(CollectionItemArtifact)
            .join(CollectionItem, CollectionItem.id == CollectionItemArtifact.collection_item_id)
            .where(CollectionItem.collection_id == collection.id)
        )
        or 0
    )
    collection_artifact_count = int(
        db.scalar(
            select(func.count())
            .select_from(CollectionArtifact)
            .where(CollectionArtifact.collection_id == collection.id)
        )
        or 0
    )
    job_id = uuid.uuid4()
    job = CollectionDeletionJob(
        id=job_id,
        collection_id=collection.id,
        tenant_id=collection.tenant_id,
        client_id=collection.client_id,
        collection_name=collection.name,
        previous_collection_status=collection.status,
        status="QUEUED",
        workflow_id=f"collection-delete:{job_id}:attempt:1",
        attempt_count=1,
        item_count=item_count,
        artifact_count=item_artifact_count + collection_artifact_count,
        requested_by_user_id=requested_by_user_id,
    )
    collection.status = "DELETING"
    db.add(job)
    db.flush()
    return job


def retry_deletion_job(db: Session, job_id: uuid.UUID) -> CollectionDeletionJob:
    job = db.scalar(
        select(CollectionDeletionJob).where(CollectionDeletionJob.id == job_id).with_for_update()
    )
    if job is None:
        raise ValueError("Collection deletion job not found")
    if job.status != "FAILED":
        raise ValueError("Only failed deletions can be retried")
    collection = db.get(ClientCollection, job.collection_id)
    if collection is not None:
        collection.status = "DELETING"
    job.attempt_count += 1
    job.workflow_id = f"collection-delete:{job.id}:attempt:{job.attempt_count}"
    job.status = "QUEUED"
    job.error_message = None
    job.completed_at = None
    db.flush()
    return job


def prepare_deletion(db: Session, job_id: uuid.UUID) -> CollectionDeletionJob:
    job = db.get(CollectionDeletionJob, job_id)
    if job is None:
        raise ValueError("Collection deletion job not found")
    if job.status == "COMPLETED":
        return job
    job.status = "DELETING_DATABASE_ROWS"
    job.started_at = job.started_at or utcnow()
    job.error_message = None
    # Break family parent links up front so arbitrary item batches can be deleted.
    db.execute(
        update(CollectionItem)
        .where(
            CollectionItem.collection_id == job.collection_id,
            CollectionItem.parent_collection_item_id.is_not(None),
        )
        .values(parent_collection_item_id=None)
    )
    db.commit()
    return job


def _delete_artifacts_and_schedule_blobs(
    db: Session,
    job: CollectionDeletionJob,
    artifact_ids: list[uuid.UUID],
) -> int:
    if not artifact_ids:
        return 0
    blob_ids = list(
        db.scalars(select(Artifact.content_blob_id).where(Artifact.id.in_(artifact_ids)).distinct())
    )
    db.execute(
        delete(ArtifactLineage).where(
            or_(
                ArtifactLineage.artifact_id.in_(artifact_ids),
                ArtifactLineage.source_artifact_id.in_(artifact_ids),
            )
        )
    )
    db.execute(delete(CollectionItemArtifact).where(CollectionItemArtifact.artifact_id.in_(artifact_ids)))
    db.execute(delete(CollectionArtifact).where(CollectionArtifact.artifact_id.in_(artifact_ids)))
    db.execute(delete(Artifact).where(Artifact.id.in_(artifact_ids)))
    db.flush()

    scheduled = 0
    for blob_id in blob_ids:
        still_referenced = db.scalar(
            select(Artifact.id).where(Artifact.content_blob_id == blob_id).limit(1)
        )
        if still_referenced is not None:
            continue
        blob = db.get(ContentBlob, blob_id)
        if blob is None:
            continue
        db.add(
            CollectionDeletionBlob(
                deletion_job_id=job.id,
                content_blob_id=blob.id,
                bucket_name=blob.bucket_name,
                storage_key=blob.storage_key,
                byte_length=blob.byte_length,
                status="PENDING",
            )
        )
        db.delete(blob)
        scheduled += 1
    job.blob_count += scheduled
    return len(artifact_ids)


def delete_item_batch(
    db: Session,
    job_id: uuid.UUID,
    *,
    batch_size: int = DATABASE_BATCH_SIZE,
) -> dict[str, int | bool]:
    job = db.get(CollectionDeletionJob, job_id)
    if job is None:
        raise ValueError("Collection deletion job not found")
    item_ids = list(
        db.scalars(
            select(CollectionItem.id)
            .where(CollectionItem.collection_id == job.collection_id)
            .order_by(CollectionItem.id)
            .limit(batch_size)
        )
    )
    if not item_ids:
        return {"deleted_items": 0, "deleted_artifacts": 0, "has_more": False}

    artifact_ids = list(
        db.scalars(
            select(CollectionItemArtifact.artifact_id).where(
                CollectionItemArtifact.collection_item_id.in_(item_ids)
            )
        )
    )
    selection_ids = list(
        db.scalars(
            select(CollectionSelection.id).where(CollectionSelection.collection_id == job.collection_id)
        )
    )
    if selection_ids:
        db.execute(
            delete(CollectionSelectionItem).where(
                CollectionSelectionItem.selection_id.in_(selection_ids),
                CollectionSelectionItem.collection_item_id.in_(item_ids),
            )
        )
    db.execute(
        delete(CollectionItemEmailRecipient).where(
            CollectionItemEmailRecipient.collection_item_id.in_(item_ids)
        )
    )
    db.execute(delete(CollectionItemEmail).where(CollectionItemEmail.collection_item_id.in_(item_ids)))
    db.execute(
        delete(CollectionItemCustodian).where(CollectionItemCustodian.collection_item_id.in_(item_ids))
    )
    artifact_count = _delete_artifacts_and_schedule_blobs(db, job, artifact_ids)
    db.execute(delete(CollectionItem).where(CollectionItem.id.in_(item_ids)))
    job.deleted_item_count += len(item_ids)
    job.deleted_artifact_count += artifact_count
    db.commit()
    has_more = db.scalar(
        select(CollectionItem.id).where(CollectionItem.collection_id == job.collection_id).limit(1)
    ) is not None
    return {
        "deleted_items": len(item_ids),
        "deleted_artifacts": artifact_count,
        "has_more": has_more,
    }


def delete_collection_record(db: Session, job_id: uuid.UUID) -> dict[str, int]:
    job = db.get(CollectionDeletionJob, job_id)
    if job is None:
        raise ValueError("Collection deletion job not found")
    collection = db.get(ClientCollection, job.collection_id)
    if collection is None:
        job.status = "DELETING_BLOBS"
        db.commit()
        return {"deleted_artifacts": 0}
    remaining_item = db.scalar(
        select(CollectionItem.id).where(CollectionItem.collection_id == collection.id).limit(1)
    )
    if remaining_item is not None:
        raise ValueError("Collection items remain")

    artifact_ids = list(
        db.scalars(
            select(CollectionArtifact.artifact_id).where(CollectionArtifact.collection_id == collection.id)
        )
    )
    artifact_count = _delete_artifacts_and_schedule_blobs(db, job, artifact_ids)
    selection_ids = list(
        db.scalars(select(CollectionSelection.id).where(CollectionSelection.collection_id == collection.id))
    )
    if selection_ids:
        db.execute(
            delete(CollectionSelectionItem).where(CollectionSelectionItem.selection_id.in_(selection_ids))
        )
    db.execute(delete(CollectionSelection).where(CollectionSelection.collection_id == collection.id))
    db.execute(
        delete(CollectionTextProcessingProfile).where(
            CollectionTextProcessingProfile.collection_id == collection.id
        )
    )
    db.execute(
        delete(CollectionTextProcessingRun).where(CollectionTextProcessingRun.collection_id == collection.id)
    )
    db.delete(collection)
    job.deleted_artifact_count += artifact_count
    job.status = "DELETING_BLOBS"
    db.commit()
    return {"deleted_artifacts": artifact_count}


def delete_blob_batch(
    db: Session,
    storage: BlobStorage,
    job_id: uuid.UUID,
    *,
    batch_size: int = BLOB_BATCH_SIZE,
) -> dict[str, int | bool]:
    job = db.get(CollectionDeletionJob, job_id)
    if job is None:
        raise ValueError("Collection deletion job not found")
    blobs = list(
        db.scalars(
            select(CollectionDeletionBlob)
            .where(
                CollectionDeletionBlob.deletion_job_id == job.id,
                CollectionDeletionBlob.status == "PENDING",
            )
            .order_by(CollectionDeletionBlob.id)
            .limit(batch_size)
        )
    )
    for blob in blobs:
        storage.delete(blob.bucket_name, blob.storage_key)
        blob.status = "DELETED"
    job.deleted_blob_count += len(blobs)
    db.commit()
    has_more = db.scalar(
        select(CollectionDeletionBlob.id)
        .where(
            CollectionDeletionBlob.deletion_job_id == job.id,
            CollectionDeletionBlob.status == "PENDING",
        )
        .limit(1)
    ) is not None
    return {"deleted_blobs": len(blobs), "has_more": has_more}


def complete_deletion(db: Session, job_id: uuid.UUID) -> CollectionDeletionJob:
    job = db.get(CollectionDeletionJob, job_id)
    if job is None:
        raise ValueError("Collection deletion job not found")
    pending = db.scalar(
        select(CollectionDeletionBlob.id)
        .where(
            CollectionDeletionBlob.deletion_job_id == job.id,
            CollectionDeletionBlob.status == "PENDING",
        )
        .limit(1)
    )
    if pending is not None:
        raise ValueError("Collection blob deletions remain")
    # The permanent job retains aggregate audit counts; per-object cleanup rows are
    # only needed while deletion is resumable.
    db.execute(
        delete(CollectionDeletionBlob).where(CollectionDeletionBlob.deletion_job_id == job.id)
    )
    job.status = "COMPLETED"
    job.completed_at = utcnow()
    job.error_message = None
    db.commit()
    return job


def fail_deletion(db: Session, job_id: uuid.UUID, message: str) -> CollectionDeletionJob | None:
    job = db.get(CollectionDeletionJob, job_id)
    if job is None:
        return None
    job.status = "FAILED"
    job.error_message = message[:4000]
    job.completed_at = utcnow()
    collection = db.get(ClientCollection, job.collection_id)
    if (
        collection is not None
        and job.deleted_item_count == 0
        and job.deleted_artifact_count == 0
        and job.blob_count == 0
    ):
        collection.status = job.previous_collection_status
    db.commit()
    return job
