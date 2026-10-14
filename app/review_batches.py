import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.job_executions import (
    EXECUTION_TERMINAL_STATUSES,
    REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
    JobExecutionConflict,
    JobScope,
    create_job_execution,
    current_job_execution,
    execution_cancel_requested,
    transition_job_execution,
)
from app.models import (
    Matter,
    MatterDefinitionAssessmentCandidate,
    MatterDocument,
    MetadataDefinition,
    ReviewBatch,
    ReviewBatchDocument,
    SearchIndexGeneration,
    WorkflowRun,
)
from app.schemas import MatterSearchRequest
from app.search.client import OpenSearchClient
from app.search.query import compile_search_request


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


MATERIALIZATION_CHUNK_SIZE = 1_000


def create_review_batch_execution(
    db: Session,
    batch: ReviewBatch,
    *,
    initiated_by_user_id: uuid.UUID,
    retry_of: WorkflowRun | None = None,
) -> WorkflowRun:
    matter = db.get(Matter, batch.matter_id)
    if matter is None:
        raise ValueError("Matter not found")
    attempt_number = (retry_of.attempt_number + 1) if retry_of is not None else 1
    workflow_id = (
        batch.workflow_id
        if retry_of is None
        else f"review-batch:{batch.id}:attempt:{attempt_number}"
    )
    execution = create_job_execution(
        db,
        job_type=REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
        job_id=batch.id,
        scope=JobScope(
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
            matter_id=matter.id,
        ),
        workflow_key="review_batch_build",
        code_version="1",
        dbos_workflow_id=workflow_id,
        initiated_by_user_id=initiated_by_user_id,
        idempotency_key=f"review-batch:{batch.id}:attempt:{attempt_number}",
        current_stage="QUEUED",
        progress_total=batch.sample_size,
        progress_unit="documents",
        input_snapshot={
            "matter_id": str(batch.matter_id),
            "selection_type": batch.selection_type,
            "selection_definition": batch.selection_definition,
            "source_batch_id": str(batch.source_batch_id) if batch.source_batch_id else None,
            "sample_size": batch.sample_size,
            "random_seed": batch.random_seed,
        },
        configuration_snapshot={"materialization_chunk_size": MATERIALIZATION_CHUNK_SIZE},
        retry_of=retry_of,
    )
    batch.workflow_id = execution.dbos_workflow_id
    return execution


def retry_review_batch_execution(
    db: Session,
    batch: ReviewBatch,
    *,
    initiated_by_user_id: uuid.UUID,
) -> WorkflowRun:
    current = current_job_execution(
        db,
        job_type=REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
        job_id=batch.id,
        for_update=True,
    )
    if current is None:
        raise JobExecutionConflict("Review batch has no execution to retry")
    if current.status not in {"FAILED", "CANCELED"}:
        raise JobExecutionConflict("Only failed or canceled batch materialization can be retried")
    batch.status = "QUEUED"
    batch.search_status = "QUEUED"
    batch.error_message = None
    batch.search_error_message = None
    batch.document_count = 0
    batch.completed_at = None
    return create_review_batch_execution(
        db,
        batch,
        initiated_by_user_id=initiated_by_user_id,
        retry_of=current,
    )


def _active_execution(db: Session, batch_id: uuid.UUID) -> WorkflowRun | None:
    execution = current_job_execution(
        db,
        job_type=REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
        job_id=batch_id,
    )
    if execution is None or execution.status in EXECUTION_TERMINAL_STATUSES:
        return None
    return execution


def _transition_materialization(
    db: Session,
    batch_id: uuid.UUID,
    **values: object,
) -> WorkflowRun | None:
    if _active_execution(db, batch_id) is None:
        return None
    return transition_job_execution(
        db,
        job_type=REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
        job_id=batch_id,
        **values,
    )


def complete_review_batch_execution(
    db: Session,
    batch_id: uuid.UUID,
    *,
    search_error: str | None = None,
) -> WorkflowRun | None:
    execution = _active_execution(db, batch_id)
    if execution is None:
        return None
    total = execution.progress_total
    return transition_job_execution(
        db,
        job_type=REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
        job_id=batch_id,
        status="COMPLETED_WITH_ERRORS" if search_error else "COMPLETED",
        current_stage="COMPLETED_WITH_ERRORS" if search_error else "COMPLETED",
        progress_completed=total if total is not None else execution.progress_completed,
        progress_total=total,
        progress={
            **execution.progress,
            "membership_status": "READY",
            "search_status": "FAILED" if search_error else "READY",
        },
        failure_code="REVIEW_BATCH_SEARCH_PROJECTION_FAILED" if search_error else None,
        failure_summary=search_error,
        failure_retriable=bool(search_error),
    )


def fail_review_batch_execution(
    db: Session,
    batch_id: uuid.UUID,
    error: Exception | str,
) -> WorkflowRun | None:
    execution = _active_execution(db, batch_id)
    if execution is None:
        return None
    return transition_job_execution(
        db,
        job_type=REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
        job_id=batch_id,
        status="FAILED",
        current_stage="FAILED",
        failure_code="REVIEW_BATCH_MATERIALIZATION_FAILED",
        failure_summary=str(error),
        failure_retriable=True,
    )


