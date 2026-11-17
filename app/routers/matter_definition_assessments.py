import copy
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from itertools import pairwise

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.assessment_guidance_refinement import queue_guidance_refinement
from app.assessment_provider_batches import cancel_provider_batches
from app.assessment_workflow_reconciliation import reconcile_terminal_assessment_workflows
from app.audit import record_audit
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import Principal, can_admin_matter, can_admin_tenant, get_principal
from app.evidence_outcome_launcher import prepare_evidence_outcome_manifest
from app.evidence_retry import EvidenceStageRetryError, schedule_evidence_stage_retry
from app.matter_definition_assessments import (
    AssessmentError,
    regenerate_assessment_document_analyses,
    regenerate_assessment_synthesis,
    retry_assessment,
    start_assessment,
    utcnow,
)
from app.models import (
    Matter,
    MatterDefinition,
    MatterDefinitionAssessmentQuery,
    MatterDefinitionAssessmentQuestion,
    MatterDefinitionAssessmentRun,
    MatterDefinitionAssessmentSynthesisAttempt,
    MatterDefinitionAssessmentSynthesisUnit,
    MatterDefinitionRevision,
    ReviewBatch,
    ReviewBatchRunDocument,
    SkillRun,
    WorkflowRun,
    WorkflowStepRun,
)
from app.schemas import (
    EvidenceAnalysisOutcomeRead,
    EvidenceStageRetryRead,
    EvidenceStageRetryRequest,
    MatterDefinitionAssessmentArchiveFailed,
    MatterDefinitionAssessmentArchiveResult,
    MatterDefinitionAssessmentCreate,
    MatterDefinitionAssessmentQueryRead,
    MatterDefinitionAssessmentQuestionRead,
    MatterDefinitionAssessmentQuestionUpdate,
    MatterDefinitionAssessmentRead,
    MatterDefinitionAssessmentSynthesisUnitRead,
    MatterDefinitionAssessmentUpdate,
    SkillRunExecutionRead,
    WorkflowExecutionRead,
    WorkflowStepExecutionRead,
)
from app.workflows.dispatcher import cancel_definition_assessment

router = APIRouter(prefix="/v1/matters/{matter_id}/definition-assessments", tags=["matter definition assessments"])
guidance_router = APIRouter(
    prefix="/v1/matters/{matter_id}/guidance/{guidance_id}/assessments",
    tags=["review guidance assessments"],
)

TERMINAL_STATUSES = {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _matter(db: Session, matter_id: uuid.UUID, principal: Principal) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=404, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=403, detail="Matter ADMIN required")
    return matter


def _assessment(db: Session, matter_id: uuid.UUID, assessment_id: uuid.UUID) -> MatterDefinitionAssessmentRun:
    assessment = db.scalar(
        select(MatterDefinitionAssessmentRun).where(
            MatterDefinitionAssessmentRun.id == assessment_id,
            MatterDefinitionAssessmentRun.matter_id == matter_id,
        )
    )
    if assessment is None:
        raise HTTPException(status_code=404, detail="Matter Definition assessment not found")
    return assessment


def _guidance_audit_details(
    db: Session,
    assessment: MatterDefinitionAssessmentRun,
) -> dict[str, str | int]:
    revision = db.get(MatterDefinitionRevision, assessment.matter_definition_revision_id)
    definition = db.get(MatterDefinition, revision.matter_definition_id) if revision else None
    if revision is None or definition is None:
        return {"matter_definition_revision_id": str(assessment.matter_definition_revision_id)}
    return {
        "guidance_id": str(definition.id),
        "guidance_key": definition.key,
        "revision": revision.revision,
        "matter_definition_revision_id": str(revision.id),
    }


