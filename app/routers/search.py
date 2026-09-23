import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, get_principal
from app.embedding_gateway import get_query_embedding_gateway
from app.models import Custodian, Matter, MetadataDefinition, SearchIndexGeneration, SearchProjectionOperation
from app.schemas import (
    MatterDateHistogramRequest,
    MatterDateHistogramResponse,
    MatterFacetValuesRequest,
    MatterFacetValuesResponse,
    MatterSearchRequest,
    MatterSearchResponse,
    SearchIndexGenerationRead,
    SearchProjectionOperationRead,
    SearchProjectionRetryResponse,
)
from app.search.client import OpenSearchClient, OpenSearchError
from app.search.operations import create_search_operation
from app.search.query import execute_batch_topic_facets, execute_date_histogram, execute_facet_values, execute_search
from app.workflows.dispatcher import enqueue_search_projection, get_dbos_client

router = APIRouter(prefix="/v1/matters/{matter_id}", tags=["matter search"])
logger = logging.getLogger(__name__)
_DBOS_ERROR_STATUSES = {"ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"}


def _matter(db: Session, matter_id: uuid.UUID, principal: Principal) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    return matter


def _durable_error_workflow_statuses(settings: Settings, workflow_ids: list[str]) -> dict[str, str]:
    if not settings.dbos_enabled or not workflow_ids:
        return {}
    workflows = get_dbos_client().list_workflows(
        workflow_ids=workflow_ids,
        status=list(_DBOS_ERROR_STATUSES),
        load_input=False,
        load_output=False,
    )
    return {workflow.workflow_id: workflow.status for workflow in workflows}


def _retryable_document_upserts(
    db: Session,
    matter_id: uuid.UUID,
    settings: Settings,
    *,
    for_update: bool = False,
) -> list[tuple[SearchProjectionOperation, str | None]]:
    statement = (
        select(SearchProjectionOperation)
        .where(
            SearchProjectionOperation.matter_id == matter_id,
            SearchProjectionOperation.kind == "DOCUMENT_UPSERT",
            SearchProjectionOperation.status.in_(["FAILED", "RUNNING"]),
        )
        .order_by(SearchProjectionOperation.created_at, SearchProjectionOperation.id)
    )
    if for_update:
        statement = statement.with_for_update()
    candidates = list(db.scalars(statement))
    running = [operation for operation in candidates if operation.status == "RUNNING"]
    try:
        durable_errors = _durable_error_workflow_statuses(
            settings,
            [operation.workflow_id for operation in running],
        )
    except Exception as exc:
        logger.exception("Could not read durable workflow statuses for matter_id=%s", matter_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Durable workflow status is temporarily unavailable",
        ) from exc
    return [
        (operation, durable_errors.get(operation.workflow_id))
        for operation in candidates
        if operation.status == "FAILED" or operation.workflow_id in durable_errors
    ]


@router.post("/search", response_model=MatterSearchResponse)
def search_matter(
    matter_id: uuid.UUID,
    payload: MatterSearchRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterSearchResponse:
    matter = _matter(db, matter_id, principal)
    return execute_matter_search(matter, payload, db=db, settings=settings)


def execute_matter_search(
    matter: Matter,
    payload: MatterSearchRequest,
    *,
    db: Session,
    settings: Settings,
    required_filters: list[dict[str, Any]] | None = None,
) -> MatterSearchResponse:
    if not settings.search_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Search is disabled")
    generation = db.scalar(
        select(SearchIndexGeneration).where(
            SearchIndexGeneration.matter_id == matter.id,
            SearchIndexGeneration.status == "ACTIVE",
        )
    )
    if generation is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter search index is not ready")
    definitions = list(
        db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter.id,
                MetadataDefinition.status == "ACTIVE",
            )
        )
    )
    client = OpenSearchClient(settings)
    try:
        query_vector = None
        if payload.search_mode != "KEYWORD":
            try:
                query_vector = get_query_embedding_gateway().embed([payload.query or ""], "query").embeddings[0]
            except Exception as exc:
                logger.exception(
                    "Query embedding failed matter_id=%s search_mode=%s",
                    matter.id,
                    payload.search_mode,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Semantic query embedding is temporarily unavailable",
                ) from exc
        return execute_search(
            client,
            generation.alias_name,
            payload,
            definitions,
            tenant_id=str(matter.client.tenant_id),
            matter_id=str(matter.id),
            query_vector=query_vector,
            required_filters=required_filters,
        )
    except OpenSearchError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Search is temporarily unavailable"
        ) from exc
    finally:
        client.close()


