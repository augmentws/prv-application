import math
import uuid

from dbos import DBOS, Queue, SetWorkflowID
from sqlalchemy import select

from app.artifact_gateway import (
    SelectionBatchItem,
    SelectionCustodian,
    create_selection,
    remove_selection,
    selection_batch,
)
from app.config import get_settings
from app.database import SessionLocal
from app.job_executions import (
    MATTER_DOCUMENT_IMPORT_JOB_TYPE,
    execution_cancel_requested,
    transition_job_execution,
)
from app.matter_imports import (
    mark_failed,
    metadata_definition_snapshot,
    process_batch,
    refresh_progress,
)
from app.models import MatterDocumentImportJob, MetadataDefinition

PLAN_QUEUE = Queue("matter-import-plans", global_concurrency=2)
BATCH_QUEUE = Queue("matter-import-batches", global_concurrency=8)

@DBOS.step(name="freeze_matter_import_selection", retries_allowed=True, max_attempts=5)
def freeze_selection(job_id: str) -> dict[str, object]:
    job_uuid = uuid.UUID(job_id)
    with SessionLocal() as db:
        job = db.get(MatterDocumentImportJob, job_uuid)
        if job is None:
            raise ValueError("Matter document import job not found")
        if job.status == "CANCELED" or execution_cancel_requested(
            db,
            job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
            job_id=job.id,
        ):
            return {"canceled": True}
        transition_job_execution(
            db,
            job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
            job_id=job.id,
            status="RUNNING",
            current_stage="FREEZING_SELECTION",
        )
        db.commit()
        actor_id = job.created_by_user_id
        tenant_id = job.matter.client.tenant_id
        client_id = job.matter.client_id
        collection_id = job.source_collection_id
        selection = job.selection
        metadata_definitions = [
            metadata_definition_snapshot(definition)
            for definition in db.scalars(
                select(MetadataDefinition)
                .where(
                    MetadataDefinition.matter_id == job.matter_id,
                    MetadataDefinition.status == "ACTIVE",
                    MetadataDefinition.value_source == "ASSERTED",
                )
                .order_by(MetadataDefinition.key)
            )
        ]

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
        if job.status == "CANCELED" or execution_cancel_requested(
            db,
            job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
            job_id=job.id,
        ):
            canceled = True
        else:
            job.artifact_selection_id = selection_id
            job.matched_count = total_count
            job.batch_count = math.ceil(total_count / get_settings().matter_import_batch_size)
            transition_job_execution(
                db,
                job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
                job_id=job.id,
                status="RUNNING",
                current_stage="PROCESSING_BATCHES",
                progress_total=total_count,
                progress={"batch_count": job.batch_count},
            )
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
        "job_id": job_id,
        "selection_id": str(selection_id),
        "total_count": total_count,
        "actor_id": str(actor_id),
        "tenant_id": str(tenant_id),
        "client_id": str(client_id),
        "metadata_definitions": metadata_definitions,
    }


@DBOS.step(name="load_matter_import_batch", retries_allowed=True, max_attempts=5)
def load_batch(context: dict[str, object], offset: int, limit: int) -> list[dict[str, object]]:
    job_id = uuid.UUID(str(context["job_id"]))
    with SessionLocal() as db:
        if execution_cancel_requested(
            db,
            job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
            job_id=job_id,
        ):
            return []
    items = [
        {
            "item_id": str(item.item_id),
            "custodians": [
                {
                    "custodian_id": str(custodian.custodian_id),
                    "relationship_type": custodian.relationship_type,
                }
                for custodian in item.custodians
            ],
            "unmapped_metadata": item.unmapped_metadata,
        }
        for item in selection_batch(
            selection_id=uuid.UUID(str(context["selection_id"])),
            offset=offset,
            limit=limit,
            actor_user_id=uuid.UUID(str(context["actor_id"])),
            tenant_id=uuid.UUID(str(context["tenant_id"])),
            client_id=uuid.UUID(str(context["client_id"])),
            metadata_keys=[
                str(definition["key"])
                for definition in context.get("metadata_definitions", [])  # type: ignore[union-attr]
            ],
        )
    ]
    with SessionLocal() as db:
        if execution_cancel_requested(
            db,
            job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
            job_id=job_id,
        ):
            return []
        transition_job_execution(
            db,
            job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
            job_id=job_id,
            status="RUNNING",
            current_stage="LOADING_SELECTION",
            progress={
                "selection_offset": offset,
                "selection_loaded_count": len(items),
            },
        )
        db.commit()
    return items


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
            if job.status != "CANCELED":
                transition_job_execution(
                    db,
                    job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
                    job_id=job.id,
                    status="RUNNING",
                    current_stage="PROCESSING_BATCHES",
                    progress_completed=job.processed_count,
                    progress_total=job.matched_count or None,
                )
            db.commit()


