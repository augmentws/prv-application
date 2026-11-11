import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analysis_input_manifests import AnalysisInputManifestError, AnalysisInputManifestReader
from app.assessment_provider_batches import cancel_provider_batches
from app.assessment_workflow_reconciliation import reconcile_terminal_assessment_workflows
from app.authorization import ResourceRef, require
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import Principal, get_principal
from app.document_metadata import event_value
from app.models import (
    AnalysisInputManifest,
    AnalysisTaskExecutionProfile,
    ClassificationModel,
    Matter,
    MatterDefinitionAssessmentRun,
    MatterDocument,
    MetadataDefinition,
    ReviewBatchRunValue,
    ReviewDecisionResult,
    ReviewProgram,
    ReviewProgramChild,
    ReviewProgramCompletionReport,
    ReviewProgramRun,
)
from app.review_batch_comparisons import (
    ComparisonDocument,
    SourceValue,
    build_coding_comparison,
)
from app.review_programs import (
    ReviewProgramError,
    _alignment_value_key,
    _classifier_alignment_predictions,
    _human_adjudicated_issue_keys,
    _program_qa_result_run_profile_map,
    _qa_task_records,
    cancel_review_program_discovery_children,
    cancel_review_program_discovery_qa,
    cancel_review_program_run,
    create_review_program,
    current_review_program_version,
    get_review_program,
    get_review_program_run,
    prepare_review_program_negative_population_qc_inventory,
    reconcile_review_program_run,
    reset_review_program_discovery_qa,
    resolve_review_program_action,
    retry_review_program_run,
    review_program_action_decision_context,
    review_program_assessment_cancellation_targets,
    review_program_run_details,
    setup_review_program_classifier_improvement,
    setup_review_program_definition,
    setup_review_program_discovery,
    setup_review_program_negative_population_qc,
    setup_review_program_qa_task,
    start_review_program_run,
)
from app.review_workflow_templates import (
    REVIEW_WORKFLOW_TEMPLATES,
    ReviewWorkflowTemplateError,
    ReviewWorkflowTemplateSpec,
)
from app.schemas import (
    ReviewBatchComparisonFieldOptionRead,
    ReviewBatchComparisonRequest,
    ReviewBatchComparisonSourceOptionRead,
    ReviewProgramActionRead,
    ReviewProgramActionResolve,
    ReviewProgramClassifierImprovementSetup,
    ReviewProgramCompletionReportRead,
    ReviewProgramCreate,
    ReviewProgramDefinitionSetup,
    ReviewProgramDiscoverySetup,
    ReviewProgramEventRead,
    ReviewProgramFinalAlignmentCodingRead,
    ReviewProgramFinalAlignmentPageRead,
    ReviewProgramFinalAlignmentRowRead,
    ReviewProgramNegativePopulationQCInventoryPrepare,
    ReviewProgramNegativePopulationQCSetup,
    ReviewProgramQAPopulationStatisticsRead,
    ReviewProgramQASetup,
    ReviewProgramRead,
    ReviewProgramRunCommand,
    ReviewProgramRunRead,
    ReviewProgramRunStart,
    ReviewProgramStageAttemptRead,
    ReviewProgramVersionRead,
    ReviewWorkflowTemplateRead,
)
from app.workflows.dispatcher import (
    cancel_analysis_task_batch,
    cancel_analysis_task_batch_set_run_workflow,
    cancel_analysis_task_batch_set_workflow,
    cancel_classification_model_scoring,
    cancel_classification_training_job,
    cancel_definition_assessment,
    cancel_review_batch,
)

template_router = APIRouter(prefix="/v1/review-workflow-templates", tags=["review programs"])
router = APIRouter(prefix="/v1/matters/{matter_id}/review-programs", tags=["review programs"])


def _template_read(template: ReviewWorkflowTemplateSpec) -> ReviewWorkflowTemplateRead:
    return ReviewWorkflowTemplateRead.model_validate(
        {
            **template.contract(),
            "content_hash": template.content_hash,
        }
    )


def _program_read(db: Session, program: ReviewProgram) -> ReviewProgramRead:
    version = current_review_program_version(db, program)
    return ReviewProgramRead.model_validate(program).model_copy(
        update={"current_version": ReviewProgramVersionRead.model_validate(version)}
    )


def _run_read(db: Session, run: ReviewProgramRun) -> ReviewProgramRunRead:
    attempts, actions, events = review_program_run_details(db, run)
    action_reads = [
        ReviewProgramActionRead.model_validate(item).model_copy(
            update={
                "decision_context": review_program_action_decision_context(
                    db,
                    run=run,
                    action=item,
                )
            }
        )
        for item in actions
    ]
    return ReviewProgramRunRead.model_validate(run).model_copy(
        update={
            "stage_attempts": [ReviewProgramStageAttemptRead.model_validate(item) for item in attempts],
            "actions": action_reads,
            "events": [ReviewProgramEventRead.model_validate(item) for item in events],
        }
    )


def _reconcile_definition_assessment_runs(
    db: Session,
    runs: list[ReviewProgramRun],
    *,
    settings: Settings,
) -> None:
    active_run_ids = [
        run.id
        for run in runs
        if run.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}
        and run.current_stage in {"DEFINITION_ASSESSMENT", "DEFINITION_REFINEMENT", "DEFINITION_ACCEPTANCE"}
    ]
    if not active_run_ids:
        return
    assessment_ids = list(
        db.scalars(
            select(ReviewProgramChild.child_id).where(
                ReviewProgramChild.run_id.in_(active_run_ids),
                ReviewProgramChild.child_type == "MATTER_DEFINITION_ASSESSMENT",
            )
        )
    )
    if not assessment_ids:
        return
    assessments = list(
        db.scalars(
            select(MatterDefinitionAssessmentRun).where(
                MatterDefinitionAssessmentRun.id.in_(assessment_ids)
            )
        )
    )
    reconcile_terminal_assessment_workflows(db, assessments, settings=settings)


