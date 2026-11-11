import logging
import uuid

from dbos import DBOS, Queue

from app.artifact_gateway import delete_workflow_artifacts_in_scopes
from app.config import get_settings
from app.database import SessionLocal
from app.matter_deletions import (
    complete_matter_deletion,
    delete_matter_database_rows,
    fail_matter_deletion,
    mark_matter_deletion_stage,
    matter_deletion_blockers,
)
from app.models import MatterDeletionJob
from app.search.client import OpenSearchClient
from app.search.service import document_alias
from artifact_service.schemas import WorkflowArtifactScopeDelete

logger = logging.getLogger(__name__)

MATTER_DELETION_QUEUE = Queue("matter-deletions", global_concurrency=2)


@DBOS.step(name="validate_matter_deletion", retries_allowed=True, max_attempts=5)
def validate(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(MatterDeletionJob, uuid.UUID(job_id))
        if job is None:
            raise ValueError("Matter deletion job not found")
        if job.status == "COMPLETED":
            return
        job.status = "VALIDATING"
        db.commit()
        blockers = matter_deletion_blockers(db, job.matter_id)
        if blockers.blocked:
            raise ValueError(blockers.message())


@DBOS.step(name="delete_matter_search_indexes", retries_allowed=True, max_attempts=10)
def delete_search_indexes(job_id: str) -> int:
    settings = get_settings()
    with SessionLocal() as db:
        job = mark_matter_deletion_stage(db, uuid.UUID(job_id), "DELETING_SEARCH")
        matter_id = job.matter_id
    if not settings.search_enabled:
        return 0
    client = OpenSearchClient(settings)
    try:
        names = client.resolve_indices(f"{document_alias(settings, matter_id)}-v*")
        for name in names:
            client.delete_index(name)
    finally:
        client.close()
    with SessionLocal() as db:
        job = db.get(MatterDeletionJob, uuid.UUID(job_id))
        if job is None:
            raise ValueError("Matter deletion job not found")
        job.deleted_index_count = max(job.deleted_index_count, len(names))
        db.commit()
    return len(names)


@DBOS.step(name="delete_matter_artifact_batch", retries_allowed=True, max_attempts=10)
def delete_artifact_batch(job_id: str) -> dict[str, int | bool]:
    with SessionLocal() as db:
        job = mark_matter_deletion_stage(db, uuid.UUID(job_id), "DELETING_ARTIFACTS")
        actor_user_id = job.requested_by_user_id
        tenant_id = job.tenant_id
        client_id = job.client_id
        scopes = [WorkflowArtifactScopeDelete.model_validate(value) for value in job.artifact_scopes]
    result = delete_workflow_artifacts_in_scopes(
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
        scopes=scopes,
    )
    with SessionLocal() as db:
        job = db.get(MatterDeletionJob, uuid.UUID(job_id))
        if job is None:
            raise ValueError("Matter deletion job not found")
        job.deleted_artifact_count += result.deleted_artifact_count
        job.deleted_blob_count += result.deleted_blob_count
        db.commit()
    return {
        "deleted_artifacts": result.deleted_artifact_count,
        "deleted_blobs": result.deleted_blob_count,
        "has_more": result.has_more,
    }


@DBOS.step(name="delete_matter_database_rows", retries_allowed=True, max_attempts=5)
def delete_database_rows(job_id: str) -> int:
    with SessionLocal() as db:
        mark_matter_deletion_stage(db, uuid.UUID(job_id), "DELETING_DATABASE_ROWS")
        return delete_matter_database_rows(db, uuid.UUID(job_id))


@DBOS.step(name="complete_matter_deletion", retries_allowed=True, max_attempts=5)
def complete(job_id: str) -> None:
    with SessionLocal() as db:
        complete_matter_deletion(db, uuid.UUID(job_id))


@DBOS.step(name="fail_matter_deletion", retries_allowed=True, max_attempts=5)
def fail(job_id: str, message: str) -> None:
    with SessionLocal() as db:
        fail_matter_deletion(db, uuid.UUID(job_id), message)


@DBOS.workflow(name="matter_deletion")
def matter_deletion(job_id: str) -> None:
    logger.info("Starting matter deletion job_id=%s", job_id)
    try:
        validate(job_id)
        delete_search_indexes(job_id)
        while True:
            result = delete_artifact_batch(job_id)
            if not result["has_more"]:
                break
        delete_database_rows(job_id)
        complete(job_id)
        logger.info("Completed matter deletion job_id=%s", job_id)
    except Exception as exc:
        logger.exception("Matter deletion failed job_id=%s", job_id)
        fail(job_id, str(exc))
        raise