def _assessment_reads(
    db: Session,
    assessments: list[MatterDefinitionAssessmentRun],
    *,
    include_technical_diagnostics: bool = False,
) -> list[MatterDefinitionAssessmentRead]:
    revision_ids = (
        {assessment.matter_definition_revision_id for assessment in assessments}
        if hasattr(db, "scalars")
        else set()
    )
    revisions = {
        revision.id: revision
        for revision in db.scalars(
            select(MatterDefinitionRevision).where(MatterDefinitionRevision.id.in_(revision_ids))
        )
    } if revision_ids else {}
    guidance_ids = {revision.matter_definition_id for revision in revisions.values()}
    guidance = {
        definition.id: definition
        for definition in db.scalars(
            select(MatterDefinition).where(MatterDefinition.id.in_(guidance_ids))
        )
    } if guidance_ids else {}
    run_ids = [assessment.review_batch_run_id for assessment in assessments if assessment.review_batch_run_id]
    live_counts: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
    if run_ids:
        rows = db.execute(
            select(
                ReviewBatchRunDocument.review_batch_run_id,
                ReviewBatchRunDocument.status,
                func.count(),
            )
            .where(ReviewBatchRunDocument.review_batch_run_id.in_(run_ids))
            .group_by(ReviewBatchRunDocument.review_batch_run_id, ReviewBatchRunDocument.status)
        )
        for run_id, document_status, count in rows:
            live_counts[run_id][document_status] = int(count)

    results: list[MatterDefinitionAssessmentRead] = []
    for assessment in assessments:
        result = MatterDefinitionAssessmentRead.model_validate(assessment)
        if include_technical_diagnostics:
            result = result.model_copy(update={"technical_diagnostics_visible": True})
        else:
            synthesis_result = copy.deepcopy(result.synthesis_result)
            if isinstance(synthesis_result, dict):
                synthesis_result.pop("other_unmapped_criteria", None)
                synthesis_result.pop("synthesis_diagnostics", None)
            operator_diagnostic_keys = {
                "coverage_status",
                "document_warnings",
                "operator_message",
                "finalization_error",
            }
            synthesis_diagnostics = {
                key: copy.deepcopy(value)
                for key, value in result.synthesis_diagnostics.items()
                if key in operator_diagnostic_keys
            }
            result = result.model_copy(
                update={
                    "synthesis_result": synthesis_result,
                    "synthesis_diagnostics": synthesis_diagnostics,
                    "technical_diagnostics_visible": False,
                }
            )
        revision = revisions.get(assessment.matter_definition_revision_id)
        definition = guidance.get(revision.matter_definition_id) if revision else None
        if definition is not None:
            result = result.model_copy(
                update={
                    "guidance_id": definition.id,
                    "guidance_key": definition.key,
                    "guidance_name": definition.name,
                }
            )
        if assessment.review_batch_run_id in live_counts:
            counts = live_counts[assessment.review_batch_run_id]
            failed_count = counts.get("FAILED", 0)
            effective_status = result.status
            # Older runs treated an intentionally skipped, textless document as
            # an execution error. Normalize those completed results at read time
            # while preserving genuine document and synthesis failures.
            if (
                effective_status == "COMPLETED_WITH_ERRORS"
                and failed_count == 0
                and result.partial_coverage_count == 0
                and result.invalid_result_count == 0
                and result.synthesis_status == "COMPLETED"
                and result.completion_disposition != "ACTION_REQUIRED"
            ):
                effective_status = "COMPLETED"
            result = result.model_copy(
                update={
                    "summarized_count": counts.get("COMPLETED", 0),
                    "skipped_count": counts.get("SKIPPED", 0),
                    "failed_count": failed_count,
                    "status": effective_status,
                }
            )
        results.append(result)
    return results


def _include_technical_diagnostics(principal: Principal, matter: Matter) -> bool:
    return can_admin_tenant(principal, matter.client.tenant_id)


