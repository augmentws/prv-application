import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.artifact_gateway import SearchItemSnapshot, get_search_item_snapshot, load_current_chunk_artifacts
from app.config import Settings, get_settings
from app.database import SessionLocal
from app.document_metadata import current_metadata_values
from app.embeddings.configuration import canonical_hash, processing_configuration
from app.embeddings.parquet import read_chunk_set, read_vector_set
from app.models import (
    BatchTopic,
    BatchTopicAssignment,
    BatchTopicTaxonomy,
    Custodian,
    DocumentMetadataCurrent,
    Matter,
    MatterDefinitionAssessmentRun,
    MatterDocument,
    MatterDocumentImportJob,
    MatterEmbeddingBatch,
    MatterEmbeddingJob,
    MatterTopicBatch,
    MatterTopicJob,
    MetadataDefinition,
    ReviewBatch,
    ReviewBatchDocument,
    SearchIndexGeneration,
    SearchProjectionOperation,
)
from app.search.client import OpenSearchBulkError, OpenSearchClient
from app.search.mappings import compile_document_index, schema_hash
from app.search.schema import SearchReindexRequired, plan_schema_change
from embedding_service.config import get_embedding_settings

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _save_rebuild_progress(
    operation_id: uuid.UUID | None,
    *,
    phase: str,
    processed_documents: int,
    total_documents: int,
    index_name: str | None = None,
) -> None:
    if operation_id is None:
        return
    try:
        with SessionLocal() as progress_db:
            operation = progress_db.get(SearchProjectionOperation, operation_id)
            if operation is None:
                return
            now = utcnow()
            prior = operation.payload.get("progress")
            prior = prior if isinstance(prior, dict) else {}
            documents_per_second = prior.get("documents_per_second")
            if not isinstance(documents_per_second, (int, float)) or documents_per_second <= 0:
                documents_per_second = None
            prior_processed = prior.get("processed_documents")
            prior_updated_at = prior.get("updated_at")
            if (
                isinstance(prior_processed, int)
                and processed_documents > prior_processed
                and isinstance(prior_updated_at, str)
            ):
                try:
                    prior_time = datetime.fromisoformat(prior_updated_at.replace("Z", "+00:00"))
                    elapsed = (now - prior_time).total_seconds()
                    if elapsed > 0:
                        instantaneous_rate = (processed_documents - prior_processed) / elapsed
                        documents_per_second = (
                            instantaneous_rate
                            if documents_per_second is None
                            else (documents_per_second * 0.7) + (instantaneous_rate * 0.3)
                        )
                except ValueError:
                    pass
            estimated_completion_at = None
            if documents_per_second and total_documents > processed_documents:
                remaining_seconds = (total_documents - processed_documents) / documents_per_second
                estimated_completion_at = (now + timedelta(seconds=remaining_seconds)).isoformat()
            elif total_documents and processed_documents >= total_documents:
                estimated_completion_at = now.isoformat()
            operation.payload = {
                **operation.payload,
                "progress": {
                    "phase": phase,
                    "processed_documents": processed_documents,
                    "total_documents": total_documents,
                    "index_name": index_name,
                    "documents_per_second": documents_per_second,
                    "estimated_completion_at": estimated_completion_at,
                    "updated_at": now.isoformat(),
                },
            }
            progress_db.commit()
    except Exception:
        logger.warning(
            "Could not persist search rebuild progress operation_id=%s phase=%s",
            operation_id,
            phase,
            exc_info=True,
        )


def document_alias(settings: Settings, matter_id: uuid.UUID) -> str:
    return f"{settings.opensearch_index_prefix}-matter-{matter_id.hex}-documents"


def document_index_name(settings: Settings, matter_id: uuid.UUID, generation: int) -> str:
    return f"{document_alias(settings, matter_id)}-v{generation:06d}"


def _metadata_lookup(snapshot: SearchItemSnapshot, *keys: str) -> Any:
    combined = {str(key).casefold(): value for key, value in {**snapshot.unmapped_metadata, **snapshot.raw_metadata}.items()}
    for key in keys:
        value = combined.get(key.casefold())
        if value not in (None, ""):
            return value
    return None


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _index_metadata_value(value: Any, definition: MetadataDefinition) -> Any:
    serialized = _json_value(value)
    if not definition.normalize_to_lowercase:
        return serialized
    if isinstance(serialized, str):
        return serialized.lower()
    if isinstance(serialized, list):
        return [item.lower() if isinstance(item, str) else item for item in serialized]
    return serialized


