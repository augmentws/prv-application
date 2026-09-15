import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.artifact_gateway import (
    find_derived_artifact_reference,
    get_embedding_text_source,
    read_artifact_bytes,
    store_derived_artifact,
)
from app.config import get_settings
from app.database import SessionLocal
from app.embedding_gateway import get_embedding_gateway
from app.embeddings.chunking import semantic_chunks
from app.embeddings.configuration import canonical_hash, derivation_key, processing_configuration
from app.embeddings.parquet import read_chunk_set, write_chunk_set, write_vector_set
from app.models import (
    MatterDocument,
    MatterEmbeddingBatch,
    MatterEmbeddingJob,
    SearchProjectionOperation,
)
from app.search.service import process_search_operation
from embedding_service.config import get_embedding_settings

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class DocumentEmbeddingResult:
    outcome: str
    chunk_count: int


def _scope(job: MatterEmbeddingJob) -> tuple[uuid.UUID, uuid.UUID]:
    return job.matter.client.tenant_id, job.matter.client_id


def _assert_worker_configuration(job: MatterEmbeddingJob) -> None:
    current = processing_configuration(get_settings(), get_embedding_settings())
    if canonical_hash(current) != job.configuration_hash:
        raise RuntimeError(
            "Embedding worker configuration changed after this job was queued; cancel it and start a new job"
        )