def _workflow_step_reads(
    db: Session,
    assessment: MatterDefinitionAssessmentRun,
    steps: list[WorkflowStepRun],
) -> list[WorkflowStepExecutionRead]:
    reads = [WorkflowStepExecutionRead.model_validate(step) for step in steps]
    if assessment.review_batch_run_id is None:
        return reads
    counts = dict(
        db.execute(
            select(ReviewBatchRunDocument.status, func.count())
            .where(
                ReviewBatchRunDocument.review_batch_run_id == assessment.review_batch_run_id
            )
            .group_by(ReviewBatchRunDocument.status)
        ).all()
    )
    completed_count = int(counts.get("COMPLETED", 0)) + int(counts.get("SKIPPED", 0))
    failed_count = int(counts.get("FAILED", 0))
    return [
        step.model_copy(
            update={
                "completed_count": completed_count,
                "failed_count": failed_count,
            }
        )
        if step.role_key == "document_analysis"
        else step
        for step in reads
    ]


def _create_assessment_for_guidance(
    *,
    db: Session,
    matter: Matter,
    guidance_id: uuid.UUID | None,
    payload: MatterDefinitionAssessmentCreate,
    principal: Principal,
    settings: Settings,
) -> MatterDefinitionAssessmentRun:
    if payload.execution_mode == "EVIDENCE_FIRST_V1" and not can_admin_tenant(
        principal,
        matter.client.tenant_id,
    ):
        raise HTTPException(
            status_code=403,
            detail="Tenant administrator role required for evidence-first assessment routing",
        )
    try:
        assessment = start_assessment(
            db,
            matter=matter,
            matter_definition_id=guidance_id,
            initiated_by_user_id=principal.user.id,
            settings=settings,
            name=payload.name,
            revision_number=payload.revision,
            target_document_count=payload.target_document_count,
            control_sample_size=payload.control_sample_size,
            use_batching=payload.use_batching,
            acknowledge_large_run_warning=payload.acknowledge_large_run_warning,
            execution_mode=payload.execution_mode,
        )
        db.commit()
    except AssessmentError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="The assessment could not be queued") from exc
    db.refresh(assessment)
    return assessment