def _matter_with_permission(
    db: Session,
    principal: Principal,
    matter_id: uuid.UUID,
    permission_key: str,
) -> Matter:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    require(db, principal, permission_key, ResourceRef.matter(matter_id))
    return matter


def _http_error(exc: ReviewProgramError | ReviewWorkflowTemplateError) -> HTTPException:
    if "not found" in str(exc).lower():
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _alignment_issue_labels(db: Session, matter_id: uuid.UUID) -> dict[str, str]:
    labels: dict[str, str] = {}
    definitions = list(
        db.scalars(select(MetadataDefinition).where(MetadataDefinition.matter_id == matter_id))
    )
    for definition in definitions:
        for value in definition.allowed_values or []:
            key = value.get("key")
            if key is not None:
                labels[f"{definition.key}:{key}"] = (
                    f"{definition.display_name}: {value.get('label') or key}"
                )
        if definition.type == "BOOLEAN":
            labels[f"{definition.key}:true"] = f"{definition.display_name}: Yes"
            labels[f"{definition.key}:false"] = f"{definition.display_name}: No"
    return labels


def _alignment_codings(
    value: object,
    *,
    issue_labels: dict[str, str],
) -> list[ReviewProgramFinalAlignmentCodingRead]:
    try:
        raw = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        raw = []
    keys = raw if isinstance(raw, list) else []
    return [
        ReviewProgramFinalAlignmentCodingRead(
            key=str(key),
            label=issue_labels.get(str(key), str(key).replace(":", ": ")),
        )
        for key in keys
    ]


def _qa_result_field_values(result: object, field_key: str) -> tuple[tuple[str, ...], tuple[object, ...], float | None]:
    recommendations = getattr(result, "recommendations", {})
    if not isinstance(recommendations, dict):
        return (), (), None
    values_by_key: dict[str, object] = {}
    confidences: list[float] = []
    for recommendation in recommendations.values():
        if (
            not isinstance(recommendation, dict)
            or recommendation.get("matched") is not True
            or recommendation.get("metadata_definition_key") != field_key
            or "value" not in recommendation
        ):
            continue
        value = recommendation["value"]
        values_by_key[_alignment_value_key(value)] = value
        raw_confidence = recommendation.get("confidence", recommendation.get("probability"))
        if isinstance(raw_confidence, int | float) and not isinstance(raw_confidence, bool):
            confidence = float(raw_confidence)
            if 0 <= confidence <= 1:
                confidences.append(confidence)
    ordered = sorted(values_by_key.items())
    return (
        tuple(key for key, _value in ordered),
        tuple(value for _key, value in ordered),
        max(confidences) if confidences else None,
    )


def _classifier_population_source_value(
    prediction: dict[str, object] | None,
    field_key: str,
) -> SourceValue:
    field_results = prediction.get("field_results") if prediction is not None else None
    field_prediction = (
        field_results.get(field_key)
        if isinstance(field_results, dict) and isinstance(field_results.get(field_key), dict)
        else None
    )
    selected_prediction = field_prediction or prediction
    statuses = selected_prediction.get("statuses", []) if selected_prediction is not None else []
    evaluated = (
        selected_prediction is not None
        and isinstance(statuses, list)
        and "SUCCESS" in statuses
        and selected_prediction.get("abstained") is not True
    )
    prefix = f"{field_key}:"
    if evaluated and field_prediction is not None:
        selected_keys = tuple(sorted(str(issue) for issue in field_prediction.get("issues", set())))
    elif evaluated and prediction is not None:
        selected_keys = tuple(
            sorted(
                str(issue)[len(prefix) :]
                for issue in prediction.get("issues", set())
                if str(issue).startswith(prefix)
            )
        )
    else:
        selected_keys = ()
    raw_confidence = (
        selected_prediction.get("maximum_candidate_probability")
        if selected_prediction is not None
        else None
    )
    return SourceValue(
        state="EVALUATED" if evaluated else "NOT_EVALUATED",
        value_keys=selected_keys,
        values=selected_keys,
        top_confidence=(
            float(raw_confidence)
            if isinstance(raw_confidence, int | float) and not isinstance(raw_confidence, bool)
            else None
        ),
    )


def _human_population_source_value(
    human_by_document: dict[uuid.UUID, set[str]],
    document_id: uuid.UUID,
    field_key: str,
) -> SourceValue:
    prefix = f"{field_key}:"
    selected_keys = tuple(
        sorted(
            issue[len(prefix) :]
            for issue in human_by_document.get(document_id, set())
            if issue.startswith(prefix)
        )
    )
    return SourceValue(
        state="EVALUATED" if document_id in human_by_document else "NOT_EVALUATED",
        value_keys=selected_keys,
        values=selected_keys,
        top_confidence=None,
    )


