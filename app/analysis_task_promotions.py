from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.document_metadata import event_value, replace_metadata_values
from app.job_executions import (
    REVIEW_BATCH_CODING_PROMOTION_JOB_TYPE,
    JobScope,
    create_job_execution,
    current_job_execution,
    transition_job_execution,
)
from app.models import (
    Matter,
    MatterAnalysisTaskVersion,
    MetadataDefinition,
    MetadataEvent,
    ReviewBatch,
    ReviewBatchCodingField,
    ReviewBatchCodingGroup,
    ReviewBatchPromotionDocument,
    ReviewBatchPromotionValue,
    ReviewBatchReviewerCoding,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewBatchRunPromotion,
    ReviewBatchRunValue,
    ReviewDecisionResult,
)
from app.reviewer_coding import resolve_reviewer_coding_as_of
from app.search.operations import create_search_operation

PROMOTION_CHUNK_SIZE = 50


class AnalysisTaskPromotionError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _create_promotion_execution(
    db: Session,
    *,
    promotion: ReviewBatchRunPromotion,
    matter: Matter,
    retry: bool,
) -> None:
    previous = current_job_execution(
        db,
        job_type=REVIEW_BATCH_CODING_PROMOTION_JOB_TYPE,
        job_id=promotion.id,
        for_update=retry,
    )
    create_job_execution(
        db,
        job_type=REVIEW_BATCH_CODING_PROMOTION_JOB_TYPE,
        job_id=promotion.id,
        scope=JobScope(matter.client.tenant_id, matter.client_id, matter.id),
        workflow_key="review_batch_coding_promotion_v1",
        code_version="1",
        dbos_workflow_id=promotion.dbos_workflow_id,
        initiated_by_user_id=promotion.created_by_user_id,
        idempotency_key=f"review-batch-promotion:{promotion.id}",
        progress_total=promotion.total_document_count,
        progress_unit="documents",
        input_snapshot={
            "review_batch_id": str(promotion.review_batch_id),
            "review_batch_run_id": str(promotion.review_batch_run_id) if promotion.review_batch_run_id else None,
            "reviewer_coding_id": str(promotion.reviewer_coding_id) if promotion.reviewer_coding_id else None,
            "matter_analysis_task_version_id": (
                str(promotion.matter_analysis_task_version_id) if promotion.matter_analysis_task_version_id else None
            ),
        },
        configuration_snapshot={
            "mode": promotion.mode,
            "destinations": promotion.destination_snapshot,
        },
        retry_of=previous if retry else None,
    )


def _task_version(db: Session, run: ReviewBatchRun) -> MatterAnalysisTaskVersion:
    raw_id = run.configuration_snapshot.get("task_version_id")
    try:
        version_id = uuid.UUID(str(raw_id))
    except (TypeError, ValueError) as exc:
        raise AnalysisTaskPromotionError("Analysis run does not pin a task version") from exc
    version = db.get(MatterAnalysisTaskVersion, version_id)
    if version is None:
        raise AnalysisTaskPromotionError("Pinned analysis task version was not found")
    return version


def _destinations(
    db: Session,
    *,
    matter_id: uuid.UUID,
    version: MatterAnalysisTaskVersion,
) -> list[MetadataDefinition]:
    raw_ids = version.destination_metadata_definition_ids
    if not raw_ids:
        raise AnalysisTaskPromotionError("The published analysis task version has no explicit destination fields")
    destination_ids = [uuid.UUID(value) for value in raw_ids]
    definitions = {
        definition.id: definition
        for definition in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter_id,
                MetadataDefinition.id.in_(destination_ids),
            )
        )
    }
    invalid = [
        destination_id
        for destination_id in destination_ids
        if destination_id not in definitions
        or definitions[destination_id].status != "ACTIVE"
        or definitions[destination_id].value_source != "ASSERTED"
        or not definitions[destination_id].ai_assignable
    ]
    if invalid:
        raise AnalysisTaskPromotionError(
            "One or more destination fields are no longer active, asserted, and AI-assignable"
        )
    return [definitions[destination_id] for destination_id in destination_ids]