@guidance_router.post(
    "",
    response_model=MatterDefinitionAssessmentRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_guidance_assessment(
    matter_id: uuid.UUID,
    guidance_id: uuid.UUID,
    payload: MatterDefinitionAssessmentCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterDefinitionAssessmentRead:
    matter = _matter(db, matter_id, principal)
    assessment = _create_assessment_for_guidance(
        db=db,
        matter=matter,
        guidance_id=guidance_id,
        payload=payload,
        principal=principal,
        settings=settings,
    )
    return _assessment_reads(
        db,
        [assessment],
        include_technical_diagnostics=_include_technical_diagnostics(principal, matter),
    )[0]


@guidance_router.get("", response_model=list[MatterDefinitionAssessmentRead])
def list_guidance_assessments(
    matter_id: uuid.UUID,
    guidance_id: uuid.UUID,
    include_archived: bool = False,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[MatterDefinitionAssessmentRead]:
    matter = _matter(db, matter_id, principal)
    guidance_exists = db.scalar(
        select(MatterDefinition.id).where(
            MatterDefinition.id == guidance_id,
            MatterDefinition.matter_id == matter_id,
        )
    )
    if guidance_exists is None:
        raise HTTPException(status_code=404, detail="Review Guidance not found")
    statement = (
        select(MatterDefinitionAssessmentRun)
        .join(
            MatterDefinitionRevision,
            MatterDefinitionRevision.id
            == MatterDefinitionAssessmentRun.matter_definition_revision_id,
        )
        .where(
            MatterDefinitionAssessmentRun.matter_id == matter_id,
            MatterDefinitionRevision.matter_definition_id == guidance_id,
        )
    )
    if not include_archived:
        statement = statement.where(MatterDefinitionAssessmentRun.archived_at.is_(None))
    assessments = list(db.scalars(statement.order_by(MatterDefinitionAssessmentRun.created_at.desc())))
    reconcile_terminal_assessment_workflows(db, assessments, settings=settings)
    return _assessment_reads(
        db,
        assessments,
        include_technical_diagnostics=_include_technical_diagnostics(principal, matter),
    )


@router.post("", response_model=MatterDefinitionAssessmentRead, status_code=status.HTTP_202_ACCEPTED)
def create_assessment(
    matter_id: uuid.UUID,
    payload: MatterDefinitionAssessmentCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterDefinitionAssessmentRead:
    matter = _matter(db, matter_id, principal)
    assessment = _create_assessment_for_guidance(
        db=db,
        matter=matter,
        guidance_id=None,
        payload=payload,
        principal=principal,
        settings=settings,
    )
    return _assessment_reads(
        db,
        [assessment],
        include_technical_diagnostics=_include_technical_diagnostics(principal, matter),
    )[0]


@router.patch("/{assessment_id}", response_model=MatterDefinitionAssessmentRead)
def update_assessment(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    payload: MatterDefinitionAssessmentUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentRead:
    matter = _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    previous_name = assessment.name
    assessment.name = payload.name
    batch_renamed = False
    if assessment.review_batch_id is not None:
        batch = db.get(ReviewBatch, assessment.review_batch_id)
        if batch is not None and batch.name == previous_name:
            batch.name = payload.name
            batch_renamed = True
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_definition.assessment.renamed",
        target_type="matter_definition_assessment_run",
        target_id=assessment.id,
        details={
            "matter_id": str(matter.id),
            **_guidance_audit_details(db, assessment),
            "previous_name": previous_name,
            "name": assessment.name,
            "review_batch_renamed": batch_renamed,
        },
    )
    db.commit()
    db.refresh(assessment)
    return _assessment_reads(
        db,
        [assessment],
        include_technical_diagnostics=_include_technical_diagnostics(principal, matter),
    )[0]


@router.post("/archive-failed", response_model=MatterDefinitionAssessmentArchiveResult)
def archive_failed_assessments(
    matter_id: uuid.UUID,
    payload: MatterDefinitionAssessmentArchiveFailed,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentArchiveResult:
    matter = _matter(db, matter_id, principal)
    statement = select(MatterDefinitionAssessmentRun).where(
        MatterDefinitionAssessmentRun.matter_id == matter_id,
        MatterDefinitionAssessmentRun.status.in_(("FAILED", "CANCELED")),
        MatterDefinitionAssessmentRun.archived_at.is_(None),
    )
    if payload.guidance_id is not None:
        guidance_exists = db.scalar(
            select(MatterDefinition.id).where(
                MatterDefinition.id == payload.guidance_id,
                MatterDefinition.matter_id == matter_id,
            )
        )
        if guidance_exists is None:
            raise HTTPException(status_code=404, detail="Review Guidance not found")
        statement = statement.join(
            MatterDefinitionRevision,
            MatterDefinitionRevision.id
            == MatterDefinitionAssessmentRun.matter_definition_revision_id,
        ).where(MatterDefinitionRevision.matter_definition_id == payload.guidance_id)
    assessments = list(db.scalars(statement))
    archived_at = utcnow()
    for assessment in assessments:
        assessment.archived_at = archived_at
        assessment.archived_by_user_id = principal.user.id
        assessment.archive_reason = "MANUAL"
        assessment.superseded_by_assessment_id = None
    assessment_ids = [assessment.id for assessment in assessments]
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_definition.assessments.archived",
        target_type="matter",
        target_id=matter.id,
        details={
            "matter_id": str(matter.id),
            "guidance_id": str(payload.guidance_id) if payload.guidance_id else None,
            "assessment_ids": [str(value) for value in assessment_ids],
            "archived_count": len(assessment_ids),
        },
    )
    db.commit()
    return MatterDefinitionAssessmentArchiveResult(
        archived_count=len(assessment_ids),
        assessment_ids=assessment_ids,
    )


@router.post("/{assessment_id}/restore", response_model=MatterDefinitionAssessmentRead)
def restore_archived_assessment(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentRead:
    matter = _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    if assessment.archived_at is None:
        raise HTTPException(status_code=409, detail="Assessment is not archived")
    archive_reason = assessment.archive_reason
    superseded_by_assessment_id = assessment.superseded_by_assessment_id
    assessment.archived_at = None
    assessment.archived_by_user_id = None
    assessment.archive_reason = None
    assessment.superseded_by_assessment_id = None
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_definition.assessment.restored",
        target_type="matter_definition_assessment_run",
        target_id=assessment.id,
        details={
            "matter_id": str(matter.id),
            "archive_reason": archive_reason,
            "superseded_by_assessment_id": (
                str(superseded_by_assessment_id) if superseded_by_assessment_id else None
            ),
        },
    )
    db.commit()
    db.refresh(assessment)
    return _assessment_reads(
        db,
        [assessment],
        include_technical_diagnostics=_include_technical_diagnostics(principal, matter),
    )[0]


@router.post("/{assessment_id}/retry", response_model=MatterDefinitionAssessmentRead, status_code=status.HTTP_202_ACCEPTED)
def retry_failed_assessment(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentRead:
    matter = _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    try:
        retry_assessment(db, assessment, initiated_by_user_id=principal.user.id)
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=principal.user.id,
            action="matter_definition.assessment.retried",
            target_type="matter_definition_assessment_run",
            target_id=assessment.id,
            details={"matter_id": str(matter.id), **_guidance_audit_details(db, assessment)},
        )
        db.commit()
    except AssessmentError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(assessment)
    return _assessment_reads(
        db,
        [assessment],
        include_technical_diagnostics=_include_technical_diagnostics(principal, matter),
    )[0]


@router.post(
    "/{assessment_id}/regenerate-synthesis",
    response_model=MatterDefinitionAssessmentRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def regenerate_synthesis(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentRead:
    matter = _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    try:
        regenerate_assessment_synthesis(db, assessment, initiated_by_user_id=principal.user.id)
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=principal.user.id,
            action="matter_definition.assessment.synthesis_regenerated",
            target_type="matter_definition_assessment_run",
            target_id=assessment.id,
            details={"matter_id": str(matter.id), **_guidance_audit_details(db, assessment)},
        )
        db.commit()
    except AssessmentError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(assessment)
    return _assessment_reads(
        db,
        [assessment],
        include_technical_diagnostics=_include_technical_diagnostics(principal, matter),
    )[0]


@router.get(
    "/{assessment_id}/synthesis-units",
    response_model=list[MatterDefinitionAssessmentSynthesisUnitRead],
)
def list_synthesis_units(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterDefinitionAssessmentSynthesisUnitRead]:
    matter = _matter(db, matter_id, principal)
    if not _include_technical_diagnostics(principal, matter):
        raise HTTPException(status_code=403, detail="Tenant administrator role required")
    assessment = _assessment(db, matter_id, assessment_id)
    if assessment.current_synthesis_attempt_id is None:
        return []
    rows = list(
        db.scalars(
            select(MatterDefinitionAssessmentSynthesisUnit)
            .where(
                MatterDefinitionAssessmentSynthesisUnit.attempt_id
                == assessment.current_synthesis_attempt_id
            )
            .order_by(MatterDefinitionAssessmentSynthesisUnit.ordinal)
        )
    )
    return [MatterDefinitionAssessmentSynthesisUnitRead.model_validate(row) for row in rows]


@router.post(
    "/{assessment_id}/regenerate-document-analyses",
    response_model=MatterDefinitionAssessmentRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def regenerate_document_analyses(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentRead:
    matter = _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    try:
        regenerate_assessment_document_analyses(db, assessment, initiated_by_user_id=principal.user.id)
        record_audit(
            db,
            tenant_id=matter.client.tenant_id,
            actor_user_id=principal.user.id,
            action="matter_definition.assessment.document_analyses_regenerated",
            target_type="matter_definition_assessment_run",
            target_id=assessment.id,
            details={
                "matter_id": str(matter.id),
                **_guidance_audit_details(db, assessment),
                "review_batch_id": str(assessment.review_batch_id),
                "review_batch_run_id": str(assessment.review_batch_run_id),
            },
        )
        db.commit()
    except AssessmentError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.refresh(assessment)
    return _assessment_reads(
        db,
        [assessment],
        include_technical_diagnostics=_include_technical_diagnostics(principal, matter),
    )[0]


@router.get("", response_model=list[MatterDefinitionAssessmentRead])
def list_assessments(
    matter_id: uuid.UUID,
    include_archived: bool = False,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[MatterDefinitionAssessmentRead]:
    matter = _matter(db, matter_id, principal)
    statement = select(MatterDefinitionAssessmentRun).where(
        MatterDefinitionAssessmentRun.matter_id == matter_id
    )
    if not include_archived:
        statement = statement.where(MatterDefinitionAssessmentRun.archived_at.is_(None))
    assessments = list(db.scalars(statement.order_by(MatterDefinitionAssessmentRun.created_at.desc())))
    reconcile_terminal_assessment_workflows(db, assessments, settings=settings)
    return _assessment_reads(
        db,
        assessments,
        include_technical_diagnostics=_include_technical_diagnostics(principal, matter),
    )


@router.get("/{assessment_id}", response_model=MatterDefinitionAssessmentRead)
def get_assessment(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MatterDefinitionAssessmentRead:
    matter = _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    reconcile_terminal_assessment_workflows(db, [assessment], settings=settings)
    return _assessment_reads(
        db,
        [assessment],
        include_technical_diagnostics=_include_technical_diagnostics(principal, matter),
    )[0]


@router.get("/{assessment_id}/evidence-outcomes", response_model=EvidenceAnalysisOutcomeRead)
def get_assessment_evidence_outcomes(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> EvidenceAnalysisOutcomeRead:
    matter = _matter(db, matter_id, principal)
    _assessment(db, matter_id, assessment_id)
    try:
        prepared = prepare_evidence_outcome_manifest(
            db,
            actor_user_id=principal.user.id,
            assessment_id=assessment_id,
            require_operator_role=False,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    technical = _include_technical_diagnostics(principal, matter)
    payload = prepared.manifest.model_dump(mode="json")
    documents = payload["documents"]
    if not technical:
        for document in documents:
            document["selected_output_artifact_id"] = None
            for stage_name in (
                "extraction",
                "adjudication",
                "document_decision",
                "compatibility_projection",
                "narrative",
            ):
                document[stage_name]["workflow_run_id"] = None
                document[stage_name]["artifact_ids"] = {}
    return EvidenceAnalysisOutcomeRead(
        assessment_id=prepared.assessment.id,
        document_count=prepared.manifest.document_count,
        readiness_counts=prepared.manifest.readiness_counts,
        stage_status_counts=prepared.manifest.stage_status_counts,
        source_workflow_count=len(prepared.manifest.source_workflow_ids),
        source_artifact_count=len(prepared.manifest.source_artifact_ids),
        technical_diagnostics_visible=technical,
        usage=prepared.manifest.usage.model_dump(mode="json") if technical else None,
        documents=documents,
    )


@router.post(
    "/{assessment_id}/evidence-outcomes/{matter_document_id}/retry",
    response_model=EvidenceStageRetryRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_assessment_evidence_stage(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    matter_document_id: uuid.UUID,
    payload: EvidenceStageRetryRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> EvidenceStageRetryRead:
    matter = _matter(db, matter_id, principal)
    _assessment(db, matter_id, assessment_id)
    if not _include_technical_diagnostics(principal, matter):
        raise HTTPException(
            status_code=403,
            detail="Tenant administrator role required for evidence-stage retries",
        )
    try:
        retry = schedule_evidence_stage_retry(
            db,
            actor_user_id=principal.user.id,
            assessment_id=assessment_id,
            matter_document_id=matter_document_id,
            stage=payload.stage,
        )
        db.commit()
    except (EvidenceStageRetryError, PermissionError, TypeError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return EvidenceStageRetryRead(
        assessment_id=retry.assessment_id,
        matter_document_id=retry.matter_document_id,
        stage=retry.stage,
        target_workflow_run_id=retry.target_workflow_run_id,
        operation_workflow_id=retry.operation_workflow_id,
        reused_completed_work=retry.reused_completed_work,
        operator_message=(
            f"{retry.stage.title()} retry queued. Completed durable work will be reused. "
            + (
                "Adjudication and narrative will be rebuilt from the repaired extraction."
                if retry.stage == "EXTRACTION"
                else (
                    "Narrative will be rebuilt from the repaired adjudication."
                    if retry.stage == "ADJUDICATION"
                    else "The deterministic adjudication decision will not be reopened."
                )
            )
        ),
    )


@router.get("/{assessment_id}/execution", response_model=WorkflowExecutionRead)
def get_assessment_execution(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    include_skill_runs: bool = True,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> WorkflowExecutionRead:
    _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    reconcile_terminal_assessment_workflows(db, [assessment], settings=settings)
    workflow = db.get(WorkflowRun, assessment.workflow_run_id)
    if workflow is None:
        raise HTTPException(status_code=409, detail="Assessment workflow record is unavailable")
    steps = list(
        db.scalars(
            select(WorkflowStepRun)
            .where(WorkflowStepRun.workflow_run_id == workflow.id)
            .order_by(WorkflowStepRun.ordinal)
        )
    )
    skill_runs = (
        list(
            db.scalars(
                select(SkillRun)
                .where(SkillRun.workflow_run_id == workflow.id)
                .order_by(SkillRun.created_at, SkillRun.id)
            )
        )
        if include_skill_runs
        else []
    )
    now = utcnow()
    elapsed_seconds = max(
        0,
        int(
            (
                _utc(workflow.completed_at or now)
                - _utc(workflow.started_at or workflow.created_at)
            ).total_seconds()
        ),
    )
    attempts = list(
        db.scalars(
            select(MatterDefinitionAssessmentSynthesisAttempt)
            .where(MatterDefinitionAssessmentSynthesisAttempt.assessment_run_id == assessment.id)
            .order_by(MatterDefinitionAssessmentSynthesisAttempt.started_at)
        )
    )
    operator_wait_seconds = sum(
        max(0, int((_utc(current.started_at) - _utc(previous.completed_at)).total_seconds()))
        for previous, current in pairwise(attempts)
        if previous.completed_at is not None
    )
    return WorkflowExecutionRead(
        id=workflow.id,
        workflow_key=workflow.workflow_key,
        code_version=workflow.code_version,
        status=workflow.status,
        request_count=workflow.request_count,
        input_tokens=workflow.input_tokens,
        cached_input_tokens=workflow.cached_input_tokens,
        cache_write_tokens=workflow.cache_write_tokens,
        output_tokens=workflow.output_tokens,
        error_message=workflow.error_message,
        started_at=workflow.started_at,
        completed_at=workflow.completed_at,
        elapsed_seconds=elapsed_seconds,
        processing_seconds=max(0, elapsed_seconds - operator_wait_seconds),
        operator_wait_seconds=operator_wait_seconds,
        steps=_workflow_step_reads(db, assessment, steps),
        skill_runs=[SkillRunExecutionRead.model_validate(run) for run in skill_runs],
    )


@router.post("/{assessment_id}/cancel", response_model=MatterDefinitionAssessmentRead)
def cancel_assessment(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentRead:
    matter = _matter(db, matter_id, principal)
    assessment = _assessment(db, matter_id, assessment_id)
    if assessment.status in TERMINAL_STATUSES:
        return _assessment_reads(
            db,
            [assessment],
            include_technical_diagnostics=_include_technical_diagnostics(principal, matter),
        )[0]
    now = utcnow()
    assessment.status = "CANCELED"
    assessment.canceled_at = now
    assessment.completed_at = now
    workflow = db.get(WorkflowRun, assessment.workflow_run_id)
    if workflow is not None:
        workflow.status = "CANCELED"
        workflow.completed_at = now
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_definition.assessment.canceled",
        target_type="matter_definition_assessment_run",
        target_id=assessment.id,
        details={"matter_id": str(matter.id), **_guidance_audit_details(db, assessment)},
    )
    from app.review_programs import project_review_program_assessment_terminal

    project_review_program_assessment_terminal(db, assessment=assessment)
    db.commit()
    cancel_provider_batches(db, assessment.id)
    if workflow is not None:
        cancel_definition_assessment(workflow.dbos_workflow_id)
    db.refresh(assessment)
    return _assessment_reads(
        db,
        [assessment],
        include_technical_diagnostics=_include_technical_diagnostics(principal, matter),
    )[0]


@router.get("/{assessment_id}/queries", response_model=list[MatterDefinitionAssessmentQueryRead])
def list_assessment_queries(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterDefinitionAssessmentQuery]:
    _matter(db, matter_id, principal)
    _assessment(db, matter_id, assessment_id)
    return list(
        db.scalars(
            select(MatterDefinitionAssessmentQuery)
            .where(MatterDefinitionAssessmentQuery.assessment_run_id == assessment_id)
            .order_by(MatterDefinitionAssessmentQuery.ordinal)
        )
    )


@router.get("/{assessment_id}/questions", response_model=list[MatterDefinitionAssessmentQuestionRead])
def list_assessment_questions(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[MatterDefinitionAssessmentQuestion]:
    _matter(db, matter_id, principal)
    _assessment(db, matter_id, assessment_id)
    return list(
        db.scalars(
            select(MatterDefinitionAssessmentQuestion)
            .where(MatterDefinitionAssessmentQuestion.assessment_run_id == assessment_id)
            .order_by(MatterDefinitionAssessmentQuestion.created_at, MatterDefinitionAssessmentQuestion.id)
        )
    )


@router.put(
    "/{assessment_id}/questions/{question_id}", response_model=MatterDefinitionAssessmentQuestionRead
)
def update_assessment_question(
    matter_id: uuid.UUID,
    assessment_id: uuid.UUID,
    question_id: uuid.UUID,
    payload: MatterDefinitionAssessmentQuestionUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterDefinitionAssessmentQuestion:
    matter = _matter(db, matter_id, principal)
    _assessment(db, matter_id, assessment_id)
    question = db.scalar(
        select(MatterDefinitionAssessmentQuestion).where(
            MatterDefinitionAssessmentQuestion.id == question_id,
            MatterDefinitionAssessmentQuestion.assessment_run_id == assessment_id,
        )
    )
    if question is None:
        raise HTTPException(status_code=404, detail="Assessment question not found")
    question.status = payload.status
    question.answer = payload.answer.strip() if payload.answer else None
    question.answered_by_user_id = principal.user.id
    question.answered_at = utcnow()
    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=principal.user.id,
        action="matter_definition.assessment_question.updated",
        target_type="matter_definition_assessment_question",
        target_id=question.id,
        details={"assessment_id": str(assessment_id), "status": question.status},
    )
    try:
        queue_guidance_refinement(
            db,
            assessment_id,
            initiated_by_user_id=principal.user.id,
        )
    except ValueError as exc:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        if assessment is not None:
            assessment.guidance_refinement_status = "FAILED"
            assessment.guidance_refinement_error_message = str(exc)[:4000]
    db.commit()
    db.refresh(question)
    return question
