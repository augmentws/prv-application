import json
import logging
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

from app.artifact_gateway import (
    DerivedArtifactReference,
    find_derived_artifact_reference,
    get_embedding_text_source,
    read_artifact_bytes,
    store_derived_artifact,
)
from app.config import get_settings
from app.database import SessionLocal
from app.embedding_gateway import get_embedding_gateway
from app.embeddings.chunking import TextChunk, semantic_chunks
from app.embeddings.configuration import (
    canonical_hash,
    derivation_key,
    embedding_execution_configuration,
    processing_configuration,
)
from app.embeddings.parquet import read_chunk_set, write_chunk_set, write_vector_set
from app.models import (
    MatterDocument,
    MatterEmbeddingBatch,
    MatterEmbeddingJob,
    SearchProjectionOperation,
)
from app.provider_usage import record_external_provider_usage
from app.search.service import process_search_operation
from app.voyage_batch import VoyageBatchClient
from embedding_service.config import get_embedding_settings

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class DocumentEmbeddingResult:
    outcome: str
    chunk_count: int


@dataclass(frozen=True)
class PreparedDocumentEmbedding:
    collection_item_id: uuid.UUID
    chunks: list[TextChunk]
    chunk_ref: DerivedArtifactReference
    vector_key: str


@dataclass(frozen=True)
class EmbeddingJobRuntime:
    id: uuid.UUID
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    created_by_user_id: uuid.UUID
    configuration: dict
    configuration_hash: str
    embedding_model: str
    embedding_model_revision: str | None
    embedding_dimensions: int
    embedding_normalized: bool


EmbeddingProgressCallback = Callable[[str, int, int], None]


@dataclass
class EmbeddingProcessingMetrics:
    document_count: int
    preparation_seconds: float = 0.0
    inference_document_count: int = 0
    inference_chunk_count: int = 0
    inference_request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    inference_seconds: float = 0.0
    persistence_seconds: float = 0.0
    total_seconds: float = 0.0


EmbeddingMetricsCallback = Callable[[EmbeddingProcessingMetrics], None]


def _scope(job: MatterEmbeddingJob) -> tuple[uuid.UUID, uuid.UUID]:
    return job.matter.client.tenant_id, job.matter.client_id


def _runtime(job: MatterEmbeddingJob) -> EmbeddingJobRuntime:
    tenant_id, client_id = _scope(job)
    return EmbeddingJobRuntime(
        id=job.id,
        tenant_id=tenant_id,
        client_id=client_id,
        created_by_user_id=job.created_by_user_id,
        configuration=job.configuration,
        configuration_hash=job.configuration_hash,
        embedding_model=job.embedding_model,
        embedding_model_revision=job.embedding_model_revision,
        embedding_dimensions=job.embedding_dimensions,
        embedding_normalized=job.embedding_normalized,
    )


def _assert_worker_configuration(job: MatterEmbeddingJob) -> None:
    embedding_settings = get_embedding_settings()
    current = processing_configuration(get_settings(), embedding_settings)
    current_execution = embedding_execution_configuration(embedding_settings)
    frozen_execution = job.configuration.get("execution")
    execution_matches = (
        current_execution.get("mode") == "synchronous"
        if frozen_execution is None
        else frozen_execution == current_execution
    )
    if canonical_hash(current) != job.configuration_hash or not execution_matches:
        raise RuntimeError(
            "Embedding worker configuration changed after this job was queued; cancel it and start a new job"
        )


def _prepare_document(
    job: EmbeddingJobRuntime,
    collection_item_id: uuid.UUID,
) -> DocumentEmbeddingResult | PreparedDocumentEmbedding:
    settings = get_settings()
    source = get_embedding_text_source(
        collection_item_id=collection_item_id,
        actor_user_id=job.created_by_user_id,
        tenant_id=job.tenant_id,
        client_id=job.client_id,
        max_bytes=settings.embedding_text_max_bytes,
    )
    if source is None:
        return DocumentEmbeddingResult("SKIPPED", 0)

    chunking = job.configuration["chunking"]
    chunk_key = derivation_key(source_hash=source.content_hash, configuration=chunking)
    chunk_ref = find_derived_artifact_reference(
        collection_item_id=collection_item_id,
        artifact_role="CHUNK_SET",
        derivation_key=chunk_key,
        actor_user_id=job.created_by_user_id,
        tenant_id=job.tenant_id,
        client_id=job.client_id,
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
            collection_item_id=collection_item_id,
            content=write_chunk_set(chunks, {"source_content_hash": source.content_hash}),
            artifact_type="CHUNK_SET",
            source_artifact_id=source.artifact_id,
            relationship="CHUNKED_FROM",
            processing_run_id=job.id,
            derivation_key=chunk_key,
            artifact_metadata=chunk_metadata,
            actor_user_id=job.created_by_user_id,
            tenant_id=job.tenant_id,
            client_id=job.client_id,
        )
    else:
        chunks = read_chunk_set(
            read_artifact_bytes(
                artifact_id=chunk_ref.artifact_id,
                actor_user_id=job.created_by_user_id,
                tenant_id=job.tenant_id,
                client_id=job.client_id,
            )
        )

    embedding = job.configuration["embedding"]
    vector_key = derivation_key(source_hash=chunk_ref.content_hash, configuration=embedding)
    vector_ref = find_derived_artifact_reference(
        collection_item_id=collection_item_id,
        artifact_role="CHUNK_VECTOR_SET",
        derivation_key=vector_key,
        actor_user_id=job.created_by_user_id,
        tenant_id=job.tenant_id,
        client_id=job.client_id,
    )
    if vector_ref is not None:
        return DocumentEmbeddingResult("SKIPPED", len(chunks))

    return PreparedDocumentEmbedding(
        collection_item_id=collection_item_id,
        chunks=chunks,
        chunk_ref=chunk_ref,
        vector_key=vector_key,
    )


