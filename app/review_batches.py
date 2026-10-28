import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.artifact_gateway import get_collection_item_relationship_snapshots
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
from app.review_batch_comparisons import comparison_document_ids
from app.schemas import MatterSearchRequest, ReviewBatchComparisonSourceOptionRead
from app.search.client import OpenSearchClient
from app.search.query import compile_search_request


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


MATERIALIZATION_CHUNK_SIZE = 1_000
FAMILY_LOOKUP_CHUNK_SIZE = 5_000


def create_review_batch_execution(
    db: Session,
    batch: ReviewBatch,
    *,
    initiated_by_user_id: uuid.UUID,
    retry_of: WorkflowRun | None = None,
    notification_parent_type: str | None = None,
    notification_parent_id: uuid.UUID | None = None,
) -> WorkflowRun:
    if (notification_parent_type is None) != (notification_parent_id is None):
        raise ValueError("Review batch notification parent type and id must be supplied together")
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
        notification_policy="PARENT_MANAGED" if notification_parent_id is not None else "DEFAULT",
        notification_parent_type=notification_parent_type,
        notification_parent_id=notification_parent_id,
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
        notification_parent_type=current.notification_parent_type,
        notification_parent_id=current.notification_parent_id,
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


def _expand_complete_families(
    db: Session,
    *,
    batch: ReviewBatch,
    matter: Matter,
    seed_document_ids: list[uuid.UUID],
) -> list[uuid.UUID]:
    """Add every linked family member that is already part of the matter."""

    seed_documents = list(
        db.scalars(
            select(MatterDocument).where(
                MatterDocument.matter_id == matter.id,
                MatterDocument.id.in_(seed_document_ids),
            )
        )
    )
    if len(seed_documents) != len(seed_document_ids):
        raise ValueError("Family expansion seed contains documents outside the matter")
    seed_by_item_id = {document.collection_item_id: document for document in seed_documents}
    pending_item_ids = list(seed_by_item_id)
    visited_item_ids: set[uuid.UUID] = set()
    family_item_ids: set[uuid.UUID] = set()
    family_ids: set[uuid.UUID] = set()

    while pending_item_ids:
        page = pending_item_ids[:FAMILY_LOOKUP_CHUNK_SIZE]
        pending_item_ids = pending_item_ids[FAMILY_LOOKUP_CHUNK_SIZE:]
        page = list(dict.fromkeys(item_id for item_id in page if item_id not in visited_item_ids))
        if not page:
            continue
        snapshots = get_collection_item_relationship_snapshots(
            item_ids=page,
            actor_user_id=batch.created_by_user_id,
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
        )
        missing_item_ids = set(page).difference(snapshots)
        if missing_item_ids:
            raise ValueError(
                f"Document family data is unavailable for {len(missing_item_ids)} selected documents"
            )
        visited_item_ids.update(page)
        for item_id in page:
            snapshot = snapshots[item_id]
            if snapshot.family_id is None:
                continue
            family_ids.add(snapshot.family_id)
            family_item_ids.add(item_id)
            related_item_ids = [
                *([snapshot.parent_item_id] if snapshot.parent_item_id is not None else []),
                *snapshot.child_item_ids,
            ]
            for related_item_id in related_item_ids:
                family_item_ids.add(related_item_id)
                if related_item_id not in visited_item_ids:
                    pending_item_ids.append(related_item_id)

    family_documents: list[MatterDocument] = []
    ordered_family_item_ids = sorted(family_item_ids, key=str)
    for offset in range(0, len(ordered_family_item_ids), FAMILY_LOOKUP_CHUNK_SIZE):
        family_documents.extend(
            db.scalars(
                select(MatterDocument)
                .where(
                    MatterDocument.matter_id == matter.id,
                    MatterDocument.collection_item_id.in_(
                        ordered_family_item_ids[offset : offset + FAMILY_LOOKUP_CHUNK_SIZE]
                    ),
                )
                .order_by(MatterDocument.created_at, MatterDocument.id)
            )
        )

    expanded_document_ids = list(seed_document_ids)
    expanded_set = set(seed_document_ids)
    for document in family_documents:
        if document.id not in expanded_set:
            expanded_document_ids.append(document.id)
            expanded_set.add(document.id)
    matter_family_item_ids = {document.collection_item_id for document in family_documents}
    batch.selection_definition = {
        **batch.selection_definition,
        "family_expansion": {
            "policy": "EXPAND_COMPLETE_FAMILIES_IN_MATTER",
            "seed_document_count": len(seed_document_ids),
            "family_count": len(family_ids),
            "discovered_family_item_count": len(family_item_ids),
            "matter_family_document_count": len(family_documents),
            "added_document_count": len(expanded_document_ids) - len(seed_document_ids),
            "family_items_outside_matter_count": len(family_item_ids.difference(matter_family_item_ids)),
            "final_document_count": len(expanded_document_ids),
        },
    }
    return expanded_document_ids


