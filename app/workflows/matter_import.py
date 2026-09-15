import math
import uuid
from datetime import datetime, timezone

from dbos import DBOS, Queue, SetWorkflowID

from app.artifact_gateway import (
    SelectionBatchItem,
    SelectionCustodian,
    create_selection,
    remove_selection,
    selection_batch,
)
from app.config import get_settings
from app.database import SessionLocal
from app.matter_imports import mark_failed, process_batch, refresh_progress
from app.models import MatterDocumentImportJob

PLAN_QUEUE = Queue("matter-import-plans", global_concurrency=2)
BATCH_QUEUE = Queue("matter-import-batches", global_concurrency=8)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@DBOS.step(name="freeze_matter_import_selection", retries_allowed=True, max_attempts=5)
def freeze_selection(job_id: str) -> dict[str, object]:
    job_uuid = uuid.UUID(job_id)
    with SessionLocal() as db:
        job = db.get(MatterDocumentImportJob, job_uuid)
        if job is None:
            raise ValueError("Matter document import job not found")
        if job.status == "CANCELED":
            return {"canceled": True}
        job.status = "SNAPSHOTTING"
        job.started_at = job.started_at or utcnow()
        db.commit()
        actor_id = job.created_by_user_id
        tenant_id = job.matter.client.tenant_id
        client_id = job.matter.client_id
        collection_id = job.source_collection_id
        selection = job.selection

    selection_id, total_count = create_selection(
        collection_id=collection_id,
        request_id=job_uuid,
        selection=selection,
        actor_user_id=actor_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    canceled = False
    with SessionLocal() as db:
        job = db.get(MatterDocumentImportJob, job_uuid)
        if job is None:
            raise ValueError("Matter document import job not found")
        if job.status == "CANCELED":
            canceled = True
        else:
            job.artifact_selection_id = selection_id
            job.matched_count = total_count
            job.batch_count = math.ceil(total_count / get_settings().matter_import_batch_size)
            job.status = "RUNNING"
        db.commit()
    if canceled:
        remove_selection(
            selection_id=selection_id,
            actor_user_id=actor_id,
            tenant_id=tenant_id,
            client_id=client_id,
        )
        return {"canceled": True}
    return {
        "canceled": False,
        "selection_id": str(selection_id),
        "total_count": total_count,
        "actor_id": str(actor_id),
        "tenant_id": str(tenant_id),
        "client_id": str(client_id),
    }


@DBOS.step(name="load_matter_import_batch", retries_allowed=True, max_attempts=5)
def load_batch(context: dict[str, object], offset: int, limit: int) -> list[dict[str, object]]:
    return [
        {
            "item_id": str(item.item_id),
            "custodians": [
                {
                    "custodian_id": str(custodian.custodian_id),
                    "relationship_type": custodian.relationship_type,
                }
                for custodian in item.custodians
            ],
        }
        for item in selection_batch(
            selection_id=uuid.UUID(str(context["selection_id"])),
            offset=offset,
            limit=limit,
            actor_user_id=uuid.UUID(str(context["actor_id"])),
            tenant_id=uuid.UUID(str(context["tenant_id"])),
            client_id=uuid.UUID(str(context["client_id"])),
        )
    ]


@DBOS.step(name="remove_matter_import_selection", retries_allowed=True, max_attempts=5)
def cleanup_selection(job_id: str, context: dict[str, object]) -> None:
    remove_selection(
        selection_id=uuid.UUID(str(context["selection_id"])),
        actor_user_id=uuid.UUID(str(context["actor_id"])),
        tenant_id=uuid.UUID(str(context["tenant_id"])),
        client_id=uuid.UUID(str(context["client_id"])),
    )
    with SessionLocal() as db:
        job = db.get(MatterDocumentImportJob, uuid.UUID(job_id))
        if job is not None:
            job.artifact_selection_id = None
            db.commit()


@DBOS.step(name="process_matter_import_batch", retries_allowed=True, max_attempts=5)
def persist_batch(job_id: str, batch_number: int, items: list[dict[str, object]]) -> dict[str, int]:
    batch_items = [
        SelectionBatchItem(
            item_id=uuid.UUID(str(item["item_id"])),
            custodians=[
                SelectionCustodian(
                    custodian_id=uuid.UUID(str(custodian["custodian_id"])),
                    relationship_type=str(custodian["relationship_type"]),
                )
                for custodian in item["custodians"]  # type: ignore[union-attr]
            ],
        )
        for item in items
    ]
    result = process_batch(uuid.UUID(job_id), batch_number, batch_items)
    return {
        "item_count": result.item_count,
        "added_count": result.added_count,
        "duplicate_count": result.duplicate_count,
        "failed_count": result.failed_count,
    }


@DBOS.workflow(name="matter_document_import_batch")
def matter_document_import_batch(job_id: str, batch_number: int, items: list[dict[str, object]]) -> dict[str, int]:
    return persist_batch(job_id, batch_number, items)


@DBOS.step(name="complete_matter_import")
def complete_import(job_id: str) -> None:
    job_uuid = uuid.UUID(job_id)
    refresh_progress(job_uuid)
    with SessionLocal() as db:
        job = db.get(MatterDocumentImportJob, job_uuid)
        if job is not None and job.status != "CANCELED":
            job.status = "COMPLETED"
            job.processed_count = job.matched_count
            job.completed_at = utcnow()
            job.artifact_selection_id = None
            db.commit()


@DBOS.step(name="fail_matter_import")
def fail_import(job_id: str, message: str) -> None:
    mark_failed(uuid.UUID(job_id), message)


@DBOS.workflow(name="matter_document_import")
def matter_document_import(job_id: str) -> None:
    try:
        context = freeze_selection(job_id)
        if context.get("canceled"):
            return
        batch_size = get_settings().matter_import_batch_size
        total_count = int(context["total_count"])
        handles = []
        for batch_number, offset in enumerate(range(0, total_count, batch_size)):
            item_ids = load_batch(context, offset, batch_size)
            with SetWorkflowID(f"matter-import:{job_id}:batch:{batch_number}"):
                handles.append(BATCH_QUEUE.enqueue(matter_document_import_batch, job_id, batch_number, item_ids))
        cleanup_selection(job_id, context)
        for handle in handles:
            handle.get_result()
        complete_import(job_id)
    except Exception as exc:
        fail_import(job_id, str(exc))
        raise
