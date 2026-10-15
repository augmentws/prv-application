import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from app.artifact_gateway import SelectionBatchItem
from app.audit import record_audit
from app.database import SessionLocal
from app.document_metadata import apply_imported_metadata_value
from app.job_executions import (
    MATTER_DOCUMENT_IMPORT_JOB_TYPE,
    JobExecutionConflict,
    JobExecutionNotFound,
    execution_cancel_requested,
    transition_job_execution,
)
from app.models import (
    Custodian,
    MatterDocument,
    MatterDocumentCustodian,
    MatterDocumentImportBatch,
    MatterDocumentImportJob,
    MetadataDefinition,
)
from app.search.operations import create_search_operation


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class BatchResult:
    item_count: int
    added_count: int
    duplicate_count: int
    failed_count: int = 0
    imported_label_document_count: int = 0
    imported_label_event_count: int = 0
    imported_label_warning_count: int = 0


def metadata_definition_snapshot(definition: MetadataDefinition) -> dict[str, Any]:
    return {
        "id": str(definition.id),
        "key": definition.key,
        "type": definition.type,
        "cardinality": definition.cardinality,
        "allowed_values": definition.allowed_values,
        "assertion_policy": definition.assertion_policy,
        "resolution_policy": definition.resolution_policy,
        "normalize_to_lowercase": definition.normalize_to_lowercase,
        "hierarchy_separator": definition.hierarchy_separator,
        "searchable": definition.searchable,
    }


def _import_definitions(
    db,
    *,
    matter_id: uuid.UUID,
    snapshots: list[dict[str, Any]] | None,
) -> dict[str, MetadataDefinition]:
    statement = select(MetadataDefinition).where(
        MetadataDefinition.matter_id == matter_id,
        MetadataDefinition.status == "ACTIVE",
        MetadataDefinition.value_source == "ASSERTED",
    )
    if snapshots is not None:
        definition_ids = [uuid.UUID(str(snapshot["id"])) for snapshot in snapshots]
        if not definition_ids:
            return {}
        statement = statement.where(MetadataDefinition.id.in_(definition_ids))
    definitions = list(db.scalars(statement))
    by_key = {definition.key.casefold(): definition for definition in definitions}
    if snapshots is not None:
        expected = {str(snapshot["key"]).casefold(): snapshot for snapshot in snapshots}
        actual = {
            key: metadata_definition_snapshot(definition)
            for key, definition in by_key.items()
        }
        if actual != expected:
            raise ValueError("Matter metadata definitions changed while documents were being published")
    return by_key