def _database_document_ids(db: Session, batch: ReviewBatch) -> list[uuid.UUID]:
    if batch.selection_type == "EXPLICIT_DOCUMENTS":
        raw_ids = batch.selection_definition.get("document_ids")
        if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 100_000:
            raise ValueError("Explicit-document batch membership is invalid")
        try:
            ids = list(dict.fromkeys(uuid.UUID(str(value)) for value in raw_ids))
        except (TypeError, ValueError) as exc:
            raise ValueError("Explicit-document batch membership is invalid") from exc
        matter_count = int(
            db.scalar(
                select(func.count())
                .select_from(MatterDocument)
                .where(MatterDocument.matter_id == batch.matter_id, MatterDocument.id.in_(ids))
            )
            or 0
        )
        if matter_count != len(ids):
            raise ValueError("Explicit-document batch contains documents outside the matter")
    elif batch.selection_type == "CLASSIFICATION_PREDICTIONS":
        from app.classification_projections import classification_projection_document_ids

        ids = classification_projection_document_ids(db, batch)
    elif batch.selection_type == "COMPARISON_RESULT":
        raw_definition_id = batch.selection_definition.get("metadata_definition_id")
        if raw_definition_id is None:
            raise ValueError("Comparison-result selection provenance is incomplete")
        definition_id = uuid.UUID(str(raw_definition_id))
        definition = db.get(MetadataDefinition, definition_id)
        if definition is None or definition.matter_id != batch.matter_id:
            raise ValueError("Comparison-result coding field is unavailable")
        raw_source_batches = batch.selection_definition.get("source_batches")
        if isinstance(raw_source_batches, list):
            source_specs = raw_source_batches
        elif batch.source_batch_id is not None:
            source_specs = [{
                "source_batch_id": str(batch.source_batch_id),
                "sources": batch.selection_definition.get("sources"),
            }]
        else:
            raise ValueError("Comparison-result batch is missing its source batches")
        ids = []
        for source_spec in source_specs:
            if not isinstance(source_spec, dict):
                raise TypeError("Comparison-result source-batch provenance is invalid")
            try:
                source_batch_id = uuid.UUID(str(source_spec["source_batch_id"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Comparison-result source-batch provenance is invalid") from exc
            source_batch = db.get(ReviewBatch, source_batch_id)
            if source_batch is None or source_batch.matter_id != batch.matter_id:
                raise ValueError("Comparison-result source batch is unavailable")
            if source_batch.status != "READY":
                raise ValueError("Comparison-result source batch is not ready")
            raw_sources = source_spec.get("sources")
            if not isinstance(raw_sources, list) or len(raw_sources) < 2:
                raise ValueError("Comparison-result selection provenance is incomplete")
            source_options = [ReviewBatchComparisonSourceOptionRead.model_validate(item) for item in raw_sources]
            reviewer_revisions = {
                source.id: int(raw_source["reviewer_revision"])
                for source, raw_source in zip(source_options, raw_sources, strict=True)
                if source.source_type == "REVIEWER" and raw_source.get("reviewer_revision") is not None
            }
            if len(reviewer_revisions) != sum(source.source_type == "REVIEWER" for source in source_options):
                raise ValueError("Comparison-result reviewer revisions are incomplete")
            ids.extend(
                comparison_document_ids(
                    db,
                    batch_id=source_batch.id,
                    definition_id=definition.id,
                    source_options=source_options,
                    row_filter="DISAGREEMENT",
                    reviewer_revisions=reviewer_revisions,
                )
            )
        ids = list(dict.fromkeys(ids))
    elif batch.selection_type == "RANDOM_BATCH":
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
        if batch.selection_definition.get("family_policy") == "EXPAND_COMPLETE_FAMILIES_IN_MATTER":
            document_ids = _expand_complete_families(
                db,
                batch=batch,
                matter=matter,
                seed_document_ids=document_ids,
            )
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