def _validate_embedding_response(job: EmbeddingJobRuntime, response) -> None:
    if (
        response.model != job.embedding_model
        or response.model_revision != job.embedding_model_revision
        or response.dimensions != job.embedding_dimensions
        or response.normalized != job.embedding_normalized
    ):
        raise RuntimeError("Embedding response does not match the job's immutable model configuration")


def _embed_prepared_documents(
    job: EmbeddingJobRuntime,
    prepared_documents: Sequence[PreparedDocumentEmbedding],
    gateway,
    executor: ThreadPoolExecutor,
    metrics: EmbeddingProcessingMetrics,
    result_callback: Callable[[int, DocumentEmbeddingResult | Exception], None] | None = None,
) -> list[DocumentEmbeddingResult | Exception]:
    positions = [
        (document_index, chunk)
        for document_index, prepared in enumerate(prepared_documents)
        for chunk in prepared.chunks
    ]
    vectors_by_document: list[list[list[float]]] = [[] for _ in prepared_documents]
    for offset in range(0, len(positions), gateway.settings.max_inputs):
        page = positions[offset : offset + gateway.settings.max_inputs]
        inference_started = time.perf_counter()
        try:
            response = gateway.embed([chunk.text for _, chunk in page], "document")
        finally:
            metrics.inference_seconds += time.perf_counter() - inference_started
            metrics.inference_request_count += 1
            metrics.inference_chunk_count += len(page)
        _validate_embedding_response(job, response)
        metrics.input_tokens += response.input_tokens
        metrics.output_tokens += response.output_tokens
        if len(response.embeddings) != len(page):
            raise RuntimeError("Embedding response count does not match the submitted chunk count")
        for (document_index, _), vector in zip(
            page,
            response.embeddings,
            strict=True,
        ):
            vectors_by_document[document_index].append(vector)

    vector_metadata = {
        "schema_version": 1,
        "configuration_hash": job.configuration_hash,
        "embedding": job.configuration["embedding"],
    }

    results: list[DocumentEmbeddingResult | Exception | None] = [None for _ in prepared_documents]
    metrics.inference_document_count += len(prepared_documents)
    persistence_started = time.perf_counter()
    futures = {
        executor.submit(
            _persist_prepared_document,
            job,
            prepared,
            vectors,
            vector_metadata,
        ): document_index
        for document_index, (prepared, vectors) in enumerate(zip(prepared_documents, vectors_by_document, strict=True))
    }
    for future in as_completed(futures):
        document_index = futures[future]
        try:
            outcome: DocumentEmbeddingResult | Exception = future.result()
        except Exception as exc:  # noqa: BLE001
            outcome = exc
        results[document_index] = outcome
        if result_callback is not None:
            result_callback(document_index, outcome)
    metrics.persistence_seconds += time.perf_counter() - persistence_started
    if any(result is None for result in results):
        raise RuntimeError("Embedding persistence produced an incomplete result")
    return [result for result in results if result is not None]


def _persist_prepared_document(
    job: EmbeddingJobRuntime,
    prepared: PreparedDocumentEmbedding,
    vectors: list[list[float]],
    vector_metadata: dict | None = None,
) -> DocumentEmbeddingResult:
    if len(vectors) != len(prepared.chunks):
        raise RuntimeError("Embedding vector count does not match the document chunk count")
    if any(len(vector) != job.embedding_dimensions for vector in vectors):
        raise RuntimeError("Embedding vector dimensions do not match the job configuration")
    metadata = vector_metadata or {
        "schema_version": 1,
        "configuration_hash": job.configuration_hash,
        "embedding": job.configuration["embedding"],
    }
    _, created = store_derived_artifact(
        collection_item_id=prepared.collection_item_id,
        content=write_vector_set(
            prepared.chunks,
            vectors,
            dimensions=job.embedding_dimensions,
            metadata={
                "model": job.embedding_model,
                "configuration_hash": job.configuration_hash,
            },
        ),
        artifact_type="CHUNK_VECTOR_SET",
        source_artifact_id=prepared.chunk_ref.artifact_id,
        relationship="EMBEDDED_FROM",
        processing_run_id=job.id,
        derivation_key=prepared.vector_key,
        artifact_metadata={
            **metadata,
            "chunk_set_artifact_id": str(prepared.chunk_ref.artifact_id),
            "chunk_set_content_hash": prepared.chunk_ref.content_hash,
            "chunk_count": len(prepared.chunks),
        },
        actor_user_id=job.created_by_user_id,
        tenant_id=job.tenant_id,
        client_id=job.client_id,
    )
    return DocumentEmbeddingResult(
        "EMBEDDED" if created else "SKIPPED",
        len(prepared.chunks),
    )


