import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.artifact_gateway import (
    get_workflow_artifact_reference,
    iter_workflow_artifact_content,
    list_workflow_artifact_references,
)
from app.audit import record_audit
from app.authorization import ResourceRef, can
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.email_domains import EmailDomainUpdateConflict, ensure_email_domain_metadata
from app.embedding_gateway import get_query_embedding_gateway
from app.file_dates import FileDateUpdateConflict, ensure_file_date_metadata
from app.job_archives import job_not_archived
from app.job_executions import MATTER_SEARCH_EXPORT_JOB_TYPE, JobExecutionConflict
from app.models import (
    Custodian,
    Matter,
    MatterSearchExportJob,
    MetadataDefinition,
    SearchIndexGeneration,
    SearchProjectionOperation,
)
from app.schemas import (
    MatterDateHistogramRequest,
    MatterDateHistogramResponse,
    MatterFacetValuesRequest,
    MatterFacetValuesResponse,
    MatterSearchExportArtifactRead,
    MatterSearchExportCreate,
    MatterSearchExportRead,
    MatterSearchRequest,
    MatterSearchResponse,
    SearchExportFieldRead,
    SearchIndexGenerationRead,
    SearchProjectionOperationRead,
    SearchProjectionRetryResponse,
)
from app.search.client import OpenSearchClient, OpenSearchError
from app.search.coding_provenance import coded_by_labels, matching_coded_by_tokens
from app.search.mappings import CODED_BY_FIELD
from app.search.operations import create_search_operation
from app.search.query import (
    execute_batch_coding_facets,
    execute_batch_topic_facets,
    execute_date_histogram,
    execute_facet_values,
    execute_search,
)
from app.search_exports import as_utc, create_search_export_job, search_export_field_catalog
from app.workflows.dispatcher import enqueue_search_export, enqueue_search_projection, get_dbos_client
from classification_service.schemas import ArtifactType

router = APIRouter(prefix="/v1/matters/{matter_id}", tags=["matter search"])
logger = logging.getLogger(__name__)
_DBOS_ERROR_STATUSES = {"ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"}


