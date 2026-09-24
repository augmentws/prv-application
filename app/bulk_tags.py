import logging
import math
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select

from app.audit import record_audit
from app.config import get_settings
from app.database import SessionLocal
from app.document_metadata import add_metadata_event, apply_metadata_values
from app.embedding_gateway import get_query_embedding_gateway
from app.models import (
    MatterBulkTagBatch,
    MatterBulkTagJob,
    MatterDocument,
    MetadataDefinition,
    SearchProjectionOperation,
)
from app.schemas import MatterSearchRequest
from app.search.client import OpenSearchClient
from app.search.query import RRF_SEARCH_PIPELINE, compile_search_request
from app.search.service import process_search_operation

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _remove_inner_hits(value: object) -> None:
    if isinstance(value, dict):
        value.pop("inner_hits", None)
        for child in value.values():
            _remove_inner_hits(child)
    elif isinstance(value, list):
        for child in value:
            _remove_inner_hits(child)


def preview_ranked_bulk_tag_scope(
    *,
    request: MatterSearchRequest,
    candidate_limit: int,
    definitions: list[MetadataDefinition],
    tenant_id: str,
    matter_id: str,
    index_name: str,
) -> tuple[int, int]:
    settings = get_settings()
    query_vector = get_query_embedding_gateway().embed([request.query or ""], "query").embeddings[0]
    ranking_request = request.model_copy(
        update={"filters": [], "offset": 0, "size": candidate_limit, "facets": [], "candidate_limit": candidate_limit}
    )
    body = compile_search_request(
        ranking_request,
        definitions,
        tenant_id=tenant_id,
        matter_id=matter_id,
        query_vector=query_vector,
    )
    body.pop("highlight", None)
    body.pop("from", None)
    body.pop("sort", None)
    body["size"] = candidate_limit
    body["_source"] = ["document_id"]
    _remove_inner_hits(body["query"])
    client = OpenSearchClient(settings)
    try:
        if request.search_mode == "HYBRID":
            client.ensure_rrf_search_pipeline(RRF_SEARCH_PIPELINE)
            raw = client.search(index_name, body, search_pipeline=RRF_SEARCH_PIPELINE)
        else:
            raw = client.search(index_name, body)
        candidate_ids = [str(hit.get("_id")) for hit in raw.get("hits", {}).get("hits", [])]
        if not request.filters or not candidate_ids:
            return len(candidate_ids), len(candidate_ids)
        filter_request = request.model_copy(
            update={
                "query": None,
                "search_mode": "KEYWORD",
                "minimum_similarity": None,
                "candidate_limit": None,
                "offset": 0,
                "size": 1,
                "facets": [],
                "sort": [],
            }
        )
        filter_body = compile_search_request(
            filter_request,
            definitions,
            tenant_id=tenant_id,
            matter_id=matter_id,
            required_filters=[{"ids": {"values": candidate_ids}}],
        )
        filter_body.pop("highlight", None)
        filter_body["size"] = 0
        filtered = client.search(index_name, filter_body)
        total = filtered.get("hits", {}).get("total", 0)
        matched_count = int(total.get("value", 0) if isinstance(total, dict) else total)
        return len(candidate_ids), matched_count
    finally:
        client.close()


