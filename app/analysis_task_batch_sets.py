from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.artifact_gateway import get_collection_item_grouping_snapshots
from app.job_executions import (
    ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
    JobExecutionConflict,
    JobScope,
    cancel_job_execution,
    create_job_execution,
    current_job_execution,
    execution_cancel_requested,
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
    WorkflowRun,
    utcnow,
)

ALLOCATION_ALGORITHM_VERSION = "stable-sequence-v1"
FAMILY_ALLOCATION_ALGORITHM_VERSION = "stable-family-least-loaded-v1"


class AnalysisTaskBatchSetError(ValueError):
    pass


class AnalysisTaskBatchSetCanceled(AnalysisTaskBatchSetError):
    pass


@dataclass(frozen=True)
class BatchSetPlan:
    document_ids: list[uuid.UUID]
    partitions: list[list[uuid.UUID]]
    population_hash: str
    warnings: list[dict[str, object]]
    oversized_groups: list[dict[str, object]]


def _validate_task_version(
    *,
    matter: Matter,
    task: MatterAnalysisTask,
    task_version: MatterAnalysisTaskVersion,
) -> None:
    if task.matter_id != matter.id or task_version.matter_analysis_task_id != task.id:
        raise AnalysisTaskBatchSetError("Analysis Task version does not belong to this matter")
    if task_version.status != "PUBLISHED":
        raise AnalysisTaskBatchSetError("Partitioned batch sets require a published task version")
    if "PARTITIONED_SET" not in (task_version.scope_policy or {}).get("allowed_modes", []):
        raise AnalysisTaskBatchSetError("This task version does not allow partitioned batch sets")


def _source_batch(
    db: Session,
    *,
    matter_id: uuid.UUID,
    population_source_type: str,
    source_review_batch_id: uuid.UUID | None,
) -> ReviewBatch | None:
    if population_source_type == "SOURCE_BATCH":
        source_batch = db.scalar(
            select(ReviewBatch).where(
                ReviewBatch.id == source_review_batch_id,
                ReviewBatch.matter_id == matter_id,
            )
        )
        if source_batch is None or source_batch.status != "READY":
            raise AnalysisTaskBatchSetError("A ready source Review Batch is required")
        return source_batch
    if population_source_type != "ALL_MATTER":
        raise AnalysisTaskBatchSetError("Unsupported batch-set population source")
    return None


def _execution_for_batch_set(
    db: Session,
    *,
    batch_set: AnalysisTaskBatchSet,
    matter: Matter,
    initiated_by_user_id: uuid.UUID,
    retry_of: WorkflowRun | None = None,
) -> WorkflowRun:
    attempt_number = retry_of.attempt_number + 1 if retry_of is not None else 1
    workflow_id = (
        batch_set.workflow_id
        if retry_of is None
        else f"analysis-task-batch-set:{batch_set.id}:attempt:{attempt_number}"
    )
    return create_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
        job_id=batch_set.id,
        scope=JobScope(batch_set.tenant_id, matter.client_id, matter.id),
        workflow_key="analysis_task_batch_set_materialization_v1",
        code_version="1",
        dbos_workflow_id=workflow_id,
        initiated_by_user_id=initiated_by_user_id,
        idempotency_key=f"analysis-task-batch-set:{batch_set.id}:attempt:{attempt_number}",
        current_stage="QUEUED",
        progress_unit="documents",
        input_snapshot={
            "task_id": str(batch_set.matter_analysis_task_id),
            "task_version_id": str(batch_set.matter_analysis_task_version_id),
            "population_source_type": batch_set.population_source_type,
            "source_review_batch_id": (
                str(batch_set.source_review_batch_id) if batch_set.source_review_batch_id else None
            ),
            "target_batch_size": batch_set.target_batch_size,
            "allocation_policy": batch_set.allocation_policy,
        },
        configuration_snapshot={
            "allocation_algorithm_version": batch_set.allocation_algorithm_version,
        },
        retry_of=retry_of,
    )


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
    keep_together: list[str],
    random_seed: str | None,
    actor_user_id: uuid.UUID,
) -> AnalysisTaskBatchSet:
    _validate_task_version(matter=matter, task=task, task_version=task_version)
    source_batch = _source_batch(
        db,
        matter_id=matter.id,
        population_source_type=population_source_type,
        source_review_batch_id=source_review_batch_id,
    )

    batch_set_id = uuid.uuid4()
    algorithm_version = (
        FAMILY_ALLOCATION_ALGORITHM_VERSION if "FAMILY" in keep_together else ALLOCATION_ALGORITHM_VERSION
    )
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
        allocation_policy={
            "ordering": "SOURCE_SEQUENCE",
            "keep_together": keep_together,
            "oversized_group_behavior": "ALLOW_WITH_WARNING",
        },
        random_seed=random_seed or str(batch_set_id),
        allocation_algorithm_version=algorithm_version,
        status="QUEUED",
        workflow_id=f"analysis-task-batch-set:{batch_set_id}",
        created_by_user_id=actor_user_id,
    )
    db.add(batch_set)
    db.flush()
    execution = _execution_for_batch_set(
        db,
        batch_set=batch_set,
        matter=matter,
        initiated_by_user_id=actor_user_id,
    )
    batch_set.current_workflow_run_id = execution.id
    return batch_set