def process_batch(
    job_id: uuid.UUID,
    batch_number: int,
    items: list[SelectionBatchItem],
    definition_snapshots: list[dict[str, Any]] | None = None,
) -> BatchResult:
    item_ids = [item.item_id for item in items]
    with SessionLocal() as db:
        job = db.get(MatterDocumentImportJob, job_id)
        if job is None:
            raise ValueError("Matter document import job not found")
        existing_batch = db.scalar(
            select(MatterDocumentImportBatch).where(
                MatterDocumentImportBatch.job_id == job_id,
                MatterDocumentImportBatch.batch_number == batch_number,
            )
        )
        if existing_batch is not None and existing_batch.status == "COMPLETED":
            return BatchResult(
                item_count=existing_batch.item_count,
                added_count=existing_batch.added_count,
                duplicate_count=existing_batch.duplicate_count,
                failed_count=existing_batch.failed_count,
                imported_label_document_count=existing_batch.imported_label_document_count,
                imported_label_event_count=existing_batch.imported_label_event_count,
                imported_label_warning_count=existing_batch.imported_label_warning_count,
            )
        batch = existing_batch or MatterDocumentImportBatch(
            job_id=job_id,
            batch_number=batch_number,
            status="QUEUED",
            item_count=len(item_ids),
        )
        db.add(batch)
        if job.status == "CANCELED" or execution_cancel_requested(
            db,
            job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
            job_id=job.id,
        ):
            batch.status = "COMPLETED"
            batch.added_count = 0
            batch.duplicate_count = 0
            db.commit()
            return BatchResult(item_count=len(item_ids), added_count=0, duplicate_count=0)

        definitions = _import_definitions(
            db,
            matter_id=job.matter_id,
            snapshots=definition_snapshots,
        )

        documents = {
            document.collection_item_id: document
            for document in db.scalars(
                select(MatterDocument).where(
                    MatterDocument.matter_id == job.matter_id,
                    MatterDocument.collection_item_id.in_(item_ids),
                ).with_for_update()
            )
        }
        new_ids = [item_id for item_id in item_ids if item_id not in documents]
        for item_id in new_ids:
            document = MatterDocument(
                matter_id=job.matter_id,
                source_collection_id=job.source_collection_id,
                collection_item_id=item_id,
                added_by_import_job_id=job.id,
            )
            db.add(document)
            documents[item_id] = document
        db.flush()

        referenced_custodian_ids = {
            custodian.custodian_id
            for item in items
            for custodian in item.custodians
        }
        known_custodian_ids = set(
            db.scalars(
                select(Custodian.id).where(
                    Custodian.client_id == job.matter.client_id,
                    Custodian.id.in_(referenced_custodian_ids),
                )
            )
        )
        if unknown_ids := referenced_custodian_ids - known_custodian_ids:
            raise ValueError(f"Collection items reference unknown client custodians: {sorted(map(str, unknown_ids))}")
        existing_links = set(
            db.execute(
                select(MatterDocumentCustodian.matter_document_id, MatterDocumentCustodian.custodian_id).where(
                    MatterDocumentCustodian.matter_document_id.in_([document.id for document in documents.values()])
                )
            ).all()
        )
        for item in items:
            document = documents[item.item_id]
            for custodian in item.custodians:
                if (document.id, custodian.custodian_id) not in existing_links:
                    db.add(
                        MatterDocumentCustodian(
                            matter_document_id=document.id,
                            custodian_id=custodian.custodian_id,
                            relationship_type=custodian.relationship_type,
                        )
                    )
        imported_document_ids: set[uuid.UUID] = set()
        imported_event_count = 0
        warning_messages: list[str] = []
        metadata_changed_document_ids: set[uuid.UUID] = set()
        for item in items:
            document = documents[item.item_id]
            for source_key, raw_value in item.unmapped_metadata.items():
                definition = definitions.get(source_key.casefold())
                if definition is None:
                    continue
                application = apply_imported_metadata_value(
                    db,
                    document=document,
                    definition=definition,
                    raw_value=raw_value,
                    actor_id=job.created_by_user_id,
                    collection_item_id=item.item_id,
                )
                warning_messages.extend(
                    f"{document.id}: {message}" for message in application.warnings
                )
                if not application.events:
                    continue
                imported_document_ids.add(document.id)
                imported_event_count += len(application.events)
                metadata_changed_document_ids.add(document.id)
                for event in application.events:
                    record_audit(
                        db,
                        tenant_id=job.matter.client.tenant_id,
                        actor_user_id=job.created_by_user_id,
                        action="document.metadata_imported",
                        target_type="metadata_event",
                        target_id=event.id,
                        details={
                            "matter_id": str(job.matter_id),
                            "document_id": str(document.id),
                            "metadata_definition_id": str(definition.id),
                            "field_key": definition.key,
                            "collection_item_id": str(item.item_id),
                            "matter_document_import_job_id": str(job.id),
                            "operation": event.operation,
                        },
                    )
        batch.status = "COMPLETED"
        batch.added_count = len(new_ids)
        batch.duplicate_count = len(item_ids) - len(new_ids)
        batch.imported_label_document_count = len(imported_document_ids)
        batch.imported_label_event_count = imported_event_count
        batch.imported_label_warning_count = len(warning_messages)
        batch.error_message = "\n".join(warning_messages[:20]) or None
        search_document_ids = {
            documents[item_id].id for item_id in new_ids
        } | metadata_changed_document_ids
        if search_document_ids:
            create_search_operation(
                db,
                matter_id=job.matter_id,
                kind="DOCUMENT_UPSERT",
                payload={"document_ids": sorted(str(document_id) for document_id in search_document_ids)},
                created_by_user_id=job.created_by_user_id,
                priority=100,
            )
        db.commit()
        refresh_progress(job_id)
        return BatchResult(
            item_count=len(item_ids),
            added_count=len(new_ids),
            duplicate_count=len(item_ids) - len(new_ids),
            imported_label_document_count=len(imported_document_ids),
            imported_label_event_count=imported_event_count,
            imported_label_warning_count=len(warning_messages),
        )