def snapshot_bulk_tag_scope(job_id: uuid.UUID) -> list[uuid.UUID]:
    settings = get_settings()
    with SessionLocal() as db:
        job = db.scalar(select(MatterBulkTagJob).where(MatterBulkTagJob.id == job_id).with_for_update())
        if job is None:
            raise ValueError("Bulk tag job not found")
        if job.status in {"RUNNING", "COMPLETED", "COMPLETED_WITH_ERRORS"}:
            return list(
                db.scalars(
                    select(MatterBulkTagBatch.id)
                    .where(MatterBulkTagBatch.job_id == job.id)
                    .order_by(MatterBulkTagBatch.batch_number)
                )
            )
        job.status = "SNAPSHOTTING"
        job.started_at = job.started_at or utcnow()
        job.error_message = None
        db.execute(delete(MatterBulkTagBatch).where(MatterBulkTagBatch.job_id == job.id))
        db.commit()

        request = MatterSearchRequest.model_validate(job.search_definition)
        definitions = list(
            db.scalars(select(MetadataDefinition).where(MetadataDefinition.matter_id == job.matter_id))
        )
        candidate_limit = job.search_index_snapshot.get("candidate_limit")
        is_ranked_scope = request.search_mode != "KEYWORD"
        if is_ranked_scope and not isinstance(candidate_limit, int):
            raise ValueError("The bulk tag job is missing its semantic candidate limit")
        snapshot_size = candidate_limit if is_ranked_scope else 500
        query_vector = None
        if is_ranked_scope:
            query_vector = get_query_embedding_gateway().embed([request.query or ""], "query").embeddings[0]
        ranking_request = request.model_copy(
            update={
                "filters": [] if is_ranked_scope else request.filters,
                "offset": 0,
                "size": snapshot_size,
                "facets": [],
            }
        )
        body = compile_search_request(
            ranking_request,
            definitions,
            tenant_id=str(job.matter.client.tenant_id),
            matter_id=str(job.matter_id),
            query_vector=query_vector,
        )
        body.pop("highlight", None)
        body.pop("from", None)
        body["_source"] = ["document_id"]
        _remove_inner_hits(body["query"])
        if is_ranked_scope:
            body.pop("sort", None)
            body["size"] = candidate_limit
        else:
            body["sort"] = [{"document_id": "asc"}]
        generation = job.search_index_generation
        if generation is None:
            raise ValueError("The pinned search index generation is no longer available")

        document_ids: list[uuid.UUID] = []
        client = OpenSearchClient(settings)
        try:
            if request.search_mode == "HYBRID":
                client.ensure_rrf_search_pipeline(RRF_SEARCH_PIPELINE)
                raw = client.search(generation.index_name, body, search_pipeline=RRF_SEARCH_PIPELINE)
                hits = raw.get("hits", {}).get("hits", [])
                document_ids.extend(
                    uuid.UUID(str((hit.get("_source") or {}).get("document_id") or hit.get("_id")))
                    for hit in hits
                )
            elif is_ranked_scope:
                raw = client.search(generation.index_name, body)
                hits = raw.get("hits", {}).get("hits", [])
                document_ids.extend(
                    uuid.UUID(str((hit.get("_source") or {}).get("document_id") or hit.get("_id")))
                    for hit in hits
                )
            else:
                while True:
                    raw = client.search(generation.index_name, body)
                    hits = raw.get("hits", {}).get("hits", [])
                    if not hits:
                        break
                    document_ids.extend(
                        uuid.UUID(str((hit.get("_source") or {}).get("document_id") or hit.get("_id")))
                        for hit in hits
                    )
                    if len(hits) < body["size"]:
                        break
                    body["search_after"] = hits[-1]["sort"]

            if is_ranked_scope and request.filters and document_ids:
                ranked_document_ids = document_ids
                filter_request = request.model_copy(
                    update={
                        "query": None,
                        "search_mode": "KEYWORD",
                        "minimum_similarity": None,
                        "offset": 0,
                        "size": 500,
                        "facets": [],
                        "sort": [],
                    }
                )
                filter_body = compile_search_request(
                    filter_request,
                    definitions,
                    tenant_id=str(job.matter.client.tenant_id),
                    matter_id=str(job.matter_id),
                    required_filters=[{"ids": {"values": [str(value) for value in ranked_document_ids]}}],
                )
                filter_body.pop("from", None)
                filter_body["_source"] = ["document_id"]
                filter_body["sort"] = [{"document_id": "asc"}]
                document_ids = []
                while True:
                    raw = client.search(generation.index_name, filter_body)
                    hits = raw.get("hits", {}).get("hits", [])
                    if not hits:
                        break
                    document_ids.extend(
                        uuid.UUID(str((hit.get("_source") or {}).get("document_id") or hit.get("_id")))
                        for hit in hits
                    )
                    if len(hits) < filter_body["size"]:
                        break
                    filter_body["search_after"] = hits[-1]["sort"]
        finally:
            client.close()

        batch_size = settings.matter_bulk_tag_batch_size
        batches = [
            MatterBulkTagBatch(
                job_id=job.id,
                batch_number=batch_number,
                document_ids=[str(value) for value in document_ids[offset : offset + batch_size]],
                item_count=len(document_ids[offset : offset + batch_size]),
            )
            for batch_number, offset in enumerate(range(0, len(document_ids), batch_size), start=1)
        ]
        db.add_all(batches)
        job.matched_count = len(document_ids)
        job.batch_count = math.ceil(len(document_ids) / batch_size) if document_ids else 0
        job.status = "RUNNING"
        db.commit()
        return [batch.id for batch in batches]


