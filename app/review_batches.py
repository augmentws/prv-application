import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Matter,
    MatterDocument,
    MetadataDefinition,
    ReviewBatch,
    ReviewBatchDocument,
    SearchIndexGeneration,
)
from app.schemas import MatterSearchRequest
from app.search.client import OpenSearchClient
from app.search.query import compile_search_request


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stable_key(document_id: uuid.UUID, seed: str) -> bytes:
    return hashlib.sha256(f"{seed}:{document_id}".encode()).digest()


def _database_document_ids(db: Session, batch: ReviewBatch) -> list[uuid.UUID]:
    if batch.selection_type == "RANDOM_BATCH":
        ids = list(
            db.scalars(
                select(ReviewBatchDocument.matter_document_id)
                .where(ReviewBatchDocument.review_batch_id == batch.source_batch_id)
                .order_by(ReviewBatchDocument.sequence_number)
            )
        )
    else:
        ids = list(
            db.scalars(
                select(MatterDocument.id)
                .where(MatterDocument.matter_id == batch.matter_id)
                .order_by(MatterDocument.created_at, MatterDocument.id)
            )
        )
    if batch.selection_type.startswith("RANDOM"):
        ids.sort(key=lambda value: _stable_key(value, batch.random_seed or str(batch.id)))
        if batch.sample_size is not None:
            ids = ids[: batch.sample_size]
    return ids


def _search_document_ids(
    db: Session,
    batch: ReviewBatch,
    matter: Matter,
    settings: Settings,
) -> list[uuid.UUID]:
    generation = db.scalar(
        select(SearchIndexGeneration).where(
            SearchIndexGeneration.matter_id == matter.id,
            SearchIndexGeneration.status == "ACTIVE",
        )
    )
    if generation is None:
        raise ValueError("The matter search index must be ready before creating a search-query batch")
    batch.search_index_generation_id = generation.id
    batch.selection_definition = {
        **batch.selection_definition,
        "search_index_generation": {
            "generation": generation.generation,
            "index_name": generation.index_name,
            "schema_hash": generation.schema_hash,
            "activated_at": generation.activated_at.isoformat() if generation.activated_at else None,
        },
    }
    request = MatterSearchRequest.model_validate(batch.selection_definition["search"])
    definitions = list(db.scalars(select(MetadataDefinition).where(MetadataDefinition.matter_id == matter.id)))
    body = compile_search_request(
        request.model_copy(update={"offset": 0, "size": 500, "facets": []}),
        definitions,
        tenant_id=str(matter.client.tenant_id),
        matter_id=str(matter.id),
    )
    body.pop("highlight", None)
    body["_source"] = ["document_id"]
    body["sort"] = [{"document_id": "asc"}]
    body.pop("from", None)
    client = OpenSearchClient(settings)
    document_ids: list[uuid.UUID] = []
    try:
        while True:
            raw = client.search(generation.index_name, body)
            hits = raw.get("hits", {}).get("hits", [])
            if not hits:
                break
            for hit in hits:
                value = (hit.get("_source") or {}).get("document_id") or hit.get("_id")
                document_ids.append(uuid.UUID(str(value)))
            if len(hits) < body["size"]:
                break
            body["search_after"] = hits[-1]["sort"]
    finally:
        client.close()
    return document_ids


def materialize_review_batch(db: Session, batch_id: uuid.UUID, settings: Settings) -> ReviewBatch:
    batch = db.scalar(select(ReviewBatch).where(ReviewBatch.id == batch_id).with_for_update())
    if batch is None:
        raise ValueError("Review batch not found")
    if batch.status in {"READY", "ARCHIVED"}:
        return batch
    batch.status = "BUILDING"
    batch.error_message = None
    db.flush()
    matter = db.get(Matter, batch.matter_id)
    if matter is None:
        raise ValueError("Matter not found")
    try:
        if batch.selection_type == "SEARCH_QUERY":
            document_ids = _search_document_ids(db, batch, matter, settings)
        else:
            document_ids = _database_document_ids(db, batch)
        db.execute(delete(ReviewBatchDocument).where(ReviewBatchDocument.review_batch_id == batch.id))
        db.add_all(
            ReviewBatchDocument(
                review_batch_id=batch.id,
                matter_document_id=document_id,
                sequence_number=sequence,
            )
            for sequence, document_id in enumerate(document_ids, start=1)
        )
        batch.document_count = len(document_ids)
        batch.status = "READY"
        batch.completed_at = utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        failed = db.get(ReviewBatch, batch_id)
        if failed is not None:
            failed.status = "FAILED"
            failed.error_message = str(exc)[:4000]
            failed.completed_at = utcnow()
            db.commit()
        raise
    return batch


def refresh_run_document_count(db: Session, run_id: uuid.UUID) -> int:
    from app.models import ReviewBatchRun, ReviewBatchRunDocument

    run = db.get(ReviewBatchRun, run_id)
    if run is None:
        raise ValueError("Review batch run not found")
    run.processed_document_count = int(
        db.scalar(
            select(func.count(ReviewBatchRunDocument.matter_document_id)).where(
                ReviewBatchRunDocument.review_batch_run_id == run.id,
                ReviewBatchRunDocument.status.in_(["COMPLETED", "SKIPPED"]),
            )
        )
        or 0
    )
    return run.processed_document_count