def execute_matter_facet_values(
    matter: Matter,
    field: str,
    payload: MatterFacetValuesRequest,
    *,
    db: Session,
    settings: Settings,
    required_filters: list[dict[str, Any]] | None = None,
) -> MatterFacetValuesResponse:
    if not settings.search_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Search is disabled")
    generation = db.scalar(
        select(SearchIndexGeneration).where(
            SearchIndexGeneration.matter_id == matter.id,
            SearchIndexGeneration.status == "ACTIVE",
        )
    )
    if generation is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter search index is not ready")
    definitions = list(
        db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter.id,
                MetadataDefinition.status == "ACTIVE",
            )
        )
    )
    definition = next((item for item in definitions if item.key == field and item.facetable), None)
    if definition is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unknown or non-facetable field")

    value_query = (payload.query or "").strip()
    include_values: list[str] | None = None
    if value_query and definition.type == "ENUM":
        needle = value_query.casefold()
        include_values = [
            str(option["key"])
            for option in definition.allowed_values or []
            if option.get("active", True)
            and (needle in str(option.get("label", "")).casefold() or needle in str(option["key"]).casefold())
        ]
    elif value_query and definition.reference_target == "CUSTODIAN":
        include_values = [
            str(value)
            for value in db.scalars(
                select(Custodian.id)
                .where(
                    Custodian.client_id == matter.client_id,
                    Custodian.status == "ACTIVE",
                    func.lower(Custodian.display_name).contains(value_query.casefold()),
                )
                .limit(500)
            )
        ]
    elif value_query and definition.type == "BOOLEAN":
        needle = value_query.casefold()
        include_values = [
            value
            for value, labels in (("true", ("true", "yes")), ("false", ("false", "no")))
            if any(needle in label for label in labels)
        ]

    query_vector = None
    if payload.search.search_mode != "KEYWORD":
        try:
            query_vector = get_query_embedding_gateway().embed([payload.search.query or ""], "query").embeddings[0]
        except Exception as exc:
            logger.exception(
                "Facet query embedding failed matter_id=%s search_mode=%s",
                matter.id,
                payload.search.search_mode,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Semantic query embedding is temporarily unavailable",
            ) from exc
    client = OpenSearchClient(settings)
    try:
        return execute_facet_values(
            client,
            generation.alias_name,
            payload.search,
            definitions,
            field=field,
            tenant_id=str(matter.client.tenant_id),
            matter_id=str(matter.id),
            value_query=value_query or None,
            size=payload.size,
            include_values=include_values,
            query_vector=query_vector,
            required_filters=required_filters,
        )
    except OpenSearchError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Search is temporarily unavailable"
        ) from exc
    finally:
        client.close()