def _ensure_batch_search_operation(job: MatterBulkTagJob, batch: MatterBulkTagBatch) -> uuid.UUID:
    workflow_id = f"bulk-tag-index:{batch.id}"
    with SessionLocal() as db:
        operation = db.scalar(
            select(SearchProjectionOperation).where(SearchProjectionOperation.workflow_id == workflow_id)
        )
        if operation is None:
            operation = SearchProjectionOperation(
                matter_id=job.matter_id,
                kind="DOCUMENT_UPSERT",
                payload={"document_ids": batch.document_ids},
                status="QUEUED",
                workflow_id=workflow_id,
                created_by_user_id=job.created_by_user_id,
            )
            db.add(operation)
            db.commit()
        return operation.id


def process_bulk_tag_batch(batch_id: uuid.UUID) -> dict[str, int]:
    with SessionLocal() as db:
        batch = db.get(MatterBulkTagBatch, batch_id)
        if batch is None:
            raise ValueError("Bulk tag batch not found")
        job = db.get(MatterBulkTagJob, batch.job_id)
        if job is None:
            raise ValueError("Bulk tag job not found")
        if batch.status == "COMPLETED":
            return {
                "processed_count": batch.processed_count,
                "tagged_count": batch.tagged_count,
                "failed_count": batch.failed_count,
            }
        assignments = job.assignments or [
            {"metadata_definition_id": str(job.metadata_definition_id), "value": job.value}
        ]
        definition_ids = [uuid.UUID(str(assignment["metadata_definition_id"])) for assignment in assignments]
        definitions = {
            definition.id: definition
            for definition in db.scalars(select(MetadataDefinition).where(MetadataDefinition.id.in_(definition_ids)))
        }
        if len(definitions) != len(definition_ids):
            raise ValueError("A bulk tag field no longer exists")
        for definition in definitions.values():
            if definition.matter_id != job.matter_id:
                raise ValueError("Bulk tag field no longer exists")
            if definition.status != "ACTIVE" or definition.value_source != "ASSERTED":
                raise ValueError("A bulk tag field is no longer an active asserted field")

        batch.status = "RUNNING"
        db.commit()
        processed = tagged = failed = 0
        errors: list[str] = []
        if not batch.metadata_applied:
            for value in batch.document_ids:
                document_id = uuid.UUID(value)
                try:
                    with db.begin_nested():
                        document = db.get(MatterDocument, document_id)
                        if document is None or document.matter_id != job.matter_id:
                            raise ValueError("Matter document not found")
                        for assignment in assignments:
                            definition_id = uuid.UUID(str(assignment["metadata_definition_id"]))
                            definition = definitions[definition_id]
                            assignment_value = assignment["value"]
                            if definition.cardinality == "MULTIPLE":
                                values = assignment_value if isinstance(assignment_value, list) else [assignment_value]
                                events, _ = apply_metadata_values(
                                    db,
                                    matter_id=job.matter_id,
                                    document_id=document.id,
                                    definition_id=definition.id,
                                    values=values,
                                    replace=False,
                                    source_type="HUMAN",
                                    actor_id=job.created_by_user_id,
                                    source_id=f"bulk-tag-job:{job.id}",
                                )
                            else:
                                event, _ = add_metadata_event(
                                    db,
                                    matter_id=job.matter_id,
                                    document_id=document.id,
                                    definition_id=definition.id,
                                    operation="SET",
                                    value=assignment_value,
                                    source_type="HUMAN",
                                    actor_id=job.created_by_user_id,
                                    source_id=f"bulk-tag-job:{job.id}",
                                )
                                events = [event]
                            for event in events:
                                record_audit(
                                    db,
                                    tenant_id=job.matter.client.tenant_id,
                                    actor_user_id=job.created_by_user_id,
                                    action="document.metadata_bulk_tagged",
                                    target_type="metadata_event",
                                    target_id=event.id,
                                    details={
                                        "matter_id": str(job.matter_id),
                                        "document_id": str(document.id),
                                        "metadata_definition_id": str(definition.id),
                                        "bulk_tag_job_id": str(job.id),
                                        "operation": event.operation,
                                    },
                                )
                    tagged += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    errors.append(f"{document_id}: {exc}")
                    logger.warning(
                        "Bulk tag failed job_id=%s document_id=%s error=%s",
                        job.id,
                        document_id,
                        exc,
                    )
                processed += 1
            batch.processed_count = processed
            batch.tagged_count = tagged
            batch.failed_count = failed
            batch.error_message = "\n".join(errors[:20]) or None
            batch.metadata_applied = True
            db.commit()

        operation_id = _ensure_batch_search_operation(job, batch)
        process_search_operation(operation_id)
        batch.status = "COMPLETED"
        db.commit()
        return {
            "processed_count": batch.processed_count,
            "tagged_count": batch.tagged_count,
            "failed_count": batch.failed_count,
        }