@DBOS.step(name="process_matter_import_batch", retries_allowed=True, max_attempts=5)
def persist_batch(
    job_id: str,
    batch_number: int,
    items: list[dict[str, object]],
    metadata_definitions: list[dict[str, object]],
) -> dict[str, int]:
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
            unmapped_metadata=dict(item.get("unmapped_metadata") or {}),  # type: ignore[arg-type]
        )
        for item in items
    ]
    result = process_batch(
        uuid.UUID(job_id),
        batch_number,
        batch_items,
        definition_snapshots=metadata_definitions,
    )
    return {
        "item_count": result.item_count,
        "added_count": result.added_count,
        "duplicate_count": result.duplicate_count,
        "failed_count": result.failed_count,
        "imported_label_document_count": result.imported_label_document_count,
        "imported_label_event_count": result.imported_label_event_count,
        "imported_label_warning_count": result.imported_label_warning_count,
    }


@DBOS.workflow(name="matter_document_import_batch")
def matter_document_import_batch(
    job_id: str,
    batch_number: int,
    items: list[dict[str, object]],
    metadata_definitions: list[dict[str, object]],
) -> dict[str, int]:
    return persist_batch(job_id, batch_number, items, metadata_definitions)


@DBOS.step(name="complete_matter_import")
def complete_import(job_id: str) -> None:
    job_uuid = uuid.UUID(job_id)
    refresh_progress(job_uuid)
    with SessionLocal() as db:
        job = db.get(MatterDocumentImportJob, job_uuid)
        if job is not None and job.status != "CANCELED":
            job.artifact_selection_id = None
            transition_job_execution(
                db,
                job_type=MATTER_DOCUMENT_IMPORT_JOB_TYPE,
                job_id=job.id,
                status="COMPLETED_WITH_ERRORS" if job.failed_count else "COMPLETED",
                current_stage="COMPLETED_WITH_ERRORS" if job.failed_count else "COMPLETED",
                progress_completed=job.matched_count,
                progress_total=job.matched_count,
                progress={
                    "batch_count": job.batch_count,
                    "added_count": job.added_count,
                    "duplicate_count": job.duplicate_count,
                    "failed_count": job.failed_count,
                    "imported_label_document_count": job.imported_label_document_count,
                    "imported_label_event_count": job.imported_label_event_count,
                    "imported_label_warning_count": job.imported_label_warning_count,
                },
                failure_code=("MATTER_DOCUMENT_IMPORT_PARTIAL_FAILURE" if job.failed_count else None),
                failure_summary=job.error_message if job.failed_count else None,
                failure_retriable=False,
            )
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
        metadata_definitions = list(context.get("metadata_definitions", []))  # type: ignore[arg-type]
        handles = []
        for batch_number, offset in enumerate(range(0, total_count, batch_size)):
            item_ids = load_batch(context, offset, batch_size)
            with SetWorkflowID(f"matter-import:{job_id}:batch:{batch_number}"):
                handles.append(
                    BATCH_QUEUE.enqueue(
                        matter_document_import_batch,
                        job_id,
                        batch_number,
                        item_ids,
                        metadata_definitions,
                    )
                )
        cleanup_selection(job_id, context)
        for handle in handles:
            handle.get_result()
        complete_import(job_id)
    except Exception as exc:
        fail_import(job_id, str(exc))
        raise
