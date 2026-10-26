import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.analysis_task_batch import queue_analysis_task_batch
from app.analysis_task_playground import materialize_decision_recommendations
from app.analysis_task_promotions import (
    AnalysisTaskPromotionError,
    queue_analysis_task_promotion,
    queue_human_batch_promotion,
)
from app.analysis_tasks import MatterAnalysisTaskConflict
from app.artifact_gateway import read_artifact_bytes
from app.audit import record_audit
from app.authorization import ResourceRef, can, user_can
from app.classification_projections import (
    ClassificationProjectionError,
    create_classifier_projection_run,
    execute_classifier_projection,
    fail_classifier_projection,
)
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.document_metadata import event_value
from app.job_archives import job_not_archived
from app.job_executions import (
    ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
    CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
    CLASSIFICATION_PROJECTION_JOB_TYPE,
    REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
    JobExecutionConflict,
    cancel_job_execution,
    current_job_execution,
)
from app.models import (
    AgentDefinition,
    AgentDefinitionVersion,
    AnalysisTaskBatchSet,
    BatchTopic,
    BatchTopicAssignment,
    BatchTopicTaxonomy,
    ClassificationScoringRun,
    JobArchive,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskRun,
    MatterAnalysisTaskVersion,
    MatterDefinitionAssessmentCandidate,
    MatterDefinitionAssessmentRun,
    MatterDocument,
    MetadataDefinition,
    MetadataGroup,
    MetadataGroupField,
    ReviewBatch,
    ReviewBatchCodingField,
    ReviewBatchCodingGroup,
    ReviewBatchDocument,
    ReviewBatchNote,
    ReviewBatchReviewerCoding,
    ReviewBatchReviewerDocument,
    ReviewBatchReviewerDocumentRevision,
    ReviewBatchReviewerFieldRevision,
    ReviewBatchReviewerRevision,
    ReviewBatchReviewerValue,
    ReviewBatchReviewerValueRevision,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewBatchRunPromotion,
    ReviewBatchRunValue,
    ReviewBatchSearchCodingProjection,
    ReviewBatchSearchCodingRun,
    ReviewDecisionResult,
    SearchIndexGeneration,
    SearchProjectionOperation,
    SkillRun,
    User,
)
from app.review_batch_comparisons import (
    build_coding_comparison,
    comparison_document,
    comparison_document_ids,
    export_coding_comparison,
)
from app.review_batches import (
    complete_review_batch_execution,
    create_review_batch_execution,
    fail_review_batch_execution,
    materialize_review_batch,
)
from app.reviewer_coding import ReviewerFieldReplacement, append_reviewer_coding_revision
from app.routers.saved_searches import _saved_search
from app.routers.search import (
    execute_matter_batch_coding_facets,
    execute_matter_batch_topic_facets,
    execute_matter_facet_values,
    execute_matter_search,
)
from app.schemas import (
    BatchTopicRead,
    BatchTopicTaxonomyRead,
    ClassificationReviewBatchCreate,
    JobExecutionRead,
    MatterAnalysisTaskRunRead,
    MatterFacetValuesResponse,
    MatterSavedSearchUserRead,
    MatterSearchExportRead,
    MatterSearchRequest,
    MatterSearchResponse,
    ReviewBatchAnalysisJobRead,
    ReviewBatchAnalysisRunCreate,
    ReviewBatchAssignmentUpdate,
    ReviewBatchClassifierRunCreate,
    ReviewBatchCodingComparisonRead,
    ReviewBatchCodingFieldRead,
    ReviewBatchCodingGroupRead,
    ReviewBatchCodingGroupsAdd,
    ReviewBatchCodingGroupsUpdate,
    ReviewBatchCodingHistoryEntryRead,
    ReviewBatchComparisonFieldOptionRead,
    ReviewBatchComparisonFieldRead,
    ReviewBatchComparisonOptionsRead,
    ReviewBatchComparisonRead,
    ReviewBatchComparisonRequest,
    ReviewBatchComparisonSourceOptionRead,
    ReviewBatchCreate,
    ReviewBatchDisagreementBatchCreate,
    ReviewBatchDisagreementBatchPreviewRead,
    ReviewBatchDisagreementBatchPreviewRequest,
    ReviewBatchDisagreementDocumentRead,
    ReviewBatchDisagreementDocumentSourceRead,
    ReviewBatchDisagreementSourceSnapshotRead,
    ReviewBatchDocumentAnalysisRead,
    ReviewBatchDocumentCodingRead,
    ReviewBatchDocumentRead,
    ReviewBatchFacetValuesRequest,
    ReviewBatchFieldCodingCountRead,
    ReviewBatchNoteCreate,
    ReviewBatchNoteRead,
    ReviewBatchRead,
    ReviewBatchReviewerCodingRead,
    ReviewBatchReviewerCodingValueRead,
    ReviewBatchReviewerProgressRead,
    ReviewBatchReviewerStatisticsRead,
    ReviewBatchReviewerValueRead,
    ReviewBatchRunCreate,
    ReviewBatchRunDocumentValues,
    ReviewBatchRunProgressRead,
    ReviewBatchRunPromotionRead,
    ReviewBatchRunRead,
    ReviewBatchRunValueRead,
    ReviewBatchSearchCodingFieldRead,
    ReviewBatchSearchCodingSelectionRead,
    ReviewBatchSearchExportCreate,
    ReviewBatchSearchRequest,
    ReviewBatchStatisticsRead,
    ReviewDecisionResultRead,
)
from app.search.operations import create_search_operation
from app.search.query import batch_coding_filter, batch_topic_filter
from app.search.service import sync_review_batch_search
from app.search_exports import create_search_export_job
from app.workflows.dispatcher import enqueue_review_batch, enqueue_search_export, get_dbos_client
from classification_service.hashing import canonical_value_hash

router = APIRouter(prefix="/v1/matters/{matter_id}/review-batches", tags=["review batches"])
logger = logging.getLogger(__name__)
_DBOS_TERMINAL_STATUSES = {"SUCCESS", "ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED", "CANCELLED"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _synchronize_review_batch_executions(
    db: Session,
    batches: list[ReviewBatch],
    settings: Settings,
) -> None:
    if not settings.dbos_enabled:
        return
    active: list[tuple[ReviewBatch, Any]] = []
    for batch in batches:
        if batch.selection_type == "DEFINITION_ASSESSMENT":
            continue
        execution = current_job_execution(
            db,
            job_type=REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
            job_id=batch.id,
        )
        if execution is not None and execution.status in {"QUEUED", "RUNNING", "WAITING"}:
            active.append((batch, execution))
    if not active:
        return
    try:
        workflows = get_dbos_client().list_workflows(
            workflow_ids=[execution.dbos_workflow_id for _, execution in active],
            status=list(_DBOS_TERMINAL_STATUSES),
            load_input=False,
            load_output=True,
        )
    except Exception:
        logger.warning("Could not reconcile durable review batch workflows", exc_info=True)
        return
    terminal = {workflow.workflow_id: workflow for workflow in workflows}
    changed = False
    for batch, execution in active:
        workflow = terminal.get(execution.dbos_workflow_id)
        if workflow is None:
            continue
        try:
            if workflow.status == "CANCELLED":
                cancel_job_execution(
                    db,
                    job_type=REVIEW_BATCH_MATERIALIZATION_JOB_TYPE,
                    job_id=batch.id,
                    actor_user_id=batch.created_by_user_id,
                )
            elif workflow.status == "SUCCESS" and batch.status == "READY":
                complete_review_batch_execution(
                    db,
                    batch.id,
                    search_error=(batch.search_error_message if batch.search_status == "FAILED" else None),
                )
            else:
                fail_review_batch_execution(
                    db,
                    batch.id,
                    str(workflow.error)
                    if workflow.error is not None
                    else "Durable review batch workflow ended before finalization",
                )
            changed = True
        except Exception:
            logger.warning("Could not reconcile review batch %s", batch.id, exc_info=True)
    if changed:
        db.commit()


def _matter(
    db: Session,
    matter_id: uuid.UUID,
    principal: Principal,
    permission_key: str = "batch.view",
) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=404, detail="Matter not found")
    if not can(db, principal, permission_key, ResourceRef.matter(matter.id)):
        raise HTTPException(status_code=403, detail=f"Permission required: {permission_key}")
    return matter


def _batch(db: Session, matter_id: uuid.UUID, batch_id: uuid.UUID) -> ReviewBatch:
    batch = db.scalar(select(ReviewBatch).where(ReviewBatch.id == batch_id, ReviewBatch.matter_id == matter_id))
    if batch is None:
        raise HTTPException(status_code=404, detail="Review batch not found")
    return batch


def _can_manage_batch_assignment(db: Session, principal: Principal, matter_id: uuid.UUID) -> bool:
    resource = ResourceRef.matter(matter_id)
    return any(
        can(db, principal, permission_key, resource)
        for permission_key in (
            "batch.assign",
            "batch.configure",
            "batch.run_analysis",
            "batch.publish_coding",
            "batch.publish_analysis",
        )
    )


def _require_batch_assignment(
    db: Session,
    principal: Principal,
    batch: ReviewBatch,
) -> None:
    """Restrict ordinary reviewers to unassigned or personally assigned work.

    Users who can assign batches retain administrative visibility over every
    batch in the matter.
    """

    if _can_manage_batch_assignment(db, principal, batch.matter_id):
        return
    if batch.assigned_user_id not in {None, principal.user.id}:
        raise HTTPException(status_code=403, detail="This review batch is assigned to another user")


def _coding_groups(db: Session, batch_id: uuid.UUID) -> list[ReviewBatchCodingGroupRead]:
    groups = list(
        db.scalars(
            select(ReviewBatchCodingGroup)
            .where(ReviewBatchCodingGroup.review_batch_id == batch_id)
            .order_by(ReviewBatchCodingGroup.sort_order)
        )
    )
    fields = (
        list(
            db.scalars(
                select(ReviewBatchCodingField)
                .where(ReviewBatchCodingField.review_batch_coding_group_id.in_([group.id for group in groups]))
                .order_by(ReviewBatchCodingField.sort_order)
            )
        )
        if groups
        else []
    )
    by_group: dict[uuid.UUID, list[ReviewBatchCodingFieldRead]] = defaultdict(list)
    for field in fields:
        by_group[field.review_batch_coding_group_id].append(
            ReviewBatchCodingFieldRead(
                id=field.id,
                metadata_definition_id=field.metadata_definition_id,
                sort_order=field.sort_order,
                definition_snapshot=field.definition_snapshot,
            )
        )
    return [
        ReviewBatchCodingGroupRead(
            id=group.id,
            source_metadata_group_id=group.source_metadata_group_id,
            display_name=group.display_name,
            description=group.description,
            sort_order=group.sort_order,
            fields=by_group[group.id],
        )
        for group in groups
    ]


def _latest_batch_coding_operation(
    db: Session,
    batch: ReviewBatch,
    run_id: uuid.UUID,
) -> SearchProjectionOperation | None:
    return db.scalar(
        select(SearchProjectionOperation)
        .where(
            SearchProjectionOperation.matter_id == batch.matter_id,
            SearchProjectionOperation.kind == "BATCH_CODING_SYNC",
            SearchProjectionOperation.payload["batch_id"].as_string() == str(batch.id),
            SearchProjectionOperation.payload["run_id"].as_string() == str(run_id),
        )
        .order_by(SearchProjectionOperation.created_at.desc())
        .limit(1)
    )


def _read_batch(db: Session, batch: ReviewBatch) -> ReviewBatchRead:
    assigned = db.get(User, batch.assigned_user_id) if batch.assigned_user_id else None
    coding_selection = db.get(ReviewBatchSearchCodingRun, batch.id)
    searchable_coding_fields: list[ReviewBatchSearchCodingFieldRead] = []
    if coding_selection is not None:
        searchable_coding_fields = [
            ReviewBatchSearchCodingFieldRead(
                metadata_definition_id=definition.id,
                key=definition.key,
                display_name=definition.display_name,
                type=definition.type,
                value_count=value_count,
            )
            for definition, value_count in db.execute(
                select(MetadataDefinition, func.count(ReviewBatchRunValue.value_ordinal))
                .join(
                    ReviewBatchRunValue,
                    ReviewBatchRunValue.metadata_definition_id == MetadataDefinition.id,
                )
                .where(
                    ReviewBatchRunValue.review_batch_run_id == coding_selection.review_batch_run_id,
                    MetadataDefinition.status == "ACTIVE",
                    MetadataDefinition.type != "JSON",
                )
                .group_by(MetadataDefinition.id)
                .order_by(MetadataDefinition.display_name)
            )
        ]
    coding_operation = (
        _latest_batch_coding_operation(db, batch, coding_selection.review_batch_run_id)
        if coding_selection is not None
        else None
    )
    coding_status = coding_selection.status if coding_selection is not None else None
    coding_error = coding_selection.error_message if coding_selection is not None else None
    if coding_operation is not None and coding_operation.status == "AWAITING_USER":
        coding_status = "FAILED"
        coding_error = "The projection was paused by an unrelated search schema change. Retry the projection."
    return ReviewBatchRead(
        id=batch.id,
        matter_id=batch.matter_id,
        name=batch.name,
        description=batch.description,
        selection_type=batch.selection_type,
        selection_definition=batch.selection_definition,
        source_batch_id=batch.source_batch_id,
        analysis_task_batch_set_id=batch.analysis_task_batch_set_id,
        batch_set_ordinal=batch.batch_set_ordinal,
        search_index_generation_id=batch.search_index_generation_id,
        sample_size=batch.sample_size,
        random_seed=batch.random_seed,
        assigned_user_id=batch.assigned_user_id,
        assigned_user=MatterSavedSearchUserRead(
            id=assigned.id, display_name=assigned.display_name, email=assigned.email
        )
        if assigned
        else None,
        reviewer_value_visibility=batch.reviewer_value_visibility,
        status=batch.status,
        search_status=batch.search_status,
        search_error_message=batch.search_error_message,
        searchable_coding_run_id=(coding_selection.review_batch_run_id if coding_selection is not None else None),
        searchable_coding_status=coding_status,
        searchable_coding_error_message=coding_error,
        searchable_coding_fields=searchable_coding_fields,
        workflow_id=batch.workflow_id,
        document_count=batch.document_count,
        error_message=batch.error_message,
        created_by_user_id=batch.created_by_user_id,
        coding_groups=_coding_groups(db, batch.id),
        created_at=batch.created_at,
        updated_at=batch.updated_at,
        completed_at=batch.completed_at,
        execution=(
            JobExecutionRead.model_validate(batch.current_workflow_run)
            if batch.current_workflow_run is not None
            else None
        ),
    )


def _validate_user(db: Session, matter: Matter, user_id: uuid.UUID | None) -> User | None:
    if user_id is None:
        return None
    user = db.scalar(
        select(User).where(
            User.id == user_id,
            User.tenant_id == matter.client.tenant_id,
            User.status == "ACTIVE",
        )
    )
    if user is None:
        raise HTTPException(status_code=422, detail="Assignee must be an active user in the matter tenant")
    if not user_can(db, user, "batch.review", ResourceRef.matter(matter.id)):
        raise HTTPException(status_code=422, detail="Assignee must have batch.review access to this matter")
    return user