def queue_analysis_task_promotion(
    db: Session,
    *,
    matter: Matter,
    batch: ReviewBatch,
    run: ReviewBatchRun,
    actor_user_id: uuid.UUID,
) -> ReviewBatchRunPromotion:
    if run.review_batch_id != batch.id or batch.matter_id != matter.id:
        raise AnalysisTaskPromotionError("Analysis run is outside the requested matter and batch")
    if run.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise AnalysisTaskPromotionError("Only a completed analysis run can be promoted")
    if run.run_type != "WORKFLOW" or run.configuration_snapshot.get("mode") != "BATCH":
        raise AnalysisTaskPromotionError("Only a batch Analysis Task run can be promoted")

    version = _task_version(db, run)
    destinations = _destinations(db, matter_id=matter.id, version=version)
    existing = db.scalar(select(ReviewBatchRunPromotion).where(ReviewBatchRunPromotion.review_batch_run_id == run.id))
    if existing is not None and existing.status != "FAILED":
        return existing

    total = int(
        db.scalar(
            select(func.count())
            .select_from(ReviewBatchRunDocument)
            .where(ReviewBatchRunDocument.review_batch_run_id == run.id)
        )
        or 0
    )
    snapshot = [
        {
            "id": str(definition.id),
            "key": definition.key,
            "display_name": definition.display_name,
            "type": definition.type,
            "cardinality": definition.cardinality,
            "assertion_policy": definition.assertion_policy,
            "resolution_policy": definition.resolution_policy,
        }
        for definition in destinations
    ]
    if existing is None:
        promotion_id = uuid.uuid4()
        promotion = ReviewBatchRunPromotion(
            id=promotion_id,
            matter_id=matter.id,
            review_batch_id=batch.id,
            review_batch_run_id=run.id,
            matter_analysis_task_version_id=version.id,
            status="QUEUED",
            mode="REPLACE",
            destination_snapshot=snapshot,
            total_document_count=total,
            created_by_user_id=actor_user_id,
            dbos_workflow_id=f"analysis-task-promotion:{promotion_id}:{uuid.uuid4()}",
        )
        db.add(promotion)
    else:
        promotion = existing
        promotion.status = "QUEUED"
        promotion.destination_snapshot = snapshot
        promotion.total_document_count = total
        promotion.eligible_document_count = 0
        promotion.processed_document_count = 0
        promotion.skipped_document_count = 0
        promotion.event_count = 0
        promotion.error_message = None
        promotion.started_at = None
        promotion.completed_at = None
        promotion.search_operation_id = None
        promotion.created_by_user_id = actor_user_id
        promotion.dbos_workflow_id = f"analysis-task-promotion:{promotion.id}:{uuid.uuid4()}"
    db.flush()
    _create_promotion_execution(
        db,
        promotion=promotion,
        matter=matter,
        retry=existing is not None,
    )
    from app.workflows.dispatcher import enqueue_analysis_task_promotion

    enqueue_analysis_task_promotion(db, promotion.dbos_workflow_id, str(promotion.id))
    return promotion