def _population_documents(
    db: Session,
    *,
    matter_id: uuid.UUID,
    population_source_type: str,
    source_review_batch_id: uuid.UUID | None,
    population_snapshot_at: datetime,
) -> list[MatterDocument]:
    if population_source_type == "SOURCE_BATCH":
        return list(
            db.scalars(
                select(MatterDocument)
                .join(
                    ReviewBatchDocument,
                    ReviewBatchDocument.matter_document_id == MatterDocument.id,
                )
                .where(
                    ReviewBatchDocument.review_batch_id == source_review_batch_id,
                    MatterDocument.matter_id == matter_id,
                )
                .order_by(ReviewBatchDocument.sequence_number)
            )
        )
    return list(
        db.scalars(
            select(MatterDocument)
            .where(
                MatterDocument.matter_id == matter_id,
                MatterDocument.created_at <= population_snapshot_at,
            )
            .order_by(MatterDocument.created_at, MatterDocument.id)
        )
    )


def _stable_partitions(document_ids: list[uuid.UUID], target_batch_size: int) -> list[list[uuid.UUID]]:
    return [
        document_ids[offset : offset + target_batch_size]
        for offset in range(0, len(document_ids), target_batch_size)
    ]


def _family_partitions(
    documents: list[MatterDocument],
    *,
    matter: Matter,
    target_batch_size: int,
    actor_user_id: uuid.UUID,
) -> tuple[list[list[uuid.UUID]], list[dict[str, object]]]:
    item_ids = [document.collection_item_id for document in documents]
    try:
        grouping = get_collection_item_grouping_snapshots(
            item_ids=item_ids,
            actor_user_id=actor_user_id,
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
        )
    except AnalysisTaskBatchSetError:
        raise
    except Exception as exc:
        raise AnalysisTaskBatchSetError(f"Document family lookup failed: {exc}") from exc
    missing_count = len(set(item_ids).difference(grouping))
    if missing_count:
        raise AnalysisTaskBatchSetError(
            f"Document family data is unavailable for {missing_count} selected documents"
        )
    units_by_key: dict[str, list[uuid.UUID]] = {}
    ordered_keys: list[str] = []
    for document in documents:
        snapshot = grouping.get(document.collection_item_id)
        key = (
            f"family:{snapshot.family_id}"
            if snapshot is not None and snapshot.family_id
            else f"document:{document.id}"
        )
        if key not in units_by_key:
            units_by_key[key] = []
            ordered_keys.append(key)
        units_by_key[key].append(document.id)

    partition_count = min(len(ordered_keys), math.ceil(len(documents) / target_batch_size))
    partitions: list[list[uuid.UUID]] = [[] for _ in range(partition_count)]
    warnings: list[dict[str, object]] = []
    for key in ordered_keys:
        unit = units_by_key[key]
        if key.startswith("family:") and len(unit) > target_batch_size:
            warnings.append(
                {
                    "code": "OVERSIZED_FAMILY",
                    "family_id": key.removeprefix("family:"),
                    "document_count": len(unit),
                    "target_batch_size": target_batch_size,
                }
            )
        partition_index = min(range(partition_count), key=lambda index: (len(partitions[index]), index))
        partitions[partition_index].extend(unit)
    return partitions, warnings