def execute_matter_batch_topic_facets(
    matter: Matter,
    payload: MatterSearchRequest,
    *,
    batch_id: uuid.UUID,
    taxonomy_id: uuid.UUID,
    db: Session,
    settings: Settings,
    required_filters: list[dict[str, Any]] | None = None,
) -> MatterFacetValuesResponse:
    if not settings.search_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Search is disabled")
    generation = db.scalar(
        select(SearchIndexGeneration).where(
            SearchIndexGeneration.matter_id == matter.id,
            SearchIndexGeneration.status == "ACTIVE",
        )
    )
    if generation is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter search index is not ready")
    definitions = list(
        db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter.id,
                MetadataDefinition.status == "ACTIVE",
            )
        )
    )
    query_vector = None
    if payload.search_mode != "KEYWORD":
        try:
            query_vector = get_query_embedding_gateway().embed([payload.query or ""], "query").embeddings[0]
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Semantic query embedding is temporarily unavailable",
            ) from exc
    client = OpenSearchClient(settings)
    try:
        return execute_batch_topic_facets(
            client,
            generation.alias_name,
            payload,
            definitions,
            tenant_id=str(matter.client.tenant_id),
            matter_id=str(matter.id),
            batch_id=str(batch_id),
            taxonomy_id=str(taxonomy_id),
            query_vector=query_vector,
            required_filters=required_filters,
        )
    except OpenSearchError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search is temporarily unavailable",
        ) from exc
    finally:
        client.close()


@router.post("/facets/{field}/values", response_model=MatterFacetValuesResponse)
def search_facet_values(
    matter_id: uuid.UUID,
    field: str,
    payload: MatterFacetValuesRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterFacetValuesResponse:
    matter = _matter(db, matter_id, principal)
    return execute_matter_facet_values(matter, field, payload, db=db, settings=settings)


@router.post("/facets/{field}/date-histogram", response_model=MatterDateHistogramResponse)
def search_date_histogram(
    matter_id: uuid.UUID,
    field: str,
    payload: MatterDateHistogramRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterDateHistogramResponse:
    matter = _matter(db, matter_id, principal)
    if not settings.search_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Search is disabled")
    generation = db.scalar(
        select(SearchIndexGeneration).where(
            SearchIndexGeneration.matter_id == matter.id,
            SearchIndexGeneration.status == "ACTIVE",
        )
    )
    if generation is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter search index is not ready")
    definitions = list(
        db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter.id,
                MetadataDefinition.status == "ACTIVE",
            )
        )
    )
    definition = next(
        (item for item in definitions if item.key == field and item.searchable and item.type in {"DATE", "DATETIME"}),
        None,
    )
    if definition is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unknown or non-date field")

    query_vector = None
    if payload.search.search_mode != "KEYWORD":
        try:
            query_vector = get_query_embedding_gateway().embed([payload.search.query or ""], "query").embeddings[0]
        except Exception as exc:
            logger.exception(
                "Date histogram query embedding failed matter_id=%s search_mode=%s",
                matter.id,
                payload.search.search_mode,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Semantic query embedding is temporarily unavailable",
            ) from exc
    client = OpenSearchClient(settings)
    try:
        return execute_date_histogram(
            client,
            generation.alias_name,
            payload.search,
            definitions,
            field=field,
            interval=payload.interval,
            tenant_id=str(matter.client.tenant_id),
            matter_id=str(matter.id),
            query_vector=query_vector,
        )
    except OpenSearchError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Search is temporarily unavailable"
        ) from exc
    finally:
        client.close()