def _snapshot_groups(
    db: Session,
    batch: ReviewBatch,
    group_ids: list[uuid.UUID],
    *,
    start_order: int = 0,
) -> None:
    if not group_ids:
        return
    groups = list(
        db.scalars(
            select(MetadataGroup).where(
                MetadataGroup.id.in_(set(group_ids)),
                MetadataGroup.matter_id == batch.matter_id,
                MetadataGroup.status == "ACTIVE",
                MetadataGroup.scope.in_(["SYSTEM", "MATTER"]),
            )
        )
    )
    if len(groups) != len(set(group_ids)):
        raise HTTPException(status_code=422, detail="Coding groups must be active shared groups in this matter")
    by_id = {group.id: group for group in groups}
    for group_order, group_id in enumerate(group_ids, start=start_order):
        group = by_id[group_id]
        snapshot = ReviewBatchCodingGroup(
            review_batch_id=batch.id,
            source_metadata_group_id=group.id,
            display_name=group.display_name,
            description=group.description,
            sort_order=group_order,
        )
        db.add(snapshot)
        db.flush()
        rows = db.execute(
            select(MetadataGroupField, MetadataDefinition)
            .join(MetadataDefinition, MetadataDefinition.id == MetadataGroupField.metadata_definition_id)
            .where(
                MetadataGroupField.metadata_group_id == group.id,
                MetadataDefinition.status == "ACTIVE",
                MetadataDefinition.reviewable.is_(True),
                MetadataDefinition.value_source == "ASSERTED",
            )
            .order_by(MetadataGroupField.sort_order)
        ).all()
        for field_order, (_, definition) in enumerate(rows):
            db.add(
                ReviewBatchCodingField(
                    review_batch_coding_group_id=snapshot.id,
                    metadata_definition_id=definition.id,
                    sort_order=field_order,
                    definition_snapshot={
                        "key": definition.key,
                        "display_name": definition.display_name,
                        "description": definition.description,
                        "type": definition.type,
                        "cardinality": definition.cardinality,
                        "allowed_values": definition.allowed_values,
                    },
                )
            )


def _clone_batch_coding_groups(
    db: Session,
    *,
    source_batch: ReviewBatch,
    target_batch: ReviewBatch,
    comparison_definition: MetadataDefinition,
) -> None:
    """Copy the parent's frozen coding layout and ensure the compared field is reviewable."""

    source_groups = list(
        db.scalars(
            select(ReviewBatchCodingGroup)
            .where(ReviewBatchCodingGroup.review_batch_id == source_batch.id)
            .order_by(ReviewBatchCodingGroup.sort_order)
        )
    )
    copied_definition_ids: set[uuid.UUID] = set()
    next_group_order = 0
    for source_group in source_groups:
        target_group = ReviewBatchCodingGroup(
            review_batch_id=target_batch.id,
            source_metadata_group_id=source_group.source_metadata_group_id,
            display_name=source_group.display_name,
            description=source_group.description,
            sort_order=source_group.sort_order,
        )
        db.add(target_group)
        db.flush()
        fields = list(
            db.scalars(
                select(ReviewBatchCodingField)
                .where(ReviewBatchCodingField.review_batch_coding_group_id == source_group.id)
                .order_by(ReviewBatchCodingField.sort_order)
            )
        )
        for source_field in fields:
            copied_definition_ids.add(source_field.metadata_definition_id)
            db.add(
                ReviewBatchCodingField(
                    review_batch_coding_group_id=target_group.id,
                    metadata_definition_id=source_field.metadata_definition_id,
                    sort_order=source_field.sort_order,
                    definition_snapshot=source_field.definition_snapshot,
                )
            )
        next_group_order = max(next_group_order, source_group.sort_order + 1)

    if comparison_definition.id in copied_definition_ids:
        return
    target_group = ReviewBatchCodingGroup(
        review_batch_id=target_batch.id,
        source_metadata_group_id=None,
        display_name="Disagreement review",
        description="The coding field used to select this disagreement batch.",
        sort_order=next_group_order,
    )
    db.add(target_group)
    db.flush()
    db.add(
        ReviewBatchCodingField(
            review_batch_coding_group_id=target_group.id,
            metadata_definition_id=comparison_definition.id,
            sort_order=0,
            definition_snapshot={
                "key": comparison_definition.key,
                "display_name": comparison_definition.display_name,
                "description": comparison_definition.description,
                "type": comparison_definition.type,
                "cardinality": comparison_definition.cardinality,
                "allowed_values": comparison_definition.allowed_values,
            },
        )
    )


@router.post("", response_model=ReviewBatchRead, status_code=status.HTTP_202_ACCEPTED)
def create_review_batch(
    matter_id: uuid.UUID,
    payload: ReviewBatchCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ReviewBatchRead:
    matter = _matter(db, matter_id, principal, "batch.create")
    _validate_user(db, matter, payload.assigned_user_id)
    if payload.source_batch_id:
        source = _batch(db, matter_id, payload.source_batch_id)
        if source.status != "READY":
            raise HTTPException(status_code=409, detail="Source batch is not ready")
    saved_search = None
    if payload.saved_search_id is not None:
        saved_search = _saved_search(db, matter.id, payload.saved_search_id, principal)
        saved_request = MatterSearchRequest.model_validate(saved_search.search_definition)
        if saved_request.search_mode != "KEYWORD":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Review batch sampling currently requires a Keyword saved search",
            )
    assessment = None
    assessment_candidate_count = 0
    if payload.assessment_run_id is not None:
        assessment = db.scalar(
            select(MatterDefinitionAssessmentRun).where(
                MatterDefinitionAssessmentRun.id == payload.assessment_run_id,
                MatterDefinitionAssessmentRun.matter_id == matter.id,
            )
        )
        if assessment is None:
            raise HTTPException(status_code=404, detail="Assessment not found")
        assessment_candidate_count = int(
            db.scalar(
                select(func.count(MatterDefinitionAssessmentCandidate.id)).where(
                    MatterDefinitionAssessmentCandidate.assessment_run_id == assessment.id
                )
            )
            or 0
        )
        if assessment_candidate_count == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The assessment does not have a persisted retrieval candidate pool",
            )
    batch_id = uuid.uuid4()
    seed = payload.random_seed or (str(batch_id) if payload.selection_type.startswith("RANDOM") else None)
    selection = {"type": payload.selection_type}
    if payload.include_complete_families:
        selection["family_policy"] = "EXPAND_COMPLETE_FAMILIES_IN_MATTER"
    if payload.search is not None:
        selection["search"] = payload.search.model_copy(update={"offset": 0}).model_dump(mode="json", by_alias=True)
    if saved_search is not None:
        selection.update(
            {
                "saved_search_id": str(saved_search.id),
                "saved_search_name": saved_search.name,
                "saved_search_updated_at": saved_search.updated_at.isoformat(),
                "search": MatterSearchRequest.model_validate(saved_search.search_definition)
                .model_copy(update={"offset": 0})
                .model_dump(mode="json", by_alias=True),
            }
        )
    if assessment is not None:
        selection.update(
            {
                "assessment_run_id": str(assessment.id),
                "assessment_name": assessment.name,
                "assessment_candidate_count": assessment_candidate_count,
                "assessment_selected_count": assessment.selected_count,
                "matter_definition_revision_id": str(assessment.matter_definition_revision_id),
                "definition_content_hash": assessment.definition_content_hash,
                "source_review_batch_id": (str(assessment.review_batch_id) if assessment.review_batch_id else None),
                "search_index_generation_id": (
                    str(assessment.search_index_generation_id) if assessment.search_index_generation_id else None
                ),
            }
        )
    batch = ReviewBatch(
        id=batch_id,
        matter_id=matter.id,
        name=" ".join(payload.name.split()),
        description=payload.description.strip() if payload.description else None,
        selection_type=payload.selection_type,
        selection_definition=selection,
        source_batch_id=payload.source_batch_id,
        search_index_generation_id=(assessment.search_index_generation_id if assessment is not None else None),
        sample_size=payload.sample_size,
        random_seed=seed,
        assigned_user_id=payload.assigned_user_id,
        assigned_by_user_id=principal.user.id if payload.assigned_user_id else None,
        assigned_at=utcnow() if payload.assigned_user_id else None,
        reviewer_value_visibility=payload.reviewer_value_visibility,
        status="QUEUED",
        workflow_id=f"review-batch:{batch_id}",
        created_by_user_id=principal.user.id,
    )
    db.add(batch)
    db.flush()
    _snapshot_groups(db, batch, payload.coding_group_ids)
    execution = create_review_batch_execution(
        db,
        batch,
        initiated_by_user_id=principal.user.id,
    )
    if payload.note:
        db.add(ReviewBatchNote(review_batch_id=batch.id, body=payload.note.strip(), author_user_id=principal.user.id))
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="review_batch.created",
        target_type="review_batch",
        target_id=batch.id,
        details={
            "matter_id": str(matter.id),
            "selection_type": batch.selection_type,
            "saved_search_id": str(saved_search.id) if saved_search is not None else None,
            "assessment_run_id": str(assessment.id) if assessment is not None else None,
            "family_policy": selection.get("family_policy"),
        },
    )
    enqueue_review_batch(db, execution.dbos_workflow_id, str(batch.id))
    db.commit()
    if not settings.dbos_enabled:
        try:
            materialize_review_batch(db, batch.id, settings)
        except Exception as exc:  # noqa: BLE001 - inline mode persists the same durable failure contract
            fail_review_batch_execution(db, batch.id, exc)
            db.commit()
        batch = db.get(ReviewBatch, batch.id) or batch
        if batch.status == "READY":
            if settings.search_enabled and db.get_bind().dialect.name == "postgresql":
                try:
                    sync_review_batch_search(batch.id, settings)
                except Exception as exc:  # noqa: BLE001 - valid membership survives projection failure
                    complete_review_batch_execution(db, batch.id, search_error=str(exc)[:4000])
                    db.commit()
                else:
                    complete_review_batch_execution(db, batch.id)
                    db.commit()
            else:
                batch = db.get(ReviewBatch, batch.id) or batch
                batch.search_status = "NOT_CONFIGURED"
                batch.search_error_message = None
                complete_review_batch_execution(db, batch.id)
                db.commit()
        batch = db.get(ReviewBatch, batch.id) or batch
    return _read_batch(db, batch)


@router.post("/from-classification-run", response_model=ReviewBatchRead, status_code=status.HTTP_202_ACCEPTED)
def create_classification_review_batch(
    matter_id: uuid.UUID,
    payload: ClassificationReviewBatchCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ReviewBatchRead:
    """Freeze prediction matches and attach their isolated classifier coding."""

    matter = _matter(db, matter_id, principal, "batch.create")
    resource = ResourceRef.matter(matter.id)
    for permission_key in ("batch.configure", "classification.score"):
        if not can(db, principal, permission_key, resource):
            raise HTTPException(status_code=403, detail=f"Permission required: {permission_key}")
    _validate_user(db, matter, payload.assigned_user_id)
    analysis_run = db.scalar(
        select(MatterAnalysisTaskRun).where(
            MatterAnalysisTaskRun.id == payload.classification_analysis_run_id,
            MatterAnalysisTaskRun.matter_id == matter.id,
        )
    )
    if analysis_run is None or analysis_run.classification_scoring_run_id is None:
        raise HTTPException(status_code=404, detail="Classification analysis run was not found")
    if analysis_run.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise HTTPException(status_code=409, detail="Classification analysis must complete before creating a batch")
    scoring_run = db.get(ClassificationScoringRun, analysis_run.classification_scoring_run_id)
    if (
        scoring_run is None
        or scoring_run.matter_id != matter.id
        or scoring_run.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
        or scoring_run.prediction_artifact_id is None
        or scoring_run.prediction_content_hash is None
    ):
        raise HTTPException(status_code=409, detail="Completed classification predictions are unavailable")

    derivation_key = canonical_value_hash(
        {
            "classification_analysis_run_id": str(analysis_run.id),
            "classification_scoring_run_id": str(scoring_run.id),
            "prediction_content_hash": scoring_run.prediction_content_hash,
            "minimum_confidence": payload.minimum_confidence,
            "maximum_confidence": payload.maximum_confidence,
            "selected_class_keys": payload.selected_class_keys,
            "name": payload.name,
            "description": payload.description,
            "assigned_user_id": str(payload.assigned_user_id) if payload.assigned_user_id else None,
            "reviewer_value_visibility": payload.reviewer_value_visibility,
        }
    )
    batch_id = uuid.uuid5(analysis_run.id, payload.idempotency_key)
    existing = db.get(ReviewBatch, batch_id)
    if existing is not None:
        if existing.selection_definition.get("derivation_key") != derivation_key:
            raise HTTPException(status_code=409, detail="Idempotency key was already used for another batch")
        return _read_batch(db, existing)

    definition = scoring_run.classification_model.target_metadata_definition
    if definition.matter_id != matter.id or definition.status != "ACTIVE":
        raise HTTPException(status_code=409, detail="Classification target field is no longer active")
    selection = {
        "type": "CLASSIFICATION_PREDICTIONS",
        "contract_version": 1,
        "classification_analysis_run_id": str(analysis_run.id),
        "classification_scoring_run_id": str(scoring_run.id),
        "prediction_artifact_id": str(scoring_run.prediction_artifact_id),
        "prediction_content_hash": scoring_run.prediction_content_hash,
        "minimum_confidence": payload.minimum_confidence,
        "maximum_confidence": payload.maximum_confidence,
        "selected_class_keys": payload.selected_class_keys,
        "confidence_interval": "LOWER_INCLUSIVE_UPPER_EXCLUSIVE",
        "derivation_key": derivation_key,
    }
    batch = ReviewBatch(
        id=batch_id,
        matter_id=matter.id,
        name=payload.name,
        description=payload.description.strip() if payload.description else None,
        selection_type="CLASSIFICATION_PREDICTIONS",
        selection_definition=selection,
        source_batch_id=(analysis_run.scope_id if analysis_run.scope_type == "REVIEW_BATCH" else None),
        sample_size=None,
        random_seed=None,
        assigned_user_id=payload.assigned_user_id,
        assigned_by_user_id=principal.user.id if payload.assigned_user_id else None,
        assigned_at=utcnow() if payload.assigned_user_id else None,
        reviewer_value_visibility=payload.reviewer_value_visibility,
        status="QUEUED",
        workflow_id=f"review-batch:{batch_id}",
        created_by_user_id=principal.user.id,
    )
    db.add(batch)
    db.flush()
    group = ReviewBatchCodingGroup(
        review_batch_id=batch.id,
        source_metadata_group_id=None,
        display_name="Classifier review",
        description=f"Review values predicted for {definition.display_name}.",
        sort_order=0,
    )
    db.add(group)
    db.flush()
    db.add(
        ReviewBatchCodingField(
            review_batch_coding_group_id=group.id,
            metadata_definition_id=definition.id,
            sort_order=0,
            definition_snapshot={
                "key": definition.key,
                "display_name": definition.display_name,
                "description": definition.description,
                "type": definition.type,
                "cardinality": definition.cardinality,
                "allowed_values": definition.allowed_values,
            },
        )
    )
    db.flush()
    projection, _ = create_classifier_projection_run(
        db,
        matter=matter,
        batch=batch,
        payload=ReviewBatchClassifierRunCreate(
            classification_scoring_run_id=scoring_run.id,
            minimum_confidence=payload.minimum_confidence,
            maximum_confidence=payload.maximum_confidence,
            selected_class_keys=payload.selected_class_keys,
            idempotency_key=f"classification-review-batch:{batch.id}",
        ),
        actor_user_id=principal.user.id,
        allow_pending_batch=True,
        enqueue=False,
    )
    batch.selection_definition = {**selection, "classification_projection_run_id": str(projection.id)}
    execution = create_review_batch_execution(db, batch, initiated_by_user_id=principal.user.id)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="classification.review_batch.created",
        target_type="review_batch",
        target_id=batch.id,
        details={
            "classification_analysis_run_id": str(analysis_run.id),
            "classification_scoring_run_id": str(scoring_run.id),
            "classification_projection_run_id": str(projection.id),
            "minimum_confidence": payload.minimum_confidence,
            "maximum_confidence": payload.maximum_confidence,
            "selected_class_keys": payload.selected_class_keys,
        },
    )
    enqueue_review_batch(db, execution.dbos_workflow_id, str(batch.id))
    db.commit()
    if not settings.dbos_enabled:
        try:
            materialize_review_batch(db, batch.id, settings)
            batch = db.get(ReviewBatch, batch.id) or batch
            if batch.status == "READY":
                if settings.search_enabled and db.get_bind().dialect.name == "postgresql":
                    sync_review_batch_search(batch.id, settings)
                else:
                    batch.search_status = "READY"
                    db.commit()
                complete_review_batch_execution(db, batch.id)
                db.commit()
                execute_classifier_projection(db, projection.id)
        except Exception as exc:  # noqa: BLE001 - inline mode preserves durable failures
            fail_review_batch_execution(db, batch.id, exc)
            fail_classifier_projection(db, projection.id, f"Derived review batch failed: {exc}")
            db.commit()
    return _read_batch(db, db.get(ReviewBatch, batch.id) or batch)