def plan_batch_set(
    db: Session,
    *,
    matter: Matter,
    population_source_type: str,
    source_review_batch_id: uuid.UUID | None,
    target_batch_size: int,
    keep_together: list[str],
    actor_user_id: uuid.UUID,
    population_snapshot_at: datetime | None = None,
) -> BatchSetPlan:
    _source_batch(
        db,
        matter_id=matter.id,
        population_source_type=population_source_type,
        source_review_batch_id=source_review_batch_id,
    )
    documents = _population_documents(
        db,
        matter_id=matter.id,
        population_source_type=population_source_type,
        source_review_batch_id=source_review_batch_id,
        population_snapshot_at=population_snapshot_at or utcnow(),
    )
    if not documents:
        raise AnalysisTaskBatchSetError("The selected population has no documents")
    document_ids = [document.id for document in documents]
    population_hash = hashlib.sha256("\n".join(str(value) for value in document_ids).encode()).hexdigest()
    if "FAMILY" in keep_together:
        partitions, warnings = _family_partitions(
            documents,
            matter=matter,
            target_batch_size=target_batch_size,
            actor_user_id=actor_user_id,
        )
    else:
        partitions = _stable_partitions(document_ids, target_batch_size)
        warnings = []
    return BatchSetPlan(
        document_ids=document_ids,
        partitions=partitions,
        population_hash=population_hash,
        warnings=warnings,
        oversized_groups=[warning for warning in warnings if warning["code"] == "OVERSIZED_FAMILY"],
    )


def preview_batch_set(
    db: Session,
    *,
    matter: Matter,
    task: MatterAnalysisTask,
    task_version: MatterAnalysisTaskVersion,
    population_source_type: str,
    source_review_batch_id: uuid.UUID | None,
    target_batch_size: int,
    keep_together: list[str],
    actor_user_id: uuid.UUID,
) -> BatchSetPlan:
    _validate_task_version(matter=matter, task=task, task_version=task_version)
    return plan_batch_set(
        db,
        matter=matter,
        population_source_type=population_source_type,
        source_review_batch_id=source_review_batch_id,
        target_batch_size=target_batch_size,
        keep_together=keep_together,
        actor_user_id=actor_user_id,
    )


def _assert_not_canceled(db: Session, batch_set_id: uuid.UUID) -> None:
    if execution_cancel_requested(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
        job_id=batch_set_id,
    ):
        raise AnalysisTaskBatchSetCanceled("Analysis Task Batch Set materialization was canceled")


def _mark_children(db: Session, batch_set_id: uuid.UUID, *, status: str, error_message: str | None = None) -> None:
    values: dict[str, object] = {"status": status, "error_message": error_message}
    if status in {"READY", "FAILED", "CANCELED"}:
        values["completed_at"] = utcnow()
    db.execute(
        update(ReviewBatch)
        .where(ReviewBatch.analysis_task_batch_set_id == batch_set_id)
        .values(**values)
    )