def process_documents(
    job: MatterEmbeddingJob,
    documents: Sequence[MatterDocument],
    *,
    progress_callback: EmbeddingProgressCallback | None = None,
    metrics_callback: EmbeddingMetricsCallback | None = None,
) -> list[DocumentEmbeddingResult | Exception]:
    """Prepare documents independently and embed their chunks in shared batches."""

    metrics = EmbeddingProcessingMetrics(document_count=len(documents))
    total_started = time.perf_counter()
    if not documents:
        metrics.total_seconds = time.perf_counter() - total_started
        if metrics_callback is not None:
            metrics_callback(metrics)
        return []
    runtime = _runtime(job)
    settings = get_settings()
    outcomes: list[DocumentEmbeddingResult | Exception | None] = [None for _ in documents]
    gateway = get_embedding_gateway()
    pending: list[tuple[int, PreparedDocumentEmbedding]] = []
    pending_chunk_count = 0
    completed_count = 0

    def report(stage: str, current: int) -> None:
        if progress_callback is not None:
            progress_callback(stage, current, len(documents))

    def record_outcome(
        document_index: int,
        outcome: DocumentEmbeddingResult | Exception,
    ) -> None:
        nonlocal completed_count
        outcomes[document_index] = outcome
        completed_count += 1
        report("completed", completed_count)

    def flush(executor: ThreadPoolExecutor) -> None:
        nonlocal pending_chunk_count
        if not pending:
            return
        current = list(pending)

        def persisted(
            local_index: int,
            outcome: DocumentEmbeddingResult | Exception,
        ) -> None:
            record_outcome(current[local_index][0], outcome)

        _embed_prepared_documents(
            runtime,
            [prepared for _, prepared in pending],
            gateway,
            executor,
            metrics,
            persisted,
        )
        pending.clear()
        pending_chunk_count = 0

    worker_count = min(settings.matter_embedding_document_concurrency, len(documents))
    prepared: list[PreparedDocumentEmbedding | None] = [None for _ in documents]
    preparation_started = time.perf_counter()
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="matter-embedding-document",
    ) as executor:
        futures = {
            executor.submit(
                _prepare_document,
                runtime,
                document.collection_item_id,
            ): document_index
            for document_index, document in enumerate(documents)
        }
        for prepared_count, future in enumerate(as_completed(futures), start=1):
            document_index = futures[future]
            try:
                prepared_or_result: DocumentEmbeddingResult | PreparedDocumentEmbedding | Exception = future.result()
            except Exception as exc:  # noqa: BLE001
                prepared_or_result = exc
            report("prepared", prepared_count)
            if isinstance(prepared_or_result, PreparedDocumentEmbedding):
                prepared[document_index] = prepared_or_result
            else:
                record_outcome(document_index, prepared_or_result)
        metrics.preparation_seconds = time.perf_counter() - preparation_started

        for document_index, prepared_document in enumerate(prepared):
            if prepared_document is None:
                continue
            document_chunk_count = len(prepared_document.chunks)
            if pending and pending_chunk_count + document_chunk_count > gateway.settings.max_inputs:
                flush(executor)
            pending.append((document_index, prepared_document))
            pending_chunk_count += document_chunk_count
            if pending_chunk_count >= gateway.settings.max_inputs:
                flush(executor)
        flush(executor)
    if any(outcome is None for outcome in outcomes):
        raise RuntimeError("Embedding document processing produced an incomplete result")
    metrics.total_seconds = time.perf_counter() - total_started
    if metrics_callback is not None:
        metrics_callback(metrics)
    return [outcome for outcome in outcomes if outcome is not None]


def process_document(
    job: MatterEmbeddingJob,
    document: MatterDocument,
) -> DocumentEmbeddingResult:
    outcome = process_documents(job, [document])[0]
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


def uses_voyage_batch(job_id: uuid.UUID) -> bool:
    with SessionLocal() as db:
        job = db.get(MatterEmbeddingJob, job_id)
        if job is None:
            raise ValueError("Matter embedding job not found")
        return (job.configuration.get("execution") or {}).get("mode") == "voyage_batch"


def embedding_execution(job_id: uuid.UUID) -> dict:
    with SessionLocal() as db:
        job = db.get(MatterEmbeddingJob, job_id)
        if job is None:
            raise ValueError("Matter embedding job not found")
        return dict(job.configuration.get("execution") or {"mode": "synchronous"})


def _get_voyage_batch_client() -> VoyageBatchClient:
    return VoyageBatchClient(get_embedding_settings())