def queue_human_batch_promotion(
    db: Session,
    *,
    matter: Matter,
    batch: ReviewBatch,
    reviewer_coding: ReviewBatchReviewerCoding,
    actor_user_id: uuid.UUID,
) -> ReviewBatchRunPromotion:
    if reviewer_coding.review_batch_id != batch.id or batch.matter_id != matter.id:
        raise AnalysisTaskPromotionError("Reviewer coding is outside the requested matter and batch")

    field_rows = db.execute(
        select(ReviewBatchCodingField, ReviewBatchCodingGroup, MetadataDefinition)
        .join(
            ReviewBatchCodingGroup,
            ReviewBatchCodingGroup.id == ReviewBatchCodingField.review_batch_coding_group_id,
        )
        .join(MetadataDefinition, MetadataDefinition.id == ReviewBatchCodingField.metadata_definition_id)
        .where(ReviewBatchCodingGroup.review_batch_id == batch.id)
        .order_by(ReviewBatchCodingGroup.sort_order, ReviewBatchCodingField.sort_order)
    ).all()
    invalid = [
        definition.display_name
        for _, _, definition in field_rows
        if definition.matter_id != matter.id or definition.status != "ACTIVE" or definition.value_source != "ASSERTED"
    ]
    if invalid:
        raise AnalysisTaskPromotionError(
            "One or more batch fields are no longer active asserted fields: " + ", ".join(invalid)
        )
    if not field_rows:
        raise AnalysisTaskPromotionError("The batch has no coding fields to promote")

    existing = db.scalar(
        select(ReviewBatchRunPromotion)
        .where(
            ReviewBatchRunPromotion.reviewer_coding_id == reviewer_coding.id,
            ReviewBatchRunPromotion.reviewer_coding_revision == reviewer_coding.revision,
        )
        .order_by(ReviewBatchRunPromotion.created_at.desc())
        .limit(1)
    )
    if existing is not None and existing.status != "FAILED":
        return existing
    snapshot = [
        {
            "id": str(definition.id),
            "key": definition.key,
            "display_name": definition.display_name,
            "type": definition.type,
            "cardinality": definition.cardinality,
            "assertion_policy": definition.assertion_policy,
            "resolution_policy": definition.resolution_policy,
            "coding_group_id": str(group.id),
            "coding_group_name": group.display_name,
        }
        for _, group, definition in field_rows
    ]
    if existing is None:
        promotion_id = uuid.uuid4()
        promotion = ReviewBatchRunPromotion(
            id=promotion_id,
            matter_id=matter.id,
            review_batch_id=batch.id,
            review_batch_run_id=None,
            reviewer_coding_id=reviewer_coding.id,
            reviewer_coding_revision=reviewer_coding.revision,
            matter_analysis_task_version_id=None,
            status="QUEUED",
            mode="REPLACE",
            destination_snapshot=snapshot,
            total_document_count=batch.document_count,
            created_by_user_id=actor_user_id,
            dbos_workflow_id=f"human-batch-promotion:{promotion_id}:{uuid.uuid4()}",
        )
        db.add(promotion)
    else:
        promotion = existing
        promotion.status = "QUEUED"
        promotion.destination_snapshot = snapshot
        promotion.total_document_count = batch.document_count
        promotion.eligible_document_count = 0
        promotion.processed_document_count = 0
        promotion.skipped_document_count = 0
        promotion.event_count = 0
        promotion.error_message = None
        promotion.started_at = None
        promotion.completed_at = None
        promotion.search_operation_id = None
        promotion.created_by_user_id = actor_user_id
        promotion.dbos_workflow_id = f"human-batch-promotion:{promotion.id}:{uuid.uuid4()}"
    db.flush()
    db.query(ReviewBatchPromotionValue).filter(ReviewBatchPromotionValue.promotion_id == promotion.id).delete()
    db.query(ReviewBatchPromotionDocument).filter(ReviewBatchPromotionDocument.promotion_id == promotion.id).delete()
    resolved = resolve_reviewer_coding_as_of(
        db,
        reviewer_coding_id=reviewer_coding.id,
        revision=reviewer_coding.revision,
    )
    completed_document_ids = [row.matter_document_id for row in resolved.documents if row.status == "COMPLETED"]
    for document_id in completed_document_ids:
        db.add(ReviewBatchPromotionDocument(promotion_id=promotion.id, matter_document_id=document_id))
    completed_document_id_set = set(completed_document_ids)
    for value in resolved.values:
        if value.matter_document_id not in completed_document_id_set:
            continue
        db.add(
            ReviewBatchPromotionValue(
                promotion_id=promotion.id,
                matter_document_id=value.matter_document_id,
                metadata_definition_id=value.metadata_definition_id,
                value_ordinal=value.value_ordinal,
                value_text=value.value_text,
                value_long=value.value_long,
                value_float=value.value_float,
                value_boolean=value.value_boolean,
                value_date=value.value_date,
                value_datetime=value.value_datetime,
                value_json=value.value_json,
            )
        )
    promotion.eligible_document_count = len(completed_document_ids)
    promotion.skipped_document_count = max(0, batch.document_count - len(completed_document_ids))
    _create_promotion_execution(
        db,
        promotion=promotion,
        matter=matter,
        retry=existing is not None,
    )
    from app.workflows.dispatcher import enqueue_analysis_task_promotion

    enqueue_analysis_task_promotion(db, promotion.dbos_workflow_id, str(promotion.id))
    return promotion


