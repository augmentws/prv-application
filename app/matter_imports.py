import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.artifact_gateway import SelectionBatchItem
from app.database import SessionLocal
from app.models import (
    Custodian,
    MatterDocument,
    MatterDocumentCustodian,
    MatterDocumentImportBatch,
    MatterDocumentImportJob,
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


def process_batch(job_id: uuid.UUID, batch_number: int, items: list[SelectionBatchItem]) -> BatchResult:
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
            )
        batch = existing_batch or MatterDocumentImportBatch(
            job_id=job_id,
            batch_number=batch_number,
            status="QUEUED",
            item_count=len(item_ids),
        )
        db.add(batch)
        if job.status == "CANCELED":
            batch.status = "COMPLETED"
            batch.added_count = 0
            batch.duplicate_count = 0
            db.commit()
            return BatchResult(item_count=len(item_ids), added_count=0, duplicate_count=0)

        documents = {
            document.collection_item_id: document
            for document in db.scalars(
                select(MatterDocument).where(
                    MatterDocument.matter_id == job.matter_id,
                    MatterDocument.collection_item_id.in_(item_ids),
                )
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
        batch.status = "COMPLETED"
        batch.added_count = len(new_ids)
        batch.duplicate_count = len(item_ids) - len(new_ids)
        if new_ids:
            create_search_operation(
                db,
                matter_id=job.matter_id,
                kind="DOCUMENT_UPSERT",
                payload={"document_ids": [str(documents[item_id].id) for item_id in new_ids]},
                created_by_user_id=job.created_by_user_id,
                priority=100,
            )
        db.commit()
        refresh_progress(job_id)
        return BatchResult(
            item_count=len(item_ids),
            added_count=len(new_ids),
            duplicate_count=len(item_ids) - len(new_ids),
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
            ).where(
                MatterDocumentImportBatch.job_id == job_id,
                MatterDocumentImportBatch.status == "COMPLETED",
            )
        ).one()
        job.processed_count = totals[0]
        job.added_count = totals[1]
        job.duplicate_count = totals[2]
        job.failed_count = totals[3]
        db.commit()


def mark_failed(job_id: uuid.UUID, message: str) -> None:
    with SessionLocal() as db:
        job = db.get(MatterDocumentImportJob, job_id)
        if job is not None and job.status != "CANCELED":
            job.status = "FAILED"
            job.error_message = message[:4000]
            job.completed_at = utcnow()
            db.commit()