def _program_qa_population_source_values(
    db: Session,
    *,
    run: ReviewProgramRun,
    task_version_id: uuid.UUID,
    profile_ids: set[uuid.UUID],
    definition: MetadataDefinition,
) -> dict[uuid.UUID, dict[uuid.UUID, SourceValue]]:
    """Load only typed coding values needed by the population statistics page.

    ReviewDecisionResult contains the complete model response and can be very large. Population statistics need only
    result usability plus the selected field's already-materialized ReviewBatchRunValue rows.
    """

    run_profile_map = _program_qa_result_run_profile_map(
        db,
        run=run,
        task_version_id=task_version_id,
        profile_ids=profile_ids,
        purposes={
            "DISCOVERY_QA_PROFILE",
            "CLASSIFIER_IMPROVEMENT_QA_PROFILE",
            "CLASSIFIER_POSITIVE_COVERAGE_QA_PROFILE",
        },
    )
    if not run_profile_map:
        return {}
    result_rows = db.execute(
        select(
            ReviewDecisionResult.id,
            ReviewDecisionResult.matter_document_id,
            ReviewDecisionResult.review_batch_run_id,
            ReviewDecisionResult.status,
            ReviewDecisionResult.coverage,
            ReviewDecisionResult.created_at,
        )
        .where(
            ReviewDecisionResult.review_batch_run_id.in_(list(run_profile_map)),
            ReviewDecisionResult.matter_analysis_task_version_id == task_version_id,
            ReviewDecisionResult.status.in_(("COMPLETED", "PARTIAL")),
        )
        .order_by(ReviewDecisionResult.created_at, ReviewDecisionResult.id)
    )
    selected_results: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID] = {}
    for row in result_rows:
        coverage = row.coverage if isinstance(row.coverage, dict) else {}
        if row.status == "PARTIAL" and not (
            coverage.get("input_complete") is True
            and coverage.get("question_complete") is True
        ):
            continue
        profile_id = run_profile_map[row.review_batch_run_id]
        selected_results[(row.matter_document_id, profile_id)] = row.id

    values_by_source: dict[
        tuple[uuid.UUID, uuid.UUID],
        dict[str, tuple[object, float | None]],
    ] = {key: {} for key in selected_results}
    selected_result_keys = {result_id: key for key, result_id in selected_results.items()}
    for row in db.scalars(
        select(ReviewBatchRunValue).where(
            ReviewBatchRunValue.review_batch_run_id.in_(list(run_profile_map)),
            ReviewBatchRunValue.metadata_definition_id == definition.id,
        )
    ):
        if row.review_decision_result_id is None:
            continue
        source_key = selected_result_keys.get(row.review_decision_result_id)
        if source_key is None:
            continue
        value = event_value(row)
        if value is None:
            continue
        value_key = _alignment_value_key(value)
        previous = values_by_source[source_key].get(value_key)
        confidence = row.confidence
        if previous is None or (confidence is not None and (previous[1] is None or confidence > previous[1])):
            values_by_source[source_key][value_key] = (value, confidence)

    by_document: dict[uuid.UUID, dict[uuid.UUID, SourceValue]] = {}
    for (document_id, profile_id), values_by_key in values_by_source.items():
        ordered_values = sorted(values_by_key.items())
        confidences = [confidence for _key, (_value, confidence) in ordered_values if confidence is not None]
        by_document.setdefault(document_id, {})[profile_id] = SourceValue(
            state="EVALUATED",
            value_keys=tuple(key for key, _item in ordered_values),
            values=tuple(item[0] for _key, item in ordered_values),
            top_confidence=max(confidences) if confidences else None,
        )
    return by_document


@template_router.get("", response_model=list[ReviewWorkflowTemplateRead])
def list_review_workflow_templates(
    _: Principal = Depends(get_principal),
) -> list[ReviewWorkflowTemplateRead]:
    return [_template_read(template) for template in REVIEW_WORKFLOW_TEMPLATES.values()]


@router.get("", response_model=list[ReviewProgramRead])
def list_review_programs(
    matter_id: uuid.UUID,
    include_archived: bool = False,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ReviewProgramRead]:
    _matter_with_permission(db, principal, matter_id, "review_program.view")
    statement = select(ReviewProgram).where(ReviewProgram.matter_id == matter_id)
    if not include_archived:
        statement = statement.where(ReviewProgram.status != "ARCHIVED")
    programs = list(db.scalars(statement.order_by(ReviewProgram.created_at, ReviewProgram.id)))
    return [_program_read(db, program) for program in programs]


@router.post("", response_model=ReviewProgramRead, status_code=status.HTTP_201_CREATED)
def create_program(
    matter_id: uuid.UUID,
    payload: ReviewProgramCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRead:
    matter = _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program, _ = create_review_program(
            db,
            matter=matter,
            actor=principal.user,
            template_key=payload.template_key,
            key=payload.key,
            name=payload.name,
            description=payload.description,
            owner_user_id=payload.owner_user_id,
            matter_definition_id=payload.matter_definition_id,
        )
        db.commit()
    except (ReviewProgramError, ReviewWorkflowTemplateError) as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The Review Program conflicts with an existing record",
        ) from exc
    db.refresh(program)
    return _program_read(db, program)