def _prepare_voyage_batch(
    job: MatterEmbeddingJob,
    batch: MatterEmbeddingBatch,
    documents: Sequence[MatterDocument | None],
) -> tuple[dict, list[dict]]:
    runtime = _runtime(job)
    settings = get_settings()
    embedding_settings = get_embedding_settings()
    request_size = min(
        embedding_settings.max_inputs,
        embedding_settings.voyage_batch_request_size,
    )
    prepared_outcomes: list[DocumentEmbeddingResult | PreparedDocumentEmbedding | Exception | None] = [
        None for _ in documents
    ]
    available = [(index, document) for index, document in enumerate(documents) if document is not None]
    if available:
        with ThreadPoolExecutor(
            max_workers=min(
                settings.matter_embedding_document_concurrency,
                len(available),
            ),
            thread_name_prefix="matter-embedding-voyage-prepare",
        ) as executor:
            futures = {
                executor.submit(
                    _prepare_document,
                    runtime,
                    document.collection_item_id,
                ): document_index
                for document_index, document in available
            }
            for future in as_completed(futures):
                document_index = futures[future]
                try:
                    prepared_outcomes[document_index] = future.result()
                except Exception as exc:  # noqa: BLE001
                    prepared_outcomes[document_index] = exc

    manifest_documents: list[dict] = []
    requests: list[dict] = []
    for document_index, (document_id_value, document, outcome) in enumerate(
        zip(batch.document_ids, documents, prepared_outcomes, strict=True)
    ):
        if document is None:
            manifest_documents.append(
                {
                    "document_id": document_id_value,
                    "state": "FAILED",
                    "error": "matter document not found",
                    "chunk_count": 0,
                }
            )
            continue
        if isinstance(outcome, DocumentEmbeddingResult):
            manifest_documents.append(
                {
                    "document_id": str(document.id),
                    "collection_item_id": str(document.collection_item_id),
                    "state": outcome.outcome,
                    "chunk_count": outcome.chunk_count,
                }
            )
            continue
        if isinstance(outcome, Exception) or outcome is None:
            manifest_documents.append(
                {
                    "document_id": str(document.id),
                    "collection_item_id": str(document.collection_item_id),
                    "state": "FAILED",
                    "error": str(outcome or "document preparation produced no result")[:2000],
                    "chunk_count": 0,
                }
            )
            continue

        request_manifest: list[dict] = []
        for page_number, start in enumerate(range(0, len(outcome.chunks), request_size)):
            end = min(start + request_size, len(outcome.chunks))
            custom_id = f"document-{document_index:06d}-page-{page_number:06d}"
            requests.append(
                {
                    "custom_id": custom_id,
                    "body": {"input": [chunk.text for chunk in outcome.chunks[start:end]]},
                }
            )
            request_manifest.append({"custom_id": custom_id, "start": start, "end": end})
        manifest_documents.append(
            {
                "document_id": str(document.id),
                "collection_item_id": str(document.collection_item_id),
                "state": "PENDING",
                "chunk_count": len(outcome.chunks),
                "chunk_artifact_id": str(outcome.chunk_ref.artifact_id),
                "chunk_content_hash": outcome.chunk_ref.content_hash,
                "vector_key": outcome.vector_key,
                "requests": request_manifest,
            }
        )
    if len(requests) > 100_000:
        raise RuntimeError("Voyage batch exceeds the 100,000-request provider limit")
    return (
        {
            "schema_version": 1,
            "request_size": request_size,
            "documents": manifest_documents,
        },
        requests,
    )


def _manifest_counts(manifest: dict) -> tuple[int, int, int, int, int, list[str]]:
    processed = embedded = skipped = failed = chunk_count = 0
    errors: list[str] = []
    for document in manifest.get("documents", []):
        state = document.get("state")
        if state == "PENDING":
            continue
        processed += 1
        if state == "EMBEDDED":
            embedded += 1
        elif state == "SKIPPED":
            skipped += 1
            chunk_count += int(document.get("chunk_count", 0))
        else:
            failed += 1
            errors.append(
                f"{document.get('document_id', 'unknown')}: {document.get('error', 'document preparation failed')}"
            )
    return processed, embedded, skipped, failed, chunk_count, errors


