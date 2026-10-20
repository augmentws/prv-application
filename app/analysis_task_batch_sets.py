from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.job_executions import (
    ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
    JobScope,
    create_job_execution,
    transition_job_execution,
)
from app.models import (
    AnalysisTaskBatchSet,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    MatterDocument,
    ReviewBatch,
    ReviewBatchDocument,
    utcnow,
)

ALLOCATION_ALGORITHM_VERSION = "stable-sequence-v1"


class AnalysisTaskBatchSetError(ValueError):
    pass


def create_batch_set(
    db: Session,
    *,
    matter: Matter,
    task: MatterAnalysisTask,
    task_version: MatterAnalysisTaskVersion,
    name: str,
    description: str | None,
    population_source_type: str,
    source_review_batch_id: uuid.UUID | None,
    target_batch_size: int,
    random_seed: str | None,
    actor_user_id: uuid.UUID,
) -> AnalysisTaskBatchSet:
    if task.matter_id != matter.id or task_version.matter_analysis_task_id != task.id:
        raise AnalysisTaskBatchSetError("Analysis Task version does not belong to this matter")
    if task_version.status != "PUBLISHED":
        raise AnalysisTaskBatchSetError("Partitioned batch sets require a published task version")
    if "PARTITIONED_SET" not in (task_version.scope_policy or {}).get("allowed_modes", []):
        raise AnalysisTaskBatchSetError("This task version does not allow partitioned batch sets")
    source_batch = None
    if population_source_type == "SOURCE_BATCH":
        source_batch = db.scalar(
            select(ReviewBatch).where(
                ReviewBatch.id == source_review_batch_id,
                ReviewBatch.matter_id == matter.id,
            )
        )
        if source_batch is None or source_batch.status != "READY":
            raise AnalysisTaskBatchSetError("A ready source Review Batch is required")
    elif population_source_type != "ALL_MATTER":
        raise AnalysisTaskBatchSetError("Unsupported batch-set population source")

    batch_set_id = uuid.uuid4()
    batch_set = AnalysisTaskBatchSet(
        id=batch_set_id,
        tenant_id=matter.client.tenant_id,
        matter_id=matter.id,
        matter_analysis_task_id=task.id,
        matter_analysis_task_version_id=task_version.id,
        name=" ".join(name.split()),
        description=description.strip() if description else None,
        population_source_type=population_source_type,
        selection_definition={
            "type": population_source_type,
            "source_review_batch_id": str(source_batch.id) if source_batch else None,
            "source_document_count": source_batch.document_count if source_batch else None,
            "population_snapshot_at": utcnow().isoformat(),
        },
        source_review_batch_id=source_batch.id if source_batch else None,
        target_batch_size=target_batch_size,
        allocation_policy={"ordering": "SOURCE_SEQUENCE", "keep_together": []},
        random_seed=random_seed or str(batch_set_id),
        allocation_algorithm_version=ALLOCATION_ALGORITHM_VERSION,
        status="QUEUED",
        workflow_id=f"analysis-task-batch-set:{batch_set_id}",
        created_by_user_id=actor_user_id,
    )
    db.add(batch_set)
    db.flush()
    execution = create_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
        job_id=batch_set.id,
        scope=JobScope(matter.client.tenant_id, matter.client_id, matter.id),
        workflow_key="analysis_task_batch_set_materialization_v1",
        code_version="1",
        dbos_workflow_id=batch_set.workflow_id,
        initiated_by_user_id=actor_user_id,
        idempotency_key=f"analysis-task-batch-set:{batch_set.id}:attempt:1",
        current_stage="QUEUED",
        progress_unit="documents",
        input_snapshot={
            "task_id": str(task.id),
            "task_version_id": str(task_version.id),
            "population_source_type": population_source_type,
            "source_review_batch_id": str(source_batch.id) if source_batch else None,
            "target_batch_size": target_batch_size,
        },
        configuration_snapshot={"allocation_algorithm_version": ALLOCATION_ALGORITHM_VERSION},
    )
    batch_set.current_workflow_run_id = execution.id
    return batch_set


def _population(db: Session, batch_set: AnalysisTaskBatchSet) -> list[uuid.UUID]:
    if batch_set.population_source_type == "SOURCE_BATCH":
        return list(
            db.scalars(
                select(ReviewBatchDocument.matter_document_id)
                .where(ReviewBatchDocument.review_batch_id == batch_set.source_review_batch_id)
                .order_by(ReviewBatchDocument.sequence_number)
            )
        )
    raw_snapshot_at = batch_set.selection_definition.get("population_snapshot_at")
    if not raw_snapshot_at:
        raise AnalysisTaskBatchSetError("Batch Set population snapshot time is missing")
    snapshot_at = datetime.fromisoformat(str(raw_snapshot_at))
    return list(
        db.scalars(
            select(MatterDocument.id)
            .where(
                MatterDocument.matter_id == batch_set.matter_id,
                MatterDocument.created_at <= snapshot_at,
            )
            .order_by(MatterDocument.created_at, MatterDocument.id)
        )
    )