def refresh_bulk_tag_job(job_id: uuid.UUID) -> None:
    with SessionLocal() as db:
        job = db.get(MatterBulkTagJob, job_id)
        if job is None:
            return
        totals = db.execute(
            select(
                func.coalesce(func.sum(MatterBulkTagBatch.processed_count), 0),
                func.coalesce(func.sum(MatterBulkTagBatch.tagged_count), 0),
                func.coalesce(func.sum(MatterBulkTagBatch.failed_count), 0),
            ).where(MatterBulkTagBatch.job_id == job.id)
        ).one()
        job.processed_count, job.tagged_count, job.failed_count = (int(value) for value in totals)
        db.commit()


def complete_bulk_tag_job(job_id: uuid.UUID) -> None:
    refresh_bulk_tag_job(job_id)
    with SessionLocal() as db:
        job = db.get(MatterBulkTagJob, job_id)
        if job is None:
            return
        job.status = "COMPLETED_WITH_ERRORS" if job.failed_count else "COMPLETED"
        errors = list(
            db.scalars(
                select(MatterBulkTagBatch.error_message)
                .where(MatterBulkTagBatch.job_id == job.id, MatterBulkTagBatch.error_message.is_not(None))
                .order_by(MatterBulkTagBatch.batch_number)
                .limit(10)
            )
        )
        job.error_message = "\n".join(errors)[:4000] if errors else None
        job.completed_at = utcnow()
        job.search_index_generation_id = None
        db.commit()


def fail_bulk_tag_job(job_id: uuid.UUID, message: str) -> None:
    with SessionLocal() as db:
        job = db.get(MatterBulkTagJob, job_id)
        if job is None:
            return
        job.status = "FAILED"
        job.error_message = message[:4000]
        job.completed_at = utcnow()
        job.search_index_generation_id = None
        db.commit()