def process_document(job: MatterEmbeddingJob, document: MatterDocument) -> DocumentEmbeddingResult:
    settings = get_settings()
    tenant_id, client_id = _scope(job)
    source = get_embedding_text_source(
        collection_item_id=document.collection_item_id,
        actor_user_id=job.created_by_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
        max_bytes=settings.embedding_text_max_bytes,
    )
    if source is None:
        return DocumentEmbeddingResult("SKIPPED", 0)

    chunking = job.configuration["chunking"]
    chunk_key = derivation_key(source_hash=source.content_hash, configuration=chunking)
    chunk_ref = find_derived_artifact_reference(
        collection_item_id=document.collection_item_id,
        artifact_role="CHUNK_SET",
        derivation_key=chunk_key,
        actor_user_id=job.created_by_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    if chunk_ref is None:
        chunks = semantic_chunks(
            source.text,
            source_hash=source.content_hash,
            target_characters=int(chunking["target_characters"]),
            max_characters=int(chunking["max_characters"]),
            overlap_characters=int(chunking["overlap_characters"]),
        )
        if not chunks:
            return DocumentEmbeddingResult("SKIPPED", 0)
        chunk_metadata = {
            "schema_version": 1,
            "chunking": chunking,
            "source_artifact_id": str(source.artifact_id),
            "source_content_hash": source.content_hash,
            "chunk_count": len(chunks),
        }
        chunk_ref, _ = store_derived_artifact(
            collection_item_id=document.collection_item_id,
            content=write_chunk_set(chunks, {"source_content_hash": source.content_hash}),
            artifact_type="CHUNK_SET",
            source_artifact_id=source.artifact_id,
            relationship="CHUNKED_FROM",
            processing_run_id=job.id,
            derivation_key=chunk_key,
            artifact_metadata=chunk_metadata,
            actor_user_id=job.created_by_user_id,
            tenant_id=tenant_id,
            client_id=client_id,
        )
    else:
        chunks = read_chunk_set(
            read_artifact_bytes(
                artifact_id=chunk_ref.artifact_id,
                actor_user_id=job.created_by_user_id,
                tenant_id=tenant_id,
                client_id=client_id,
            )
        )

    embedding = job.configuration["embedding"]
    vector_key = derivation_key(source_hash=chunk_ref.content_hash, configuration=embedding)
    vector_ref = find_derived_artifact_reference(
        collection_item_id=document.collection_item_id,
        artifact_role="CHUNK_VECTOR_SET",
        derivation_key=vector_key,
        actor_user_id=job.created_by_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    if vector_ref is not None:
        return DocumentEmbeddingResult("SKIPPED", len(chunks))

    gateway = get_embedding_gateway()
    vectors: list[list[float]] = []
    for offset in range(0, len(chunks), gateway.settings.max_inputs):
        response = gateway.embed(
            [chunk.text for chunk in chunks[offset : offset + gateway.settings.max_inputs]],
            "document",
        )
        if (
            response.model != job.embedding_model
            or response.model_revision != job.embedding_model_revision
            or response.dimensions != job.embedding_dimensions
            or response.normalized != job.embedding_normalized
        ):
            raise RuntimeError("Embedding response does not match the job's immutable model configuration")
        vectors.extend(response.embeddings)

    vector_metadata = {
        "schema_version": 1,
        "configuration_hash": job.configuration_hash,
        "embedding": embedding,
        "chunk_set_artifact_id": str(chunk_ref.artifact_id),
        "chunk_set_content_hash": chunk_ref.content_hash,
        "chunk_count": len(chunks),
    }
    _, created = store_derived_artifact(
        collection_item_id=document.collection_item_id,
        content=write_vector_set(
            chunks,
            vectors,
            dimensions=job.embedding_dimensions,
            metadata={"model": job.embedding_model, "configuration_hash": job.configuration_hash},
        ),
        artifact_type="CHUNK_VECTOR_SET",
        source_artifact_id=chunk_ref.artifact_id,
        relationship="EMBEDDED_FROM",
        processing_run_id=job.id,
        derivation_key=vector_key,
        artifact_metadata=vector_metadata,
        actor_user_id=job.created_by_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    return DocumentEmbeddingResult("EMBEDDED" if created else "SKIPPED", len(chunks))


def plan_job(job_id: uuid.UUID) -> list[uuid.UUID]:
    with SessionLocal() as db:
        job = db.get(MatterEmbeddingJob, job_id)
        if job is None:
            raise ValueError("Matter embedding job not found")
        if job.status == "CANCELED":
            return []
        existing = list(
            db.scalars(
                select(MatterEmbeddingBatch.id)
                .where(MatterEmbeddingBatch.job_id == job.id)
                .order_by(MatterEmbeddingBatch.batch_number)
            )
        )
        if existing:
            return existing
        _assert_worker_configuration(job)
        job.status = "PLANNING"
        job.started_at = job.started_at or utcnow()
        db.flush()
        batch_size = get_settings().matter_embedding_batch_size
        batch_ids: list[uuid.UUID] = []
        current: list[str] = []
        batch_number = 0
        document_rows = db.scalars(
            select(MatterDocument.id)
            .where(MatterDocument.matter_id == job.matter_id)
            .order_by(MatterDocument.id)
        )
        for document_id in document_rows:
            current.append(str(document_id))
            if len(current) < batch_size:
                continue
            batch = MatterEmbeddingBatch(
                job_id=job.id,
                batch_number=batch_number,
                document_ids=current,
                item_count=len(current),
            )
            db.add(batch)
            db.flush()
            batch_ids.append(batch.id)
            current = []
            batch_number += 1
        if current:
            batch = MatterEmbeddingBatch(
                job_id=job.id,
                batch_number=batch_number,
                document_ids=current,
                item_count=len(current),
            )
            db.add(batch)
            db.flush()
            batch_ids.append(batch.id)
        job.total_count = sum(
            len(value)
            for value in db.scalars(
                select(MatterEmbeddingBatch.document_ids).where(MatterEmbeddingBatch.job_id == job.id)
            )
        )
        job.batch_count = len(batch_ids)
        job.status = "RUNNING"
        db.commit()
        return batch_ids


def _index_documents(job: MatterEmbeddingJob, batch: MatterEmbeddingBatch, document_ids: list[uuid.UUID]) -> None:
    if not document_ids or not get_settings().search_enabled:
        return
    workflow_id = f"embedding-index:{batch.id}"
    with SessionLocal() as db:
        operation = db.scalar(
            select(SearchProjectionOperation).where(SearchProjectionOperation.workflow_id == workflow_id)
        )
        if operation is None:
            operation = SearchProjectionOperation(
                matter_id=job.matter_id,
                kind="DOCUMENT_UPSERT",
                payload={"document_ids": [str(value) for value in document_ids]},
                status="QUEUED",
                workflow_id=workflow_id,
                created_by_user_id=job.created_by_user_id,
            )
            db.add(operation)
            db.commit()
        operation_id = operation.id
    process_search_operation(operation_id)


def process_batch(batch_id: uuid.UUID) -> dict[str, int]:
    with SessionLocal() as db:
        batch = db.get(MatterEmbeddingBatch, batch_id)
        if batch is None:
            raise ValueError("Matter embedding batch not found")
        job = db.get(MatterEmbeddingJob, batch.job_id)
        if job is None:
            raise ValueError("Matter embedding job not found")
        if batch.status == "COMPLETED":
            return {
                "processed_count": batch.processed_count,
                "embedded_count": batch.embedded_count,
                "skipped_count": batch.skipped_count,
                "failed_count": batch.failed_count,
                "chunk_count": batch.chunk_count,
            }
        if job.status == "CANCELED":
            batch.status = "CANCELED"
            db.commit()
            return {key: 0 for key in ("processed_count", "embedded_count", "skipped_count", "failed_count", "chunk_count")}
        _assert_worker_configuration(job)
        batch.status = "RUNNING"
        db.commit()
        logger.info(
            "Starting embedding batch job_id=%s batch_id=%s batch_number=%s documents=%s",
            job.id,
            batch.id,
            batch.batch_number,
            len(batch.document_ids),
        )

        # Load and validate the model before entering the per-document error boundary.
        # A missing or incompatible model is a batch infrastructure failure, not a
        # separate failure for every document in the batch.
        get_embedding_gateway().warmup()

        processed = embedded = skipped = failed = chunk_count = 0
        index_ids: list[uuid.UUID] = []
        errors: list[str] = []
        for value in batch.document_ids:
            document_id = uuid.UUID(value)
            document = db.get(MatterDocument, document_id)
            if document is None or document.matter_id != job.matter_id:
                failed += 1
                processed += 1
                errors.append(f"{document_id}: matter document not found")
                continue
            try:
                result = process_document(job, document)
                embedded += int(result.outcome == "EMBEDDED")
                skipped += int(result.outcome == "SKIPPED")
                chunk_count += result.chunk_count
                if result.chunk_count:
                    index_ids.append(document.id)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                errors.append(f"{document_id}: {exc}")
                logger.warning(
                    "Embedding document failed job_id=%s batch_id=%s document_id=%s error=%s",
                    job.id,
                    batch.id,
                    document_id,
                    exc,
                )
            processed += 1

        _index_documents(job, batch, index_ids)
        batch.status = "COMPLETED"
        batch.processed_count = processed
        batch.embedded_count = embedded
        batch.skipped_count = skipped
        batch.failed_count = failed
        batch.chunk_count = chunk_count
        batch.error_message = "\n".join(errors[:20]) or None
        db.commit()
        logger.info(
            "Completed embedding batch job_id=%s batch_id=%s processed=%s embedded=%s skipped=%s failed=%s chunks=%s",
            job.id,
            batch.id,
            processed,
            embedded,
            skipped,
            failed,
            chunk_count,
        )
        return {
            "processed_count": processed,
            "embedded_count": embedded,
            "skipped_count": skipped,
            "failed_count": failed,
            "chunk_count": chunk_count,
        }


def refresh_job(job_id: uuid.UUID) -> None:
    with SessionLocal() as db:
        job = db.get(MatterEmbeddingJob, job_id)
        if job is None:
            return
        totals = db.execute(
            select(
                func.coalesce(func.sum(MatterEmbeddingBatch.processed_count), 0),
                func.coalesce(func.sum(MatterEmbeddingBatch.embedded_count), 0),
                func.coalesce(func.sum(MatterEmbeddingBatch.skipped_count), 0),
                func.coalesce(func.sum(MatterEmbeddingBatch.failed_count), 0),
                func.coalesce(func.sum(MatterEmbeddingBatch.chunk_count), 0),
            ).where(MatterEmbeddingBatch.job_id == job.id)
        ).one()
        (
            job.processed_count,
            job.embedded_count,
            job.skipped_count,
            job.failed_count,
            job.chunk_count,
        ) = (int(value) for value in totals)
        db.commit()


def complete_job(job_id: uuid.UUID) -> None:
    refresh_job(job_id)
    with SessionLocal() as db:
        job = db.get(MatterEmbeddingJob, job_id)
        if job is not None and job.status != "CANCELED":
            job.status = "COMPLETED_WITH_ERRORS" if job.failed_count else "COMPLETED"
            batch_errors = list(
                db.scalars(
                    select(MatterEmbeddingBatch.error_message)
                    .where(
                        MatterEmbeddingBatch.job_id == job.id,
                        MatterEmbeddingBatch.error_message.is_not(None),
                    )
                    .order_by(MatterEmbeddingBatch.batch_number)
                    .limit(10)
                )
            )
            job.error_message = "\n".join(batch_errors)[:4000] if batch_errors else None
            job.completed_at = utcnow()
            db.commit()


def fail_job(job_id: uuid.UUID, message: str) -> None:
    with SessionLocal() as db:
        job = db.get(MatterEmbeddingJob, job_id)
        if job is not None and job.status != "CANCELED":
            job.status = "FAILED"
            job.error_message = message[:4000]
            job.completed_at = utcnow()
            db.commit()