def _ensure_rebuild_metadata(db: Session, matter: Matter, principal: Principal) -> None:
    """Install derived system definitions that a full rebuild must project."""

    try:
        ensure_email_domain_metadata(
            db,
            matter=matter,
            actor_user_id=principal.user.id,
            queue_backfill=False,
        )
        ensure_file_date_metadata(
            db,
            matter=matter,
            actor_user_id=principal.user.id,
        )
    except (EmailDomainUpdateConflict, FileDateUpdateConflict) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _export_matter(db: Session, matter_id: uuid.UUID, principal: Principal) -> Matter:
    matter = _matter(db, matter_id, principal, "document.view")
    if not can(db, principal, "document.export", ResourceRef.matter(matter.id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission required: document.export")
    return matter


@router.get("/search-export-fields", response_model=list[SearchExportFieldRead])
def list_search_export_fields(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[SearchExportFieldRead]:
    matter = _export_matter(db, matter_id, principal)
    generation = db.scalar(
        select(SearchIndexGeneration).where(
            SearchIndexGeneration.matter_id == matter.id,
            SearchIndexGeneration.status == "ACTIVE",
        )
    )
    if generation is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Matter search index is not ready")
    return search_export_field_catalog(db, matter.id, generation)


@router.post("/search-exports", response_model=MatterSearchExportRead, status_code=status.HTTP_202_ACCEPTED)
def create_search_export(
    matter_id: uuid.UUID,
    payload: MatterSearchExportCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterSearchExportJob:
    matter = _export_matter(db, matter_id, principal)
    if len(payload.field_ids) > settings.search_export_max_columns:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"At most {settings.search_export_max_columns} export fields may be selected",
        )
    try:
        job, created = create_search_export_job(
            db,
            matter,
            payload,
            actor_user_id=principal.user.id,
            settings=settings,
        )
    except JobExecutionConflict as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if created:
        enqueue_search_export(db, job.workflow_id, str(job.id))
    db.commit()
    db.refresh(job)
    return job


@router.get("/search-exports", response_model=list[MatterSearchExportRead])
def list_search_exports(
    matter_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterSearchExportJob]:
    matter = _export_matter(db, matter_id, principal)
    return list(
        db.scalars(
            select(MatterSearchExportJob)
            .where(
                MatterSearchExportJob.matter_id == matter.id,
                job_not_archived(MATTER_SEARCH_EXPORT_JOB_TYPE, MatterSearchExportJob.id),
            )
            .order_by(MatterSearchExportJob.created_at.desc(), MatterSearchExportJob.id.desc())
            .limit(limit)
        )
    )


@router.get("/search-exports/artifacts", response_model=list[MatterSearchExportArtifactRead])
def list_search_export_artifacts(
    matter_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterSearchExportArtifactRead]:
    matter = _export_matter(db, matter_id, principal)
    references = list_workflow_artifact_references(
        actor_user_id=principal.user.id,
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        core_scope_kind="MATTER",
        core_scope_id=matter.id,
        artifact_type=ArtifactType.SEARCH_RESULT_EXPORT,
        limit=limit,
    )
    results: list[MatterSearchExportArtifactRead] = []
    for reference in references:
        metadata = reference.metadata
        try:
            export_job_id = uuid.UUID(str(metadata["export_job_id"]))
        except (KeyError, ValueError):
            continue
        results.append(
            MatterSearchExportArtifactRead(
                artifact_id=reference.artifact_id,
                export_job_id=export_job_id,
                filename=str(metadata.get("filename") or "search-results.csv"),
                byte_length=reference.byte_length,
                content_hash=reference.content_hash,
                row_count=int(metadata.get("row_count") or 0),
                column_count=int(metadata.get("column_count") or 0),
                created_at=reference.created_at,
            )
        )
    return results


def _search_export_or_404(db: Session, matter_id: uuid.UUID, export_id: uuid.UUID) -> MatterSearchExportJob:
    job = db.scalar(
        select(MatterSearchExportJob).where(
            MatterSearchExportJob.id == export_id,
            MatterSearchExportJob.matter_id == matter_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search export not found")
    return job


@router.get("/search-exports/{export_id}", response_model=MatterSearchExportRead)
def get_search_export(
    matter_id: uuid.UUID,
    export_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterSearchExportJob:
    _export_matter(db, matter_id, principal)
    return _search_export_or_404(db, matter_id, export_id)


@router.get("/search-exports/{export_id}/download")
def download_search_export(
    matter_id: uuid.UUID,
    export_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    matter = _export_matter(db, matter_id, principal)
    job = _search_export_or_404(db, matter_id, export_id)
    if job.status != "COMPLETED" or job.output_artifact_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Search export is not ready")
    if job.expires_at is not None and as_utc(job.expires_at) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Search export has expired")
    try:
        artifact = get_workflow_artifact_reference(
            artifact_id=job.output_artifact_id,
            actor_user_id=principal.user.id,
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
        )
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Search export artifact is unavailable") from exc
    expected = {
        "type": artifact.artifact_type == ArtifactType.SEARCH_RESULT_EXPORT,
        "scope": artifact.core_scope_kind == "MATTER" and artifact.core_scope_id == matter.id,
        "producer": artifact.producer_run_id == job.id,
        "hash": artifact.content_hash == job.output_content_hash,
        "length": artifact.byte_length == job.output_byte_length,
        "job": str(artifact.metadata.get("export_job_id")) == str(job.id),
    }
    if not all(expected.values()):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Search export artifact provenance is invalid")
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="search_export.downloaded",
        target_type="matter_search_export_job",
        target_id=job.id,
        details={
            "matter_id": str(matter.id),
            "artifact_id": str(job.output_artifact_id),
            "content_hash": job.output_content_hash,
        },
    )
    db.commit()
    filename = job.output_filename or job.requested_filename
    return StreamingResponse(
        iter_workflow_artifact_content(
            artifact_id=job.output_artifact_id,
            actor_user_id=principal.user.id,
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
        ),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Content-Length": str(job.output_byte_length or 0),
            "ETag": job.output_content_hash or "",
        },
    )


def _matter(
    db: Session,
    matter_id: uuid.UUID,
    principal: Principal,
    permission_key: str = "document.view",
) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can(db, principal, permission_key, ResourceRef.matter(matter.id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission required: {permission_key}")
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
        logger.exception(
            "Matter search failed matter_id=%s",
            matter.id,
        )
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
    if definition is None and field != CODED_BY_FIELD:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unknown or non-facetable field")

    value_query = (payload.query or "").strip()
    include_values: list[str] | None = None
    if value_query and field == CODED_BY_FIELD:
        include_values = matching_coded_by_tokens(db, matter.client.tenant_id, value_query)
    elif value_query and definition is not None and definition.type == "ENUM":
        needle = value_query.casefold()
        include_values = [
            str(option["key"])
            for option in definition.allowed_values or []
            if option.get("active", True)
            and (needle in str(option.get("label", "")).casefold() or needle in str(option["key"]).casefold())
        ]
    elif value_query and definition is not None and definition.reference_target == "CUSTODIAN":
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
    elif value_query and definition is not None and definition.type == "BOOLEAN":
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
        response = execute_facet_values(
            client,
            generation.alias_name,
            payload.search,
            definitions,
            field=field,
            tenant_id=str(matter.client.tenant_id),
            matter_id=str(matter.id),
            value_query=value_query or None,
            size=payload.size,
            parent=payload.parent,
            include_values=include_values,
            query_vector=query_vector,
            required_filters=required_filters,
        )
        if field == CODED_BY_FIELD:
            tokens = [str(item.value) for item in response.values]
            labels = coded_by_labels(db, tokens)
            response.values = [
                item.model_copy(update={"label": labels.get(str(item.value), str(item.value))})
                for item in response.values
            ]
        return response
    except OpenSearchError as exc:
        logger.exception(
            "Facet value search failed matter_id=%s field=%s",
            matter.id,
            field,
        )
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


def execute_matter_batch_coding_facets(
    matter: Matter,
    payload: MatterSearchRequest,
    *,
    definition: MetadataDefinition,
    batch_id: uuid.UUID,
    run_id: uuid.UUID,
    minimum_confidence: float | None = None,
    maximum_confidence: float | None = None,
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
        return execute_batch_coding_facets(
            client,
            generation.alias_name,
            payload,
            definitions,
            definition=definition,
            tenant_id=str(matter.client.tenant_id),
            matter_id=str(matter.id),
            batch_id=str(batch_id),
            run_id=str(run_id),
            minimum_confidence=minimum_confidence,
            maximum_confidence=maximum_confidence,
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
    settings: Settings = Depends(get_settings),
) -> list[SearchIndexGenerationRead]:
    _matter(db, matter_id, principal, "job.view")
    generations = list(
        db.scalars(
            select(SearchIndexGeneration)
            .where(SearchIndexGeneration.matter_id == matter_id)
            .order_by(SearchIndexGeneration.generation.desc())
        )
    )
    if not generations:
        return []
    if not settings.search_enabled:
        return [
            SearchIndexGenerationRead.model_validate(generation).model_copy(
                update={"verification_error": "Search is disabled"}
            )
            for generation in generations
        ]

    client = OpenSearchClient(settings)
    try:
        aliases: dict[str, list[str]] = {}
        reads: list[SearchIndexGenerationRead] = []
        for generation in generations:
            try:
                if generation.alias_name not in aliases:
                    aliases[generation.alias_name] = client.alias_indices(generation.alias_name)
                physical_exists = client.index_exists(generation.index_name)
                alias_points_to_index = generation.index_name in aliases[generation.alias_name]
                live_count = client.count(generation.index_name) if physical_exists else 0
                reads.append(
                    SearchIndexGenerationRead.model_validate(generation).model_copy(
                        update={
                            "physical_index_exists": physical_exists,
                            "alias_points_to_index": alias_points_to_index,
                            "live_document_count": live_count,
                        }
                    )
                )
            except OpenSearchError as exc:
                reads.append(
                    SearchIndexGenerationRead.model_validate(generation).model_copy(
                        update={"verification_error": str(exc)[:500]}
                    )
                )
        return reads
    finally:
        client.close()


@router.get("/search-operations", response_model=list[SearchProjectionOperationRead])
def list_search_operations(
    matter_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[SearchProjectionOperation]:
    _matter(db, matter_id, principal, "job.view")
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
    matter = _matter(db, matter_id, principal, "job.view")
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
    matter = _matter(db, matter_id, principal, "search.rebuild")
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
    matter = _matter(db, matter_id, principal, "search.rebuild")
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

    _ensure_rebuild_metadata(db, matter, principal)

    confirmed_at = datetime.now(timezone.utc)
    operation.kind = "REBUILD"
    operation.status = "QUEUED"
    operation.workflow_id = f"search-projection:{operation.id}:confirmed:{uuid.uuid4()}"
    operation.payload = {
        **operation.payload,
        "progress": {
            "phase": "QUEUED",
            "processed_documents": 0,
            "total_documents": 0,
            "index_name": None,
            "updated_at": confirmed_at.isoformat(),
        },
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
    matter = _matter(db, matter_id, principal, "search.rebuild")
    if not settings.search_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Search is disabled")
    _ensure_rebuild_metadata(db, matter, principal)
    operation = create_search_operation(
        db,
        matter_id=matter.id,
        kind="REBUILD",
        payload={
            "progress": {
                "phase": "QUEUED",
                "processed_documents": 0,
                "total_documents": 0,
                "index_name": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        },
        created_by_user_id=principal.user.id,
        priority=1000,
    )
    db.commit()
    db.refresh(operation)
    return operation
