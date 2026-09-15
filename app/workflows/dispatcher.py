from functools import lru_cache

from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.orm import Session

from app.config import get_settings


@lru_cache
def get_dbos_client() -> DBOSClient:
    settings = get_settings()
    return DBOSClient(
        system_database_url=settings.dbos_system_database_url or settings.database_url,
        dbos_system_schema=settings.dbos_system_schema,
        application_name=settings.dbos_application_name,
    )


def enqueue_matter_import(db: Session, workflow_id: str, job_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_document_import",
        "queue_name": "matter-import-plans",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, job_id)


def cancel_matter_import(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def enqueue_matter_embedding(db: Session, workflow_id: str, job_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_embedding_job",
        "queue_name": "matter-embedding-plans",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, job_id)


def cancel_matter_embedding(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def enqueue_matter_topics(db: Session, workflow_id: str, job_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "matter_topic_job",
        "queue_name": "matter-topic-plans",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, job_id)


def cancel_matter_topics(workflow_id: str) -> None:
    if get_settings().dbos_enabled:
        get_dbos_client().cancel_workflow(workflow_id, cancel_children=True)


def enqueue_review_batch(db: Session, workflow_id: str, batch_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "review_batch_build",
        "queue_name": "review-batch-builds",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, batch_id)


def enqueue_search_projection(
    db: Session,
    workflow_id: str,
    operation_id: str,
    *,
    priority: int | None = None,
) -> None:
    settings = get_settings()
    if not settings.dbos_enabled or not settings.search_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "search_projection",
        "queue_name": "search-projections",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    if priority is not None:
        options["priority"] = priority
    get_dbos_client().enqueue_in_transaction(db, options, operation_id)


def enqueue_agent_run(db: Session, workflow_id: str, run_id: str) -> None:
    settings = get_settings()
    if not settings.dbos_enabled:
        return
    options: EnqueueOptions = {
        "workflow_name": "agent_turn",
        "queue_name": "agent-turns",
        "workflow_id": workflow_id,
        "application_name": settings.dbos_application_name,
    }
    get_dbos_client().enqueue_in_transaction(db, options, run_id)