@router.get("", response_model=list[ReviewBatchRead])
def list_review_batches(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[ReviewBatchRead]:
    _matter(db, matter_id, principal, "batch.view")
    batches = list(
        db.scalars(
            select(ReviewBatch)
            .where(ReviewBatch.matter_id == matter_id, ReviewBatch.status != "ARCHIVED")
            .order_by(ReviewBatch.created_at.desc())
        )
    )
    _synchronize_review_batch_executions(db, batches, settings)
    if not _can_manage_batch_assignment(db, principal, matter_id):
        batches = [batch for batch in batches if batch.assigned_user_id in {None, principal.user.id}]
    return [_read_batch(db, batch) for batch in batches]


@router.delete("/{batch_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_review_batch(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Response:
    matter = _matter(db, matter_id, principal, "batch.delete")
    batch = _batch(db, matter_id, batch_id)
    if batch.status == "ARCHIVED":
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if batch.analysis_task_batch_set_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Managed partition batches are archived through their Analysis Task Batch Set",
        )
    if batch.status in {"QUEUED", "BUILDING"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cancel the active batch build before deleting the batch",
        )
    active_run = db.scalar(
        select(ReviewBatchRun.id).where(
            ReviewBatchRun.review_batch_id == batch.id,
            ReviewBatchRun.status.in_(("QUEUED", "RUNNING")),
        ).limit(1)
    )
    if active_run is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cancel active batch analysis or review work before deleting the batch",
        )
    active_promotion = db.scalar(
        select(ReviewBatchRunPromotion.id)
        .where(
            ReviewBatchRunPromotion.review_batch_id == batch.id,
            ReviewBatchRunPromotion.status.in_(("QUEUED", "RUNNING")),
        )
        .limit(1)
    )
    if active_promotion is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Wait for the active coding promotion before deleting the batch",
        )
    previous_status = batch.status
    batch.status = "ARCHIVED"
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="review_batch.deleted",
        target_type="review_batch",
        target_id=batch.id,
        details={
            "matter_id": str(matter.id),
            "name": batch.name,
            "previous_status": previous_status,
            "soft_delete": True,
        },
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{batch_id}", response_model=ReviewBatchRead)
def get_review_batch(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ReviewBatchRead:
    _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    _synchronize_review_batch_executions(db, [batch], settings)
    return _read_batch(db, batch)


@router.put("/{batch_id}/assignment", response_model=ReviewBatchRead)
def update_review_batch_assignment(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchAssignmentUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRead:
    matter = _matter(db, matter_id, principal, "batch.assign")
    batch = _batch(db, matter_id, batch_id)
    _validate_user(db, matter, payload.assigned_user_id)
    batch.assigned_user_id = payload.assigned_user_id
    batch.assigned_by_user_id = principal.user.id if payload.assigned_user_id else None
    batch.assigned_at = utcnow() if payload.assigned_user_id else None
    db.commit()
    return _read_batch(db, batch)


@router.post("/{batch_id}/coding-groups", response_model=ReviewBatchRead)
def add_review_batch_coding_groups(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchCodingGroupsAdd,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRead:
    matter = _matter(db, matter_id, principal, "batch.configure")
    batch = _batch(db, matter_id, batch_id)
    existing = list(
        db.scalars(
            select(ReviewBatchCodingGroup)
            .where(ReviewBatchCodingGroup.review_batch_id == batch.id)
            .order_by(ReviewBatchCodingGroup.sort_order)
        )
    )
    existing_source_ids = {
        group.source_metadata_group_id for group in existing if group.source_metadata_group_id is not None
    }
    requested_ids = list(dict.fromkeys(payload.coding_group_ids))
    new_ids = [group_id for group_id in requested_ids if group_id not in existing_source_ids]
    if new_ids:
        _snapshot_groups(db, batch, new_ids, start_order=len(existing))
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=principal.user.id,
            action="review_batch.coding_groups.added",
            target_type="review_batch",
            target_id=batch.id,
            details={
                "matter_id": str(matter.id),
                "coding_group_ids": [str(group_id) for group_id in new_ids],
            },
        )
        db.commit()
    return _read_batch(db, batch)


@router.put("/{batch_id}/coding-groups", response_model=ReviewBatchRead)
def replace_review_batch_coding_groups(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchCodingGroupsUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRead:
    matter = _matter(db, matter_id, principal, "batch.configure")
    batch = _batch(db, matter_id, batch_id)
    existing = list(
        db.scalars(
            select(ReviewBatchCodingGroup)
            .where(ReviewBatchCodingGroup.review_batch_id == batch.id)
            .order_by(ReviewBatchCodingGroup.sort_order)
        )
    )
    existing_by_source = {
        group.source_metadata_group_id: group for group in existing if group.source_metadata_group_id is not None
    }
    requested_ids = list(dict.fromkeys(payload.coding_group_ids))
    requested_set = set(requested_ids)
    new_ids = [group_id for group_id in requested_ids if group_id not in existing_by_source]

    # Validate additions before changing the existing snapshot. Retained snapshots
    # remain valid even if their source group has since been suspended.
    if new_ids:
        valid_new_ids = set(
            db.scalars(
                select(MetadataGroup.id).where(
                    MetadataGroup.id.in_(new_ids),
                    MetadataGroup.matter_id == batch.matter_id,
                    MetadataGroup.status == "ACTIVE",
                    MetadataGroup.scope.in_(["SYSTEM", "MATTER"]),
                )
            )
        )
        if valid_new_ids != set(new_ids):
            raise HTTPException(status_code=422, detail="Coding groups must be active shared groups in this matter")

    removed = [
        group
        for group in existing
        if group.source_metadata_group_id is not None and group.source_metadata_group_id not in requested_set
    ]
    if removed:
        removed_ids = [group.id for group in removed]
        db.execute(
            delete(ReviewBatchCodingField).where(ReviewBatchCodingField.review_batch_coding_group_id.in_(removed_ids))
        )
        db.execute(delete(ReviewBatchCodingGroup).where(ReviewBatchCodingGroup.id.in_(removed_ids)))
        db.flush()

    retained = [existing_by_source[group_id] for group_id in requested_ids if group_id in existing_by_source]
    orphaned = [group for group in existing if group.source_metadata_group_id is None]
    # Move retained rows out of the non-negative display range before assigning
    # the requested order, avoiding transient uniqueness conflicts.
    for index, group in enumerate([*retained, *orphaned], start=1):
        group.sort_order = -index
    db.flush()

    for index, group_id in enumerate(requested_ids):
        retained_group = existing_by_source.get(group_id)
        if retained_group is not None:
            retained_group.sort_order = index
        else:
            _snapshot_groups(db, batch, [group_id], start_order=index)
    for index, group in enumerate(orphaned, start=len(requested_ids)):
        group.sort_order = index

    removed_source_ids = [
        group.source_metadata_group_id for group in removed if group.source_metadata_group_id is not None
    ]
    if new_ids or removed_source_ids:
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=principal.user.id,
            action="review_batch.coding_groups.updated",
            target_type="review_batch",
            target_id=batch.id,
            details={
                "matter_id": str(matter.id),
                "added_coding_group_ids": [str(group_id) for group_id in new_ids],
                "removed_coding_group_ids": [str(group_id) for group_id in removed_source_ids],
            },
        )
    db.commit()
    return _read_batch(db, batch)


def _require_batch_search(batch: ReviewBatch) -> None:
    if batch.search_status != "READY":
        detail = batch.search_error_message or f"Batch search is {batch.search_status.lower().replace('_', ' ')}"
        raise HTTPException(status_code=409, detail=detail)


def _active_taxonomy(db: Session, batch_id: uuid.UUID) -> BatchTopicTaxonomy | None:
    return db.scalar(
        select(BatchTopicTaxonomy).where(
            BatchTopicTaxonomy.review_batch_id == batch_id,
            BatchTopicTaxonomy.status == "ACTIVE",
        )
    )


def _batch_coding_required_filters(
    db: Session,
    batch: ReviewBatch,
    payload: ReviewBatchSearchRequest,
    *,
    exclude_field: str | None = None,
) -> tuple[ReviewBatchSearchCodingRun | None, list[dict[str, Any]]]:
    requested = [item for item in payload.coding_filters if item.field != exclude_field]
    selection = db.get(ReviewBatchSearchCodingRun, batch.id)
    if not requested:
        return selection, []
    if selection is None or selection.status != "READY":
        raise HTTPException(status_code=409, detail="Batch coding search is not ready")
    definitions = {
        definition.key: definition
        for definition in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == batch.matter_id,
                MetadataDefinition.status == "ACTIVE",
                MetadataDefinition.id.in_(
                    select(ReviewBatchRunValue.metadata_definition_id).where(
                        ReviewBatchRunValue.review_batch_run_id == selection.review_batch_run_id
                    )
                ),
            )
        )
    }
    filters: list[dict[str, Any]] = []
    for item in requested:
        definition = definitions.get(item.field)
        if definition is None:
            raise HTTPException(status_code=422, detail=f"Unknown batch coding field: {item.field}")
        filters.append(
            batch_coding_filter(
                batch_id=str(batch.id),
                run_id=str(selection.review_batch_run_id),
                definition=definition,
                values=item.values,
                minimum_confidence=item.minimum_confidence,
                maximum_confidence=item.maximum_confidence,
            )
        )
    return selection, filters