@router.get("/{program_id}", response_model=ReviewProgramRead)
def read_review_program(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRead:
    _matter_with_permission(db, principal, matter_id, "review_program.view")
    try:
        return _program_read(db, get_review_program(db, matter_id=matter_id, program_id=program_id))
    except ReviewProgramError as exc:
        raise _http_error(exc) from exc


@router.get("/{program_id}/runs", response_model=list[ReviewProgramRunRead])
def list_review_program_runs(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[ReviewProgramRunRead]:
    _matter_with_permission(db, principal, matter_id, "review_program.view")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id)
    except ReviewProgramError as exc:
        raise _http_error(exc) from exc
    runs = list(
        db.scalars(
            select(ReviewProgramRun)
            .where(ReviewProgramRun.program_id == program.id)
            .order_by(ReviewProgramRun.created_at.desc(), ReviewProgramRun.id.desc())
        )
    )
    _reconcile_definition_assessment_runs(db, runs, settings=settings)
    return [_run_read(db, run) for run in runs]


@router.post("/{program_id}/runs", response_model=ReviewProgramRunRead, status_code=status.HTTP_201_CREATED)
def start_program_run(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    payload: ReviewProgramRunStart,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = start_review_program_run(
            db,
            program=program,
            actor=principal.user,
            idempotency_key=payload.idempotency_key,
            confirm_replace_unfinished_run=payload.confirm_replace_unfinished_run,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The Review Program run conflicts with an existing run",
        ) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.get("/{program_id}/runs/{run_id}", response_model=ReviewProgramRunRead)
def read_program_run(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.view")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id)
        run = get_review_program_run(db, program=program, run_id=run_id)
        _reconcile_definition_assessment_runs(db, [run], settings=settings)
        return _run_read(db, run)
    except ReviewProgramError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/{program_id}/runs/{run_id}/final-alignment",
    response_model=ReviewProgramFinalAlignmentPageRead,
)
def read_program_final_alignment(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    offset: int = 0,
    limit: int = 50,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramFinalAlignmentPageRead:
    _matter_with_permission(db, principal, matter_id, "review_program.view")
    if offset < 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="offset must be non-negative")
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="limit must be between 1 and 200")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id)
        run = get_review_program_run(db, program=program, run_id=run_id)
    except ReviewProgramError as exc:
        raise _http_error(exc) from exc
    report = dict((run.progress_summary or {}).get("final_alignment") or {})
    manifest_id = report.get("manifest_id")
    if not manifest_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Final alignment output not found")
    try:
        parsed_manifest_id = uuid.UUID(str(manifest_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Final alignment manifest reference is invalid",
        ) from exc
    manifest = db.get(AnalysisInputManifest, parsed_manifest_id)
    if (
        manifest is None
        or manifest.matter_id != matter_id
        or manifest.owner_type != "REVIEW_PROGRAM_FINAL_ALIGNMENT"
        or manifest.producer_run_id != run.id
        or manifest.manifest_status != "FINALIZED"
        or manifest.artifact_id is None
        or manifest.artifact_content_hash is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Final alignment manifest is unavailable or does not belong to this run",
        )
    if offset > manifest.item_count:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="offset is outside the final alignment population",
        )
    count = min(limit, manifest.item_count - offset)
    try:
        rows = AnalysisInputManifestReader(
            manifest,
            actor_user_id=principal.user.id,
        ).rows(offset, count)
    except AnalysisInputManifestError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Final alignment output could not be read: {exc}",
        ) from exc

    profile_ids: list[uuid.UUID] = []
    for raw_id in report.get("profile_ids") or []:
        try:
            profile_ids.append(uuid.UUID(str(raw_id)))
        except ValueError:
            continue
    profile_records = {
        item.id: item
        for item in db.scalars(
            select(AnalysisTaskExecutionProfile).where(
                AnalysisTaskExecutionProfile.id.in_(profile_ids)
            )
        )
    }
    profile_names = [
        profile_records[profile_id].resolved_model
        if profile_id in profile_records
        else f"Q&A profile {index + 1}"
        for index, profile_id in enumerate(profile_ids[:2])
    ]
    while len(profile_names) < 2:
        profile_names.append(f"Q&A profile {len(profile_names) + 1}")

    issue_labels = _alignment_issue_labels(db, matter_id)
    items: list[ReviewProgramFinalAlignmentRowRead] = []
    for row in rows:
        resolved_inputs = row.get("resolved_inputs")
        text_source = (
            resolved_inputs.get("text_source")
            if isinstance(resolved_inputs, dict)
            and isinstance(resolved_inputs.get("text_source"), dict)
            else {}
        )
        family_id = row.get("family_id")
        items.append(
            ReviewProgramFinalAlignmentRowRead(
                matter_document_id=uuid.UUID(str(row["matter_document_id"])),
                collection_item_id=uuid.UUID(str(row["collection_item_id"])),
                scope_ordinal=int(row["scope_ordinal"]),
                filename=str(text_source.get("original_filename"))
                if text_source.get("original_filename")
                else None,
                input_status=str(row.get("input_status") or "UNKNOWN"),
                classifier_codings=_alignment_codings(
                    row.get("classifier_issue_keys"), issue_labels=issue_labels
                ),
                profile_a_codings=_alignment_codings(
                    row.get("profile_a_issue_keys"), issue_labels=issue_labels
                ),
                profile_b_codings=_alignment_codings(
                    row.get("profile_b_issue_keys"), issue_labels=issue_labels
                ),
                human_codings=_alignment_codings(
                    row.get("human_issue_keys"), issue_labels=issue_labels
                ),
                alignment_status=str(row.get("alignment_status") or "UNKNOWN"),
                source_status=str(row.get("source_status") or "UNKNOWN"),
                confidence_band=str(row.get("confidence_band") or "UNKNOWN"),
                training_overlap=bool(row.get("training_overlap")),
                label_authority=str(row.get("label_authority") or "UNKNOWN"),
                family_id=uuid.UUID(str(family_id)) if family_id else None,
                record_type=str(row.get("record_type") or "UNKNOWN"),
            )
        )
    return ReviewProgramFinalAlignmentPageRead(
        manifest_id=manifest.id,
        artifact_id=manifest.artifact_id,
        manifest_content_hash=manifest.artifact_content_hash,
        population_scope=str(report.get("population_scope"))
        if report.get("population_scope")
        else None,
        minimum_confidence=float(report["minimum_confidence"])
        if report.get("minimum_confidence") is not None
        else None,
        profile_a_name=profile_names[0],
        profile_b_name=profile_names[1],
        total=manifest.item_count,
        offset=offset,
        limit=limit,
        items=items,
    )


