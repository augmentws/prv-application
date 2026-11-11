import logging
import uuid

from dbos import DBOS, Queue

from app.collection_deletions import collection_deletion_blockers
from app.config import get_settings
from app.database import SessionLocal
from app.search.client import OpenSearchClient
from artifact_service.database import ArtifactSessionLocal
from artifact_service.deletion import (
    complete_deletion,
    delete_blob_batch,
    delete_collection_record,
    delete_item_batch,
    fail_deletion,
    prepare_deletion,
)
from artifact_service.models import CollectionDeletionJob
from artifact_service.storage import get_storage

logger = logging.getLogger(__name__)

DELETION_QUEUE = Queue("collection-deletions", global_concurrency=2)


@DBOS.step(name="validate_collection_deletion", retries_allowed=True, max_attempts=5)
def validate(job_id: str) -> None:
    job_uuid = uuid.UUID(job_id)
    with ArtifactSessionLocal() as artifact_db:
        job = artifact_db.get(CollectionDeletionJob, job_uuid)
        if job is None:
            raise ValueError("Collection deletion job not found")
        if job.status == "COMPLETED":
            return
        job.status = "VALIDATING"
        artifact_db.commit()
        collection_id = job.collection_id
    with SessionLocal() as db:
        blockers = collection_deletion_blockers(db, collection_id)
    if blockers.blocked:
        raise ValueError(blockers.message())


@DBOS.step(name="delete_collection_search_projections", retries_allowed=True, max_attempts=10)
def delete_search_projections(job_id: str) -> int:
    settings = get_settings()
    if not settings.search_enabled:
        return 0
    with ArtifactSessionLocal() as artifact_db:
        job = artifact_db.get(CollectionDeletionJob, uuid.UUID(job_id))
        if job is None:
            raise ValueError("Collection deletion job not found")
        collection_id = job.collection_id
    client = OpenSearchClient(settings)
    try:
        index_pattern = f"{settings.opensearch_index_prefix}-matter-*-documents-v*"
        indexes = client.resolve_indices(index_pattern)
        return sum(
            client.delete_by_query(
                index_name,
                {"term": {"source_collection_id": str(collection_id)}},
            )
            for index_name in indexes
        )
    finally:
        client.close()


@DBOS.step(name="prepare_collection_deletion", retries_allowed=True, max_attempts=5)
def prepare(job_id: str) -> None:
    with ArtifactSessionLocal() as db:
        prepare_deletion(db, uuid.UUID(job_id))


@DBOS.step(name="delete_collection_item_batch", retries_allowed=True, max_attempts=10)
def delete_items(job_id: str) -> dict[str, int | bool]:
    with ArtifactSessionLocal() as db:
        return delete_item_batch(db, uuid.UUID(job_id))


@DBOS.step(name="delete_collection_database_record", retries_allowed=True, max_attempts=10)
def delete_database_record(job_id: str) -> dict[str, int]:
    with ArtifactSessionLocal() as db:
        return delete_collection_record(db, uuid.UUID(job_id))


@DBOS.step(name="delete_collection_blob_batch", retries_allowed=True, max_attempts=10)
def delete_blobs(job_id: str) -> dict[str, int | bool]:
    with ArtifactSessionLocal() as db:
        return delete_blob_batch(db, get_storage(), uuid.UUID(job_id))


@DBOS.step(name="complete_collection_deletion", retries_allowed=True, max_attempts=5)
def complete(job_id: str) -> None:
    with ArtifactSessionLocal() as db:
        complete_deletion(db, uuid.UUID(job_id))


@DBOS.step(name="fail_collection_deletion", retries_allowed=True, max_attempts=5)
def fail(job_id: str, message: str) -> None:
    with ArtifactSessionLocal() as db:
        fail_deletion(db, uuid.UUID(job_id), message)


@DBOS.workflow(name="collection_deletion")
def collection_deletion(job_id: str) -> None:
    logger.info("Starting collection deletion job_id=%s", job_id)
    try:
        validate(job_id)
        delete_search_projections(job_id)
        prepare(job_id)
        while True:
            result = delete_items(job_id)
            if not result["has_more"]:
                break
        delete_database_record(job_id)
        while True:
            result = delete_blobs(job_id)
            if not result["has_more"]:
                break
        complete(job_id)
        logger.info("Completed collection deletion job_id=%s", job_id)
    except Exception as exc:
        logger.exception("Collection deletion failed job_id=%s", job_id)
        fail(job_id, str(exc))
        raise