def submit_voyage_batch(batch_id: uuid.UUID) -> dict[str, str | int | None]:
    with SessionLocal() as db:
        batch = db.get(MatterEmbeddingBatch, batch_id)
        if batch is None:
            raise ValueError("Matter embedding batch not found")
        job = db.get(MatterEmbeddingJob, batch.job_id)
        if job is None:
            raise ValueError("Matter embedding job not found")
        if batch.status == "COMPLETED":
            return {
                "status": "completed",
                "provider_batch_id": batch.provider_batch_id,
                "request_count": batch.provider_request_count,
            }
        if job.status == "CANCELED":
            batch.status = "CANCELED"
            db.commit()
            return {"status": "cancelled", "provider_batch_id": None, "request_count": 0}
        _assert_worker_configuration(job)
        if (job.configuration.get("execution") or {}).get("mode") != "voyage_batch":
            raise RuntimeError("Embedding job is not configured for Voyage Batch API execution")
        if batch.provider_batch_id:
            return {
                "status": batch.provider_status or "submitted",
                "provider_batch_id": batch.provider_batch_id,
                "request_count": batch.provider_request_count,
            }

        batch.status = "RUNNING"
        db.commit()
        documents: list[MatterDocument | None] = []
        for value in batch.document_ids:
            document = db.get(MatterDocument, uuid.UUID(value))
            if document is not None and document.matter_id != job.matter_id:
                document = None
            documents.append(document)

        logger.info(
            "Preparing Voyage embedding batch job_id=%s batch_id=%s batch_number=%s documents=%s",
            job.id,
            batch.id,
            batch.batch_number,
            len(batch.document_ids),
        )
        manifest, requests = _prepare_voyage_batch(job, batch, documents)
        batch.provider_manifest = manifest
        batch.provider_request_count = len(requests)
        db.commit()

        if not requests:
            processed, embedded, skipped, failed, chunk_count, errors = _manifest_counts(manifest)
            batch.status = "COMPLETED"
            batch.processed_count = processed
            batch.embedded_count = embedded
            batch.skipped_count = skipped
            batch.failed_count = failed
            batch.chunk_count = chunk_count
            batch.error_message = "\n".join(errors[:20]) or None
            batch.provider_status = "not_required"
            db.commit()
            return {"status": "not_required", "provider_batch_id": None, "request_count": 0}

        with tempfile.TemporaryDirectory(prefix="priv-view-voyage-") as directory:
            request_path = Path(directory) / f"priv-view-{batch.id}.jsonl"
            with request_path.open("w", encoding="utf-8") as handle:
                for request in requests:
                    handle.write(json.dumps(request, ensure_ascii=False) + "\n")
            submission = _get_voyage_batch_client().submit_reconciled(
                request_path,
                metadata={
                    "priv_view_batch_id": str(batch.id),
                    "priv_view_job_id": str(job.id),
                    "priv_view_configuration": job.configuration_hash,
                },
            )
        input_file = submission["input_file"]
        provider_batch = submission["batch"]
        batch.provider_input_file_id = str(input_file["id"])
        batch.provider_batch_id = str(provider_batch["id"])
        batch.provider_status = str(provider_batch.get("status") or "validating")
        batch.provider_output_file_id = provider_batch.get("output_file_id")
        batch.provider_error_file_id = provider_batch.get("error_file_id")
        batch.provider_last_polled_at = utcnow()
        db.commit()
        logger.info(
            "Submitted Voyage embedding batch job_id=%s batch_id=%s provider_batch_id=%s requests=%s",
            job.id,
            batch.id,
            batch.provider_batch_id,
            len(requests),
        )
        return {
            "status": batch.provider_status,
            "provider_batch_id": batch.provider_batch_id,
            "request_count": len(requests),
        }


def poll_voyage_batch(batch_id: uuid.UUID) -> str:
    with SessionLocal() as db:
        batch = db.get(MatterEmbeddingBatch, batch_id)
        if batch is None:
            raise ValueError("Matter embedding batch not found")
        job = db.get(MatterEmbeddingJob, batch.job_id)
        if job is None:
            raise ValueError("Matter embedding job not found")
        if batch.status == "COMPLETED":
            return "completed"
        if not batch.provider_batch_id:
            if batch.provider_status == "not_required":
                return "completed"
            raise RuntimeError("Voyage batch has not been submitted")
        provider_batch = _get_voyage_batch_client().get_batch(batch.provider_batch_id)
        provider_status = str(provider_batch.get("status") or "unknown")
        batch.provider_status = provider_status
        batch.provider_output_file_id = provider_batch.get("output_file_id")
        batch.provider_error_file_id = provider_batch.get("error_file_id")
        batch.provider_last_polled_at = utcnow()
        request_counts = provider_batch.get("request_counts") or {}
        db.commit()
        logger.info(
            "Voyage embedding batch status job_id=%s batch_id=%s provider_batch_id=%s "
            "status=%s completed_requests=%s failed_requests=%s total_requests=%s",
            job.id,
            batch.id,
            batch.provider_batch_id,
            provider_status,
            request_counts.get("completed", 0),
            request_counts.get("failed", 0),
            request_counts.get("total", batch.provider_request_count),
        )
        if provider_status == "failed":
            batch.status = "FAILED"
            batch.error_message = f"Voyage batch {batch.provider_batch_id} failed validation or execution"
            db.commit()
            raise RuntimeError(batch.error_message)
        if provider_status == "cancelled":
            batch.status = "CANCELED" if job.status == "CANCELED" else "FAILED"
            batch.error_message = None if job.status == "CANCELED" else "Voyage batch was cancelled"
            db.commit()
            if job.status != "CANCELED":
                raise RuntimeError(batch.error_message)
        return provider_status