def _stable_key(document_id: uuid.UUID, seed: str) -> bytes:
    return hashlib.sha256(f"{seed}:{document_id}".encode()).digest()


def _sample_document_ids(ids: list[uuid.UUID], batch: ReviewBatch) -> list[uuid.UUID]:
    ids.sort(key=lambda value: _stable_key(value, batch.random_seed or str(batch.id)))
    return ids[: batch.sample_size] if batch.sample_size is not None else ids


def _database_document_ids(db: Session, batch: ReviewBatch) -> list[uuid.UUID]:
    if batch.selection_type == "RANDOM_BATCH":
        ids = list(
            db.scalars(
                select(ReviewBatchDocument.matter_document_id)
                .where(ReviewBatchDocument.review_batch_id == batch.source_batch_id)
                .order_by(ReviewBatchDocument.sequence_number)
            )
        )
    elif batch.selection_type == "RANDOM_ASSESSMENT":
        assessment_run_id = batch.selection_definition.get("assessment_run_id")
        if not assessment_run_id:
            raise ValueError("Assessment sample is missing its source assessment")
        ids = list(
            db.scalars(
                select(MatterDefinitionAssessmentCandidate.matter_document_id)
                .where(MatterDefinitionAssessmentCandidate.assessment_run_id == uuid.UUID(str(assessment_run_id)))
                .order_by(MatterDefinitionAssessmentCandidate.matter_document_id)
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
        ids = _sample_document_ids(ids, batch)
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
    if execution_cancel_requested(
        db,
        job_type=REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
        job_id=batch_id,
    ):
        return batch
    batch.status = "BUILDING"
    batch.error_message = None
    _transition_materialization(
        db,
        batch_id,
        status="RUNNING",
        current_stage="RESOLVING_POPULATION",
        progress={"membership_status": "RESOLVING", "search_status": batch.search_status},
    )
    db.commit()
    matter = db.get(Matter, batch.matter_id)
    if matter is None:
        raise ValueError("Matter not found")
    try:
        if batch.selection_type in {"SEARCH_QUERY", "RANDOM_SAVED_SEARCH"}:
            document_ids = _search_document_ids(db, batch, matter, settings)
            if batch.selection_type == "RANDOM_SAVED_SEARCH":
                document_ids = _sample_document_ids(document_ids, batch)
        else:
            document_ids = _database_document_ids(db, batch)
        if execution_cancel_requested(
            db,
            job_type=REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
            job_id=batch_id,
        ):
            return batch
        execution = _active_execution(db, batch_id)
        expected_resume = int(execution.progress_completed) if execution is not None else 0
        existing_count = int(
            db.scalar(
                select(func.count(ReviewBatchDocument.matter_document_id)).where(
                    ReviewBatchDocument.review_batch_id == batch.id
                )
            )
            or 0
        )
        if expected_resume == 0 or existing_count != expected_resume:
            db.execute(delete(ReviewBatchDocument).where(ReviewBatchDocument.review_batch_id == batch.id))
            existing_count = 0
        _transition_materialization(
            db,
            batch_id,
            status="RUNNING",
            current_stage="MATERIALIZING_POPULATION",
            progress_completed=existing_count,
            progress_total=len(document_ids),
            progress={
                "membership_status": "MATERIALIZING",
                "search_status": batch.search_status,
            },
        )
        db.commit()
        for offset in range(existing_count, len(document_ids), MATERIALIZATION_CHUNK_SIZE):
            if execution_cancel_requested(
                db,
                job_type=REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
                job_id=batch_id,
            ):
                return db.get(ReviewBatch, batch_id) or batch
            page = document_ids[offset : offset + MATERIALIZATION_CHUNK_SIZE]
            db.add_all(
                ReviewBatchDocument(
                    review_batch_id=batch.id,
                    matter_document_id=document_id,
                    sequence_number=offset + index,
                )
                for index, document_id in enumerate(page, start=1)
            )
            completed = offset + len(page)
            _transition_materialization(
                db,
                batch_id,
                status="RUNNING",
                current_stage="MATERIALIZING_POPULATION",
                progress_completed=completed,
                progress_total=len(document_ids),
                progress={
                    "membership_status": "MATERIALIZING",
                    "search_status": batch.search_status,
                },
            )
            db.commit()
            batch = db.get(ReviewBatch, batch_id) or batch
        batch.document_count = len(document_ids)
        batch.status = "READY"
        batch.completed_at = None
        _transition_materialization(
            db,
            batch_id,
            status="RUNNING",
            current_stage="SYNCING_SEARCH",
            progress_completed=len(document_ids),
            progress_total=len(document_ids),
            progress={"membership_status": "READY", "search_status": "QUEUED"},
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        failed = db.get(ReviewBatch, batch_id)
        if failed is not None and _active_execution(db, batch_id) is None:
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