def prepare_analysis_task_promotion(db: Session, promotion_id: uuid.UUID) -> list[str]:
    promotion = db.get(ReviewBatchRunPromotion, promotion_id)
    if promotion is None:
        raise AnalysisTaskPromotionError("Analysis task promotion was not found")
    if promotion.status == "COMPLETED":
        return []
    promotion.status = "RUNNING"
    promotion.started_at = promotion.started_at or utcnow()
    transition_job_execution(
        db,
        job_type=REVIEW_BATCH_CODING_PROMOTION_JOB_TYPE,
        job_id=promotion.id,
        status="RUNNING",
        current_stage="RESOLVING_ELIGIBLE_DOCUMENTS",
        progress_completed=promotion.processed_document_count,
        progress_total=promotion.total_document_count,
    )
    if promotion.reviewer_coding_id is not None:
        document_ids = list(
            db.scalars(
                select(ReviewBatchPromotionDocument.matter_document_id)
                .where(ReviewBatchPromotionDocument.promotion_id == promotion.id)
                .order_by(ReviewBatchPromotionDocument.matter_document_id)
            )
        )
        promotion.eligible_document_count = len(document_ids)
        promotion.skipped_document_count = max(0, promotion.total_document_count - len(document_ids))
        db.commit()
        return [str(document_id) for document_id in document_ids]
    run = db.get(ReviewBatchRun, promotion.review_batch_run_id)
    if run is None:
        raise AnalysisTaskPromotionError("Promotion review run was not found")
    results = list(
        db.scalars(
            select(ReviewDecisionResult)
            .join(
                ReviewBatchRunDocument,
                (ReviewBatchRunDocument.review_batch_run_id == ReviewDecisionResult.review_batch_run_id)
                & (ReviewBatchRunDocument.matter_document_id == ReviewDecisionResult.matter_document_id),
            )
            .where(
                ReviewDecisionResult.review_batch_run_id == promotion.review_batch_run_id,
                ReviewDecisionResult.status.in_(["COMPLETED", "PARTIAL"]),
                ReviewBatchRunDocument.status == "COMPLETED",
            )
            .order_by(ReviewDecisionResult.matter_document_id)
        )
    )
    eligible = [
        result
        for result in results
        if result.coverage.get("input_complete") is True and result.coverage.get("question_complete") is True
    ]
    promotion.eligible_document_count = len(eligible)
    promotion.skipped_document_count = max(0, promotion.total_document_count - len(eligible))
    db.commit()
    return [str(result.matter_document_id) for result in eligible]


def _promotion_source_id(promotion: ReviewBatchRunPromotion) -> str:
    prefix = "human-batch-promotion" if promotion.reviewer_coding_id else "analysis-task-promotion"
    return f"{prefix}:{promotion.id}"