def _read_voyage_results(
    path: Path,
    *,
    expected_model: str,
    expected_dimensions: int,
) -> tuple[dict[str, list[list[float]]], dict[str, str], int]:
    vectors: dict[str, list[list[float]]] = {}
    errors: dict[str, str] = {}
    total_tokens = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid Voyage result JSON at line {line_number}") from exc
            custom_id = record.get("custom_id")
            if not isinstance(custom_id, str) or not custom_id:
                raise RuntimeError(f"Voyage result line {line_number} has no custom_id")
            if custom_id in vectors or custom_id in errors:
                raise RuntimeError(f"Voyage returned duplicate result {custom_id}")
            response = record.get("response")
            if record.get("error") is not None or not isinstance(response, dict):
                errors[custom_id] = str(record.get("error") or "missing response")
                continue
            if response.get("status_code") != 200 or not isinstance(response.get("body"), dict):
                errors[custom_id] = str(response.get("message") or response)
                continue
            body = response["body"]
            if body.get("model") not in {None, expected_model}:
                errors[custom_id] = f"unexpected response model {body.get('model')}"
                continue
            rows = body.get("data")
            if not isinstance(rows, list):
                errors[custom_id] = "response contains no embedding rows"
                continue
            try:
                ordered = sorted(rows, key=lambda row: int(row["index"]))
                page_vectors = [row["embedding"] for row in ordered]
            except (KeyError, TypeError, ValueError) as exc:
                errors[custom_id] = f"malformed embedding rows: {exc}"
                continue
            if [int(row["index"]) for row in ordered] != list(range(len(ordered))):
                errors[custom_id] = "embedding row indexes are incomplete"
                continue
            if any(not isinstance(vector, list) or len(vector) != expected_dimensions for vector in page_vectors):
                errors[custom_id] = "embedding dimensions do not match the job"
                continue
            vectors[custom_id] = page_vectors
            usage = body.get("usage") or {}
            if isinstance(usage.get("total_tokens"), int):
                total_tokens += usage["total_tokens"]
    return vectors, errors, total_tokens


def _read_voyage_errors(path: Path) -> dict[str, str]:
    errors: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid Voyage error JSON at line {line_number}") from exc
            custom_id = record.get("custom_id")
            if not isinstance(custom_id, str) or not custom_id:
                raise RuntimeError(f"Voyage error line {line_number} has no custom_id")
            errors[custom_id] = str(record.get("error") or record.get("response") or "request failed")
    return errors