@router.get(
    "/{program_id}/runs/{run_id}/qa-population-statistics",
    response_model=ReviewProgramQAPopulationStatisticsRead,
)
def read_program_qa_population_statistics(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    metadata_definition_id: uuid.UUID | None = None,
    include_classifier: bool = False,
    include_human: bool = False,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramQAPopulationStatisticsRead:
    matter = _matter_with_permission(db, principal, matter_id, "review_program.view")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id)
        run = get_review_program_run(db, program=program, run_id=run_id)
        _task, task_version, profiles = _qa_task_records(db, run)
    except ReviewProgramError as exc:
        raise _http_error(exc) from exc

    destination_ids: list[uuid.UUID] = []
    for raw_id in task_version.destination_metadata_definition_ids or []:
        try:
            destination_ids.append(uuid.UUID(str(raw_id)))
        except ValueError:
            continue
    definitions = list(
        db.scalars(
            select(MetadataDefinition)
            .where(
                MetadataDefinition.matter_id == matter_id,
                MetadataDefinition.id.in_(destination_ids),
            )
            .order_by(MetadataDefinition.display_name, MetadataDefinition.id)
        )
    )
    if not definitions:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The accepted Q&A task has no available coding fields",
        )
    definition_by_id = {definition.id: definition for definition in definitions}
    selected_definition = (
        definition_by_id.get(metadata_definition_id) if metadata_definition_id is not None else definitions[0]
    )
    if selected_definition is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The selected coding field is not part of the accepted Q&A task",
        )

    profile_ids = {profile.id for profile in profiles}
    qa_values_by_document = _program_qa_population_source_values(
        db,
        run=run,
        task_version_id=task_version.id,
        profile_ids=profile_ids,
        definition=selected_definition,
    )
    document_ids = sorted(qa_values_by_document, key=str)
    matter_documents = {
        document.id: document
        for document in db.scalars(
            select(MatterDocument).where(
                MatterDocument.matter_id == matter_id,
                MatterDocument.id.in_(document_ids),
            )
        )
    }
    if len(matter_documents) != len(document_ids):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The Q&A population contains documents outside this matter",
        )

    qa_source_options = [
        ReviewBatchComparisonSourceOptionRead(
            id=profile.id,
            source_type="RUN",
            run_type="WORKFLOW",
            purpose=None,
            status="COMPLETED",
            label=profile.resolved_model,
            detail=f"{profile.name} · {profile.decision_provider.replace('_', ' ').title()}",
            metadata_definition_ids=[definition.id for definition in definitions],
            created_at=profile.created_at,
        )
        for profile in profiles
    ]
    optional_source_options: list[ReviewBatchComparisonSourceOptionRead] = []
    scoring = dict((run.progress_summary or {}).get("whole_collection_scoring") or {})
    matching_classifier_states = [
        item
        for item in scoring.get("models", [])
        if isinstance(item, dict) and item.get("field_key") == selected_definition.key
    ]
    classifier_source_id = uuid.uuid5(
        run.id,
        f"qa-population-statistics:classifier:{selected_definition.id}",
    )
    if matching_classifier_states:
        model_ids: list[uuid.UUID] = []
        for item in matching_classifier_states:
            try:
                model_ids.append(uuid.UUID(str(item["classification_model_id"])))
            except (KeyError, TypeError, ValueError):
                continue
        model_names = list(
            db.scalars(
                select(ClassificationModel.name)
                .where(ClassificationModel.id.in_(model_ids))
                .order_by(ClassificationModel.name)
            )
        )
        optional_source_options.append(
            ReviewBatchComparisonSourceOptionRead(
                id=classifier_source_id,
                source_type="RUN",
                run_type="CLASSIFIER",
                purpose="CANDIDATE",
                status="COMPLETED",
                label=(
                    " + ".join(model_names)
                    if model_names
                    else f"{selected_definition.display_name} classifier"
                ),
                detail=(
                    "Whole-matter classifier result"
                    if len(model_names) <= 1
                    else f"{len(model_names)} accepted whole-matter classifiers"
                ),
                metadata_definition_ids=[selected_definition.id],
                created_at=run.created_at,
            )
        )

    resolution = dict((run.progress_summary or {}).get("training_label_resolution") or {})
    has_human_source = any(
        isinstance(item, dict)
        and str(item.get("metadata_definition_id")) == str(selected_definition.id)
        for item in resolution.get("adjudication_sources", [])
    )
    human_source_id = uuid.uuid5(
        run.id,
        f"qa-population-statistics:human:{selected_definition.id}",
    )
    if has_human_source:
        optional_source_options.append(
            ReviewBatchComparisonSourceOptionRead(
                id=human_source_id,
                source_type="REVIEWER",
                label="Human reviewer decisions",
                detail="Recorded disagreement-review adjudications",
                metadata_definition_ids=[selected_definition.id],
                created_at=run.created_at,
            )
        )
    if include_classifier and not matching_classifier_states:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Accepted classifier results are unavailable for the selected coding field",
        )
    if include_human and not has_human_source:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Human reviewer results are unavailable for the selected coding field",
        )

    classifier_predictions: dict[uuid.UUID, dict[str, object]] = {}
    if include_classifier:
        try:
            classifier_predictions, _scoring_runs, _issue_universe = _classifier_alignment_predictions(
                db,
                program=program,
                run=run,
                matter=matter,
                actor_user_id=run.initiated_by_user_id,
                field_keys={selected_definition.key},
            )
        except ReviewProgramError as exc:
            raise _http_error(exc) from exc
    human_by_document: dict[uuid.UUID, set[str]] = {}
    if include_human:
        try:
            human_by_document, _human_sources = _human_adjudicated_issue_keys(
                db,
                run=run,
                definitions=definition_by_id,
            )
        except ReviewProgramError as exc:
            raise _http_error(exc) from exc

    source_options = [
        *qa_source_options,
        *([next(item for item in optional_source_options if item.id == classifier_source_id)] if include_classifier else []),
        *([next(item for item in optional_source_options if item.id == human_source_id)] if include_human else []),
    ]
    comparison_documents: list[ComparisonDocument] = []
    both_profiles_document_count = 0
    for sequence_number, document_id in enumerate(document_ids, start=1):
        document_values = qa_values_by_document[document_id]
        both_profiles_document_count += all(profile.id in document_values for profile in profiles)
        source_values: dict[uuid.UUID, SourceValue] = {}
        for profile in profiles:
            source_values[profile.id] = document_values.get(
                profile.id,
                SourceValue(
                    state="NOT_EVALUATED",
                    value_keys=(),
                    values=(),
                    top_confidence=None,
                ),
            )
        if include_classifier:
            prediction = classifier_predictions.get(document_id)
            source_values[classifier_source_id] = _classifier_population_source_value(
                prediction,
                selected_definition.key,
            )
        if include_human:
            source_values[human_source_id] = _human_population_source_value(
                human_by_document,
                document_id,
                selected_definition.key,
            )
        value_sets = [set(value.value_keys) for value in source_values.values()]
        if any(value.state != "EVALUATED" for value in source_values.values()):
            agreement_status = "INCOMPLETE"
        elif all(values == value_sets[0] for values in value_sets[1:]):
            agreement_status = "EXACT"
        elif any(
            value_sets[index].intersection(value_sets[other])
            for index in range(len(value_sets))
            for other in range(index + 1, len(value_sets))
        ):
            agreement_status = "PARTIAL"
        else:
            agreement_status = "DISAGREEMENT"
        comparison_documents.append(
            ComparisonDocument(
                matter_document_id=document_id,
                collection_item_id=matter_documents[document_id].collection_item_id,
                sequence_number=sequence_number,
                agreement_status=agreement_status,
                sources=source_values,
            )
        )

    comparison = build_coding_comparison(
        db,
        batch_id=run.id,
        definition=selected_definition,
        request=ReviewBatchComparisonRequest(
            metadata_definition_id=selected_definition.id,
            source_ids=[source.id for source in source_options],
            row_filter="ALL",
            offset=0,
            limit=1,
        ),
        source_options=source_options,
        document_count=len(document_ids),
        comparison_documents=comparison_documents,
    )
    return ReviewProgramQAPopulationStatisticsRead(
        run_id=run.id,
        task_version_id=task_version.id,
        document_count=len(document_ids),
        both_profiles_document_count=both_profiles_document_count,
        one_profile_document_count=len(document_ids) - both_profiles_document_count,
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
        selected_metadata_definition_id=selected_definition.id,
        available_sources=[*qa_source_options, *optional_source_options],
        sources=comparison.sources,
        source_summaries=comparison.source_summaries,
        multi_source_agreement=comparison.multi_source_agreement,
        pairwise_agreements=comparison.pairwise_agreements,
    )