def materialize_batch_set(db: Session, batch_set_id: uuid.UUID) -> AnalysisTaskBatchSet:
    batch_set = db.scalar(
        select(AnalysisTaskBatchSet).where(AnalysisTaskBatchSet.id == batch_set_id).with_for_update()
    )
    if batch_set is None:
        raise AnalysisTaskBatchSetError("Analysis Task Batch Set not found")
    if batch_set.status in {"READY", "ARCHIVED"}:
        return batch_set
    _assert_not_canceled(db, batch_set.id)
    matter = db.get(Matter, batch_set.matter_id)
    if matter is None:
        raise AnalysisTaskBatchSetError("Analysis Task Batch Set matter not found")
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
        raw_snapshot_at = batch_set.selection_definition.get("population_snapshot_at")
        if not raw_snapshot_at:
            raise AnalysisTaskBatchSetError("Batch Set population snapshot time is missing")
        snapshot_at = datetime.fromisoformat(str(raw_snapshot_at))
        keep_together = [str(value) for value in batch_set.allocation_policy.get("keep_together", [])]
        if "FAMILY" in keep_together:
            transition_job_execution(
                db,
                job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
                job_id=batch_set.id,
                current_stage="RESOLVING_KEEP_TOGETHER_GROUPS",
            )
        plan = plan_batch_set(
            db,
            matter=matter,
            population_source_type=batch_set.population_source_type,
            source_review_batch_id=batch_set.source_review_batch_id,
            target_batch_size=batch_set.target_batch_size,
            keep_together=keep_together,
            actor_user_id=batch_set.created_by_user_id,
            population_snapshot_at=snapshot_at,
        )
        batch_set.population_count = len(plan.document_ids)
        batch_set.batch_count = len(plan.partitions)
        batch_set.population_hash = plan.population_hash
        batch_set.warning_details = plan.warnings
        transition_job_execution(
            db,
            job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
            job_id=batch_set.id,
            current_stage="PLANNING_PARTITIONS",
            progress_total=len(plan.document_ids),
        )
        db.commit()
        batch_set = db.scalar(
            select(AnalysisTaskBatchSet)
            .where(AnalysisTaskBatchSet.id == batch_set.id)
            .with_for_update()
        )
        if batch_set is None:
            raise AnalysisTaskBatchSetError("Analysis Task Batch Set not found during planning")
        _assert_not_canceled(db, batch_set.id)

        existing_children = list(
            db.scalars(select(ReviewBatch.id).where(ReviewBatch.analysis_task_batch_set_id == batch_set.id))
        )
        if existing_children:
            db.execute(delete(ReviewBatch).where(ReviewBatch.id.in_(existing_children)))
            db.commit()

        completed = 0
        for ordinal, partition in enumerate(plan.partitions, start=1):
            _assert_not_canceled(db, batch_set.id)
            membership_hash = hashlib.sha256("\n".join(str(value) for value in partition).encode()).hexdigest()
            child = ReviewBatch(
                id=uuid.uuid5(batch_set.id, f"partition:{ordinal}"),
                matter_id=batch_set.matter_id,
                name=f"{batch_set.name} · {ordinal} of {len(plan.partitions)}",
                description=batch_set.description,
                selection_type="TASK_BATCH_PARTITION",
                selection_definition={
                    "type": "TASK_BATCH_PARTITION",
                    "analysis_task_batch_set_id": str(batch_set.id),
                    "task_id": str(batch_set.matter_analysis_task_id),
                    "task_version_id": str(batch_set.matter_analysis_task_version_id),
                    "ordinal": ordinal,
                    "population_hash": plan.population_hash,
                    "membership_hash": membership_hash,
                    "allocation_algorithm_version": batch_set.allocation_algorithm_version,
                    "keep_together": keep_together,
                },
                analysis_task_batch_set_id=batch_set.id,
                batch_set_ordinal=ordinal,
                reviewer_value_visibility="OWN_VALUES",
                status="BUILDING",
                search_status="NOT_CONFIGURED",
                workflow_id=f"analysis-task-batch-set:{batch_set.id}:batch:{ordinal}",
                document_count=len(partition),
                created_by_user_id=batch_set.created_by_user_id,
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
                for index, document_id in enumerate(partition, start=1)
            )
            completed += len(partition)
            transition_job_execution(
                db,
                job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
                job_id=batch_set.id,
                current_stage="MATERIALIZING_MEMBERSHIP",
                progress_completed=completed,
                progress_total=len(plan.document_ids),
            )
            db.commit()
            batch_set = db.get(AnalysisTaskBatchSet, batch_set.id) or batch_set

        batch_set = db.scalar(
            select(AnalysisTaskBatchSet)
            .where(AnalysisTaskBatchSet.id == batch_set.id)
            .with_for_update()
        )
        if batch_set is None:
            raise AnalysisTaskBatchSetError("Analysis Task Batch Set not found during finalization")
        _assert_not_canceled(db, batch_set.id)
        transition_job_execution(
            db,
            job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
            job_id=batch_set.id,
            current_stage="VERIFYING_MEMBERSHIP",
        )
        assigned = list(
            db.scalars(
                select(ReviewBatchDocument.matter_document_id)
                .where(ReviewBatchDocument.analysis_task_batch_set_id == batch_set.id)
                .order_by(ReviewBatchDocument.matter_document_id)
            )
        )
        if len(assigned) != len(plan.document_ids) or set(assigned) != set(plan.document_ids):
            raise AnalysisTaskBatchSetError("Partition verification did not exactly cover the frozen population")
        _mark_children(db, batch_set.id, status="READY")
        batch_set.status = "READY"
        batch_set.completed_at = utcnow()
        transition_job_execution(
            db,
            job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
            job_id=batch_set.id,
            status="COMPLETED",
            current_stage="COMPLETED",
            progress_completed=len(plan.document_ids),
            progress_total=len(plan.document_ids),
        )
        db.commit()
        return db.get(AnalysisTaskBatchSet, batch_set.id) or batch_set
    except AnalysisTaskBatchSetCanceled:
        db.rollback()
        canceled = db.get(AnalysisTaskBatchSet, batch_set_id)
        if canceled is not None:
            canceled.status = "CANCELED"
            canceled.completed_at = utcnow()
            _mark_children(db, batch_set_id, status="CANCELED")
            db.commit()
        raise
    except Exception as exc:
        db.rollback()
        failed = db.get(AnalysisTaskBatchSet, batch_set_id)
        if failed is not None:
            current = current_job_execution(
                db,
                job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
                job_id=failed.id,
            )
            if current is not None and current.status == "CANCELED":
                failed.status = "CANCELED"
                failed.completed_at = utcnow()
                _mark_children(db, batch_set_id, status="CANCELED")
                db.commit()
                raise AnalysisTaskBatchSetCanceled(
                    "Analysis Task Batch Set materialization was canceled"
                ) from exc
            failed.status = "FAILED"
            failed.error_message = str(exc)[:4000]
            failed.completed_at = utcnow()
            _mark_children(db, batch_set_id, status="FAILED", error_message=failed.error_message)
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