def materialize_batch_set(db: Session, batch_set_id: uuid.UUID) -> AnalysisTaskBatchSet:
    batch_set = db.scalar(
        select(AnalysisTaskBatchSet).where(AnalysisTaskBatchSet.id == batch_set_id).with_for_update()
    )
    if batch_set is None:
        raise AnalysisTaskBatchSetError("Analysis Task Batch Set not found")
    if batch_set.status in {"READY", "ARCHIVED"}:
        return batch_set
    batch_set.status = "BUILDING"
    batch_set.error_message = None
    transition_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
        job_id=batch_set.id,
        status="RUNNING",
        current_stage="RESOLVING_POPULATION",
        progress_completed=0,
    )
    db.commit()

    try:
        document_ids = _population(db, batch_set)
        if not document_ids:
            raise AnalysisTaskBatchSetError("The selected population has no documents")
        population_hash = hashlib.sha256("\n".join(str(value) for value in document_ids).encode()).hexdigest()
        transition_job_execution(
            db,
            job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
            job_id=batch_set.id,
            current_stage="MATERIALIZING_MEMBERSHIP",
            progress_total=len(document_ids),
        )
        existing_children = list(
            db.scalars(select(ReviewBatch.id).where(ReviewBatch.analysis_task_batch_set_id == batch_set.id))
        )
        if existing_children:
            db.execute(delete(ReviewBatch).where(ReviewBatch.id.in_(existing_children)))
            db.flush()

        chunks = [
            document_ids[offset : offset + batch_set.target_batch_size]
            for offset in range(0, len(document_ids), batch_set.target_batch_size)
        ]
        completed = 0
        for ordinal, chunk in enumerate(chunks, start=1):
            membership_hash = hashlib.sha256("\n".join(str(value) for value in chunk).encode()).hexdigest()
            child = ReviewBatch(
                matter_id=batch_set.matter_id,
                name=f"{batch_set.name} · {ordinal} of {len(chunks)}",
                description=batch_set.description,
                selection_type="TASK_BATCH_PARTITION",
                selection_definition={
                    "type": "TASK_BATCH_PARTITION",
                    "analysis_task_batch_set_id": str(batch_set.id),
                    "task_id": str(batch_set.matter_analysis_task_id),
                    "task_version_id": str(batch_set.matter_analysis_task_version_id),
                    "ordinal": ordinal,
                    "population_hash": population_hash,
                    "membership_hash": membership_hash,
                    "allocation_algorithm_version": batch_set.allocation_algorithm_version,
                },
                analysis_task_batch_set_id=batch_set.id,
                batch_set_ordinal=ordinal,
                reviewer_value_visibility="OWN_VALUES",
                status="READY",
                search_status="NOT_CONFIGURED",
                workflow_id=f"analysis-task-batch-set:{batch_set.id}:batch:{ordinal}",
                document_count=len(chunk),
                created_by_user_id=batch_set.created_by_user_id,
                completed_at=utcnow(),
            )
            db.add(child)
            db.flush()
            db.add_all(
                ReviewBatchDocument(
                    review_batch_id=child.id,
                    matter_document_id=document_id,
                    analysis_task_batch_set_id=batch_set.id,
                    sequence_number=index,
                )
                for index, document_id in enumerate(chunk, start=1)
            )
            completed += len(chunk)
            transition_job_execution(
                db,
                job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
                job_id=batch_set.id,
                current_stage="MATERIALIZING_MEMBERSHIP",
                progress_completed=completed,
                progress_total=len(document_ids),
            )
            db.commit()
            batch_set = db.get(AnalysisTaskBatchSet, batch_set.id) or batch_set

        assigned = list(
            db.scalars(
                select(ReviewBatchDocument.matter_document_id)
                .where(ReviewBatchDocument.analysis_task_batch_set_id == batch_set.id)
                .order_by(ReviewBatchDocument.matter_document_id)
            )
        )
        if len(assigned) != len(document_ids) or set(assigned) != set(document_ids):
            raise AnalysisTaskBatchSetError("Partition verification did not exactly cover the frozen population")
        batch_set.population_count = len(document_ids)
        batch_set.batch_count = len(chunks)
        batch_set.population_hash = population_hash
        batch_set.status = "READY"
        batch_set.completed_at = utcnow()
        transition_job_execution(
            db,
            job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
            job_id=batch_set.id,
            status="COMPLETED",
            current_stage="COMPLETED",
            progress_completed=len(document_ids),
            progress_total=len(document_ids),
        )
        db.commit()
        return db.get(AnalysisTaskBatchSet, batch_set.id) or batch_set
    except Exception as exc:
        db.rollback()
        failed = db.get(AnalysisTaskBatchSet, batch_set_id)
        if failed is not None:
            failed.status = "FAILED"
            failed.error_message = str(exc)[:4000]
            failed.completed_at = utcnow()
            transition_job_execution(
                db,
                job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
                job_id=failed.id,
                status="FAILED",
                current_stage="FAILED",
                failure_code="ANALYSIS_TASK_BATCH_SET_MATERIALIZATION_FAILED",
                failure_summary=failed.error_message,
                failure_retriable=True,
            )
            db.commit()
        raise