def build_document_projection(
    db: Session,
    document: MatterDocument,
    definitions: list[MetadataDefinition],
    *,
    batch_ids: list[uuid.UUID] | None = None,
    batch_topics: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    matter = db.get(Matter, document.matter_id)
    if matter is None:
        raise ValueError("Matter not found")
    import_job = db.get(MatterDocumentImportJob, document.added_by_import_job_id)
    if import_job is None:
        raise ValueError("Matter document import job not found")
    snapshot = get_search_item_snapshot(
        collection_item_id=document.collection_item_id,
        actor_user_id=import_job.created_by_user_id,
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
    )
    custodian_names = list(
        db.scalars(select(Custodian.display_name).where(Custodian.id.in_(snapshot.custodian_ids)).order_by(Custodian.display_name))
    )
    recipients = snapshot.email_recipients
    values: dict[str, Any] = {
        "control_number": _metadata_lookup(snapshot, "BEGDOC", "BEGBATES", "CONTROL_NUMBER") or snapshot.source_item_id,
        "original_filename": snapshot.original_filename,
        "document_title": _metadata_lookup(snapshot, "DOCTITLE", "DOCUMENT_TITLE"),
        "family_id": str(snapshot.family_id) if snapshot.family_id else None,
        "custodian": [str(value) for value in snapshot.custodian_ids],
        "source_path": snapshot.original_source_path,
        "email_from": snapshot.email_sender,
        "email_to": recipients["TO"],
        "email_cc": recipients["CC"],
        "email_bcc": recipients["BCC"],
        "email_subject": snapshot.email_subject,
        "email_sent_at": snapshot.email_sent_at,
        "email_received_at": snapshot.email_received_at,
        "file_extension": snapshot.original_extension,
        "file_size": snapshot.native_byte_length,
        "page_count": snapshot.page_count,
        "md5_hash": _metadata_lookup(snapshot, "MD5", "MD5_HASH"),
        "sha256_hash": snapshot.native_sha256,
        "source_created_at": snapshot.source_created_at,
        "source_modified_at": snapshot.source_modified_at,
    }
    values.update(current_metadata_values(db, document.id, definitions))
    indexed_values = {
        definition.key: _index_metadata_value(
            values.get(definition.key, _metadata_lookup(snapshot, definition.key)),
            definition,
        )
        for definition in definitions
        if definition.status == "ACTIVE"
    }
    searchable_metadata = {
        definition.key: indexed_values[definition.key]
        for definition in definitions
        if definition.status == "ACTIVE" and definition.searchable
    }
    embedding_settings = get_embedding_settings()
    configuration_hash = canonical_hash(processing_configuration(get_settings(), embedding_settings))
    chunk_artifacts = load_current_chunk_artifacts(
        collection_item_id=document.collection_item_id,
        configuration_hash=configuration_hash,
        actor_user_id=import_job.created_by_user_id,
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
    )
    chunks: list[dict[str, Any]] = []
    if chunk_artifacts is not None:
        chunk_set, vector_set = chunk_artifacts
        text_chunks = read_chunk_set(chunk_set)
        vectors = read_vector_set(vector_set, dimensions=embedding_settings.dimensions)
        chunks = [
            {
                "chunk_id": chunk.chunk_id,
                "ordinal": chunk.ordinal,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "text": chunk.text,
                "embedding": vectors[chunk.chunk_id],
            }
            for chunk in text_chunks
            if chunk.chunk_id in vectors
        ]
    return {
        "document_id": str(document.id),
        "tenant_id": str(matter.client.tenant_id),
        "client_id": str(matter.client_id),
        "matter_id": str(matter.id),
        "source_collection_id": str(document.source_collection_id),
        "collection_item_id": str(document.collection_item_id),
        "batch_ids": [str(value) for value in (batch_ids or [])],
        "batch_topics": batch_topics or [],
        "created_at": document.created_at.isoformat(),
        "record_type": snapshot.record_type,
        "processing_status": snapshot.processing_status,
        "original_filename": snapshot.original_filename,
        "source_path": snapshot.original_source_path,
        "family_id": str(snapshot.family_id) if snapshot.family_id else None,
        "custodian_ids": [str(value) for value in snapshot.custodian_ids],
        "custodian_names": custodian_names,
        "email_from": indexed_values.get("email_from", snapshot.email_sender),
        "email_to": indexed_values.get("email_to", recipients["TO"]),
        "email_cc": indexed_values.get("email_cc", recipients["CC"]),
        "email_bcc": indexed_values.get("email_bcc", recipients["BCC"]),
        "email_subject": snapshot.email_subject,
        "body_text": snapshot.body_text,
        "chunks": chunks,
        "metadata": searchable_metadata,
    }


class SearchIndexManager:
    def __init__(self, db: Session, client: OpenSearchClient, settings: Settings) -> None:
        self.db = db
        self.client = client
        self.settings = settings

    def active(self, matter_id: uuid.UUID) -> SearchIndexGeneration | None:
        return self.db.scalar(
            select(SearchIndexGeneration).where(
                SearchIndexGeneration.matter_id == matter_id,
                SearchIndexGeneration.status == "ACTIVE",
            )
        )

    def _lock_matter_search(self, matter_id: uuid.UUID) -> None:
        """Serialize search work per matter without locking the domain matter row."""

        bind = self.db.get_bind()
        if bind.dialect.name != "postgresql":
            return
        lock_key = matter_id.int & ((1 << 63) - 1)
        self.db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})

    def _activate_alias(self, generation: SearchIndexGeneration) -> None:
        current_indices = self.client.alias_indices(generation.alias_name)
        if current_indices == [generation.index_name]:
            return
        actions = [
            {"remove": {"index": index_name, "alias": generation.alias_name}}
            for index_name in current_indices
        ]
        actions.append({"add": {"index": generation.index_name, "alias": generation.alias_name}})
        self.client.update_aliases(actions)

    def _cleanup_obsolete_generations(self, active: SearchIndexGeneration) -> None:
        """Remove non-active physical indexes first, then their tracking rows."""

        try:
            generations = list(
                self.db.scalars(
                    select(SearchIndexGeneration).where(
                        SearchIndexGeneration.matter_id == active.matter_id,
                        SearchIndexGeneration.id != active.id,
                    )
                )
            )
            pinned_generation_ids = set(
                self.db.scalars(
                    select(MatterDefinitionAssessmentRun.search_index_generation_id).where(
                        MatterDefinitionAssessmentRun.matter_id == active.matter_id,
                        MatterDefinitionAssessmentRun.search_index_generation_id.is_not(None),
                        MatterDefinitionAssessmentRun.status.not_in(
                            ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"]
                        ),
                    )
                )
            )
            pinned_index_names = {
                generation.index_name for generation in generations if generation.id in pinned_generation_ids
            }
            physical_indices = set(self.client.resolve_indices(f"{active.alias_name}-v*"))
            physical_indices.update(generation.index_name for generation in generations)
            for index_name in sorted(physical_indices):
                if index_name != active.index_name and index_name not in pinned_index_names:
                    self.client.delete_index(index_name)
            generation_by_id = {generation.id: generation for generation in generations}
            batches = list(
                self.db.scalars(
                    select(ReviewBatch).where(ReviewBatch.search_index_generation_id.in_(generation_by_id))
                )
            )
            for batch in batches:
                generation = generation_by_id[batch.search_index_generation_id]
                if "search_index_generation" not in batch.selection_definition:
                    batch.selection_definition = {
                        **batch.selection_definition,
                        "search_index_generation": {
                            "generation": generation.generation,
                            "index_name": generation.index_name,
                            "schema_hash": generation.schema_hash,
                            "activated_at": generation.activated_at.isoformat() if generation.activated_at else None,
                        },
                    }
            for generation in generations:
                if generation.id not in pinned_generation_ids:
                    self.db.delete(generation)
            self.db.commit()
        except Exception:
            self.db.rollback()
            logger.exception(
                "Search generation cleanup failed matter_id=%s active_index=%s",
                active.matter_id,
                active.index_name,
            )

    def ensure(
        self,
        matter_id: uuid.UUID,
        *,
        force: bool = False,
        operation: SearchProjectionOperation | None = None,
    ) -> SearchIndexGeneration:
        self._lock_matter_search(matter_id)
        matter = self.db.get(Matter, matter_id)
        if matter is None:
            raise ValueError("Matter not found")
        definitions = list(
            self.db.scalars(
                select(MetadataDefinition)
                .where(MetadataDefinition.matter_id == matter_id, MetadataDefinition.status == "ACTIVE")
                .order_by(MetadataDefinition.key)
            )
        )
        index_body = compile_document_index(
            definitions,
            embedding_dimensions=get_embedding_settings().dimensions,
        )
        fingerprint = schema_hash(index_body)
        active = self.active(matter_id)
        if active is not None and active.schema_hash == fingerprint and not force:
            self._activate_alias(active)
            self._cleanup_obsolete_generations(active)
            return active

        if active is not None and not force:
            plan = plan_schema_change(active.schema_snapshot, index_body)
            if plan.action in {"NO_CHANGE", "IN_PLACE"}:
                if plan.mapping_update:
                    self.client.update_mapping(active.index_name, plan.mapping_update)
                if plan.metadata_backfill_fields:
                    self._backfill_metadata_fields(
                        active.index_name,
                        matter,
                        definitions,
                        plan.metadata_backfill_fields,
                    )
                active.schema_hash = fingerprint
                active.schema_snapshot = index_body
                active.error_message = None
                self.db.commit()
                self._cleanup_obsolete_generations(active)
                logger.info(
                    "Applied search schema in place matter_id=%s index=%s reasons=%s",
                    matter_id,
                    active.index_name,
                    list(plan.reasons),
                )
                return active
            raise SearchReindexRequired(plan)

        latest = self.db.scalar(
            select(func.max(SearchIndexGeneration.generation)).where(SearchIndexGeneration.matter_id == matter_id)
        )
        generation_number = int(latest or 0) + 1
        index_name = document_index_name(self.settings, matter_id, generation_number)
        while self.client.index_exists(index_name):
            logger.warning(
                "Skipping orphaned search index matter_id=%s index=%s",
                matter_id,
                index_name,
            )
            generation_number += 1
            index_name = document_index_name(self.settings, matter_id, generation_number)
        generation = SearchIndexGeneration(
            matter_id=matter_id,
            generation=generation_number,
            index_name=index_name,
            alias_name=document_alias(self.settings, matter_id),
            schema_hash=fingerprint,
            status="CREATING",
            schema_snapshot=index_body,
        )
        self.db.add(generation)
        self.db.flush()
        total_documents = int(
            self.db.scalar(
                select(func.count()).select_from(MatterDocument).where(MatterDocument.matter_id == matter.id)
            )
            or 0
        )
        operation_id = operation.id if operation is not None else None
        try:
            _save_rebuild_progress(
                operation_id,
                phase="CREATING_INDEX",
                processed_documents=0,
                total_documents=total_documents,
                index_name=generation.index_name,
            )
            self.client.create_index(generation.index_name, index_body)
            count = self._index_all_documents(
                generation.index_name,
                matter,
                definitions,
                operation_id=operation_id,
                total_documents=total_documents,
            )
            _save_rebuild_progress(
                operation_id,
                phase="ACTIVATING_ALIAS",
                processed_documents=count,
                total_documents=total_documents,
                index_name=generation.index_name,
            )
            self._activate_alias(generation)
            if active is not None:
                active.status = "RETIRED"
                self.db.flush()
            generation.status = "ACTIVE"
            generation.document_count = count
            generation.activated_at = utcnow()
            self.db.commit()
            _save_rebuild_progress(
                operation_id,
                phase="CLEANING_UP",
                processed_documents=count,
                total_documents=total_documents,
                index_name=generation.index_name,
            )
            self._cleanup_obsolete_generations(generation)
            _save_rebuild_progress(
                operation_id,
                phase="COMPLETED",
                processed_documents=count,
                total_documents=total_documents,
                index_name=generation.index_name,
            )
            return generation
        except Exception as exc:
            if not self.db.is_active:
                self.db.rollback()
                raise
            generation.status = "FAILED"
            generation.error_message = str(exc)[:4000]
            self.db.commit()
            raise

    def _index_all_documents(
        self,
        index_name: str,
        matter: Matter,
        definitions: list[MetadataDefinition],
        *,
        operation_id: uuid.UUID | None = None,
        total_documents: int | None = None,
    ) -> int:
        total = total_documents if total_documents is not None else int(
            self.db.scalar(
                select(func.count()).select_from(MatterDocument).where(MatterDocument.matter_id == matter.id)
            )
            or 0
        )
        processed = 0
        last_created_at: datetime | None = None
        last_document_id: uuid.UUID | None = None
        while True:
            statement = select(MatterDocument).where(MatterDocument.matter_id == matter.id)
            if last_created_at is not None and last_document_id is not None:
                statement = statement.where(
                    or_(
                        MatterDocument.created_at > last_created_at,
                        and_(
                            MatterDocument.created_at == last_created_at,
                            MatterDocument.id > last_document_id,
                        ),
                    )
                )
            batch = list(
                self.db.scalars(
                    statement.order_by(MatterDocument.created_at, MatterDocument.id).limit(
                        self.settings.search_bulk_batch_size
                    )
                )
            )
            if not batch:
                break
            self._bulk_upsert(index_name, batch, definitions)
            processed += len(batch)
            _save_rebuild_progress(
                operation_id,
                phase="INDEXING_DOCUMENTS",
                processed_documents=processed,
                total_documents=total,
                index_name=index_name,
            )
            last_created_at = batch[-1].created_at
            last_document_id = batch[-1].id
        _save_rebuild_progress(
            operation_id,
            phase="REFRESHING_INDEX",
            processed_documents=processed,
            total_documents=total,
            index_name=index_name,
        )
        self.client.refresh(index_name)
        return processed

    def _backfill_metadata_fields(
        self,
        index_name: str,
        matter: Matter,
        definitions: list[MetadataDefinition],
        field_keys: tuple[str, ...],
    ) -> int:
        definition_ids = [definition.id for definition in definitions if definition.key in field_keys]
        if not definition_ids:
            return 0
        document_ids = list(
            self.db.scalars(
                select(DocumentMetadataCurrent.matter_document_id)
                .where(
                    DocumentMetadataCurrent.matter_id == matter.id,
                    DocumentMetadataCurrent.metadata_definition_id.in_(definition_ids),
                )
                .distinct()
            )
        )
        if not document_ids:
            return 0
        documents = list(
            self.db.scalars(
                select(MatterDocument)
                .where(
                    MatterDocument.matter_id == matter.id,
                    MatterDocument.id.in_(document_ids),
                )
                .order_by(MatterDocument.id)
            )
        )
        self._bulk_upsert(index_name, documents, definitions)
        self.client.refresh(index_name)
        return len(documents)

    def upsert_documents(self, matter_id: uuid.UUID, document_ids: list[uuid.UUID]) -> int:
        generation = self.ensure(matter_id)
        definitions = list(
            self.db.scalars(
                select(MetadataDefinition).where(
                    MetadataDefinition.matter_id == matter_id,
                    MetadataDefinition.status == "ACTIVE",
                )
            )
        )
        documents = list(
            self.db.scalars(
                select(MatterDocument).where(
                    MatterDocument.matter_id == matter_id,
                    MatterDocument.id.in_(document_ids),
                )
            )
        )
        self._bulk_upsert(generation.index_name, documents, definitions)
        self.client.refresh(generation.index_name)
        generation.document_count = self.client.count(generation.index_name)
        self.db.commit()
        return len(documents)

    def upsert_embedding_job(
        self,
        matter_id: uuid.UUID,
        embedding_job_id: uuid.UUID,
        *,
        operation: SearchProjectionOperation | None = None,
    ) -> int:
        generation = self.ensure(matter_id)
        job = self.db.get(MatterEmbeddingJob, embedding_job_id)
        if job is None or job.matter_id != matter_id:
            raise ValueError("Matter embedding job not found")
        definitions = list(
            self.db.scalars(
                select(MetadataDefinition).where(
                    MetadataDefinition.matter_id == matter_id,
                    MetadataDefinition.status == "ACTIVE",
                )
            )
        )
        checkpoint = 0
        retry_document_ids: list[uuid.UUID] = []
        if operation is not None:
            raw_checkpoint = operation.payload.get("next_document_offset", 0)
            if isinstance(raw_checkpoint, int) and raw_checkpoint >= 0:
                checkpoint = raw_checkpoint
            raw_retry_ids = operation.payload.get("retry_document_ids", [])
            if isinstance(raw_retry_ids, list):
                retry_document_ids = [uuid.UUID(value) for value in raw_retry_ids]

        indexed_count = checkpoint
        document_ids: list[uuid.UUID] = []

        def save_progress(*, next_offset: int, failed_ids: list[str] | None = None) -> None:
            if operation is None:
                return
            operation.payload = {
                **operation.payload,
                "next_document_offset": next_offset,
                "retry_document_ids": failed_ids or [],
            }
            self.db.commit()

        def flush(*, advances_checkpoint: bool) -> None:
            nonlocal indexed_count
            if not document_ids:
                return
            attempted_ids = list(document_ids)
            documents = list(
                self.db.scalars(
                    select(MatterDocument).where(
                        MatterDocument.matter_id == matter_id,
                        MatterDocument.id.in_(document_ids),
                    )
                )
            )
            next_offset = indexed_count + len(attempted_ids) if advances_checkpoint else indexed_count
            try:
                self._bulk_upsert(generation.index_name, documents, definitions)
            except OpenSearchBulkError as exc:
                # OpenSearch applies successful items in a partially failed bulk. Move
                # the source cursor past the attempted page and retain only rejected
                # IDs for the next durable retry.
                failed_ids = exc.failed_document_ids or [str(value) for value in attempted_ids]
                save_progress(next_offset=next_offset, failed_ids=failed_ids)
                raise
            if advances_checkpoint:
                indexed_count = next_offset
            save_progress(next_offset=indexed_count)
            document_ids.clear()

        if retry_document_ids:
            document_ids.extend(retry_document_ids)
            flush(advances_checkpoint=False)

        batch_document_ids = self.db.scalars(
            select(MatterEmbeddingBatch.document_ids)
            .where(
                MatterEmbeddingBatch.job_id == job.id,
                MatterEmbeddingBatch.status == "COMPLETED",
            )
            .order_by(MatterEmbeddingBatch.batch_number)
        )
        visited_count = 0
        for batch_ids in batch_document_ids:
            for value in batch_ids:
                if visited_count < checkpoint:
                    visited_count += 1
                    continue
                visited_count += 1
                document_ids.append(uuid.UUID(value))
                if len(document_ids) >= self.settings.search_bulk_batch_size:
                    flush(advances_checkpoint=True)
        flush(advances_checkpoint=True)
        self.client.refresh(generation.index_name)
        generation.document_count = self.client.count(generation.index_name)
        self.db.commit()
        return indexed_count

    def upsert_topic_job(
        self,
        matter_id: uuid.UUID,
        topic_job_id: uuid.UUID,
        *,
        operation: SearchProjectionOperation | None = None,
    ) -> int:
        """Project one topic application's frozen document scope with durable retry checkpoints."""

        generation = self.ensure(matter_id)
        job = self.db.get(MatterTopicJob, topic_job_id)
        if job is None or job.matter_id != matter_id:
            raise ValueError("Matter topic job not found")
        definitions = list(
            self.db.scalars(
                select(MetadataDefinition).where(
                    MetadataDefinition.matter_id == matter_id,
                    MetadataDefinition.status == "ACTIVE",
                )
            )
        )
        checkpoint = 0
        retry_document_ids: list[uuid.UUID] = []
        if operation is not None:
            raw_checkpoint = operation.payload.get("next_document_offset", 0)
            if isinstance(raw_checkpoint, int) and raw_checkpoint >= 0:
                checkpoint = raw_checkpoint
            raw_retry_ids = operation.payload.get("retry_document_ids", [])
            if isinstance(raw_retry_ids, list):
                retry_document_ids = [uuid.UUID(value) for value in raw_retry_ids]

        indexed_count = checkpoint
        document_ids: list[uuid.UUID] = []

        def save_progress(*, next_offset: int, failed_ids: list[str] | None = None) -> None:
            if operation is None:
                return
            operation.payload = {
                **operation.payload,
                "next_document_offset": next_offset,
                "retry_document_ids": failed_ids or [],
            }
            self.db.commit()

        def flush(*, advances_checkpoint: bool) -> None:
            nonlocal indexed_count
            if not document_ids:
                return
            attempted_ids = list(document_ids)
            documents = list(
                self.db.scalars(
                    select(MatterDocument).where(
                        MatterDocument.matter_id == matter_id,
                        MatterDocument.id.in_(attempted_ids),
                    )
                )
            )
            next_offset = indexed_count + len(attempted_ids) if advances_checkpoint else indexed_count
            try:
                self._bulk_upsert(generation.index_name, documents, definitions)
            except OpenSearchBulkError as exc:
                failed_ids = exc.failed_document_ids or [str(value) for value in attempted_ids]
                save_progress(next_offset=next_offset, failed_ids=failed_ids)
                raise
            if advances_checkpoint:
                indexed_count = next_offset
            save_progress(next_offset=indexed_count)
            document_ids.clear()

        if retry_document_ids:
            document_ids.extend(retry_document_ids)
            flush(advances_checkpoint=False)

        batch_document_ids = self.db.scalars(
            select(MatterTopicBatch.document_ids)
            .where(
                MatterTopicBatch.job_id == job.id,
                MatterTopicBatch.status == "COMPLETED",
                MatterTopicBatch.metadata_applied.is_(True),
            )
            .order_by(MatterTopicBatch.batch_number)
        )
        visited_count = 0
        for batch_ids in batch_document_ids:
            for value in batch_ids:
                if visited_count < checkpoint:
                    visited_count += 1
                    continue
                visited_count += 1
                document_ids.append(uuid.UUID(value))
                if len(document_ids) >= self.settings.search_bulk_batch_size:
                    flush(advances_checkpoint=True)
        flush(advances_checkpoint=True)
        self.client.refresh(generation.index_name)
        generation.document_count = self.client.count(generation.index_name)
        self.db.commit()
        return indexed_count

    def delete_documents(self, matter_id: uuid.UUID, document_ids: list[uuid.UUID]) -> int:
        self._lock_matter_search(matter_id)
        generation = self.active(matter_id)
        if generation is None:
            return 0
        self.client.bulk(generation.index_name, (("delete", str(document_id), None) for document_id in document_ids))
        self.client.refresh(generation.index_name)
        generation.document_count = self.client.count(generation.index_name)
        self.db.commit()
        return len(document_ids)

    def _bulk_upsert(
        self,
        index_name: str,
        documents: list[MatterDocument],
        definitions: list[MetadataDefinition],
    ) -> None:
        batch_size = self.settings.search_bulk_batch_size
        for offset in range(0, len(documents), batch_size):
            batch = documents[offset : offset + batch_size]
            memberships: dict[uuid.UUID, list[uuid.UUID]] = {document.id: [] for document in batch}
            rows = self.db.execute(
                select(ReviewBatchDocument.matter_document_id, ReviewBatchDocument.review_batch_id)
                .join(ReviewBatch, ReviewBatch.id == ReviewBatchDocument.review_batch_id)
                .where(
                    ReviewBatchDocument.matter_document_id.in_(memberships),
                    ReviewBatch.status == "READY",
                )
                .order_by(ReviewBatchDocument.review_batch_id)
            ).all()
            for document_id, review_batch_id in rows:
                memberships[document_id].append(review_batch_id)
            batch_topics: dict[uuid.UUID, list[dict[str, str]]] = {document.id: [] for document in batch}
            topic_rows = self.db.execute(
                select(
                    BatchTopicAssignment.matter_document_id,
                    BatchTopicAssignment.review_batch_id,
                    BatchTopicAssignment.taxonomy_id,
                    BatchTopic.topic_key,
                )
                .join(BatchTopicTaxonomy, BatchTopicTaxonomy.id == BatchTopicAssignment.taxonomy_id)
                .join(BatchTopic, BatchTopic.id == BatchTopicAssignment.topic_id)
                .where(
                    BatchTopicAssignment.matter_document_id.in_(batch_topics),
                    BatchTopicTaxonomy.status == "ACTIVE",
                )
                .order_by(BatchTopicAssignment.review_batch_id, BatchTopic.ordinal)
            ).all()
            for document_id, review_batch_id, taxonomy_id, topic_key in topic_rows:
                batch_topics[document_id].append(
                    {
                        "batch_id": str(review_batch_id),
                        "taxonomy_id": str(taxonomy_id),
                        "topic_key": topic_key,
                    }
                )
            bind = self.db.get_bind()
            worker_count = min(self.settings.search_projection_document_concurrency, len(batch))
            if bind.dialect.name == "sqlite":
                # The local/test SQLite configuration uses one shared connection,
                # which cannot safely serve multiple sessions at the same time.
                worker_count = 1
            if worker_count <= 1:
                operations = [
                    (
                        "index",
                        str(document.id),
                        build_document_projection(
                            self.db,
                            document,
                            definitions,
                            batch_ids=memberships[document.id],
                            batch_topics=batch_topics[document.id],
                        ),
                    )
                    for document in batch
                ]
            else:
                projection_session = sessionmaker(
                    bind=bind,
                    autoflush=False,
                    expire_on_commit=False,
                )

                work_items = [
                    (document.id, memberships[document.id], batch_topics[document.id])
                    for document in batch
                ]

                def build_operation(
                    item: tuple[uuid.UUID, list[uuid.UUID], list[dict[str, str]]],
                    session_factory=projection_session,
                ) -> tuple[str, str, dict[str, Any]]:
                    document_id, document_batch_ids, document_batch_topics = item
                    with session_factory() as worker_db:
                        document = worker_db.get(MatterDocument, document_id)
                        if document is None:
                            raise ValueError("Matter document disappeared during search projection")
                        projection = build_document_projection(
                            worker_db,
                            document,
                            definitions,
                            batch_ids=document_batch_ids,
                            batch_topics=document_batch_topics,
                        )
                        return "index", str(document_id), projection

                with ThreadPoolExecutor(
                    max_workers=worker_count,
                    thread_name_prefix="search-projection",
                ) as executor:
                    operations = list(executor.map(build_operation, work_items))
            self.client.bulk(index_name, operations)