def cancel_batch_set(
    db: Session,
    batch_set: AnalysisTaskBatchSet,
    *,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowRun, bool]:
    execution, canceled = cancel_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
        job_id=batch_set.id,
        actor_user_id=actor_user_id,
    )
    if canceled:
        batch_set.completed_at = utcnow()
        _mark_children(db, batch_set.id, status="CANCELED")
    return execution, canceled


def retry_batch_set(
    db: Session,
    batch_set: AnalysisTaskBatchSet,
    *,
    actor_user_id: uuid.UUID,
) -> WorkflowRun:
    current = current_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_JOB_TYPE,
        job_id=batch_set.id,
        for_update=True,
    )
    if current is None:
        raise JobExecutionConflict("Analysis Task Batch Set has no execution to retry")
    if current.status not in {"FAILED", "CANCELED"}:
        raise JobExecutionConflict("Only failed or canceled Batch Set materialization can be retried")
    matter = db.get(Matter, batch_set.matter_id)
    if matter is None:
        raise JobExecutionConflict("Analysis Task Batch Set matter was not found")
    batch_set.status = "QUEUED"
    batch_set.error_message = None
    batch_set.completed_at = None
    return _execution_for_batch_set(
        db,
        batch_set=batch_set,
        matter=matter,
        initiated_by_user_id=actor_user_id,
        retry_of=current,
    )