def refresh_progress(job_id: uuid.UUID) -> None:
    with SessionLocal() as db:
        job = db.get(MatterDocumentImportJob, job_id)
        if job is None or job.status == "CANCELED":
            return
        totals = db.execute(
            select(
                func.coalesce(func.sum(MatterDocumentImportBatch.item_count), 0),
                func.coalesce(func.sum(MatterDocumentImportBatch.added_count), 0),
                func.coalesce(func.sum(MatterDocumentImportBatch.duplicate_count), 0),
                func.coalesce(func.sum(MatterDocumentImportBatch.failed_count), 0),
                func.coalesce(func.sum(MatterDocumentImportBatch.imported_label_document_count), 0),
                func.coalesce(func.sum(MatterDocumentImportBatch.imported_label_event_count), 0),
                func.coalesce(func.sum(MatterDocumentImportBatch.imported_label_warning_count), 0),
            ).where(
                MatterDocumentImportBatch.job_id == job_id,
                MatterDocumentImportBatch.status == "COMPLETED",
            )
        ).one()
        job.processed_count = totals[0]
        job.added_count = totals[1]
        job.duplicate_count = totals[2]
        job.failed_count = totals[3]
        job.imported_label_document_count = totals[4]
        job.imported_label_event_count = totals[5]
        job.imported_label_warning_count = totals[6]
        try:
            transition_job_execution(
                db,
                job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
                job_id=job.id,
                status="RUNNING",
                current_stage="PROCESSING_BATCHES",
                progress_completed=job.processed_count,
                progress_total=job.matched_count or None,
                progress={
                    "batch_count": job.batch_count,
                    "added_count": job.added_count,
                    "duplicate_count": job.duplicate_count,
                    "failed_count": job.failed_count,
                    "imported_label_document_count": job.imported_label_document_count,
                    "imported_label_event_count": job.imported_label_event_count,
                    "imported_label_warning_count": job.imported_label_warning_count,
                },
            )
        except JobExecutionNotFound:
            pass
        except JobExecutionConflict:
            if job.status != "CANCELED":
                raise
        db.commit()


def mark_failed(job_id: uuid.UUID, message: str) -> None:
    with SessionLocal() as db:
        job = db.get(MatterDocumentImportJob, job_id)
        if job is not None and job.status != "CANCELED":
            try:
                transition_job_execution(
                    db,
                    job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
                    job_id=job.id,
                    status="FAILED",
                    current_stage="FAILED",
                    progress_completed=job.processed_count,
                    progress_total=job.matched_count or None,
                    progress={
                        "batch_count": job.batch_count,
                        "added_count": job.added_count,
                        "duplicate_count": job.duplicate_count,
                        "failed_count": job.failed_count,
                        "imported_label_document_count": job.imported_label_document_count,
                        "imported_label_event_count": job.imported_label_event_count,
                        "imported_label_warning_count": job.imported_label_warning_count,
                    },
                    failure_code="MATTER_DOCUMENT_IMPORT_FAILED",
                    failure_summary=message,
                    failure_retriable=False,
                )
            except JobExecutionNotFound:
                job.status = "FAILED"
                job.error_message = message[:4000]
                job.completed_at = utcnow()
            db.commit()