def sync_review_batch_search(batch_id: uuid.UUID, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    with SessionLocal() as db:
        batch = db.get(ReviewBatch, batch_id)
        if batch is None:
            raise ValueError("Review batch not found")
        if batch.status != "READY":
            raise ValueError("Review batch membership is not ready")
        if not settings.search_enabled:
            batch.search_status = "NOT_CONFIGURED"
            batch.search_error_message = None
            db.commit()
            return
        batch.search_status = "SYNCING"
        batch.search_error_message = None
        db.commit()
        client = OpenSearchClient(settings)
        try:
            manager = SearchIndexManager(db, client, settings)
            generation = manager.ensure(batch.matter_id)
            manager._lock_matter_search(batch.matter_id)
            document_ids = list(
                db.scalars(
                    select(ReviewBatchDocument.matter_document_id)
                    .where(ReviewBatchDocument.review_batch_id == batch.id)
                    .order_by(ReviewBatchDocument.sequence_number)
                )
            )
            active_taxonomy = db.scalar(
                select(BatchTopicTaxonomy).where(
                    BatchTopicTaxonomy.review_batch_id == batch.id,
                    BatchTopicTaxonomy.status == "ACTIVE",
                )
            )
            topics_by_document: dict[uuid.UUID, list[dict[str, str]]] = {
                document_id: [] for document_id in document_ids
            }
            if active_taxonomy is not None:
                rows = db.execute(
                    select(BatchTopicAssignment.matter_document_id, BatchTopic.topic_key)
                    .join(BatchTopic, BatchTopic.id == BatchTopicAssignment.topic_id)
                    .where(BatchTopicAssignment.taxonomy_id == active_taxonomy.id)
                    .order_by(BatchTopic.ordinal)
                ).all()
                for document_id, topic_key in rows:
                    topics_by_document[document_id].append(
                        {
                            "batch_id": str(batch.id),
                            "taxonomy_id": str(active_taxonomy.id),
                            "topic_key": topic_key,
                        }
                    )
            for offset in range(0, len(document_ids), settings.search_bulk_batch_size):
                page = document_ids[offset : offset + settings.search_bulk_batch_size]
                client.bulk(
                    generation.index_name,
                    (
                        (
                            "update",
                            str(document_id),
                            {
                                "script": {
                                    "source": (
                                        "if (ctx._source.batch_ids == null) { ctx._source.batch_ids = []; } "
                                        "if (!ctx._source.batch_ids.contains(params.batch_id)) { "
                                        "ctx._source.batch_ids.add(params.batch_id); } "
                                        "if (ctx._source.batch_topics == null) { ctx._source.batch_topics = []; } "
                                        "for (int i = ctx._source.batch_topics.size() - 1; i >= 0; i--) { "
                                        "if (ctx._source.batch_topics[i].batch_id == params.batch_id) { "
                                        "ctx._source.batch_topics.remove(i); } } "
                                        "ctx._source.batch_topics.addAll(params.topics);"
                                    ),
                                    "params": {
                                        "batch_id": str(batch.id),
                                        "topics": topics_by_document[document_id],
                                    },
                                }
                            },
                        )
                        for document_id in page
                    ),
                )
            client.refresh(generation.index_name)
            batch.search_status = "READY"
            batch.search_error_message = None
            db.commit()
            logger.info(
                "Completed review batch search projection batch_id=%s matter_id=%s documents=%s",
                batch.id,
                batch.matter_id,
                len(document_ids),
            )
        except Exception as exc:
            db.rollback()
            failed = db.get(ReviewBatch, batch_id)
            if failed is not None:
                failed.search_status = "FAILED"
                failed.search_error_message = str(exc)[:4000]
                db.commit()
            logger.exception("Review batch search projection failed batch_id=%s", batch_id)
            raise
        finally:
            client.close()


def process_search_operation(operation_id: uuid.UUID) -> None:
    with SessionLocal() as db:
        operation = db.get(SearchProjectionOperation, operation_id)
        if operation is None:
            raise ValueError("Search projection operation not found")
        if operation.status == "COMPLETED":
            return
        operation.status = "RUNNING"
        operation.started_at = operation.started_at or utcnow()
        operation.attempt_count += 1
        db.commit()
        logger.info(
            "Starting search projection operation_id=%s kind=%s matter_id=%s",
            operation.id,
            operation.kind,
            operation.matter_id,
        )
        settings = get_settings()
        client = OpenSearchClient(settings)
        try:
            manager = SearchIndexManager(db, client, settings)
            if operation.kind in {"SCHEMA_SYNC", "REBUILD"}:
                manager.ensure(
                    operation.matter_id,
                    force=operation.kind == "REBUILD",
                    operation=operation if operation.kind == "REBUILD" else None,
                )
            elif operation.kind == "DOCUMENT_UPSERT":
                topic_job_id = operation.payload.get("topic_job_id")
                embedding_job_id = operation.payload.get("embedding_job_id")
                if topic_job_id:
                    manager.upsert_topic_job(
                        operation.matter_id,
                        uuid.UUID(topic_job_id),
                        operation=operation,
                    )
                elif embedding_job_id:
                    manager.upsert_embedding_job(
                        operation.matter_id,
                        uuid.UUID(embedding_job_id),
                        operation=operation,
                    )
                else:
                    manager.upsert_documents(
                        operation.matter_id,
                        [uuid.UUID(value) for value in operation.payload.get("document_ids", [])],
                    )
            elif operation.kind == "DOCUMENT_DELETE":
                manager.delete_documents(
                    operation.matter_id,
                    [uuid.UUID(value) for value in operation.payload.get("document_ids", [])],
                )
            else:
                raise ValueError(f"Unsupported search projection operation: {operation.kind}")
            operation.status = "COMPLETED"
            operation.completed_at = utcnow()
            operation.error_message = None
            db.commit()
            logger.info(
                "Completed search projection operation_id=%s kind=%s matter_id=%s",
                operation.id,
                operation.kind,
                operation.matter_id,
            )
        except SearchReindexRequired as exc:
            operation.status = "AWAITING_USER"
            operation.payload = {**operation.payload, "schema_change": exc.plan.as_dict()}
            operation.error_message = None
            db.commit()
            logger.info(
                "Search projection requires confirmation operation_id=%s matter_id=%s reasons=%s",
                operation.id,
                operation.matter_id,
                list(exc.plan.reasons),
            )
        except Exception as exc:
            operation.status = "FAILED"
            operation.error_message = str(exc)[:4000]
            db.commit()
            logger.exception(
                "Search projection failed operation_id=%s kind=%s matter_id=%s",
                operation.id,
                operation.kind,
                operation.matter_id,
            )
            raise
        finally:
            client.close()