def finalize_voyage_batch(batch_id: uuid.UUID) -> dict[str, int]:
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
        if job.status == "CANCELED" or batch.status == "CANCELED":
            return {
                key: 0 for key in ("processed_count", "embedded_count", "skipped_count", "failed_count", "chunk_count")
            }
        if batch.provider_status not in {"completed", "partially_completed"}:
            raise RuntimeError(f"Voyage batch is not ready: {batch.provider_status}")
        if not isinstance(batch.provider_manifest, dict):
            raise TypeError("Voyage batch manifest is missing")
        if not batch.provider_output_file_id:
            raise RuntimeError("Completed Voyage batch has no output file")

        client = _get_voyage_batch_client()
        with tempfile.TemporaryDirectory(prefix="priv-view-voyage-results-") as directory:
            output_path = Path(directory) / "output.jsonl"
            client.download_file(batch.provider_output_file_id, output_path)
            vectors_by_request, request_errors, total_tokens = _read_voyage_results(
                output_path,
                expected_model=job.embedding_model,
                expected_dimensions=job.embedding_dimensions,
            )
            if batch.provider_error_file_id:
                error_path = Path(directory) / "errors.jsonl"
                client.download_file(batch.provider_error_file_id, error_path)
                request_errors.update(_read_voyage_errors(error_path))

        runtime = _runtime(job)
        processed, embedded, skipped, failed, chunk_count, errors = _manifest_counts(batch.provider_manifest)
        pending: list[tuple[dict, PreparedDocumentEmbedding, list[list[float]]]] = []
        for document in batch.provider_manifest.get("documents", []):
            if document.get("state") != "PENDING":
                continue
            processed += 1
            custom_ids = [request["custom_id"] for request in document.get("requests", [])]
            document_errors = [request_errors[custom_id] for custom_id in custom_ids if custom_id in request_errors]
            missing = [custom_id for custom_id in custom_ids if custom_id not in vectors_by_request]
            if document_errors or missing:
                failed += 1
                detail = document_errors[0] if document_errors else f"missing results: {', '.join(missing[:3])}"
                errors.append(f"{document['document_id']}: {detail}")
                continue
            vectors = [vector for custom_id in custom_ids for vector in vectors_by_request[custom_id]]
            chunk_ref = DerivedArtifactReference(
                artifact_id=uuid.UUID(document["chunk_artifact_id"]),
                content_hash=document["chunk_content_hash"],
                derivation_key="",
                metadata={},
            )
            chunks = read_chunk_set(
                read_artifact_bytes(
                    artifact_id=chunk_ref.artifact_id,
                    actor_user_id=runtime.created_by_user_id,
                    tenant_id=runtime.tenant_id,
                    client_id=runtime.client_id,
                )
            )
            if len(chunks) != int(document["chunk_count"]) or len(vectors) != len(chunks):
                failed += 1
                errors.append(f"{document['document_id']}: provider result count does not match chunks")
                continue
            pending.append(
                (
                    document,
                    PreparedDocumentEmbedding(
                        collection_item_id=uuid.UUID(document["collection_item_id"]),
                        chunks=chunks,
                        chunk_ref=chunk_ref,
                        vector_key=document["vector_key"],
                    ),
                    vectors,
                )
            )

        if pending:
            with ThreadPoolExecutor(
                max_workers=min(get_settings().matter_embedding_document_concurrency, len(pending)),
                thread_name_prefix="matter-embedding-voyage-persist",
            ) as executor:
                futures = {
                    executor.submit(_persist_prepared_document, runtime, prepared, vectors): document
                    for document, prepared, vectors in pending
                }
                for future in as_completed(futures):
                    document = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        errors.append(f"{document['document_id']}: {exc}")
                        continue
                    embedded += int(result.outcome == "EMBEDDED")
                    skipped += int(result.outcome == "SKIPPED")
                    chunk_count += result.chunk_count

        batch.status = "COMPLETED"
        batch.processed_count = processed
        batch.embedded_count = embedded
        batch.skipped_count = skipped
        batch.failed_count = failed
        batch.chunk_count = chunk_count
        batch.error_message = "\n".join(errors[:20]) or None
        record_external_provider_usage(
            db,
            idempotency_key=f"matter-embedding-batch:{batch.id}:provider-usage",
            tenant_id=runtime.tenant_id,
            client_id=runtime.client_id,
            matter_id=job.matter_id,
            started_by_user_id=job.created_by_user_id,
            job_type="MATTER_EMBEDDING",
            job_id=job.id,
            job_created_at=job.created_at,
            provider="voyage",
            model=job.embedding_model,
            request_count=batch.provider_request_count,
            input_tokens=total_tokens,
            output_tokens=0,
            details={
                "execution_mode": "batch",
                "embedding_batch_id": str(batch.id),
                "provider_batch_id": batch.provider_batch_id,
                "batch_number": batch.batch_number,
            },
        )
        db.commit()
        logger.info(
            "Completed Voyage embedding batch job_id=%s batch_id=%s provider_batch_id=%s "
            "processed=%s embedded=%s skipped=%s failed=%s chunks=%s tokens=%s",
            job.id,
            batch.id,
            batch.provider_batch_id,
            processed,
            embedded,
            skipped,
            failed,
            chunk_count,
            total_tokens,
        )
        return {
            "processed_count": processed,
            "embedded_count": embedded,
            "skipped_count": skipped,
            "failed_count": failed,
            "chunk_count": chunk_count,
        }


def cancel_voyage_batches(job_id: uuid.UUID, *, db=None) -> None:
    def provider_ids(session) -> list[str]:
        return list(
            session.scalars(
                select(MatterEmbeddingBatch.provider_batch_id).where(
                    MatterEmbeddingBatch.job_id == job_id,
                    MatterEmbeddingBatch.provider_batch_id.is_not(None),
                    MatterEmbeddingBatch.provider_status.in_(("validating", "in_progress", "finalizing")),
                )
            )
        )

    if db is None:
        with SessionLocal() as session:
            batch_ids = provider_ids(session)
    else:
        batch_ids = provider_ids(db)
    if not batch_ids:
        return
    client = _get_voyage_batch_client()
    for provider_batch_id in batch_ids:
        try:
            client.cancel_batch(provider_batch_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Voyage batch cancellation failed job_id=%s provider_batch_id=%s error=%s",
                job_id,
                provider_batch_id,
                exc,
            )


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
            job.status = "RUNNING"
            job.started_at = job.started_at or utcnow()
            job.completed_at = None
            db.commit()
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
            select(MatterDocument.id).where(MatterDocument.matter_id == job.matter_id).order_by(MatterDocument.id)
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