def apply_analysis_task_promotion_chunk(
    db: Session,
    promotion_id: uuid.UUID,
    document_ids: list[uuid.UUID],
) -> None:
    promotion = db.get(ReviewBatchRunPromotion, promotion_id)
    if promotion is None:
        raise AnalysisTaskPromotionError("Analysis task promotion was not found")
    if promotion.status == "COMPLETED" or not document_ids:
        return
    run = db.get(ReviewBatchRun, promotion.review_batch_run_id) if promotion.review_batch_run_id else None
    reviewer_coding = db.get(ReviewBatchReviewerCoding, promotion.reviewer_coding_id) if promotion.reviewer_coding_id else None
    if run is None and reviewer_coding is None:
        raise AnalysisTaskPromotionError("Promotion source was not found")
    source_type = "HUMAN" if reviewer_coding is not None else "AGENT"
    coding_actor_id = reviewer_coding.reviewer_user_id if reviewer_coding is not None else promotion.created_by_user_id
    destination_ids = [uuid.UUID(item["id"]) for item in promotion.destination_snapshot]
    definitions = {
        definition.id: definition
        for definition in db.scalars(select(MetadataDefinition).where(MetadataDefinition.id.in_(destination_ids)))
    }
    if len(definitions) != len(destination_ids):
        raise AnalysisTaskPromotionError("A snapshotted destination field was not found")

    value_model = ReviewBatchPromotionValue if reviewer_coding is not None else ReviewBatchRunValue
    value_statement = select(value_model).where(
        value_model.matter_document_id.in_(document_ids),
        value_model.metadata_definition_id.in_(destination_ids),
    )
    if reviewer_coding is not None:
        value_statement = value_statement.where(ReviewBatchPromotionValue.promotion_id == promotion.id)
    else:
        value_statement = value_statement.where(ReviewBatchRunValue.review_batch_run_id == promotion.review_batch_run_id)
    grouped: dict[tuple[uuid.UUID, uuid.UUID], list[Any]] = defaultdict(list)
    for value in db.scalars(
        value_statement.order_by(
            value_model.matter_document_id,
            value_model.metadata_definition_id,
            value_model.value_ordinal,
        )
    ):
        grouped[(value.matter_document_id, value.metadata_definition_id)].append(value)

    source_id = _promotion_source_id(promotion)
    already_applied = {
        (document_id, definition_id)
        for document_id, definition_id in db.execute(
            select(
                MetadataEvent.matter_document_id,
                MetadataEvent.metadata_definition_id,
            )
            .where(
                MetadataEvent.source_type == source_type,
                MetadataEvent.source_id == source_id,
                MetadataEvent.matter_document_id.in_(document_ids),
                MetadataEvent.metadata_definition_id.in_(destination_ids),
            )
            .distinct()
        )
    }
    for document_id in document_ids:
        promoted_fields: dict[str, list[Any]] = {}
        for definition_id in destination_ids:
            if (document_id, definition_id) in already_applied:
                continue
            definition = definitions[definition_id]
            rows = grouped[(document_id, definition_id)]
            values = [event_value(row) for row in rows]
            confidences = {
                str(event_value(row)): confidence
                for row in rows
                if (confidence := getattr(row, "confidence", None)) is not None
            }
            replace_metadata_values(
                db,
                matter_id=promotion.matter_id,
                document_id=document_id,
                definition_id=definition_id,
                values=values,
                source_type=source_type,
                actor_id=coding_actor_id,
                source_id=source_id,
                confidences=confidences,
            )
            promoted_fields[definition.key] = values
        if promoted_fields:
            matter = db.get(Matter, promotion.matter_id)
            assert matter is not None
            record_audit(
                db,
                tenant_id=matter.client.tenant_id,
                actor_user_id=promotion.created_by_user_id,
                action=(
                    "review_batch.human_coding_promoted"
                    if reviewer_coding is not None
                    else "analysis_task.coding_promoted"
                ),
                target_type="matter_document",
                target_id=document_id,
                details={
                    "matter_id": str(promotion.matter_id),
                    "review_batch_id": str(promotion.review_batch_id),
                    "review_batch_run_id": (
                        str(promotion.review_batch_run_id) if promotion.review_batch_run_id else None
                    ),
                    "reviewer_coding_id": (
                        str(promotion.reviewer_coding_id) if promotion.reviewer_coding_id else None
                    ),
                    "promotion_id": str(promotion.id),
                    "mode": promotion.mode,
                    "fields": promoted_fields,
                },
            )
    db.flush()
    promotion.processed_document_count = int(
        db.scalar(
            select(func.count(distinct(MetadataEvent.matter_document_id))).where(
                MetadataEvent.source_type == source_type,
                MetadataEvent.source_id == source_id,
            )
        )
        or 0
    )
    promotion.event_count = int(
        db.scalar(
            select(func.count())
            .select_from(MetadataEvent)
            .where(
                MetadataEvent.source_type == source_type,
                MetadataEvent.source_id == source_id,
            )
        )
        or 0
    )
    transition_job_execution(
        db,
        job_type=REVIEW_BATCH_CODING_PROMOTION_JOB_TYPE,
        job_id=promotion.id,
        status="RUNNING",
        current_stage="PUBLISHING_CODING",
        progress_completed=promotion.processed_document_count,
        progress_total=promotion.eligible_document_count,
        progress={
            "eligible_document_count": promotion.eligible_document_count,
            "skipped_document_count": promotion.skipped_document_count,
            "event_count": promotion.event_count,
        },
    )
    db.commit()