@router.get("/search-indexes", response_model=list[SearchIndexGenerationRead])
def list_search_indexes(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[SearchIndexGeneration]:
    _matter(db, matter_id, principal)
    return list(
        db.scalars(
            select(SearchIndexGeneration)
            .where(SearchIndexGeneration.matter_id == matter_id)
            .order_by(SearchIndexGeneration.generation.desc())
        )
    )


@router.get("/search-operations", response_model=list[SearchProjectionOperationRead])
def list_search_operations(
    matter_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[SearchProjectionOperation]:
    _matter(db, matter_id, principal)
    return list(
        db.scalars(
            select(SearchProjectionOperation)
            .where(SearchProjectionOperation.matter_id == matter_id)
            .order_by(SearchProjectionOperation.created_at.desc())
            .limit(limit)
        )
    )


@router.get("/search-operations/retryable", response_model=SearchProjectionRetryResponse)
def summarize_retryable_search_operations(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SearchProjectionRetryResponse:
    matter = _matter(db, matter_id, principal)
    if not settings.search_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Search is disabled")
    operations = _retryable_document_upserts(db, matter.id, settings)
    document_count = sum(
        len(document_ids)
        for operation, _durable_workflow_status in operations
        if isinstance((document_ids := operation.payload.get("document_ids")), list)
    )
    return SearchProjectionRetryResponse(
        requeued_operation_count=len(operations),
        requeued_document_count=document_count,
    )


@router.post(
    "/search-operations/retry-failed",
    response_model=SearchProjectionRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_failed_search_operations(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SearchProjectionRetryResponse:
    matter = _matter(db, matter_id, principal)
    if not settings.search_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Search is disabled")

    operations = _retryable_document_upserts(db, matter.id, settings, for_update=True)
    retried_at = datetime.now(timezone.utc)
    document_count = 0
    for operation, durable_workflow_status in operations:
        document_ids = operation.payload.get("document_ids")
        if isinstance(document_ids, list):
            document_count += len(document_ids)
        retry_history = operation.payload.get("retry_history")
        if not isinstance(retry_history, list):
            retry_history = []
        operation.payload = {
            **operation.payload,
            "retry_history": [
                *retry_history,
                {
                    "workflow_id": operation.workflow_id,
                    "attempt_count": operation.attempt_count,
                    "error_message": operation.error_message,
                    "durable_workflow_status": durable_workflow_status,
                    "retried_at": retried_at.isoformat(),
                    "retried_by_user_id": str(principal.user.id),
                },
            ],
        }
        operation.status = "QUEUED"
        operation.workflow_id = f"search-projection:{operation.id}:retry:{uuid.uuid4()}"
        operation.error_message = None
        operation.started_at = None
        operation.completed_at = None
        enqueue_search_projection(db, operation.workflow_id, str(operation.id), priority=100)

    db.commit()
    return SearchProjectionRetryResponse(
        requeued_operation_count=len(operations),
        requeued_document_count=document_count,
    )


@router.post(
    "/search-operations/{operation_id}/confirm-reindex",
    response_model=SearchProjectionOperationRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def confirm_search_reindex(
    matter_id: uuid.UUID,
    operation_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> SearchProjectionOperation:
    matter = _matter(db, matter_id, principal)
    operation = db.scalar(
        select(SearchProjectionOperation)
        .where(
            SearchProjectionOperation.id == operation_id,
            SearchProjectionOperation.matter_id == matter.id,
        )
        .with_for_update()
    )
    if operation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search operation not found")
    if operation.status != "AWAITING_USER":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Search operation is not awaiting confirmation"
        )
    schema_change = operation.payload.get("schema_change")
    if not isinstance(schema_change, dict) or schema_change.get("action") != "REINDEX_REQUIRED":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Search operation has no reindex plan")

    confirmed_at = datetime.now(timezone.utc)
    operation.kind = "REBUILD"
    operation.status = "QUEUED"
    operation.workflow_id = f"search-projection:{operation.id}:confirmed:{uuid.uuid4()}"
    operation.payload = {
        **operation.payload,
        "confirmation": {
            "confirmed_at": confirmed_at.isoformat(),
            "confirmed_by_user_id": str(principal.user.id),
        },
    }
    operation.started_at = None
    operation.completed_at = None
    operation.error_message = None
    enqueue_search_projection(db, operation.workflow_id, str(operation.id), priority=1000)
    db.commit()
    db.refresh(operation)
    return operation


@router.post(
    "/search-indexes/rebuild", response_model=SearchProjectionOperationRead, status_code=status.HTTP_202_ACCEPTED
)
def rebuild_search_index(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SearchProjectionOperation:
    matter = _matter(db, matter_id, principal)
    if not settings.search_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Search is disabled")
    operation = create_search_operation(
        db,
        matter_id=matter.id,
        kind="REBUILD",
        created_by_user_id=principal.user.id,
        priority=1000,
    )
    db.commit()
    db.refresh(operation)
    return operation