def index_job(job_id: uuid.UUID) -> str:
    if not get_settings().search_enabled:
        return "NOT_CONFIGURED"
    with SessionLocal() as db:
        job = db.get(MatterEmbeddingJob, job_id)
        if job is None:
            raise ValueError("Matter embedding job not found")
        workflow_id = f"embedding-index-job:{job.id}"
        operation = db.scalar(
            select(SearchProjectionOperation).where(SearchProjectionOperation.workflow_id == workflow_id)
        )
        if operation is None:
            operation = SearchProjectionOperation(
                matter_id=job.matter_id,
                kind="DOCUMENT_UPSERT",
                payload={"embedding_job_id": str(job.id)},
                status="QUEUED",
                workflow_id=workflow_id,
                created_by_user_id=job.created_by_user_id,
            )
            db.add(operation)
            db.commit()
        if operation.status in {"COMPLETED", "AWAITING_USER"}:
            return operation.status
        operation_id = operation.id
    process_search_operation(operation_id)
    with SessionLocal() as db:
        operation = db.get(SearchProjectionOperation, operation_id)
        if operation is None:
            raise RuntimeError("Embedding search projection operation disappeared")
        return operation.status


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
            return {
                key: 0 for key in ("processed_count", "embedded_count", "skipped_count", "failed_count", "chunk_count")
            }
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
        errors: list[str] = []
        documents: list[MatterDocument] = []
        for value in batch.document_ids:
            document_id = uuid.UUID(value)
            document = db.get(MatterDocument, document_id)
            if document is None or document.matter_id != job.matter_id:
                failed += 1
                processed += 1
                errors.append(f"{document_id}: matter document not found")
                continue
            documents.append(document)

        def log_progress(stage: str, current: int, total: int) -> None:
            if current % 25 != 0 and current != total:
                return
            logger.info(
                "Embedding batch progress job_id=%s batch_id=%s batch_number=%s "
                "stage=%s documents=%s/%s concurrency=%s",
                job.id,
                batch.id,
                batch.batch_number,
                stage,
                current,
                total,
                get_settings().matter_embedding_document_concurrency,
            )

        batch_metrics: EmbeddingProcessingMetrics | None = None

        def log_metrics(metrics: EmbeddingProcessingMetrics) -> None:
            nonlocal batch_metrics
            batch_metrics = metrics
            preparation_rate = (
                metrics.document_count / metrics.preparation_seconds if metrics.preparation_seconds else 0.0
            )
            inference_rate = (
                metrics.inference_chunk_count / metrics.inference_seconds if metrics.inference_seconds else 0.0
            )
            persistence_rate = (
                metrics.inference_document_count / metrics.persistence_seconds if metrics.persistence_seconds else 0.0
            )
            total_rate = metrics.document_count / metrics.total_seconds if metrics.total_seconds else 0.0
            logger.info(
                "Embedding batch timing job_id=%s batch_id=%s batch_number=%s documents=%s "
                "preparation_seconds=%.3f preparation_documents_per_second=%.2f "
                "inference_documents=%s inference_chunks=%s inference_requests=%s "
                "inference_seconds=%.3f inference_chunks_per_second=%.2f "
                "persistence_seconds=%.3f persistence_documents_per_second=%.2f "
                "total_seconds=%.3f total_documents_per_second=%.2f",
                job.id,
                batch.id,
                batch.batch_number,
                metrics.document_count,
                metrics.preparation_seconds,
                preparation_rate,
                metrics.inference_document_count,
                metrics.inference_chunk_count,
                metrics.inference_request_count,
                metrics.inference_seconds,
                inference_rate,
                metrics.persistence_seconds,
                persistence_rate,
                metrics.total_seconds,
                total_rate,
            )

        outcomes = process_documents(
            job,
            documents,
            progress_callback=log_progress,
            metrics_callback=log_metrics,
        )
        for document, outcome in zip(documents, outcomes, strict=True):
            if isinstance(outcome, DocumentEmbeddingResult):
                result = outcome
                embedded += int(result.outcome == "EMBEDDED")
                skipped += int(result.outcome == "SKIPPED")
                chunk_count += result.chunk_count
            else:
                exc = outcome
                failed += 1
                errors.append(f"{document.id}: {exc}")
                logger.warning(
                    "Embedding document failed job_id=%s batch_id=%s document_id=%s error=%s",
                    job.id,
                    batch.id,
                    document.id,
                    exc,
                )
            processed += 1

        batch.status = "COMPLETED"
        batch.processed_count = processed
        batch.embedded_count = embedded
        batch.skipped_count = skipped
        batch.failed_count = failed
        batch.chunk_count = chunk_count
        batch.error_message = "\n".join(errors[:20]) or None
        embedding_provider = (job.configuration.get("embedding") or {}).get("provider")
        if embedding_provider == "voyage_api" and batch_metrics is not None and batch_metrics.inference_request_count:
            tenant_id, client_id = _scope(job)
            record_external_provider_usage(
                db,
                idempotency_key=f"matter-embedding-batch:{batch.id}:provider-usage",
                tenant_id=tenant_id,
                client_id=client_id,
                matter_id=job.matter_id,
                started_by_user_id=job.created_by_user_id,
                job_type="MATTER_EMBEDDING",
                job_id=job.id,
                job_created_at=job.created_at,
                provider="voyage",
                model=job.embedding_model,
                request_count=batch_metrics.inference_request_count,
                input_tokens=batch_metrics.input_tokens,
                output_tokens=batch_metrics.output_tokens,
                details={
                    "execution_mode": "realtime",
                    "embedding_batch_id": str(batch.id),
                    "batch_number": batch.batch_number,
                },
            )
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
            projection = db.scalar(
                select(SearchProjectionOperation).where(
                    SearchProjectionOperation.workflow_id == f"embedding-index-job:{job.id}"
                )
            )
            if projection is not None and projection.error_message:
                message = f"Search indexing failed: {projection.error_message}"
            job.status = "FAILED"
            job.error_message = message[:4000]
            job.completed_at = utcnow()
            db.commit()