def complete_analysis_task_promotion(db: Session, promotion_id: uuid.UUID) -> None:
    promotion = db.get(ReviewBatchRunPromotion, promotion_id)
    if promotion is None:
        raise AnalysisTaskPromotionError("Analysis task promotion was not found")
    if promotion.status == "COMPLETED":
        return
    if promotion.processed_document_count != promotion.eligible_document_count:
        raise AnalysisTaskPromotionError(
            "Promotion did not process every eligible document "
            f"({promotion.processed_document_count}/{promotion.eligible_document_count})"
        )
    run = db.get(ReviewBatchRun, promotion.review_batch_run_id) if promotion.review_batch_run_id else None
    reviewer_coding = db.get(ReviewBatchReviewerCoding, promotion.reviewer_coding_id) if promotion.reviewer_coding_id else None
    if run is None and reviewer_coding is None:
        raise AnalysisTaskPromotionError("Promotion source was not found")
    if run is not None:
        run.result_policy = "PUBLISH_TO_MATTER"
    source_id = _promotion_source_id(promotion)
    document_ids = list(
        db.scalars(
            select(distinct(MetadataEvent.matter_document_id)).where(
                MetadataEvent.source_type == ("HUMAN" if reviewer_coding is not None else "AGENT"),
                MetadataEvent.source_id == source_id,
            )
        )
    )
    if promotion.search_operation_id is None and document_ids:
        operation = create_search_operation(
            db,
            matter_id=promotion.matter_id,
            kind="DOCUMENT_UPSERT",
            payload={"document_ids": [str(value) for value in document_ids]},
            created_by_user_id=promotion.created_by_user_id,
        )
        promotion.search_operation_id = operation.id
    promotion.status = "COMPLETED"
    promotion.completed_at = utcnow()
    transition_job_execution(
        db,
        job_type=REVIEW_BATCH_CODING_PROMOTION_JOB_TYPE,
        job_id=promotion.id,
        status="COMPLETED",
        current_stage="COMPLETED",
        progress_completed=promotion.eligible_document_count,
        progress_total=promotion.eligible_document_count,
        progress={
            "eligible_document_count": promotion.eligible_document_count,
            "skipped_document_count": promotion.skipped_document_count,
            "event_count": promotion.event_count,
            "search_operation_id": (str(promotion.search_operation_id) if promotion.search_operation_id else None),
        },
    )
    db.commit()


def fail_analysis_task_promotion(
    db: Session,
    promotion_id: uuid.UUID,
    message: str,
) -> None:
    promotion = db.get(ReviewBatchRunPromotion, promotion_id)
    if promotion is None or promotion.status == "COMPLETED":
        return
    promotion.status = "FAILED"
    promotion.error_message = message[:4000]
    promotion.completed_at = utcnow()
    transition_job_execution(
        db,
        job_type=REVIEW_BATCH_CODING_PROMOTION_JOB_TYPE,
        job_id=promotion.id,
        status="FAILED",
        current_stage="FAILED",
        progress_completed=promotion.processed_document_count,
        progress_total=max(promotion.eligible_document_count, promotion.processed_document_count),
        failure_code="CODING_PROMOTION_FAILED",
        failure_summary=promotion.error_message,
        failure_retriable=True,
    )
    db.commit()