def _batch_search_required_filters(
    db: Session,
    batch: ReviewBatch,
    payload: ReviewBatchSearchRequest,
    *,
    topic_keys: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    required_filters: list[dict[str, Any]] = [{"term": {"batch_ids": str(batch.id)}}]
    taxonomy_id: uuid.UUID | None = None
    if topic_keys:
        taxonomy = _active_taxonomy(db, batch.id)
        if taxonomy is None:
            raise HTTPException(status_code=422, detail="This batch has no active diagnostic topic taxonomy")
        allowed = set(db.scalars(select(BatchTopic.topic_key).where(BatchTopic.taxonomy_id == taxonomy.id)))
        if set(topic_keys) - allowed:
            raise HTTPException(status_code=422, detail="Unknown diagnostic batch topic")
        taxonomy_id = taxonomy.id
        required_filters.append(
            batch_topic_filter(
                batch_id=str(batch.id),
                taxonomy_id=str(taxonomy.id),
                topic_keys=topic_keys,
            )
        )
    coding_selection, coding_filters = _batch_coding_required_filters(db, batch, payload)
    required_filters.extend(coding_filters)
    return required_filters, {
        "kind": "REVIEW_BATCH",
        "review_batch_id": str(batch.id),
        "review_batch_name": batch.name,
        "topic_keys": topic_keys,
        "topic_taxonomy_id": str(taxonomy_id) if taxonomy_id else None,
        "batch_coding_run_id": (
            str(coding_selection.review_batch_run_id)
            if payload.coding_filters and coding_selection is not None
            else None
        ),
    }


def _taxonomy_read(db: Session, taxonomy: BatchTopicTaxonomy) -> BatchTopicTaxonomyRead:
    rows = db.execute(
        select(BatchTopic, func.count(BatchTopicAssignment.id))
        .outerjoin(BatchTopicAssignment, BatchTopicAssignment.topic_id == BatchTopic.id)
        .where(BatchTopic.taxonomy_id == taxonomy.id)
        .group_by(BatchTopic.id)
        .order_by(BatchTopic.ordinal)
    ).all()
    assessment = db.get(MatterDefinitionAssessmentRun, taxonomy.source_assessment_run_id)
    return BatchTopicTaxonomyRead(
        id=taxonomy.id,
        review_batch_id=taxonomy.review_batch_id,
        source_assessment_run_id=taxonomy.source_assessment_run_id,
        review_batch_run_id=assessment.review_batch_run_id if assessment else None,
        version=taxonomy.version,
        status=taxonomy.status,
        topics=[
            BatchTopicRead(
                id=topic.id,
                topic_key=topic.topic_key,
                label=topic.label,
                description=topic.description,
                ordinal=topic.ordinal,
                assignment_count=count,
            )
            for topic, count in rows
        ],
        created_at=taxonomy.created_at,
    )


@router.get("/{batch_id}/topic-taxonomy", response_model=BatchTopicTaxonomyRead | None)
def get_review_batch_topic_taxonomy(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> BatchTopicTaxonomyRead | None:
    _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    taxonomy = _active_taxonomy(db, batch.id)
    return _taxonomy_read(db, taxonomy) if taxonomy is not None else None


@router.post("/{batch_id}/search", response_model=MatterSearchResponse)
def search_review_batch(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchSearchRequest,
    topic_key: list[str] = Query(default=[]),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterSearchResponse:
    matter = _matter(db, matter_id, principal, "batch.review")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    _require_batch_search(batch)
    required_filters, _ = _batch_search_required_filters(
        db,
        batch,
        payload,
        topic_keys=topic_key,
    )
    return execute_matter_search(
        matter,
        payload,
        db=db,
        settings=settings,
        required_filters=required_filters,
    )


@router.post(
    "/{batch_id}/search-exports",
    response_model=MatterSearchExportRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_review_batch_search_export(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchSearchExportCreate,
    topic_key: list[str] = Query(default=[]),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Any:
    matter = _matter(db, matter_id, principal, "batch.review")
    if not can(db, principal, "document.view", ResourceRef.matter(matter.id)) or not can(
        db, principal, "document.export", ResourceRef.matter(matter.id)
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission required: document.export")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    _require_batch_search(batch)
    if len(payload.field_ids) > settings.search_export_max_columns:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"At most {settings.search_export_max_columns} export fields may be selected",
        )
    required_filters, scope_snapshot = _batch_search_required_filters(
        db,
        batch,
        payload.search,
        topic_keys=topic_key,
    )
    try:
        job, created = create_search_export_job(
            db,
            matter,
            payload,
            actor_user_id=principal.user.id,
            settings=settings,
            required_filters=required_filters,
            scope_snapshot=scope_snapshot,
        )
    except JobExecutionConflict as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if created:
        enqueue_search_export(db, job.workflow_id, str(job.id))
    db.commit()
    db.refresh(job)
    return job


@router.post(
    "/{batch_id}/coding-facets/{field}/values",
    response_model=MatterFacetValuesResponse,
)
def search_review_batch_coding_facet_values(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    field: str,
    payload: ReviewBatchSearchRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterFacetValuesResponse:
    matter = _matter(db, matter_id, principal, "batch.review")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    _require_batch_search(batch)
    selection, other_coding_filters = _batch_coding_required_filters(
        db,
        batch,
        payload,
        exclude_field=field,
    )
    if selection is None or selection.status != "READY":
        raise HTTPException(status_code=409, detail="Batch coding search is not ready")
    definition = db.scalar(
        select(MetadataDefinition)
        .join(
            ReviewBatchRunValue,
            ReviewBatchRunValue.metadata_definition_id == MetadataDefinition.id,
        )
        .where(
            MetadataDefinition.matter_id == matter.id,
            MetadataDefinition.key == field,
            MetadataDefinition.status == "ACTIVE",
            ReviewBatchRunValue.review_batch_run_id == selection.review_batch_run_id,
        )
    )
    if definition is None or definition.type == "JSON":
        raise HTTPException(status_code=422, detail=f"Unknown batch coding field: {field}")
    required_filters = [
        {"term": {"batch_ids": str(batch.id)}},
        *other_coding_filters,
    ]
    field_filter = next((item for item in payload.coding_filters if item.field == field), None)
    return execute_matter_batch_coding_facets(
        matter,
        payload,
        definition=definition,
        batch_id=batch.id,
        run_id=selection.review_batch_run_id,
        minimum_confidence=field_filter.minimum_confidence if field_filter else None,
        maximum_confidence=field_filter.maximum_confidence if field_filter else None,
        db=db,
        settings=settings,
        required_filters=required_filters,
    )


@router.post("/{batch_id}/topic-facets", response_model=MatterFacetValuesResponse)
def search_review_batch_topic_facets(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchSearchRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterFacetValuesResponse:
    matter = _matter(db, matter_id, principal, "batch.review")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    _require_batch_search(batch)
    taxonomy = _active_taxonomy(db, batch.id)
    if taxonomy is None:
        return MatterFacetValuesResponse(field="batch_topic", values=[])
    _, coding_filters = _batch_coding_required_filters(db, batch, payload)
    return execute_matter_batch_topic_facets(
        matter,
        payload,
        batch_id=batch.id,
        taxonomy_id=taxonomy.id,
        db=db,
        settings=settings,
        required_filters=[{"term": {"batch_ids": str(batch.id)}}, *coding_filters],
    )


@router.post("/{batch_id}/facets/{field}/values", response_model=MatterFacetValuesResponse)
def search_review_batch_facet_values(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    field: str,
    payload: ReviewBatchFacetValuesRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterFacetValuesResponse:
    matter = _matter(db, matter_id, principal, "batch.review")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    _require_batch_search(batch)
    _, coding_filters = _batch_coding_required_filters(db, batch, payload.search)
    return execute_matter_facet_values(
        matter,
        field,
        payload,
        db=db,
        settings=settings,
        required_filters=[{"term": {"batch_ids": str(batch.id)}}, *coding_filters],
    )


@router.get("/{batch_id}/documents", response_model=list[ReviewBatchDocumentRead])
def list_review_batch_documents(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    run_id: uuid.UUID | None = None,
    reviewer_coding_id: uuid.UUID | None = None,
    document_id: list[uuid.UUID] = Query(default=[]),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ReviewBatchDocumentRead]:
    _matter(db, matter_id, principal, "batch.review")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    if run_id is not None and reviewer_coding_id is not None:
        raise HTTPException(status_code=422, detail="Select either an evaluation run or reviewer coding")
    status_column = ReviewBatchRunDocument.status
    statement = (
        select(ReviewBatchDocument, MatterDocument)
        .join(MatterDocument, MatterDocument.id == ReviewBatchDocument.matter_document_id)
        .where(ReviewBatchDocument.review_batch_id == batch_id)
        .order_by(ReviewBatchDocument.sequence_number)
        .offset(offset)
        .limit(limit)
    )
    if reviewer_coding_id is not None:
        coding = db.get(ReviewBatchReviewerCoding, reviewer_coding_id)
        if coding is None or coding.review_batch_id != batch.id:
            raise HTTPException(status_code=404, detail="Reviewer coding not found")
        if coding.reviewer_user_id != principal.user.id and not _can_manage_batch_assignment(
            db, principal, matter_id
        ):
            raise HTTPException(status_code=403, detail="Reviewer coding belongs to another user")
        status_column = ReviewBatchReviewerDocument.status
        statement = statement.outerjoin(
            ReviewBatchReviewerDocument,
            and_(
                ReviewBatchReviewerDocument.reviewer_coding_id == reviewer_coding_id,
                ReviewBatchReviewerDocument.matter_document_id == ReviewBatchDocument.matter_document_id,
            ),
        )
    else:
        if run_id is not None:
            _run(db, batch.id, run_id)
        statement = statement.outerjoin(
            ReviewBatchRunDocument,
            and_(
                ReviewBatchRunDocument.review_batch_run_id == run_id,
                ReviewBatchRunDocument.matter_document_id == ReviewBatchDocument.matter_document_id,
            ),
        )
    statement = statement.add_columns(status_column)
    if document_id:
        statement = statement.where(ReviewBatchDocument.matter_document_id.in_(set(document_id)))
    rows = db.execute(statement).all()
    return [
        ReviewBatchDocumentRead(
            matter_document_id=member.matter_document_id,
            source_collection_id=document.source_collection_id,
            collection_item_id=document.collection_item_id,
            sequence_number=member.sequence_number,
            review_status="NOT_STARTED" if run_status in {None, "QUEUED"} else run_status,
        )
        for member, document, run_status in rows
    ]


@router.get("/{batch_id}/notes", response_model=list[ReviewBatchNoteRead])
def list_review_batch_notes(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ReviewBatchNote]:
    _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    return list(
        db.scalars(
            select(ReviewBatchNote)
            .where(ReviewBatchNote.review_batch_id == batch_id)
            .order_by(ReviewBatchNote.created_at, ReviewBatchNote.id)
        )
    )


@router.post("/{batch_id}/notes", response_model=ReviewBatchNoteRead, status_code=201)
def create_review_batch_note(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchNoteCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchNote:
    matter = _matter(db, matter_id, principal, "batch.review")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    note = ReviewBatchNote(
        review_batch_id=batch.id,
        body=payload.body.strip(),
        author_type="USER",
        author_user_id=principal.user.id,
    )
    db.add(note)
    db.flush()
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="review_batch.note.created",
        target_type="review_batch_note",
        target_id=note.id,
        details={"batch_id": str(batch.id)},
    )
    db.commit()
    return note


def _run(db: Session, batch_id: uuid.UUID, run_id: uuid.UUID) -> ReviewBatchRun:
    run = db.scalar(
        select(ReviewBatchRun).where(
            ReviewBatchRun.id == run_id,
            ReviewBatchRun.review_batch_id == batch_id,
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Review batch run not found")
    return run


def _run_value_read(row: ReviewBatchRunValue) -> ReviewBatchRunValueRead:
    return ReviewBatchRunValueRead(
        review_batch_run_id=row.review_batch_run_id,
        matter_document_id=row.matter_document_id,
        metadata_definition_id=row.metadata_definition_id,
        value_ordinal=row.value_ordinal,
        value=event_value(row),
        confidence=row.confidence,
        confidence_kind=row.confidence_kind,
        review_decision_result_id=row.review_decision_result_id,
        question_key=row.question_key,
    )


@router.post("/{batch_id}/runs", response_model=ReviewBatchRunRead, status_code=201)
def create_review_batch_run(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchRunCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRun:
    matter = _matter(db, matter_id, principal, "batch.run_analysis")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    if batch.status != "READY":
        raise HTTPException(status_code=409, detail="Review batch is not ready")
    if payload.actor_user_id is not None:
        raise HTTPException(status_code=422, detail="Evaluation runs cannot specify a human actor")
    actor_user_id = None
    configuration: dict[str, Any] = {}
    if payload.run_type == "AGENT":
        version = db.scalar(
            select(AgentDefinitionVersion)
            .join(AgentDefinition, AgentDefinition.id == AgentDefinitionVersion.agent_definition_id)
            .where(
                AgentDefinitionVersion.id == payload.agent_definition_version_id,
                AgentDefinitionVersion.status == "PUBLISHED",
                AgentDefinition.status == "ACTIVE",
                or_(
                    AgentDefinition.owner_tenant_id == matter.client.tenant_id,
                    AgentDefinition.scope == "SYSTEM",
                ),
            )
        )
        if version is None:
            raise HTTPException(status_code=422, detail="Agent run requires an available published agent version")
        configuration = {
            "agent_definition_version_id": str(version.id),
            "version": version.version,
            "model_key": version.model_key,
            "system_prompt": version.system_prompt,
            "model_policy": version.model_policy,
            "output_schema": version.output_schema,
            "limits": version.limits,
        }
    if payload.parent_run_id and _run(db, batch.id, payload.parent_run_id).id == payload.parent_run_id:
        pass
    run = ReviewBatchRun(
        review_batch_id=batch.id,
        run_type=payload.run_type,
        purpose=payload.purpose,
        status="RUNNING",
        result_policy="ISOLATED",
        parent_run_id=payload.parent_run_id,
        actor_user_id=actor_user_id,
        agent_definition_version_id=payload.agent_definition_version_id,
        configuration_snapshot=configuration,
        initiated_by_user_id=principal.user.id,
    )
    db.add(run)
    db.flush()
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="review_batch.run.created",
        target_type="review_batch_run",
        target_id=run.id,
        details={"batch_id": str(batch.id), "run_type": run.run_type, "purpose": run.purpose},
    )
    db.commit()
    return run


@router.get("/{batch_id}/runs", response_model=list[ReviewBatchRunRead])
def list_review_batch_runs(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ReviewBatchRunRead]:
    _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    archive_join = and_(
        JobArchive.job_id == ReviewBatchRun.id,
        or_(
            and_(
                ReviewBatchRun.run_type == "WORKFLOW",
                JobArchive.job_type == ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
            ),
            and_(
                ReviewBatchRun.run_type == "CLASSIFIER",
                JobArchive.job_type == CLASSIFICATION_PROJECTION_JOB_TYPE,
            ),
        ),
    )
    rows = db.execute(
        select(ReviewBatchRun, JobArchive.archived_at)
        .outerjoin(JobArchive, archive_join)
        .where(ReviewBatchRun.review_batch_id == batch.id)
        .order_by(ReviewBatchRun.created_at.desc())
    ).all()
    return [
        ReviewBatchRunRead.model_validate(run).model_copy(
            update={"archived": archived_at is not None, "archived_at": archived_at}
        )
        for run, archived_at in rows
    ]


@router.get("/{batch_id}/statistics", response_model=ReviewBatchStatisticsRead)
def get_review_batch_statistics(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchStatisticsRead:
    matter = _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    reviewer_rows = db.execute(
        select(ReviewBatchReviewerCoding, User)
        .join(User, User.id == ReviewBatchReviewerCoding.reviewer_user_id)
        .where(ReviewBatchReviewerCoding.review_batch_id == batch.id)
        .order_by(User.display_name, User.email)
    ).all()
    coding_ids = [coding.id for coding, _ in reviewer_rows]
    status_counts: dict[tuple[uuid.UUID, str], int] = {}
    field_counts: dict[tuple[uuid.UUID, uuid.UUID], int] = {}
    promotions: dict[uuid.UUID, ReviewBatchRunPromotion] = {}
    if coding_ids:
        status_counts = {
            (coding_id, status_value): int(count)
            for coding_id, status_value, count in db.execute(
                select(
                    ReviewBatchReviewerDocument.reviewer_coding_id,
                    ReviewBatchReviewerDocument.status,
                    func.count(),
                )
                .where(ReviewBatchReviewerDocument.reviewer_coding_id.in_(coding_ids))
                .group_by(
                    ReviewBatchReviewerDocument.reviewer_coding_id,
                    ReviewBatchReviewerDocument.status,
                )
            )
        }
        field_counts = {
            (coding_id, definition_id): int(count)
            for coding_id, definition_id, count in db.execute(
                select(
                    ReviewBatchReviewerValue.reviewer_coding_id,
                    ReviewBatchReviewerValue.metadata_definition_id,
                    func.count(func.distinct(ReviewBatchReviewerValue.matter_document_id)),
                )
                .where(ReviewBatchReviewerValue.reviewer_coding_id.in_(coding_ids))
                .group_by(
                    ReviewBatchReviewerValue.reviewer_coding_id,
                    ReviewBatchReviewerValue.metadata_definition_id,
                )
            )
        }
        promotions = {
            promotion.reviewer_coding_id: promotion
            for promotion in db.scalars(
                select(ReviewBatchRunPromotion)
                .join(
                    ReviewBatchReviewerCoding,
                    ReviewBatchReviewerCoding.id == ReviewBatchRunPromotion.reviewer_coding_id,
                )
                .where(
                    ReviewBatchRunPromotion.reviewer_coding_id.in_(coding_ids),
                    ReviewBatchRunPromotion.reviewer_coding_revision == ReviewBatchReviewerCoding.revision,
                )
                .order_by(ReviewBatchRunPromotion.created_at)
            )
        }
    fields = [
        (
            group,
            field,
            field.definition_snapshot.get("key", str(field.metadata_definition_id)),
            field.definition_snapshot.get("display_name", "Coding field"),
        )
        for group in _coding_groups(db, batch.id)
        for field in group.fields
    ]
    return ReviewBatchStatisticsRead(
        review_batch_id=batch.id,
        document_count=batch.document_count,
        can_promote=can(db, principal, "batch.publish_coding", ResourceRef.matter(matter.id)),
        reviewers=[
            ReviewBatchReviewerStatisticsRead(
                reviewer_coding_id=coding.id,
                reviewer=MatterSavedSearchUserRead(
                    id=user.id,
                    display_name=user.display_name,
                    email=user.email,
                ),
                completed_document_count=status_counts.get((coding.id, "COMPLETED"), 0),
                skipped_document_count=status_counts.get((coding.id, "SKIPPED"), 0),
                incomplete_document_count=max(
                    0,
                    batch.document_count
                    - status_counts.get((coding.id, "COMPLETED"), 0)
                    - status_counts.get((coding.id, "SKIPPED"), 0),
                ),
                field_counts=[
                    ReviewBatchFieldCodingCountRead(
                        coding_group_id=group.id,
                        coding_group_name=group.display_name,
                        metadata_definition_id=field.metadata_definition_id,
                        field_key=str(field_key),
                        field_display_name=str(field_name),
                        document_count=field_counts.get((coding.id, field.metadata_definition_id), 0),
                    )
                    for group, field, field_key, field_name in fields
                ],
                promotion=promotions.get(coding.id),
                created_at=coding.created_at,
                updated_at=coding.updated_at,
            )
            for coding, user in reviewer_rows
        ],
    )


def _coding_selection_read(
    selection: ReviewBatchSearchCodingRun,
) -> ReviewBatchSearchCodingSelectionRead:
    return ReviewBatchSearchCodingSelectionRead(
        review_batch_id=selection.review_batch_id,
        review_batch_run_id=selection.review_batch_run_id,
        status=selection.status,
        selected_by_user_id=selection.selected_by_user_id,
        selected_at=selection.selected_at,
        projected_at=selection.projected_at,
        error_message=selection.error_message,
    )


@router.get(
    "/{batch_id}/coding-search",
    response_model=ReviewBatchSearchCodingSelectionRead | None,
)
def get_review_batch_coding_search(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchSearchCodingSelectionRead | None:
    _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    selection = db.get(ReviewBatchSearchCodingRun, batch.id)
    return _coding_selection_read(selection) if selection is not None else None


@router.post(
    "/{batch_id}/runs/{run_id}/coding-search",
    response_model=ReviewBatchSearchCodingSelectionRead,
    status_code=202,
)
def select_review_batch_coding_search_run(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ReviewBatchSearchCodingSelectionRead:
    matter = _matter(db, matter_id, principal, "batch.configure")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    _require_batch_search(batch)
    if not settings.search_enabled:
        raise HTTPException(status_code=409, detail="Search is not configured")
    run = _run(db, batch.id, run_id)
    if run.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise HTTPException(status_code=409, detail="Only a completed batch run can be searched")
    value_count = db.scalar(
        select(func.count()).select_from(ReviewBatchRunValue).where(ReviewBatchRunValue.review_batch_run_id == run.id)
    )
    if not value_count:
        for decision_result in db.scalars(
            select(ReviewDecisionResult).where(ReviewDecisionResult.review_batch_run_id == run.id)
        ):
            try:
                materialize_decision_recommendations(
                    db,
                    matter_id=matter.id,
                    review_run_id=run.id,
                    document_id=decision_result.matter_document_id,
                    decision_result=decision_result,
                    recommendations=decision_result.recommendations,
                )
            except MatterAnalysisTaskConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        db.flush()
        value_count = db.scalar(
            select(func.count())
            .select_from(ReviewBatchRunValue)
            .where(ReviewBatchRunValue.review_batch_run_id == run.id)
        )
    if not value_count:
        raise HTTPException(status_code=409, detail="This batch run has no coding values to search")

    selection = db.get(ReviewBatchSearchCodingRun, batch.id)
    active_generation_id = db.scalar(
        select(SearchIndexGeneration.id).where(
            SearchIndexGeneration.matter_id == matter.id,
            SearchIndexGeneration.status == "ACTIVE",
        )
    )
    projection = db.get(ReviewBatchSearchCodingProjection, (batch.id, run.id))
    prior_operation = _latest_batch_coding_operation(db, batch, run.id)
    now = utcnow()
    projection_is_current = (
        active_generation_id is not None
        and projection is not None
        and projection.status == "READY"
        and projection.search_index_generation_id == active_generation_id
    )
    projection_is_pending = projection is not None and projection.status in {"QUEUED", "SYNCING"}
    if prior_operation is not None and prior_operation.status == "AWAITING_USER":
        prior_operation.status = "FAILED"
        prior_operation.error_message = "Superseded by a Batch Coding projection retry"
        prior_operation.completed_at = now
        projection_is_pending = False
    if selection is None:
        selection = ReviewBatchSearchCodingRun(
            review_batch_id=batch.id,
            review_batch_run_id=run.id,
            status=projection.status if projection_is_pending else "READY" if projection_is_current else "QUEUED",
            selected_by_user_id=principal.user.id,
            selected_at=now,
            projected_at=projection.projected_at if projection_is_current else None,
        )
        db.add(selection)
    else:
        selection.review_batch_run_id = run.id
        selection.status = (
            projection.status if projection_is_pending else "READY" if projection_is_current else "QUEUED"
        )
        selection.selected_by_user_id = principal.user.id
        selection.selected_at = now
        selection.projected_at = projection.projected_at if projection_is_current else None
        selection.error_message = projection.error_message if projection_is_pending else None

    if projection_is_current or projection_is_pending:
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=principal.user.id,
            action="review_batch.coding_search.selected",
            target_type="review_batch_run",
            target_id=run.id,
            details={
                "batch_id": str(batch.id),
                "search_projection_operation_id": None,
                "reused_projection": projection_is_current,
                "projection_status": projection.status,
            },
        )
        db.commit()
        return _coding_selection_read(selection)

    if projection is None:
        projection = ReviewBatchSearchCodingProjection(
            review_batch_id=batch.id,
            review_batch_run_id=run.id,
            status="QUEUED",
            requested_by_user_id=principal.user.id,
        )
        db.add(projection)
    else:
        projection.status = "QUEUED"
        projection.requested_by_user_id = principal.user.id
        projection.search_index_generation_id = None
        projection.projected_at = None
        projection.error_message = None
    db.flush()
    operation = create_search_operation(
        db,
        matter_id=matter.id,
        kind="BATCH_CODING_SYNC",
        payload={"batch_id": str(batch.id), "run_id": str(run.id)},
        created_by_user_id=principal.user.id,
    )
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="review_batch.coding_search.selected",
        target_type="review_batch_run",
        target_id=run.id,
        details={
            "batch_id": str(batch.id),
            "search_projection_operation_id": str(operation.id),
            "reused_projection": False,
        },
    )
    db.commit()
    return _coding_selection_read(selection)


@router.post("/{batch_id}/classifier-runs", response_model=ReviewBatchRunRead, status_code=202)
def start_review_batch_classifier_run(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchClassifierRunCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRun:
    matter = _matter(db, matter_id, principal, "batch.configure")
    if not can(db, principal, "classification.score", ResourceRef.matter(matter.id)):
        raise HTTPException(status_code=403, detail="Permission required: classification.score")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    try:
        run, _ = create_classifier_projection_run(
            db,
            matter=matter,
            batch=batch,
            payload=payload,
            actor_user_id=principal.user.id,
        )
        db.commit()
        db.refresh(run)
        return run
    except ClassificationProjectionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError:
        db.rollback()
        try:
            run, _ = create_classifier_projection_run(
                db,
                matter=matter,
                batch=batch,
                payload=payload,
                actor_user_id=principal.user.id,
            )
            db.commit()
            db.refresh(run)
            return run
        except (ClassificationProjectionError, IntegrityError) as conflict:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Classifier projection request conflicted with another request",
            ) from conflict


@router.get("/{batch_id}/analysis-runs", response_model=list[ReviewBatchAnalysisJobRead])
def list_review_batch_analysis_runs(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ReviewBatchAnalysisJobRead]:
    """Return every non-archived Analysis Task job scoped to this batch."""

    _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)

    review_runs = list(
        db.scalars(
            select(ReviewBatchRun)
            .where(
                ReviewBatchRun.review_batch_id == batch.id,
                ReviewBatchRun.run_type == "WORKFLOW",
                job_not_archived(ANALYSIS_TASK_BATCH_RUN_JOB_TYPE, ReviewBatchRun.id),
            )
            .options(selectinload(ReviewBatchRun.current_workflow_run))
            .order_by(ReviewBatchRun.created_at.desc(), ReviewBatchRun.id.desc())
        )
    )
    classification_rows = db.execute(
        select(
            MatterAnalysisTaskRun,
            MatterAnalysisTask.name,
            MatterAnalysisTaskVersion.version,
        )
        .join(
            MatterAnalysisTask,
            MatterAnalysisTask.id == MatterAnalysisTaskRun.matter_analysis_task_id,
        )
        .join(
            MatterAnalysisTaskVersion,
            MatterAnalysisTaskVersion.id == MatterAnalysisTaskRun.matter_analysis_task_version_id,
        )
        .where(
            MatterAnalysisTaskRun.matter_id == matter_id,
            MatterAnalysisTaskRun.scope_type == "REVIEW_BATCH",
            MatterAnalysisTaskRun.scope_id == batch.id,
            MatterAnalysisTaskRun.run_kind == "CLASSIFICATION",
            job_not_archived(CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE, MatterAnalysisTaskRun.id),
        )
        .options(
            selectinload(MatterAnalysisTaskRun.dataset_snapshot),
            selectinload(MatterAnalysisTaskRun.current_workflow_run),
        )
        .order_by(MatterAnalysisTaskRun.created_at.desc(), MatterAnalysisTaskRun.id.desc())
    ).all()

    jobs: list[ReviewBatchAnalysisJobRead] = []
    for run in review_runs:
        snapshot = run.configuration_snapshot or {}
        if snapshot.get("mode") != "BATCH":
            continue
        raw_task_id = snapshot.get("task_id")
        try:
            task_id = uuid.UUID(str(raw_task_id)) if raw_task_id is not None else None
        except ValueError:
            logger.warning("Batch analysis run %s has an invalid task id", run.id)
            task_id = None
        raw_task_version = snapshot.get("task_version")
        try:
            task_version = int(raw_task_version) if raw_task_version is not None else None
        except (TypeError, ValueError):
            logger.warning("Batch analysis run %s has an invalid task version", run.id)
            task_version = None
        jobs.append(
            ReviewBatchAnalysisJobRead(
                id=run.id,
                job_type=ANALYSIS_TASK_BATCH_RUN_JOB_TYPE,
                task_type="QUESTION_ANSWERING",
                matter_analysis_task_id=task_id,
                task_name=str(snapshot.get("task_name") or "Question answering run"),
                task_version=task_version,
                status=run.status,
                created_at=run.created_at,
                updated_at=run.updated_at,
                execution=JobExecutionRead.model_validate(run.execution) if run.execution is not None else None,
                review_batch_run=ReviewBatchRunRead.model_validate(run),
            )
        )

    for run, task_name, task_version in classification_rows:
        jobs.append(
            ReviewBatchAnalysisJobRead(
                id=run.id,
                job_type=CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE,
                task_type="CLASSIFICATION",
                matter_analysis_task_id=run.matter_analysis_task_id,
                task_name=task_name,
                task_version=task_version,
                status=run.status,
                created_at=run.created_at,
                updated_at=run.updated_at,
                execution=JobExecutionRead.model_validate(run.execution) if run.execution is not None else None,
                classification_analysis_run=MatterAnalysisTaskRunRead.model_validate(run),
            )
        )

    return sorted(jobs, key=lambda job: (job.created_at, job.id), reverse=True)


@router.post("/{batch_id}/analysis-runs", response_model=ReviewBatchRunRead, status_code=202)
def start_review_batch_analysis_run(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchAnalysisRunCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRun:
    matter = _matter(db, matter_id, principal, "batch.run_analysis")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    task = db.scalar(
        select(MatterAnalysisTask).where(
            MatterAnalysisTask.id == payload.matter_analysis_task_id,
            MatterAnalysisTask.matter_id == matter.id,
            MatterAnalysisTask.status == "ACTIVE",
        )
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Analysis task not found")
    if task.task_type != "QUESTION_ANSWERING":
        raise HTTPException(
            status_code=409,
            detail="Classification tasks must be run from the matter Analysis tab",
        )
    requested_version_id = payload.matter_analysis_task_version_id
    if requested_version_id is None and batch.analysis_task_batch_set_id is not None:
        batch_set = db.get(AnalysisTaskBatchSet, batch.analysis_task_batch_set_id)
        requested_version_id = batch_set.matter_analysis_task_version_id if batch_set else None
    if requested_version_id is not None:
        task_version = db.scalar(
            select(MatterAnalysisTaskVersion).where(
                MatterAnalysisTaskVersion.id == requested_version_id,
                MatterAnalysisTaskVersion.matter_analysis_task_id == task.id,
            )
        )
    else:
        if task.published_version is None:
            raise HTTPException(status_code=409, detail="Analysis task has no published version")
        task_version = db.scalar(
            select(MatterAnalysisTaskVersion).where(
                MatterAnalysisTaskVersion.matter_analysis_task_id == task.id,
                MatterAnalysisTaskVersion.version == task.published_version,
            )
        )
    if task_version is None:
        raise HTTPException(status_code=409, detail="Published analysis task version is unavailable")
    try:
        workflow, run = queue_analysis_task_batch(
            db,
            matter=matter,
            batch=batch,
            task=task,
            task_version=task_version,
            initiated_by_user_id=principal.user.id,
        )
    except MatterAnalysisTaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="review_batch.analysis.started",
        target_type="review_batch_run",
        target_id=run.id,
        details={
            "batch_id": str(batch.id),
            "workflow_run_id": str(workflow.id),
            "task_id": str(task.id),
            "task_version_id": str(task_version.id),
            "task_version": task_version.version,
            "analysis_task_batch_set_id": (
                str(batch.analysis_task_batch_set_id) if batch.analysis_task_batch_set_id else None
            ),
            "batch_set_ordinal": batch.batch_set_ordinal,
        },
    )
    db.commit()
    return run


@router.get(
    "/{batch_id}/runs/{run_id}/promotion",
    response_model=ReviewBatchRunPromotionRead | None,
)
def get_review_batch_run_promotion(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRunPromotion | None:
    _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    run = _run(db, batch.id, run_id)
    return db.scalar(select(ReviewBatchRunPromotion).where(ReviewBatchRunPromotion.review_batch_run_id == run.id))


@router.post(
    "/{batch_id}/runs/{run_id}/promotion",
    response_model=ReviewBatchRunPromotionRead,
    status_code=202,
)
def start_review_batch_run_promotion(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRunPromotion:
    matter = _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    run = _run(db, batch.id, run_id)
    if run.run_type == "CLASSIFIER":
        raise HTTPException(
            status_code=409,
            detail="Classifier projection runs cannot be promoted directly to matter coding",
        )
    permission_key = "batch.publish_analysis"
    if not can(db, principal, permission_key, ResourceRef.matter(matter.id)):
        raise HTTPException(status_code=403, detail=f"Permission required: {permission_key}")
    try:
        promotion = queue_analysis_task_promotion(
            db, matter=matter, batch=batch, run=run, actor_user_id=principal.user.id
        )
    except AnalysisTaskPromotionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="review_batch.analysis.promotion_requested",
        target_type="review_batch_run",
        target_id=run.id,
        details={
            "batch_id": str(batch.id),
            "promotion_id": str(promotion.id),
            "mode": promotion.mode,
            "destination_fields": promotion.destination_snapshot,
        },
    )
    db.commit()
    return promotion


@router.get(
    "/{batch_id}/reviewer-coding/{reviewer_coding_id}/promotion",
    response_model=ReviewBatchRunPromotionRead | None,
)
def get_review_batch_reviewer_promotion(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    reviewer_coding_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRunPromotion | None:
    _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    coding = db.get(ReviewBatchReviewerCoding, reviewer_coding_id)
    if coding is None or coding.review_batch_id != batch.id:
        raise HTTPException(status_code=404, detail="Reviewer coding not found")
    return db.scalar(
        select(ReviewBatchRunPromotion)
        .where(
            ReviewBatchRunPromotion.reviewer_coding_id == coding.id,
            ReviewBatchRunPromotion.reviewer_coding_revision == coding.revision,
        )
        .order_by(ReviewBatchRunPromotion.created_at.desc())
        .limit(1)
    )


@router.post(
    "/{batch_id}/reviewer-coding/{reviewer_coding_id}/promotion",
    response_model=ReviewBatchRunPromotionRead,
    status_code=202,
)
def start_review_batch_reviewer_promotion(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    reviewer_coding_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRunPromotion:
    matter = _matter(db, matter_id, principal, "batch.publish_coding")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    coding = db.get(ReviewBatchReviewerCoding, reviewer_coding_id)
    if coding is None or coding.review_batch_id != batch.id:
        raise HTTPException(status_code=404, detail="Reviewer coding not found")
    try:
        promotion = queue_human_batch_promotion(
            db,
            matter=matter,
            batch=batch,
            reviewer_coding=coding,
            actor_user_id=principal.user.id,
        )
    except AnalysisTaskPromotionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="review_batch.human_coding_promotion_requested",
        target_type="review_batch_reviewer_coding",
        target_id=coding.id,
        details={
            "batch_id": str(batch.id),
            "reviewer_user_id": str(coding.reviewer_user_id),
            "reviewer_revision": coding.revision,
            "promotion_id": str(promotion.id),
            "mode": promotion.mode,
            "destination_fields": promotion.destination_snapshot,
        },
    )
    db.commit()
    return promotion


@router.get(
    "/{batch_id}/runs/{run_id}/documents/{document_id}/analysis",
    response_model=ReviewBatchDocumentAnalysisRead,
)
def get_review_batch_document_analysis(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    run_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchDocumentAnalysisRead:
    matter = _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    run = _run(db, batch.id, run_id)
    if run.run_type != "WORKFLOW" or run.purpose != "ASSESSMENT" or run.workflow_run_record_id is None:
        raise HTTPException(status_code=409, detail="This run does not contain assessment analyses")
    run_document = db.get(ReviewBatchRunDocument, (run.id, document_id))
    if run_document is None:
        raise HTTPException(status_code=404, detail="Assessment document not found")
    skill_run = db.scalar(
        select(SkillRun)
        .where(
            SkillRun.workflow_run_id == run.workflow_run_record_id,
            SkillRun.scope_type == "MATTER_DOCUMENT",
            SkillRun.scope_id == document_id,
            SkillRun.status == "COMPLETED",
        )
        .order_by(SkillRun.created_at.desc())
        .limit(1)
    )
    artifact_id = skill_run.output_artifact_id if skill_run is not None else None
    analysis = None
    if artifact_id is not None:
        try:
            analysis = json.loads(
                read_artifact_bytes(
                    artifact_id=artifact_id,
                    actor_user_id=principal.user.id,
                    tenant_id=matter.client.tenant_id,
                    client_id=matter.client_id,
                )
            )
        except (ValueError, PermissionError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=409, detail="Assessment analysis artifact is unavailable") from exc
    return ReviewBatchDocumentAnalysisRead(
        review_batch_run_id=run.id,
        matter_document_id=document_id,
        status=run_document.status,
        output_artifact_id=artifact_id,
        analysis=analysis,
    )


@router.get(
    "/{batch_id}/runs/{run_id}/documents/{document_id}/decision-result",
    response_model=ReviewDecisionResultRead,
)
def get_review_batch_document_decision_result(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    run_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewDecisionResult:
    _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    run = _run(db, batch.id, run_id)
    result = db.scalar(
        select(ReviewDecisionResult).where(
            ReviewDecisionResult.review_batch_run_id == run.id,
            ReviewDecisionResult.matter_document_id == document_id,
        )
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Decision Result not found")
    return result


@router.post("/{batch_id}/reviewer-coding", response_model=ReviewBatchReviewerCodingRead)
def get_or_create_review_batch_reviewer_coding(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchReviewerCoding:
    matter = _matter(db, matter_id, principal, "batch.review")
    batch = db.scalar(
        select(ReviewBatch).where(ReviewBatch.id == batch_id, ReviewBatch.matter_id == matter_id).with_for_update()
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Review batch not found")
    _require_batch_assignment(db, principal, batch)
    if batch.status != "READY":
        raise HTTPException(status_code=409, detail="Review batch is not ready")
    if batch.assigned_user_id not in {None, principal.user.id}:
        raise HTTPException(status_code=403, detail="This review batch is assigned to another user")
    if batch.assigned_user_id is None:
        batch.assigned_user_id = principal.user.id
        batch.assigned_by_user_id = principal.user.id
        batch.assigned_at = utcnow()

    coding = db.scalar(
        select(ReviewBatchReviewerCoding)
        .where(
            ReviewBatchReviewerCoding.review_batch_id == batch.id,
            ReviewBatchReviewerCoding.reviewer_user_id == principal.user.id,
        )
        .with_for_update()
    )
    if coding is None:
        coding = ReviewBatchReviewerCoding(
            review_batch_id=batch.id,
            reviewer_user_id=principal.user.id,
            revision=0,
        )
        db.add(coding)
        db.flush()
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=principal.user.id,
            action="review_batch.reviewer_coding.started",
            target_type="review_batch_reviewer_coding",
            target_id=coding.id,
            details={"batch_id": str(batch.id)},
        )
    db.commit()
    return coding


@router.get("/{batch_id}/reviewer-coding/progress", response_model=ReviewBatchReviewerProgressRead)
def get_review_batch_reviewer_progress(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchReviewerProgressRead:
    _matter(db, matter_id, principal, "batch.review")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    coding = db.scalar(
        select(ReviewBatchReviewerCoding).where(
            ReviewBatchReviewerCoding.review_batch_id == batch.id,
            ReviewBatchReviewerCoding.reviewer_user_id == principal.user.id,
        )
    )
    if coding is None:
        raise HTTPException(status_code=404, detail="Reviewer coding has not been started")
    counts = dict(
        db.execute(
            select(ReviewBatchReviewerDocument.status, func.count())
            .where(ReviewBatchReviewerDocument.reviewer_coding_id == coding.id)
            .group_by(ReviewBatchReviewerDocument.status)
        ).all()
    )
    in_progress = int(counts.get("IN_PROGRESS", 0))
    completed = int(counts.get("COMPLETED", 0))
    skipped = int(counts.get("SKIPPED", 0))
    return ReviewBatchReviewerProgressRead(
        reviewer_coding_id=coding.id,
        document_count=batch.document_count,
        not_started_count=max(0, batch.document_count - in_progress - completed - skipped),
        in_progress_count=in_progress,
        completed_count=completed,
        skipped_count=skipped,
    )


@router.get("/{batch_id}/runs/{run_id}/progress", response_model=ReviewBatchRunProgressRead)
def get_review_batch_run_progress(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchRunProgressRead:
    _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    run = _run(db, batch.id, run_id)
    counts = dict(
        db.execute(
            select(ReviewBatchRunDocument.status, func.count())
            .where(ReviewBatchRunDocument.review_batch_run_id == run.id)
            .group_by(ReviewBatchRunDocument.status)
        ).all()
    )
    in_progress = int(counts.get("IN_PROGRESS", 0))
    completed = int(counts.get("COMPLETED", 0))
    skipped = int(counts.get("SKIPPED", 0))
    failed = int(counts.get("FAILED", 0))
    return ReviewBatchRunProgressRead(
        review_batch_run_id=run.id,
        document_count=batch.document_count,
        not_started_count=max(0, batch.document_count - in_progress - completed - skipped - failed),
        in_progress_count=in_progress,
        completed_count=completed,
        skipped_count=skipped,
        failed_count=failed,
    )


@router.get(
    "/{batch_id}/reviewer-coding/documents/{document_id}",
    response_model=ReviewBatchDocumentCodingRead,
)
def get_review_batch_document_coding(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchDocumentCodingRead:
    _matter(db, matter_id, principal, "batch.review")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    coding = db.scalar(
        select(ReviewBatchReviewerCoding).where(
            ReviewBatchReviewerCoding.review_batch_id == batch.id,
            ReviewBatchReviewerCoding.reviewer_user_id == principal.user.id,
        )
    )
    if coding is None:
        raise HTTPException(status_code=404, detail="Reviewer coding has not been started")
    if db.get(ReviewBatchDocument, (batch.id, document_id)) is None:
        raise HTTPException(status_code=404, detail="Document is not in this review batch")
    state = db.get(ReviewBatchReviewerDocument, (coding.id, document_id))
    values = list(
        db.scalars(
            select(ReviewBatchReviewerValue)
            .where(
                ReviewBatchReviewerValue.reviewer_coding_id == coding.id,
                ReviewBatchReviewerValue.matter_document_id == document_id,
            )
            .order_by(ReviewBatchReviewerValue.metadata_definition_id, ReviewBatchReviewerValue.value_ordinal)
        )
    )
    reviewer_values: list[ReviewBatchReviewerValueRead] = []
    if batch.reviewer_value_visibility == "ALL_REVIEWER_VALUES":
        other_rows = db.execute(
            select(ReviewBatchReviewerValue, ReviewBatchReviewerCoding, User)
            .join(ReviewBatchReviewerCoding, ReviewBatchReviewerCoding.id == ReviewBatchReviewerValue.reviewer_coding_id)
            .join(User, User.id == ReviewBatchReviewerCoding.reviewer_user_id)
            .where(
                ReviewBatchReviewerCoding.review_batch_id == batch.id,
                ReviewBatchReviewerCoding.id != coding.id,
                ReviewBatchReviewerValue.matter_document_id == document_id,
            )
            .order_by(
                ReviewBatchReviewerCoding.created_at.desc(),
                ReviewBatchReviewerValue.metadata_definition_id,
                ReviewBatchReviewerValue.value_ordinal,
            )
        ).all()
        grouped: dict[tuple[uuid.UUID, uuid.UUID], ReviewBatchReviewerValueRead] = {}
        for value, other_coding, user in other_rows:
            key = (other_coding.id, value.metadata_definition_id)
            visible = grouped.get(key)
            if visible is None:
                visible = ReviewBatchReviewerValueRead(
                    reviewer_coding_id=other_coding.id,
                    actor_user_id=user.id,
                    actor_user=MatterSavedSearchUserRead(
                        id=user.id,
                        display_name=user.display_name,
                        email=user.email,
                    ),
                    metadata_definition_id=value.metadata_definition_id,
                    values=[],
                )
                grouped[key] = visible
            visible.values.append(event_value(value))
        reviewer_values = list(grouped.values())
    return ReviewBatchDocumentCodingRead(
        matter_document_id=document_id,
        review_status=state.status if state else "NOT_STARTED",
        values=[
            ReviewBatchReviewerCodingValueRead(
                reviewer_coding_id=value.reviewer_coding_id,
                matter_document_id=value.matter_document_id,
                metadata_definition_id=value.metadata_definition_id,
                value_ordinal=value.value_ordinal,
                value=event_value(value),
            )
            for value in values
        ],
        reviewer_values=reviewer_values,
    )


def _history_value_label(value: Any, snapshot: dict[str, Any]) -> str:
    if snapshot.get("type") == "ENUM" and isinstance(value, str):
        for option in snapshot.get("allowed_values") or []:
            if isinstance(option, dict) and option.get("key") == value:
                label = option.get("label")
                if isinstance(label, str) and label.strip():
                    return label
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None or value == "":
        return "Not set"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def review_batch_document_coding_history(
    db: Session,
    matter_id: uuid.UUID,
    batch: ReviewBatch,
    document_id: uuid.UUID,
) -> list[ReviewBatchCodingHistoryEntryRead]:
    """List immutable human revisions and immutable evaluation values for a document."""

    if db.get(ReviewBatchDocument, (batch.id, document_id)) is None:
        raise HTTPException(status_code=404, detail="Document is not in this review batch")

    field_rows = db.execute(
        select(ReviewBatchCodingField, MetadataDefinition)
        .join(
            ReviewBatchCodingGroup,
            ReviewBatchCodingGroup.id == ReviewBatchCodingField.review_batch_coding_group_id,
        )
        .outerjoin(MetadataDefinition, MetadataDefinition.id == ReviewBatchCodingField.metadata_definition_id)
        .where(ReviewBatchCodingGroup.review_batch_id == batch.id)
        .order_by(ReviewBatchCodingGroup.sort_order, ReviewBatchCodingField.sort_order)
    ).all()
    field_details: dict[uuid.UUID, tuple[str, str, dict[str, Any]]] = {}
    for field, definition in field_rows:
        if field.metadata_definition_id in field_details:
            continue
        snapshot = field.definition_snapshot or {}
        key = snapshot.get("key") if isinstance(snapshot.get("key"), str) else None
        display_name = snapshot.get("display_name") if isinstance(snapshot.get("display_name"), str) else None
        field_details[field.metadata_definition_id] = (
            key or (definition.key if definition is not None else str(field.metadata_definition_id)),
            display_name or (definition.display_name if definition is not None else "Coding field"),
            snapshot,
        )

    rows = db.execute(
        select(ReviewBatchRunValue, ReviewBatchRun, User, ReviewDecisionResult)
        .join(ReviewBatchRun, ReviewBatchRun.id == ReviewBatchRunValue.review_batch_run_id)
        .outerjoin(User, User.id == ReviewBatchRun.actor_user_id)
        .outerjoin(
            ReviewDecisionResult,
            and_(
                ReviewDecisionResult.review_batch_run_id == ReviewBatchRunValue.review_batch_run_id,
                ReviewDecisionResult.matter_document_id == ReviewBatchRunValue.matter_document_id,
            ),
        )
        .where(
            ReviewBatchRun.review_batch_id == batch.id,
            ReviewBatchRunValue.matter_document_id == document_id,
        )
        .order_by(
            ReviewBatchRunValue.updated_at.desc(),
            ReviewBatchRunValue.created_at.desc(),
            ReviewBatchRunValue.metadata_definition_id,
            ReviewBatchRunValue.value_ordinal,
        )
    ).all()
    reviewer_field_rows = db.execute(
        select(
            ReviewBatchReviewerFieldRevision,
            ReviewBatchReviewerValueRevision,
            ReviewBatchReviewerRevision,
            ReviewBatchReviewerCoding,
            User,
        )
        .join(
            ReviewBatchReviewerRevision,
            and_(
                ReviewBatchReviewerRevision.reviewer_coding_id
                == ReviewBatchReviewerFieldRevision.reviewer_coding_id,
                ReviewBatchReviewerRevision.revision == ReviewBatchReviewerFieldRevision.revision,
            ),
        )
        .join(
            ReviewBatchReviewerCoding,
            ReviewBatchReviewerCoding.id == ReviewBatchReviewerFieldRevision.reviewer_coding_id,
        )
        .join(User, User.id == ReviewBatchReviewerCoding.reviewer_user_id)
        .outerjoin(
            ReviewBatchReviewerValueRevision,
            and_(
                ReviewBatchReviewerValueRevision.reviewer_coding_id
                == ReviewBatchReviewerFieldRevision.reviewer_coding_id,
                ReviewBatchReviewerValueRevision.revision == ReviewBatchReviewerFieldRevision.revision,
                ReviewBatchReviewerValueRevision.matter_document_id
                == ReviewBatchReviewerFieldRevision.matter_document_id,
                ReviewBatchReviewerValueRevision.metadata_definition_id
                == ReviewBatchReviewerFieldRevision.metadata_definition_id,
            ),
        )
        .where(
            ReviewBatchReviewerCoding.review_batch_id == batch.id,
            ReviewBatchReviewerFieldRevision.matter_document_id == document_id,
        )
        .order_by(
            ReviewBatchReviewerRevision.revision.desc(),
            ReviewBatchReviewerFieldRevision.metadata_definition_id,
            ReviewBatchReviewerValueRevision.value_ordinal,
        )
    ).all()
    missing_definition_ids = {
        value.metadata_definition_id for value, _, _, _ in rows if value.metadata_definition_id not in field_details
    }
    missing_definition_ids.update(
        field.metadata_definition_id
        for field, _, _, _, _ in reviewer_field_rows
        if field.metadata_definition_id not in field_details
    )
    if missing_definition_ids:
        for definition in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter_id,
                MetadataDefinition.id.in_(missing_definition_ids),
            )
        ):
            field_details[definition.id] = (definition.key, definition.display_name, {})

    agent_version_ids = {
        run.agent_definition_version_id for _, run, _, _ in rows if run.agent_definition_version_id is not None
    }
    agent_names: dict[uuid.UUID, str] = {}
    if agent_version_ids:
        agent_names = {
            version_id: name
            for version_id, name in db.execute(
                select(AgentDefinitionVersion.id, AgentDefinition.name)
                .join(AgentDefinition, AgentDefinition.id == AgentDefinitionVersion.agent_definition_id)
                .where(AgentDefinitionVersion.id.in_(agent_version_ids))
            )
        }

    history: list[ReviewBatchCodingHistoryEntryRead] = []
    reviewer_document_rows = db.execute(
        select(
            ReviewBatchReviewerDocumentRevision,
            ReviewBatchReviewerRevision,
            ReviewBatchReviewerCoding,
            User,
        )
        .join(
            ReviewBatchReviewerRevision,
            and_(
                ReviewBatchReviewerRevision.reviewer_coding_id
                == ReviewBatchReviewerDocumentRevision.reviewer_coding_id,
                ReviewBatchReviewerRevision.revision == ReviewBatchReviewerDocumentRevision.revision,
            ),
        )
        .join(
            ReviewBatchReviewerCoding,
            ReviewBatchReviewerCoding.id == ReviewBatchReviewerDocumentRevision.reviewer_coding_id,
        )
        .join(User, User.id == ReviewBatchReviewerCoding.reviewer_user_id)
        .where(
            ReviewBatchReviewerCoding.review_batch_id == batch.id,
            ReviewBatchReviewerDocumentRevision.matter_document_id == document_id,
        )
        .order_by(ReviewBatchReviewerRevision.revision.desc())
    ).all()
    for document_revision, revision, coding, user in reviewer_document_rows:
        history.append(
            ReviewBatchCodingHistoryEntryRead(
                reviewer_coding_id=coding.id,
                reviewer_revision=revision.revision,
                reviewer_origin=revision.origin,
                event_kind="STATUS",
                document_status=document_revision.status,
                metadata_definition_id=None,
                field_key="__review_status__",
                field_display_name="Review status",
                value=document_revision.status,
                value_label=document_revision.status.replace("_", " ").title(),
                recorded_at=revision.created_at,
                source_kind="HUMAN",
                source_label=user.email,
                source_detail=user.display_name,
                score=None,
                score_kind=None,
                question_key=None,
                review_decision_result_id=None,
            )
        )
    for field, value, revision, coding, user in reviewer_field_rows:
        raw_value = event_value(value) if value is not None else None
        field_key, field_display_name, snapshot = field_details.get(
            field.metadata_definition_id,
            (str(field.metadata_definition_id), "Coding field", {}),
        )
        history.append(
            ReviewBatchCodingHistoryEntryRead(
                reviewer_coding_id=coding.id,
                reviewer_revision=revision.revision,
                reviewer_origin=revision.origin,
                event_kind="VALUE" if value is not None else "NO_VALUE",
                document_status=None,
                metadata_definition_id=field.metadata_definition_id,
                field_key=field_key,
                field_display_name=field_display_name,
                value=raw_value,
                value_label=_history_value_label(raw_value, snapshot) if value is not None else "No value",
                recorded_at=revision.created_at,
                source_kind="HUMAN",
                source_label=user.email,
                source_detail=user.display_name,
                score=None,
                score_kind=None,
                question_key=None,
                review_decision_result_id=None,
            )
        )
    for value, run, user, decision_result in rows:
        raw_value = event_value(value)
        field_key, field_display_name, snapshot = field_details.get(
            value.metadata_definition_id,
            (str(value.metadata_definition_id), "Coding field", {}),
        )
        if run.run_type == "AGENT":
            source_kind = "AGENT"
            source_label = agent_names.get(run.agent_definition_version_id, "Agent")
            model_key = run.configuration_snapshot.get("model_key")
            source_detail = str(model_key) if model_key else None
        elif run.run_type == "CLASSIFIER":
            source_kind = "CLASSIFIER"
            source_label = "Classifier"
            model_version_id = run.configuration_snapshot.get("model_version_id")
            source_detail = f"Model version {model_version_id}" if model_version_id else None
        elif decision_result is not None and (
            (decision_result.engine_key or "").casefold() == "jev"
            or (decision_result.provider or "").casefold() == "typesafe"
        ):
            source_kind = "JEV"
            source_label = "Jev"
            source_detail = decision_result.model
        else:
            source_kind = "WORKFLOW"
            source_label = "Workflow"
            configured_name = (
                run.configuration_snapshot.get("workflow_key")
                or run.configuration_snapshot.get("mode")
                or run.configuration_snapshot.get("engine_key")
            )
            source_detail = str(configured_name) if configured_name else None
        history.append(
            ReviewBatchCodingHistoryEntryRead(
                review_batch_run_id=run.id,
                reviewer_coding_id=None,
                run_type=run.run_type,
                purpose=run.purpose,
                metadata_definition_id=value.metadata_definition_id,
                field_key=field_key,
                field_display_name=field_display_name,
                value=raw_value,
                value_label=_history_value_label(raw_value, snapshot),
                recorded_at=value.updated_at,
                source_kind=source_kind,
                source_label=source_label,
                source_detail=source_detail,
                score=value.confidence,
                score_kind=value.confidence_kind,
                question_key=value.question_key,
                review_decision_result_id=value.review_decision_result_id,
            )
        )
    return sorted(history, key=lambda entry: entry.recorded_at, reverse=True)


@router.get(
    "/{batch_id}/documents/{document_id}/coding-history",
    response_model=list[ReviewBatchCodingHistoryEntryRead],
)
def list_review_batch_document_coding_history(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ReviewBatchCodingHistoryEntryRead]:
    _matter(db, matter_id, principal, "batch.review")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    return review_batch_document_coding_history(db, matter_id, batch, document_id)


@router.put(
    "/{batch_id}/reviewer-coding/documents/{document_id}/values",
    response_model=list[ReviewBatchReviewerCodingValueRead],
)
def replace_review_batch_reviewer_values(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    document_id: uuid.UUID,
    payload: ReviewBatchRunDocumentValues,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ReviewBatchReviewerCodingValueRead]:
    matter = _matter(db, matter_id, principal, "batch.review")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    coding = db.scalar(
        select(ReviewBatchReviewerCoding)
        .where(
            ReviewBatchReviewerCoding.review_batch_id == batch.id,
            ReviewBatchReviewerCoding.reviewer_user_id == principal.user.id,
        )
        .with_for_update()
    )
    if coding is None:
        raise HTTPException(status_code=404, detail="Reviewer coding has not been started")
    if payload.matter_document_id != document_id:
        raise HTTPException(status_code=422, detail="Document id does not match the request path")
    member = db.get(ReviewBatchDocument, (batch.id, document_id))
    if member is None:
        raise HTTPException(status_code=422, detail="Document is not in this review batch")
    allowed = set(
        db.scalars(
            select(ReviewBatchCodingField.metadata_definition_id)
            .join(ReviewBatchCodingGroup)
            .where(ReviewBatchCodingGroup.review_batch_id == batch.id)
        )
    )
    definitions = {
        definition.id: definition
        for definition in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter_id,
                MetadataDefinition.id.in_([field.metadata_definition_id for field in payload.fields]),
            )
        )
    }
    replacements: list[ReviewerFieldReplacement] = []
    for field in payload.fields:
        if field.metadata_definition_id not in allowed or field.metadata_definition_id not in definitions:
            raise HTTPException(status_code=422, detail="Field is not in the batch coding configuration")
        definition = definitions[field.metadata_definition_id]
        replacements.append(
            ReviewerFieldReplacement(
                definition=definition,
                values=tuple(field.values),
            )
        )
    try:
        append_reviewer_coding_revision(
            db,
            coding=coding,
            matter_document_id=document_id,
            status="COMPLETED",
            field_replacements=replacements,
            actor_user_id=principal.user.id,
            tenant_id=matter.client.tenant_id,
            matter_id=matter.id,
            origin=payload.origin,
            request_key=payload.request_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    rows = list(
        db.scalars(
            select(ReviewBatchReviewerValue)
            .where(
                ReviewBatchReviewerValue.reviewer_coding_id == coding.id,
                ReviewBatchReviewerValue.matter_document_id == document_id,
            )
            .order_by(ReviewBatchReviewerValue.metadata_definition_id, ReviewBatchReviewerValue.value_ordinal)
        )
    )
    return [
        ReviewBatchReviewerCodingValueRead(
            reviewer_coding_id=row.reviewer_coding_id,
            matter_document_id=row.matter_document_id,
            metadata_definition_id=row.metadata_definition_id,
            value_ordinal=row.value_ordinal,
            value=event_value(row),
        )
        for row in rows
    ]


@router.post(
    "/{batch_id}/reviewer-coding/documents/{document_id}/skip",
    response_model=ReviewBatchDocumentCodingRead,
)
def skip_review_batch_document(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    document_id: uuid.UUID,
    origin: Literal["REVIEW_UI", "IMPORT"] = Query(default="REVIEW_UI"),
    request_key: str | None = Query(default=None, min_length=1, max_length=200),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchDocumentCodingRead:
    matter = _matter(db, matter_id, principal, "batch.review")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    coding = db.scalar(
        select(ReviewBatchReviewerCoding)
        .where(
            ReviewBatchReviewerCoding.review_batch_id == batch.id,
            ReviewBatchReviewerCoding.reviewer_user_id == principal.user.id,
        )
        .with_for_update()
    )
    if coding is None:
        raise HTTPException(status_code=404, detail="Reviewer coding has not been started")
    if db.get(ReviewBatchDocument, (batch.id, document_id)) is None:
        raise HTTPException(status_code=404, detail="Document is not in this review batch")
    append_reviewer_coding_revision(
        db,
        coding=coding,
        matter_document_id=document_id,
        status="SKIPPED",
        field_replacements=(),
        actor_user_id=principal.user.id,
        tenant_id=matter.client.tenant_id,
        matter_id=matter.id,
        origin=origin,
        request_key=request_key,
    )
    db.commit()
    return get_review_batch_document_coding(
        matter_id=matter_id,
        batch_id=batch_id,
        document_id=document_id,
        principal=principal,
        db=db,
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _comparison_run_option(
    run: ReviewBatchRun,
    user: User | None,
    metadata_definition_ids: list[uuid.UUID],
    task_name: str | None = None,
) -> ReviewBatchComparisonSourceOptionRead:
    configuration = run.configuration_snapshot or {}
    if run.run_type == "WORKFLOW":
        label = task_name or str(configuration.get("task_name") or "Analysis task")
        version = configuration.get("task_version")
        provider = configuration.get("decision_provider")
        model = configuration.get("model_key")
        detail = (
            " · ".join(
                part
                for part in [
                    f"Published v{version}" if version is not None else None,
                    str(provider).replace("_", " ").title() if provider else None,
                    str(model) if model else None,
                ]
                if part
            )
            or None
        )
    elif run.run_type == "CLASSIFIER":
        label = task_name or "Classifier projection"
        model_version_id = configuration.get("model_version_id")
        minimum = configuration.get("minimum_confidence")
        maximum = configuration.get("maximum_confidence")
        confidence = None
        if minimum is not None or maximum is not None:
            lower = float(minimum) if minimum is not None else 0.0
            upper = float(maximum) if maximum is not None else 1.0
            confidence = f"confidence {lower:g}–{upper:g}"
        detail = (
            " · ".join(
                part
                for part in [
                    f"model {str(model_version_id)[:8]}" if model_version_id else None,
                    confidence,
                ]
                if part
            )
            or None
        )
    else:
        label = str(configuration.get("agent_name") or "Agent run")
        version = configuration.get("version")
        model = configuration.get("model_key")
        detail = (
            " · ".join(part for part in [f"v{version}" if version else None, str(model) if model else None] if part)
            or None
        )
    return ReviewBatchComparisonSourceOptionRead(
        id=run.id,
        source_type="RUN",
        run_type=run.run_type,
        purpose=run.purpose,
        status=run.status,
        label=label,
        detail=detail,
        metadata_definition_ids=metadata_definition_ids,
        created_at=run.created_at,
    )


def _comparison_run_options(
    db: Session,
    *,
    batch_id: uuid.UUID,
    source_ids: list[uuid.UUID] | None = None,
    selector_only: bool = False,
) -> list[ReviewBatchComparisonSourceOptionRead]:
    statement = (
        select(ReviewBatchRun, User)
        .outerjoin(User, User.id == ReviewBatchRun.actor_user_id)
        .where(ReviewBatchRun.review_batch_id == batch_id)
        .order_by(ReviewBatchRun.created_at.desc())
    )
    rows = db.execute(statement).all()
    task_names_by_run_id: dict[uuid.UUID, str] = {}
    workflow_task_ids_by_run_id: dict[uuid.UUID, uuid.UUID] = {}
    for run, _user in rows:
        if run.run_type != "WORKFLOW":
            continue
        raw_task_id = (run.configuration_snapshot or {}).get("task_id")
        if raw_task_id is None:
            continue
        try:
            workflow_task_ids_by_run_id[run.id] = uuid.UUID(str(raw_task_id))
        except ValueError:
            logger.warning("Comparison run %s has an invalid task id", run.id)
    if workflow_task_ids_by_run_id:
        task_names_by_id = dict(
            db.execute(
                select(MatterAnalysisTask.id, MatterAnalysisTask.name).where(
                    MatterAnalysisTask.id.in_(set(workflow_task_ids_by_run_id.values()))
                )
            ).all()
        )
        task_names_by_run_id.update(
            {
                run_id: task_names_by_id[task_id]
                for run_id, task_id in workflow_task_ids_by_run_id.items()
                if task_id in task_names_by_id
            }
        )

    classifier_scoring_run_ids = {
        run.classification_scoring_run_id
        for run, _user in rows
        if run.run_type == "CLASSIFIER" and run.classification_scoring_run_id is not None
    }
    if classifier_scoring_run_ids:
        classifier_task_names = {
            scoring_run_id: task_name
            for scoring_run_id, task_name in db.execute(
                select(
                    MatterAnalysisTaskRun.classification_scoring_run_id,
                    MatterAnalysisTask.name,
                )
                .join(
                    MatterAnalysisTask,
                    MatterAnalysisTask.id == MatterAnalysisTaskRun.matter_analysis_task_id,
                )
                .where(
                    MatterAnalysisTaskRun.classification_scoring_run_id.in_(classifier_scoring_run_ids)
                )
            )
            if scoring_run_id is not None
        }
        task_names_by_run_id.update(
            {
                run.id: classifier_task_names[run.classification_scoring_run_id]
                for run, _user in rows
                if run.classification_scoring_run_id in classifier_task_names
            }
        )
    field_ids: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for run_id, definition_id in db.execute(
        select(
            ReviewBatchRunValue.review_batch_run_id,
            ReviewBatchRunValue.metadata_definition_id,
        )
        .join(ReviewBatchRun, ReviewBatchRun.id == ReviewBatchRunValue.review_batch_run_id)
        .where(ReviewBatchRun.review_batch_id == batch_id)
        .distinct()
    ):
        field_ids[run_id].append(definition_id)
    latest_classifier_run_by_scoring_id: dict[uuid.UUID, uuid.UUID] = {}
    if selector_only:
        for run, _user in rows:
            if (
                run.run_type == "CLASSIFIER"
                and run.classification_scoring_run_id is not None
                and run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
            ):
                latest_classifier_run_by_scoring_id.setdefault(run.classification_scoring_run_id, run.id)
    options: dict[uuid.UUID, ReviewBatchComparisonSourceOptionRead] = {
        run.id: _comparison_run_option(
            run,
            user,
            sorted(field_ids[run.id], key=str),
            task_name=task_names_by_run_id.get(run.id),
        )
        for run, user in rows
        if not selector_only
        or (
            run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
            and bool(field_ids[run.id])
            and (run.run_type != "WORKFLOW" or (run.configuration_snapshot or {}).get("mode") == "BATCH")
            and (
                run.run_type != "CLASSIFIER"
                or latest_classifier_run_by_scoring_id.get(run.classification_scoring_run_id) == run.id
            )
        )
    }
    reviewer_rows = db.execute(
        select(ReviewBatchReviewerCoding, User)
        .join(User, User.id == ReviewBatchReviewerCoding.reviewer_user_id)
        .where(ReviewBatchReviewerCoding.review_batch_id == batch_id)
        .order_by(User.display_name, User.email)
    ).all()
    reviewer_field_ids: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for coding_id, definition_id in db.execute(
        select(
            ReviewBatchReviewerValue.reviewer_coding_id,
            ReviewBatchReviewerValue.metadata_definition_id,
        )
        .join(
            ReviewBatchReviewerCoding,
            ReviewBatchReviewerCoding.id == ReviewBatchReviewerValue.reviewer_coding_id,
        )
        .where(ReviewBatchReviewerCoding.review_batch_id == batch_id)
        .distinct()
    ):
        reviewer_field_ids[coding_id].append(definition_id)
    for coding, reviewer in reviewer_rows:
        options[coding.id] = ReviewBatchComparisonSourceOptionRead(
            id=coding.id,
            source_type="REVIEWER",
            label=reviewer.display_name,
            detail=reviewer.email,
            metadata_definition_ids=sorted(reviewer_field_ids[coding.id], key=str),
            created_at=coding.created_at,
        )
    if source_ids is None:
        return list(options.values())
    missing = [source_id for source_id in source_ids if source_id not in options]
    if missing:
        raise HTTPException(status_code=422, detail="Every comparison source must belong to this batch")
    return [options[source_id] for source_id in source_ids]


def _comparison_definition(db: Session, matter_id: uuid.UUID, definition_id: uuid.UUID) -> MetadataDefinition:
    definition = db.scalar(
        select(MetadataDefinition).where(
            MetadataDefinition.id == definition_id,
            MetadataDefinition.matter_id == matter_id,
        )
    )
    if definition is None:
        raise HTTPException(status_code=404, detail="Coding field not found")
    return definition


def _pinned_disagreement_selection(
    db: Session,
    *,
    batch: ReviewBatch,
    definition: MetadataDefinition,
    source_ids: list[uuid.UUID],
) -> tuple[
    list[dict[str, Any]],
    list[ReviewBatchDisagreementSourceSnapshotRead],
    list[uuid.UUID],
]:
    eligible_options = {
        source.id: source
        for source in _comparison_run_options(db, batch_id=batch.id, selector_only=True)
    }
    missing = [source_id for source_id in source_ids if source_id not in eligible_options]
    if missing:
        raise HTTPException(
            status_code=422,
            detail="Every source must be an eligible reviewer or completed analysis result from this batch",
        )
    source_options = [eligible_options[source_id] for source_id in source_ids]
    reviewer_ids = [source.id for source in source_options if source.source_type == "REVIEWER"]
    reviewer_revisions = {
        coding.id: coding.revision
        for coding in db.scalars(
            select(ReviewBatchReviewerCoding).where(ReviewBatchReviewerCoding.id.in_(reviewer_ids))
        )
    }
    if len(reviewer_revisions) != len(reviewer_ids):
        raise HTTPException(status_code=409, detail="A selected reviewer source is no longer available")

    source_snapshots: list[dict[str, Any]] = []
    source_reads: list[ReviewBatchDisagreementSourceSnapshotRead] = []
    for source in source_options:
        reviewer_revision = reviewer_revisions.get(source.id)
        source_snapshots.append(
            {
                **source.model_dump(mode="json"),
                "reviewer_revision": reviewer_revision,
            }
        )
        source_reads.append(
            ReviewBatchDisagreementSourceSnapshotRead(
                id=source.id,
                source_type=source.source_type,
                label=source.label,
                reviewer_revision=reviewer_revision,
            )
        )
    candidate_ids = comparison_document_ids(
        db,
        batch_id=batch.id,
        definition_id=definition.id,
        source_options=source_options,
        row_filter="DISAGREEMENT",
        reviewer_revisions=reviewer_revisions,
    )
    return source_snapshots, source_reads, candidate_ids


def _disagreement_preview(
    db: Session,
    *,
    batch: ReviewBatch,
    definition: MetadataDefinition,
    source_ids: list[uuid.UUID],
) -> tuple[ReviewBatchDisagreementBatchPreviewRead, list[dict[str, Any]]]:
    source_snapshots, source_reads, candidate_ids = _pinned_disagreement_selection(
        db,
        batch=batch,
        definition=definition,
        source_ids=source_ids,
    )
    return (
        ReviewBatchDisagreementBatchPreviewRead(
            source_batch_id=batch.id,
            metadata_definition_id=definition.id,
            field_display_name=definition.display_name,
            candidate_count=len(candidate_ids),
            sources=source_reads,
        ),
        source_snapshots,
    )


def _disagreement_display_value(definition: MetadataDefinition, value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    labels = {
        str(item.get("key")): str(item.get("label") or item.get("key"))
        for item in definition.allowed_values or []
        if item.get("key") is not None
    }
    return labels.get(str(value), str(value))


@router.get(
    "/{batch_id}/disagreements/documents/{document_id}",
    response_model=ReviewBatchDisagreementDocumentRead,
)
def get_disagreement_review_document(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchDisagreementDocumentRead:
    _matter(db, matter_id, principal, "batch.review")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    if batch.selection_type != "COMPARISON_RESULT" or batch.source_batch_id is None:
        raise HTTPException(status_code=404, detail="This batch has no disagreement review context")
    if db.get(ReviewBatchDocument, (batch.id, document_id)) is None:
        raise HTTPException(status_code=404, detail="Document is not in this review batch")

    raw_definition_id = batch.selection_definition.get("metadata_definition_id")
    raw_sources = batch.selection_definition.get("sources")
    if raw_definition_id is None or not isinstance(raw_sources, list) or len(raw_sources) < 2:
        raise HTTPException(status_code=409, detail="Disagreement source provenance is incomplete")
    try:
        definition_id = uuid.UUID(str(raw_definition_id))
        source_options = [ReviewBatchComparisonSourceOptionRead.model_validate(item) for item in raw_sources]
        reviewer_revisions = {
            source.id: int(raw_source["reviewer_revision"])
            for source, raw_source in zip(source_options, raw_sources, strict=True)
            if source.source_type == "REVIEWER"
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="Disagreement source provenance is invalid") from exc
    definition = db.get(MetadataDefinition, definition_id)
    if definition is None or definition.matter_id != matter_id:
        raise HTTPException(status_code=409, detail="The compared coding field is unavailable")

    comparison = comparison_document(
        db,
        batch_id=batch.source_batch_id,
        matter_document_id=document_id,
        definition_id=definition.id,
        source_options=source_options,
        reviewer_revisions=reviewer_revisions,
    )
    if comparison is None or comparison.agreement_status not in {"PARTIAL", "DISAGREEMENT"}:
        raise HTTPException(status_code=409, detail="The frozen disagreement could not be replayed")

    return ReviewBatchDisagreementDocumentRead(
        review_batch_id=batch.id,
        source_batch_id=batch.source_batch_id,
        matter_document_id=document_id,
        metadata_definition_id=definition.id,
        field_display_name=definition.display_name,
        field_type=definition.type,
        cardinality=definition.cardinality,
        allowed_values=definition.allowed_values or [],
        agreement_status=comparison.agreement_status,
        sources=[
            ReviewBatchDisagreementDocumentSourceRead(
                id=source.id,
                source_type=source.source_type,
                run_type=source.run_type,
                label=source.label,
                detail=source.detail,
                reviewer_revision=reviewer_revisions.get(source.id),
                state=comparison.sources[source.id].state,
                values=list(comparison.sources[source.id].values),
                display_values=[
                    _disagreement_display_value(definition, value)
                    for value in comparison.sources[source.id].values
                ],
                top_confidence=comparison.sources[source.id].top_confidence,
            )
            for source in source_options
        ],
    )


@router.get("/{batch_id}/comparison-options", response_model=ReviewBatchComparisonOptionsRead)
def get_review_batch_comparison_options(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchComparisonOptionsRead:
    _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    run_field_ids = (
        select(ReviewBatchRunValue.metadata_definition_id)
        .join(ReviewBatchRun, ReviewBatchRun.id == ReviewBatchRunValue.review_batch_run_id)
        .where(ReviewBatchRun.review_batch_id == batch.id)
    )
    batch_field_ids = (
        select(ReviewBatchCodingField.metadata_definition_id)
        .join(
            ReviewBatchCodingGroup,
            ReviewBatchCodingGroup.id == ReviewBatchCodingField.review_batch_coding_group_id,
        )
        .where(ReviewBatchCodingGroup.review_batch_id == batch.id)
    )
    definitions = list(
        db.scalars(
            select(MetadataDefinition)
            .where(
                MetadataDefinition.matter_id == matter_id,
                or_(MetadataDefinition.id.in_(run_field_ids), MetadataDefinition.id.in_(batch_field_ids)),
            )
            .order_by(MetadataDefinition.display_name)
        )
    )
    return ReviewBatchComparisonOptionsRead(
        review_batch_id=batch.id,
        sources=_comparison_run_options(db, batch_id=batch.id, selector_only=True),
        fields=[
            ReviewBatchComparisonFieldOptionRead(
                id=definition.id,
                key=definition.key,
                display_name=definition.display_name,
                type=definition.type,
                cardinality=definition.cardinality,
                allowed_values=definition.allowed_values or [],
            )
            for definition in definitions
        ],
    )


@router.post("/{batch_id}/coding-comparison", response_model=ReviewBatchCodingComparisonRead)
def compare_review_batch_coding(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchComparisonRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchCodingComparisonRead:
    _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    definition = _comparison_definition(db, matter_id, payload.metadata_definition_id)
    source_options = _comparison_run_options(db, batch_id=batch.id, source_ids=payload.source_ids)
    return build_coding_comparison(
        db,
        batch_id=batch.id,
        definition=definition,
        request=payload,
        source_options=source_options,
        document_count=batch.document_count,
    )


@router.post(
    "/{batch_id}/coding-comparison/review-batches/preview",
    response_model=ReviewBatchDisagreementBatchPreviewRead,
)
def preview_disagreement_review_batch(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchDisagreementBatchPreviewRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchDisagreementBatchPreviewRead:
    _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    if batch.status != "READY":
        raise HTTPException(status_code=409, detail="Source batch is not ready")
    definition = _comparison_definition(db, matter_id, payload.metadata_definition_id)
    preview, _source_snapshots = _disagreement_preview(
        db,
        batch=batch,
        definition=definition,
        source_ids=payload.source_ids,
    )
    return preview


@router.post(
    "/{batch_id}/coding-comparison/review-batches",
    response_model=ReviewBatchRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_disagreement_review_batch(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: ReviewBatchDisagreementBatchCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ReviewBatchRead:
    matter = _matter(db, matter_id, principal, "batch.create")
    source_batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, source_batch)
    if source_batch.status != "READY":
        raise HTTPException(status_code=409, detail="Source batch is not ready")
    definition = _comparison_definition(db, matter_id, payload.metadata_definition_id)
    preview, source_snapshots = _disagreement_preview(
        db,
        batch=source_batch,
        definition=definition,
        source_ids=payload.source_ids,
    )
    if preview.candidate_count == 0:
        raise HTTPException(status_code=409, detail="The selected comparison has no disagreement documents")

    target_batch_id = uuid.uuid4()
    batch = ReviewBatch(
        id=target_batch_id,
        matter_id=matter.id,
        name=" ".join(payload.name.split()),
        description=payload.description.strip() if payload.description else None,
        selection_type="COMPARISON_RESULT",
        selection_definition={
            "type": "COMPARISON_RESULT",
            "source_batch_id": str(source_batch.id),
            "source_batch_name": source_batch.name,
            "metadata_definition_id": str(definition.id),
            "field_display_name": definition.display_name,
            "row_filter": "DISAGREEMENT",
            "sources": source_snapshots,
            "candidate_count": preview.candidate_count,
        },
        source_batch_id=source_batch.id,
        reviewer_value_visibility=source_batch.reviewer_value_visibility,
        status="QUEUED",
        workflow_id=f"review-batch:{target_batch_id}",
        created_by_user_id=principal.user.id,
    )
    db.add(batch)
    db.flush()
    _clone_batch_coding_groups(
        db,
        source_batch=source_batch,
        target_batch=batch,
        comparison_definition=definition,
    )
    execution = create_review_batch_execution(db, batch, initiated_by_user_id=principal.user.id)
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="review_batch.disagreements_created",
        target_type="review_batch",
        target_id=batch.id,
        details={
            "matter_id": str(matter.id),
            "source_batch_id": str(source_batch.id),
            "metadata_definition_id": str(definition.id),
            "source_ids": [str(source_id) for source_id in payload.source_ids],
            "candidate_count": preview.candidate_count,
        },
    )
    enqueue_review_batch(db, execution.dbos_workflow_id, str(batch.id))
    db.commit()

    if not settings.dbos_enabled:
        try:
            materialize_review_batch(db, batch.id, settings)
        except Exception as exc:  # noqa: BLE001 - inline mode persists the durable failure contract
            fail_review_batch_execution(db, batch.id, exc)
            db.commit()
        batch = db.get(ReviewBatch, batch.id) or batch
        if batch.status == "READY":
            if settings.search_enabled and db.get_bind().dialect.name == "postgresql":
                try:
                    sync_review_batch_search(batch.id, settings)
                except Exception as exc:  # noqa: BLE001 - valid membership survives projection failure
                    complete_review_batch_execution(db, batch.id, search_error=str(exc)[:4000])
                    db.commit()
                else:
                    complete_review_batch_execution(db, batch.id)
                    db.commit()
            else:
                batch = db.get(ReviewBatch, batch.id) or batch
                batch.search_status = "NOT_CONFIGURED"
                batch.search_error_message = None
                complete_review_batch_execution(db, batch.id)
                db.commit()
        batch = db.get(ReviewBatch, batch.id) or batch
    return _read_batch(db, batch)


@router.get("/{batch_id}/coding-comparison/export", response_class=Response)
def export_review_batch_coding_comparison(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    metadata_definition_id: uuid.UUID,
    source_id: list[uuid.UUID] = Query(min_length=2, max_length=12),
    row_filter: Literal["ALL", "AGREEMENT", "DISAGREEMENT", "INCOMPLETE"] = "ALL",
    format: Literal["csv", "parquet"] = "csv",
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Response:
    _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    if len(source_id) != len(set(source_id)):
        raise HTTPException(status_code=422, detail="Each comparison source may be selected only once")
    definition = _comparison_definition(db, matter_id, metadata_definition_id)
    source_options = _comparison_run_options(db, batch_id=batch.id, source_ids=source_id)
    content, media_type, filename = export_coding_comparison(
        db,
        batch_id=batch.id,
        definition=definition,
        source_options=source_options,
        row_filter=row_filter,
        export_format=format,
    )
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{batch_id}/comparisons", response_model=ReviewBatchComparisonRead)
def compare_review_batch_runs(
    matter_id: uuid.UUID,
    batch_id: uuid.UUID,
    left_run_id: uuid.UUID,
    right_run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewBatchComparisonRead:
    _matter(db, matter_id, principal, "batch.view")
    batch = _batch(db, matter_id, batch_id)
    _require_batch_assignment(db, principal, batch)
    _run(db, batch.id, left_run_id)
    _run(db, batch.id, right_run_id)
    values: dict[tuple[uuid.UUID, uuid.UUID, uuid.UUID], list[str]] = defaultdict(list)
    rows = list(
        db.scalars(
            select(ReviewBatchRunValue).where(ReviewBatchRunValue.review_batch_run_id.in_([left_run_id, right_run_id]))
        )
    )
    for row in rows:
        values[(row.review_batch_run_id, row.matter_document_id, row.metadata_definition_id)].append(
            _canonical(event_value(row))
        )
    definitions = {
        definition.id: definition
        for definition in db.scalars(select(MetadataDefinition).where(MetadataDefinition.matter_id == matter_id))
    }
    document_ids = list(
        db.scalars(
            select(ReviewBatchDocument.matter_document_id).where(ReviewBatchDocument.review_batch_id == batch.id)
        )
    )
    field_ids = sorted({key[2] for key in values}, key=str)
    metrics = []
    for field_id in field_ids:
        match = mismatch = missing_left = missing_right = 0
        for document_id in document_ids:
            left = sorted(values.get((left_run_id, document_id, field_id), []))
            right = sorted(values.get((right_run_id, document_id, field_id), []))
            if not left and not right:
                continue
            if not left:
                missing_left += 1
            elif not right:
                missing_right += 1
            elif left == right:
                match += 1
            else:
                mismatch += 1
        compared = match + mismatch
        definition = definitions.get(field_id)
        metrics.append(
            ReviewBatchComparisonFieldRead(
                metadata_definition_id=field_id,
                display_name=definition.display_name if definition else str(field_id),
                compared_count=compared,
                match_count=match,
                mismatch_count=mismatch,
                missing_left_count=missing_left,
                missing_right_count=missing_right,
                agreement=(match / compared) if compared else None,
            )
        )
    return ReviewBatchComparisonRead(
        left_run_id=left_run_id,
        right_run_id=right_run_id,
        document_count=len(document_ids),
        fields=metrics,
    )