@router.post("/{program_id}/runs/{run_id}/actions/{action_id}/resolve", response_model=ReviewProgramRunRead)
def resolve_program_action(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: ReviewProgramActionResolve,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    assessment_cancellations: list[tuple[uuid.UUID, str | None]] = []
    discovery_batch_set_workflows: list[str] = []
    discovery_batch_run_workflows: list[str] = []
    discovery_child_workflows: list[str] = []
    difference_batch_workflows: list[str] = []
    classification_training_workflows: list[str] = []
    classification_scoring_workflows: list[str] = []
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        if payload.choice_key == "STOP":
            assessment_cancellations = [
                (assessment.id, workflow.dbos_workflow_id if workflow is not None else None)
                for assessment, workflow in review_program_assessment_cancellation_targets(db, run)
            ]
            (
                discovery_batch_set_workflows,
                discovery_batch_run_workflows,
                discovery_child_workflows,
                difference_batch_workflows,
                classification_training_workflows,
                classification_scoring_workflows,
            ) = cancel_review_program_discovery_children(
                db,
                run=run,
                actor_user_id=principal.user.id,
            )
        resolve_review_program_action(
            db,
            program=program,
            run=run,
            action_id=action_id,
            actor=principal.user,
            preview_hash=payload.preview_hash,
            choice_key=payload.choice_key,
            reason=payload.reason,
            input_payload=payload.input,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    for assessment_id, dbos_workflow_id in assessment_cancellations:
        cancel_provider_batches(db, assessment_id)
        if dbos_workflow_id:
            cancel_definition_assessment(dbos_workflow_id)
    for workflow_id in discovery_batch_set_workflows:
        cancel_analysis_task_batch_set_workflow(workflow_id)
    for workflow_id in discovery_batch_run_workflows:
        cancel_analysis_task_batch_set_run_workflow(workflow_id)
    for workflow_id in discovery_child_workflows:
        cancel_analysis_task_batch(workflow_id)
    for workflow_id in difference_batch_workflows:
        cancel_review_batch(workflow_id)
    for workflow_id in classification_training_workflows:
        cancel_classification_training_job(workflow_id)
    for workflow_id in classification_scoring_workflows:
        cancel_classification_model_scoring(workflow_id)
    db.refresh(run)
    return _run_read(db, run)


@router.post(
    "/{program_id}/runs/{run_id}/actions/{action_id}/definition-setup",
    response_model=ReviewProgramRunRead,
)
def setup_program_definition(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: ReviewProgramDefinitionSetup,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        setup_review_program_definition(
            db,
            program=program,
            run=run,
            action_id=action_id,
            actor=principal.user,
            preview_hash=payload.preview_hash,
            mode=payload.mode,
            existing_definition_id=payload.existing_definition_id,
            source_revision=payload.source_revision,
            definition_key=payload.definition_key,
            name=payload.name,
            description=payload.description,
            content_markdown=payload.content_markdown,
            assessment_target_document_count=payload.assessment_target_document_count,
            assessment_control_sample_size=payload.assessment_control_sample_size,
            assessment_use_batching=payload.assessment_use_batching,
            acknowledge_large_run_warning=payload.acknowledge_large_run_warning,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Matter Definition setup conflicts with an existing record",
        ) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.post(
    "/{program_id}/runs/{run_id}/actions/{action_id}/qa-setup",
    response_model=ReviewProgramRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def setup_program_qa_task(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: ReviewProgramQASetup,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        setup_review_program_qa_task(
            db,
            program=program,
            run=run,
            action_id=action_id,
            actor=principal.user,
            preview_hash=payload.preview_hash,
            task_key=payload.task_key,
            name=payload.name,
            description=payload.description,
            destination_metadata_definition_ids=payload.destination_metadata_definition_ids,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Q&A task setup conflicts with an existing record",
        ) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.post(
    "/{program_id}/runs/{run_id}/actions/{action_id}/discovery-setup",
    response_model=ReviewProgramRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def setup_program_discovery(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: ReviewProgramDiscoverySetup,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        setup_review_program_discovery(
            db,
            program=program,
            run=run,
            action_id=action_id,
            actor=principal.user,
            preview_hash=payload.preview_hash,
            target_document_count=payload.target_document_count,
            target_batch_size=payload.target_batch_size,
            keep_families_together=payload.keep_families_together,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Discovery Q&A setup conflicts with an existing record",
        ) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.post(
    "/{program_id}/runs/{run_id}/actions/{action_id}/classifier-improvement-setup",
    response_model=ReviewProgramRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def setup_program_classifier_improvement(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: ReviewProgramClassifierImprovementSetup,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        setup_review_program_classifier_improvement(
            db,
            program=program,
            run=run,
            action_id=action_id,
            actor=principal.user,
            preview_hash=payload.preview_hash,
            queries=[query.model_dump(mode="json") for query in payload.queries],
            control_sample_size=payload.control_sample_size,
            target_batch_size=payload.target_batch_size,
            keep_families_together=payload.keep_families_together,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Classifier improvement setup conflicts with an existing record",
        ) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.post(
    "/{program_id}/runs/{run_id}/actions/{action_id}/negative-population-qc-inventory",
    response_model=ReviewProgramRunRead,
    status_code=status.HTTP_200_OK,
)
def prepare_program_negative_population_qc_inventory(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: ReviewProgramNegativePopulationQCInventoryPrepare,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(
            db,
            matter_id=matter_id,
            program_id=program_id,
            for_update=True,
        )
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        prepare_review_program_negative_population_qc_inventory(
            db,
            program=program,
            run=run,
            action_id=action_id,
            actor=principal.user,
            preview_hash=payload.preview_hash,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Negative-population QC inventory conflicts with an existing record",
        ) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.post(
    "/{program_id}/runs/{run_id}/actions/{action_id}/negative-population-qc-setup",
    response_model=ReviewProgramRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def setup_program_negative_population_qc(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: ReviewProgramNegativePopulationQCSetup,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        setup_review_program_negative_population_qc(
            db,
            program=program,
            run=run,
            action_id=action_id,
            actor=principal.user,
            preview_hash=payload.preview_hash,
            inventory_content_hash=payload.inventory_content_hash,
            selected_procedures=list(payload.selected_procedures),
            selected_record_types=payload.selected_record_types,
            random_sample_size=payload.random_sample_size,
            near_threshold_sample_size=payload.near_threshold_sample_size,
            cohort_control_sample_size=payload.cohort_control_sample_size,
            mandatory_review_limit=payload.mandatory_review_limit,
            keep_families_together=payload.keep_families_together,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Negative-population QC setup conflicts with an existing record",
        ) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.post("/{program_id}/runs/{run_id}/cancel", response_model=ReviewProgramRunRead)
def cancel_program_run(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    payload: ReviewProgramRunCommand,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        assessment_cancellations = [
            (assessment.id, workflow.dbos_workflow_id if workflow is not None else None)
            for assessment, workflow in review_program_assessment_cancellation_targets(db, run)
        ]
        (
            discovery_batch_set_workflows,
            discovery_batch_run_workflows,
            discovery_child_workflows,
            difference_batch_workflows,
            classification_training_workflows,
            classification_scoring_workflows,
        ) = cancel_review_program_discovery_children(
            db,
            run=run,
            actor_user_id=principal.user.id,
        )
        cancel_review_program_run(
            db,
            program=program,
            run=run,
            actor=principal.user,
            idempotency_key=payload.idempotency_key,
            reason=payload.reason,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    for assessment_id, dbos_workflow_id in assessment_cancellations:
        cancel_provider_batches(db, assessment_id)
        if dbos_workflow_id:
            cancel_definition_assessment(dbos_workflow_id)
    for workflow_id in discovery_batch_set_workflows:
        cancel_analysis_task_batch_set_workflow(workflow_id)
    for workflow_id in discovery_batch_run_workflows:
        cancel_analysis_task_batch_set_run_workflow(workflow_id)
    for workflow_id in discovery_child_workflows:
        cancel_analysis_task_batch(workflow_id)
    for workflow_id in difference_batch_workflows:
        cancel_review_batch(workflow_id)
    for workflow_id in classification_training_workflows:
        cancel_classification_training_job(workflow_id)
    for workflow_id in classification_scoring_workflows:
        cancel_classification_model_scoring(workflow_id)
    db.refresh(run)
    return _run_read(db, run)


@router.post("/{program_id}/runs/{run_id}/reset-discovery-qa", response_model=ReviewProgramRunRead)
def reset_discovery_qa(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    payload: ReviewProgramRunCommand,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        reset_review_program_discovery_qa(
            db,
            program=program,
            run=run,
            actor=principal.user,
            idempotency_key=payload.idempotency_key,
            reason=payload.reason,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.post("/{program_id}/runs/{run_id}/cancel-discovery-qa", response_model=ReviewProgramRunRead)
def cancel_discovery_qa(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    payload: ReviewProgramRunCommand,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        already_canceled = (
            (run.progress_summary or {}).get("last_discovery_qa_cancel_key")
            == payload.idempotency_key.strip()
        )
        cancel_review_program_discovery_qa(
            db,
            program=program,
            run=run,
            actor=principal.user,
            idempotency_key=payload.idempotency_key,
            reason=payload.reason,
        )
        if already_canceled:
            cancellation_targets = ([], [], [], [], [], [])
        else:
            cancellation_targets = cancel_review_program_discovery_children(
                db,
                run=run,
                actor_user_id=principal.user.id,
                purposes={"DISCOVERY_POPULATION", "DISCOVERY_QA_PROFILE"},
            )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    (
        batch_set_workflows,
        batch_run_workflows,
        child_workflows,
        review_batch_workflows,
        classification_training_workflows,
        classification_scoring_workflows,
    ) = cancellation_targets
    for workflow_id in batch_set_workflows:
        cancel_analysis_task_batch_set_workflow(workflow_id)
    for workflow_id in batch_run_workflows:
        cancel_analysis_task_batch_set_run_workflow(workflow_id)
    for workflow_id in child_workflows:
        cancel_analysis_task_batch(workflow_id)
    for workflow_id in review_batch_workflows:
        cancel_review_batch(workflow_id)
    for workflow_id in classification_training_workflows:
        cancel_classification_training_job(workflow_id)
    for workflow_id in classification_scoring_workflows:
        cancel_classification_model_scoring(workflow_id)
    db.refresh(run)
    return _run_read(db, run)


@router.post("/{program_id}/runs/{run_id}/retry", response_model=ReviewProgramRunRead)
def retry_program_run(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    payload: ReviewProgramRunCommand,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        retry_review_program_run(
            db,
            program=program,
            run=run,
            actor=principal.user,
            idempotency_key=payload.idempotency_key,
        )
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.post("/{program_id}/runs/{run_id}/reconcile", response_model=ReviewProgramRunRead)
def reconcile_program_run(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ReviewProgramRunRead:
    _matter_with_permission(db, principal, matter_id, "review_program.manage")
    try:
        program = get_review_program(db, matter_id=matter_id, program_id=program_id, for_update=True)
        run = get_review_program_run(db, program=program, run_id=run_id, for_update=True)
        _reconcile_definition_assessment_runs(db, [run], settings=settings)
        reconcile_review_program_run(db, program=program, run=run, actor=principal.user)
        db.commit()
    except ReviewProgramError as exc:
        db.rollback()
        raise _http_error(exc) from exc
    db.refresh(run)
    return _run_read(db, run)


@router.get(
    "/{program_id}/runs/{run_id}/completion",
    response_model=ReviewProgramCompletionReportRead,
)
def get_program_completion_report(
    matter_id: uuid.UUID,
    program_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ReviewProgramCompletionReportRead:
    _matter_with_permission(db, principal, matter_id, "review_program.view")
    program = get_review_program(db, matter_id=matter_id, program_id=program_id)
    get_review_program_run(db, program=program, run_id=run_id)
    report = db.scalar(
        select(ReviewProgramCompletionReport).where(
            ReviewProgramCompletionReport.program_id == program.id,
            ReviewProgramCompletionReport.run_id == run_id,
        )
    )
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Completion report not found")
    return ReviewProgramCompletionReportRead.model_validate(report)
