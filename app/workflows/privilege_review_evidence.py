"""Durable cited-evidence extraction for Privilege Review programs."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from typing import Any

from dbos import DBOS, Queue, SetWorkflowID
from sqlalchemy import func, select

from app.artifact_gateway import (
    copy_workflow_artifact_content,
    find_workflow_artifact_reference,
    get_workflow_artifact_reference,
)
from app.assessment_provider_batches import SUCCESS_PROVIDER_STATES, TERMINAL_PROVIDER_STATES
from app.config import get_settings
from app.database import SessionLocal
from app.evidence_first_artifacts import (
    load_compacted_observations_bytes,
    load_evaluation_plan_bytes,
    merge_family_observation_results,
)
from app.evidence_first_shadow import WorkflowEvidenceShadowArtifactRepository
from app.models import (
    MatterDefinitionAssessmentRun,
    ReviewBatchDocument,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewProgramChild,
    ReviewProgramRun,
    WorkflowRun,
    utcnow,
)
from app.privilege_evidence_batches import (
    fail_privilege_evidence_batch_to_fallback,
    finalize_privilege_evidence_batch,
    plan_privilege_evidence_batches,
    poll_privilege_evidence_batch,
    submit_privilege_evidence_batch,
)
from app.privilege_evidence_context import (
    load_privilege_evidence_packets,
    privilege_evidence_family_groups,
)
from app.review_programs import project_privilege_evidence_terminal
from app.workflows.evidence_assessments import (
    EVIDENCE_EXTRACTION_UNIT_QUEUE,
    activate_evidence_first_assessment,
    evidence_extraction_unit_workflow,
    finalize_extraction_fanout,
    prepare_extraction_fanout,
)
from artifact_service.workflow_artifact_types import ArtifactType

logger = logging.getLogger(__name__)

PRIVILEGE_EVIDENCE_QUEUE = Queue("privilege-review-evidence", global_concurrency=2)
PRIVILEGE_EVIDENCE_DOCUMENT_QUEUE = Queue(
    "privilege-review-evidence-documents",
    global_concurrency=get_settings().definition_assessment_evidence_document_concurrency,
)
PRIVILEGE_EVIDENCE_CHUNK_QUEUE = Queue("privilege-review-evidence-chunks", global_concurrency=4)
PRIVILEGE_EVIDENCE_PROVIDER_QUEUE = Queue("privilege-review-evidence-provider-batches", global_concurrency=4)
DOCUMENT_CHUNK_SIZE = 100


@DBOS.step(name="activate_privilege_review_evidence", retries_allowed=True, max_attempts=3)
def activate_records(assessment_id: str) -> None:
    with SessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(assessment_id))
        if assessment is None or assessment.review_batch_run_id is None:
            raise ValueError("Privilege evidence assessment is unavailable")
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        review_run = db.get(ReviewBatchRun, assessment.review_batch_run_id)
        if workflow is None or review_run is None:
            raise ValueError("Privilege evidence execution records are unavailable")
        now = utcnow()
        assessment.status = "SUMMARIZING"
        assessment.started_at = assessment.started_at or now
        workflow.status = "RUNNING"
        workflow.started_at = workflow.started_at or now
        workflow.current_stage = "EVIDENCE_EXTRACTION"
        review_run.status = "RUNNING"
        review_run.started_at = review_run.started_at or now
        db.commit()


def _family_groups(
    db,
    *,
    assessment: MatterDefinitionAssessmentRun,
    actor_user_id: uuid.UUID,
) -> list[dict[str, Any]]:
    workflow = db.get(WorkflowRun, assessment.workflow_run_id)
    if workflow is None:
        raise ValueError("Privilege evidence workflow is unavailable")
    reference = assessment.configuration_snapshot.get("privilege_evidence_packets")
    raw_program_id = assessment.configuration_snapshot.get("privilege_review_program_id")
    if not isinstance(reference, dict) or raw_program_id is None:
        raise ValueError("Frozen privilege family packets are unavailable")
    # Artifact reads may involve object storage or a remote artifact service.
    # Release the Core transaction before doing that I/O.
    tenant_id = workflow.tenant_id
    client_id = workflow.client_id
    matter_id = assessment.matter_id
    review_batch_id = assessment.review_batch_id
    db.commit()
    packets = load_privilege_evidence_packets(
        packet_reference=reference,
        matter_id=matter_id,
        program_id=uuid.UUID(str(raw_program_id)),
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    groups = privilege_evidence_family_groups(packets)
    frozen_document_ids = {
        str(value)
        for value in db.scalars(
            select(ReviewBatchDocument.matter_document_id).where(
                ReviewBatchDocument.review_batch_id == review_batch_id
            )
        )
    }
    grouped_document_ids = {
        value
        for group in groups
        for value in group["review_target_document_ids"]
    }
    if frozen_document_ids != grouped_document_ids:
        raise ValueError("Frozen privilege family groups do not match the evidence population")
    return groups


@DBOS.step(name="load_privilege_review_evidence_members", retries_allowed=True, max_attempts=3)
def load_family_member_ids(assessment_id: str, actor_user_id: str) -> list[str]:
    with SessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(assessment_id))
        if assessment is None:
            raise ValueError("Privilege evidence assessment is unavailable")
        groups = _family_groups(
                db,
                assessment=assessment,
                actor_user_id=uuid.UUID(actor_user_id),
            )
        return sorted(
            {
                str(document_id)
                for group in groups
                for document_id in group["review_target_document_ids"]
            }
        )


@DBOS.step(name="plan_privilege_review_evidence_provider_batches", retries_allowed=True, max_attempts=3)
def plan_provider_batches(assessment_id: str, document_ids: list[str], actor_user_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        return plan_privilege_evidence_batches(
            db,
            assessment_id=uuid.UUID(assessment_id),
            document_ids=[uuid.UUID(value) for value in document_ids],
            actor_user_id=uuid.UUID(actor_user_id),
            settings=get_settings(),
        )


@DBOS.step(name="submit_privilege_review_evidence_provider_batch", retries_allowed=True, max_attempts=3)
def submit_provider_batch(batch_id: str, actor_user_id: str) -> str:
    with SessionLocal() as db:
        return submit_privilege_evidence_batch(
            db,
            batch_id=uuid.UUID(batch_id),
            actor_user_id=uuid.UUID(actor_user_id),
        )


@DBOS.step(name="poll_privilege_review_evidence_provider_batch", retries_allowed=True, max_attempts=10)
def poll_provider_batch(batch_id: str) -> str:
    with SessionLocal() as db:
        return poll_privilege_evidence_batch(db, uuid.UUID(batch_id))


@DBOS.step(name="finalize_privilege_review_evidence_provider_batch", retries_allowed=True, max_attempts=3)
def finalize_provider_batch(batch_id: str, actor_user_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        return finalize_privilege_evidence_batch(
            db,
            batch_id=uuid.UUID(batch_id),
            actor_user_id=uuid.UUID(actor_user_id),
        )


@DBOS.step(name="fallback_privilege_review_evidence_provider_batch", retries_allowed=True, max_attempts=3)
def fallback_provider_batch(batch_id: str, message: str) -> list[str]:
    with SessionLocal() as db:
        return fail_privilege_evidence_batch_to_fallback(
            db,
            batch_id=uuid.UUID(batch_id),
            message=message,
        )


@DBOS.workflow(name="privilege_review_evidence_provider_batch_v1")
def provider_batch_workflow(batch_id: str, actor_user_id: str, poll_seconds: float) -> dict[str, Any]:
    try:
        status = submit_provider_batch(batch_id, actor_user_id)
        while status not in TERMINAL_PROVIDER_STATES:
            DBOS.sleep(poll_seconds)
            status = poll_provider_batch(batch_id)
        if status not in SUCCESS_PROVIDER_STATES:
            return {
                "completed": 0,
                "fallback_document_ids": fallback_provider_batch(
                    batch_id,
                    f"Privilege evidence provider batch ended in {status}",
                ),
            }
        return finalize_provider_batch(batch_id, actor_user_id)
    except Exception as exc:
        logger.exception("Privilege evidence provider batch failed batch_id=%s", batch_id)
        message = " ".join(str(exc).split()) or type(exc).__name__
        return {
            "completed": 0,
            "fallback_document_ids": fallback_provider_batch(batch_id, message),
        }


def _failure(document_id: str, exc: BaseException) -> dict[str, Any]:
    message = " ".join(str(exc).split()) or type(exc).__name__
    return {
        "matter_document_id": document_id,
        "status": "FAILED",
        "failure_code": f"EXTRACTION_{type(exc).__name__}"[:100],
        "failure_message": message[:4_000],
        "failure_retriable": True,
    }


@DBOS.step(name="record_privilege_review_evidence_document", retries_allowed=True, max_attempts=5)
def record_document(assessment_id: str, document_id: str, result: dict[str, Any]) -> dict[str, Any]:
    with SessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(assessment_id))
        if assessment is None or assessment.review_batch_run_id is None:
            raise ValueError("Privilege evidence batch run is unavailable")
        row = db.scalar(
            select(ReviewBatchRunDocument)
            .where(
                ReviewBatchRunDocument.review_batch_run_id == assessment.review_batch_run_id,
                ReviewBatchRunDocument.matter_document_id == uuid.UUID(document_id),
            )
            .with_for_update()
        )
        if row is None:
            raise ValueError("Privilege evidence document is unavailable")
        completed = result.get("status") == "COMPLETED"
        row.status = "COMPLETED" if completed else "FAILED"
        row.completed_at = utcnow()
        row.failure_code = None if completed else str(result.get("failure_code") or "EXTRACTION_FAILED")[:100]
        row.failure_message = None if completed else str(result.get("failure_message") or "Evidence extraction failed")[:4_000]
        row.failure_retriable = False if completed else bool(result.get("failure_retriable", True))
        workflow_run_id = result.get("workflow_run_id")
        row.last_workflow_run_id = uuid.UUID(str(workflow_run_id)) if workflow_run_id else None
        db.commit()
        return result


def _workflow_observations_artifact_id(workflow: WorkflowRun) -> uuid.UUID:
    documents = (workflow.progress or {}).get("documents")
    if not isinstance(documents, list) or len(documents) != 1 or not isinstance(documents[0], dict):
        raise ValueError("Member evidence workflow does not expose one observations artifact")
    value = documents[0].get("observations_artifact_id")
    if not value:
        raise ValueError("Member evidence workflow has no observations artifact")
    return uuid.UUID(str(value))


def _verified_artifact_bytes(
    *,
    artifact_id: uuid.UUID,
    expected_hash: str,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> bytes:
    destination = BytesIO()
    copy_workflow_artifact_content(
        artifact_id=artifact_id,
        destination=destination,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    content = destination.getvalue()
    if sha256(content).hexdigest() != expected_hash:
        raise ValueError("Workflow artifact content does not match its immutable reference")
    return content


@DBOS.step(name="merge_privilege_review_family_evidence", retries_allowed=True, max_attempts=5)
def merge_family_evidence(
    assessment_id: str,
    actor_user_id: str,
) -> dict[str, int]:
    """Merge full member extraction results into one immutable family artifact."""

    with SessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(assessment_id))
        if assessment is None or assessment.review_batch_run_id is None:
            raise ValueError("Privilege evidence batch run is unavailable")
        family_groups = _family_groups(
            db,
            assessment=assessment,
            actor_user_id=uuid.UUID(actor_user_id),
        )
        db.commit()
        completed_families = 0
        failed_families = 0
        for group in family_groups:
            rows = []
            for raw_document_id in group["review_target_document_ids"]:
                row = db.get(
                    ReviewBatchRunDocument,
                    (assessment.review_batch_run_id, uuid.UUID(str(raw_document_id))),
                )
                if row is None:
                    raise ValueError("Privilege family member is unavailable in the evidence run")
                rows.append(row)
            if any(row.status not in {"COMPLETED", "FAILED", "SKIPPED"} for row in rows):
                continue
            failed = next((row for row in rows if row.status != "COMPLETED"), None)
            if failed is not None:
                failed_families += 1
                for row in rows:
                    row.status = "FAILED"
                    row.failure_code = failed.failure_code or "FAMILY_EVIDENCE_INCOMPLETE"
                    row.failure_message = (
                        failed.failure_message
                        or "At least one family member did not produce complete cited evidence"
                    )
                    row.failure_retriable = bool(failed.failure_retriable)
                    row.completed_at = row.completed_at or utcnow()
                db.commit()
                continue
            try:
                workflow_ids = {row.last_workflow_run_id for row in rows}
                if None in workflow_ids or len(workflow_ids) != len(rows):
                    raise ValueError("Family members do not have distinct completed evidence workflows")
                workflows = {
                    workflow.id: workflow
                    for workflow in db.scalars(
                        select(WorkflowRun).where(WorkflowRun.id.in_(workflow_ids))
                    )
                }
                db.commit()
                member_results = []
                member_artifact_ids: list[uuid.UUID] = []
                producer = None
                for row in sorted(rows, key=lambda value: str(value.matter_document_id)):
                    workflow = workflows.get(row.last_workflow_run_id)
                    if workflow is None:
                        raise ValueError("Completed family member workflow is unavailable")
                    producer = producer or workflow
                    artifact_id = _workflow_observations_artifact_id(workflow)
                    reference = get_workflow_artifact_reference(
                        artifact_id=artifact_id,
                        actor_user_id=uuid.UUID(actor_user_id),
                        tenant_id=workflow.tenant_id,
                        client_id=workflow.client_id,
                    )
                    observations = load_compacted_observations_bytes(
                        _verified_artifact_bytes(
                            artifact_id=reference.artifact_id,
                            expected_hash=reference.content_hash,
                            actor_user_id=uuid.UUID(actor_user_id),
                            tenant_id=workflow.tenant_id,
                            client_id=workflow.client_id,
                        )
                    )
                    plan_reference = find_workflow_artifact_reference(
                        artifact_type=ArtifactType.ANALYSIS_EVALUATION_PLAN,
                        derivation_key=observations.plan_hash,
                        actor_user_id=uuid.UUID(actor_user_id),
                        tenant_id=workflow.tenant_id,
                        client_id=workflow.client_id,
                    )
                    if plan_reference is None:
                        raise ValueError("Member evidence is missing its evaluation plan")
                    plan = load_evaluation_plan_bytes(
                        _verified_artifact_bytes(
                            artifact_id=plan_reference.artifact_id,
                            expected_hash=plan_reference.content_hash,
                            actor_user_id=uuid.UUID(actor_user_id),
                            tenant_id=workflow.tenant_id,
                            client_id=workflow.client_id,
                        )
                    )
                    member_results.append((plan, observations))
                    member_artifact_ids.append(reference.artifact_id)
                if producer is None:
                    raise ValueError("Privilege family has no evidence producer")
                family_plan, family_observations = merge_family_observation_results(member_results)
                repository = WorkflowEvidenceShadowArtifactRepository(
                    actor_user_id=uuid.UUID(actor_user_id),
                    producer_run_id=producer.id,
                )
                plan_artifact = repository.persist_plan(family_plan)
                observations_artifact = repository.persist_compacted(
                    family_plan,
                    plan_artifact.artifact_id,
                    tuple(member_artifact_ids),
                    family_observations,
                )
                producer.progress = {
                    **(producer.progress or {}),
                    "documents": [
                        {
                            "matter_document_id": str(family_plan.matter_document_id),
                            "family_key": str(group["family_key"]),
                            "family_member_count": len(rows),
                            "observations_artifact_id": str(observations_artifact.artifact_id),
                            "observation_count": len(family_observations.observations),
                            "evidence_complete": True,
                            "coverage_status": "COMPLETE",
                        }
                    ],
                }
                for row in rows:
                    row.status = "COMPLETED"
                    row.last_workflow_run_id = producer.id
                    row.failure_code = None
                    row.failure_message = None
                    row.failure_retriable = False
                    row.completed_at = row.completed_at or utcnow()
                completed_families += 1
                db.commit()
            except Exception as exc:
                logger.exception(
                    "Privilege family evidence merge failed assessment_id=%s family_key=%s",
                    assessment_id,
                    group.get("family_key"),
                )
                failed_families += 1
                message = " ".join(str(exc).split()) or type(exc).__name__
                for row in rows:
                    row.status = "FAILED"
                    row.failure_code = "FAMILY_EVIDENCE_MERGE_FAILED"
                    row.failure_message = message[:4_000]
                    row.failure_retriable = True
                    row.completed_at = utcnow()
                db.commit()
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        if workflow is not None:
            workflow.progress_completed = completed_families + failed_families
            workflow.progress = {
                **(workflow.progress or {}),
                "family_count": len(family_groups),
                "completed_family_count": completed_families,
                "failed_family_count": failed_families,
            }
        raw_program_run_id = assessment.configuration_snapshot.get("privilege_review_program_run_id")
        if raw_program_run_id:
            program_run = db.get(ReviewProgramRun, uuid.UUID(str(raw_program_run_id)))
            if program_run is not None:
                evidence = dict((program_run.progress_summary or {}).get("privilege_evidence") or {})
                program_run.progress_summary = {
                    **(program_run.progress_summary or {}),
                    "privilege_evidence": {
                        **evidence,
                        "family_count": len(family_groups),
                        "completed_family_count": completed_families,
                        "failed_family_count": failed_families,
                    },
                }
        db.commit()
        return {
            "family_count": len(family_groups),
            "completed_family_count": completed_families,
            "failed_family_count": failed_families,
        }


@DBOS.workflow(name="privilege_review_evidence_document_v1")
def document_workflow(assessment_id: str, document_id: str, actor_user_id: str) -> dict[str, Any]:
    try:
        prepared = prepare_extraction_fanout(assessment_id, document_id, actor_user_id)
        preparation_failure = prepared.get("preparation_failure")
        if isinstance(preparation_failure, dict):
            result = {
                "matter_document_id": document_id,
                "status": "FAILED",
                "failure_code": str(
                    preparation_failure.get("failure_code") or "EXTRACTION_INPUT_UNAVAILABLE"
                )[:100],
                "failure_message": str(
                    preparation_failure.get("failure_message") or "Evidence input is unavailable"
                )[:4_000],
                "failure_retriable": bool(preparation_failure.get("failure_retriable", False)),
            }
        else:
            workflow_run_id = str(prepared["workflow_run_id"])
            unit_handles = []
            for index, unit_id in enumerate(prepared["unit_ids"]):
                with SetWorkflowID(
                    f"privilege-evidence-unit:{assessment_id}:{document_id}:{prepared['plan_hash']}:{index}"
                ):
                    unit_handles.append(
                        EVIDENCE_EXTRACTION_UNIT_QUEUE.enqueue(
                            evidence_extraction_unit_workflow,
                            assessment_id,
                            document_id,
                            actor_user_id,
                            workflow_run_id,
                            unit_id,
                        )
                    )
            extraction = finalize_extraction_fanout(
                assessment_id,
                document_id,
                actor_user_id,
                workflow_run_id,
                [handle.get_result() for handle in unit_handles],
            )
            report = (extraction.get("documents") or [{}])[0]
            if not report.get("evidence_complete") or not report.get("observations_artifact_id"):
                result = {
                    "matter_document_id": document_id,
                    "status": "FAILED",
                    "workflow_run_id": workflow_run_id,
                    "failure_code": "EXTRACTION_INCOMPLETE",
                    "failure_message": "Evidence extraction did not produce a complete observations artifact",
                    "failure_retriable": True,
                }
            else:
                result = {
                    "matter_document_id": document_id,
                    "status": "COMPLETED",
                    "workflow_run_id": workflow_run_id,
                    "observations_artifact_id": report["observations_artifact_id"],
                    "observation_count": report.get("observation_count", 0),
                    "material_adjustment_count": report.get("material_adjustment_count", 0),
                }
    except Exception as exc:
        logger.exception(
            "Privilege evidence document failed assessment_id=%s document_id=%s",
            assessment_id,
            document_id,
        )
        result = _failure(document_id, exc)
    return record_document(assessment_id, document_id, result)


@DBOS.step(name="checkpoint_privilege_review_evidence_chunk", retries_allowed=True, max_attempts=5)
def checkpoint_chunk(assessment_id: str) -> dict[str, int]:
    with SessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(assessment_id))
        if assessment is None or assessment.review_batch_run_id is None:
            raise ValueError("Privilege evidence assessment is unavailable")
        counts = {
            status: int(count)
            for status, count in db.execute(
                select(ReviewBatchRunDocument.status, func.count())
                .where(ReviewBatchRunDocument.review_batch_run_id == assessment.review_batch_run_id)
                .group_by(ReviewBatchRunDocument.status)
            ).all()
        }
        completed = int(counts.get("COMPLETED", 0))
        failed = int(counts.get("FAILED", 0))
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        review_run = db.get(ReviewBatchRun, assessment.review_batch_run_id)
        if workflow is None or review_run is None:
            raise ValueError("Privilege evidence execution records are unavailable")
        family_terminal = int((workflow.progress or {}).get("completed_family_count") or 0) + int(
            (workflow.progress or {}).get("failed_family_count") or 0
        )
        workflow.progress_completed = family_terminal
        workflow.progress = {
            **(workflow.progress or {}),
            "completed": completed,
            "failed": failed,
        }
        review_run.processed_document_count = completed + failed
        assessment.summarized_count = completed
        assessment.failed_count = failed
        program_run_id = assessment.configuration_snapshot.get("privilege_review_program_run_id")
        if program_run_id:
            program_run = db.scalar(
                select(ReviewProgramRun)
                .where(ReviewProgramRun.id == uuid.UUID(str(program_run_id)))
                .with_for_update()
            )
            if program_run is not None and program_run.current_stage == "PRIVILEGE_EVIDENCE":
                current = dict((program_run.progress_summary or {}).get("privilege_evidence") or {})
                program_run.progress_summary = {
                    **(program_run.progress_summary or {}),
                    "privilege_evidence": {
                        **current,
                        "state": "RUNNING",
                        "completed_document_count": completed,
                        "failed_document_count": failed,
                    },
                }
                child = db.scalar(
                    select(ReviewProgramChild).where(
                        ReviewProgramChild.run_id == program_run.id,
                        ReviewProgramChild.child_type == "PRIVILEGE_EVIDENCE_ASSESSMENT",
                        ReviewProgramChild.child_id == assessment.id,
                    )
                )
                if child is not None:
                    child.status = "RUNNING"
        db.commit()
        return {"completed": completed, "failed": failed}


@DBOS.workflow(name="privilege_review_evidence_chunk_v1")
def chunk_workflow(
    assessment_id: str,
    actor_user_id: str,
    document_ids: list[str],
    chunk_ordinal: int,
) -> dict[str, Any]:
    handles = []
    for index, document_id in enumerate(document_ids):
        with SetWorkflowID(f"privilege-evidence-document:{assessment_id}:{chunk_ordinal}:{index}"):
            handles.append(
                PRIVILEGE_EVIDENCE_DOCUMENT_QUEUE.enqueue(
                    document_workflow,
                    assessment_id,
                    document_id,
                    actor_user_id,
                )
            )
    for handle in handles:
        handle.get_result()
    return checkpoint_chunk_without_stopping(assessment_id, chunk_ordinal)


def checkpoint_chunk_without_stopping(assessment_id: str, chunk_ordinal: int) -> dict[str, Any]:
    """Publish progress when possible without making it part of evidence correctness."""

    try:
        return checkpoint_chunk(assessment_id)
    except Exception as exc:  # noqa: BLE001 - progress checkpoints must not fail evidence extraction.
        logger.warning(
            "Privilege evidence checkpoint failed without stopping extraction assessment_id=%s "
            "chunk_ordinal=%s error=%s",
            assessment_id,
            chunk_ordinal,
            " ".join(str(exc).split()) or type(exc).__name__,
        )
        return {"checkpoint_failed": True}


@DBOS.step(name="complete_privilege_review_evidence", retries_allowed=True, max_attempts=5)
def complete(assessment_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(assessment_id))
        if assessment is None or assessment.review_batch_run_id is None:
            raise ValueError("Privilege evidence assessment is unavailable")
        counts = {
            status: int(count)
            for status, count in db.execute(
                select(ReviewBatchRunDocument.status, func.count())
                .where(ReviewBatchRunDocument.review_batch_run_id == assessment.review_batch_run_id)
                .group_by(ReviewBatchRunDocument.status)
            ).all()
        }
        completed = int(counts.get("COMPLETED", 0))
        failed = int(counts.get("FAILED", 0))
        terminal_count = completed + failed
        if terminal_count != assessment.selected_count:
            raise ValueError("Privilege evidence execution has non-terminal documents")
        status = "COMPLETED" if failed == 0 else "COMPLETED_WITH_ERRORS"
        completed_at = datetime.now(timezone.utc)
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        review_run = db.get(ReviewBatchRun, assessment.review_batch_run_id)
        if workflow is None or review_run is None:
            raise ValueError("Privilege evidence execution records are unavailable")
        assessment.status = status
        assessment.summarized_count = completed
        assessment.failed_count = failed
        assessment.completed_at = completed_at
        workflow.status = status
        workflow.current_stage = "COMPLETED"
        family_terminal = int((workflow.progress or {}).get("completed_family_count") or 0) + int(
            (workflow.progress or {}).get("failed_family_count") or 0
        )
        workflow.progress_completed = family_terminal
        workflow.progress = {
            **(workflow.progress or {}),
            "completed": completed,
            "failed": failed,
        }
        workflow.completed_at = completed_at
        review_run.status = status
        review_run.processed_document_count = terminal_count
        review_run.result_summary = {
            "completed": completed,
            "failed": failed,
            "family_count": int((workflow.progress or {}).get("family_count") or 0),
            "completed_family_count": int(
                (workflow.progress or {}).get("completed_family_count") or 0
            ),
            "failed_family_count": int((workflow.progress or {}).get("failed_family_count") or 0),
        }
        review_run.completed_at = completed_at
        project_privilege_evidence_terminal(db, assessment=assessment)
        db.commit()
        return {"status": status, "completed": completed, "failed": failed}


@DBOS.step(name="fail_privilege_review_evidence", retries_allowed=True, max_attempts=3)
def fail(assessment_id: str, message: str) -> None:
    with SessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(assessment_id))
        if assessment is None:
            return
        completed_at = utcnow()
        assessment.status = "FAILED"
        assessment.error_message = message[:4_000]
        assessment.completed_at = completed_at
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        if workflow is not None:
            workflow.status = "FAILED"
            workflow.current_stage = "COMPLETED"
            workflow.failure_code = "PRIVILEGE_EVIDENCE_FAILED"
            workflow.failure_summary = message[:4_000]
            workflow.failure_retriable = True
            workflow.completed_at = completed_at
        if assessment.review_batch_run_id is not None:
            review_run = db.get(ReviewBatchRun, assessment.review_batch_run_id)
            if review_run is not None:
                review_run.status = "FAILED"
                review_run.error_message = message[:4_000]
                review_run.completed_at = completed_at
        project_privilege_evidence_terminal(db, assessment=assessment)
        db.commit()


@DBOS.workflow(name="privilege_review_evidence_v5")
def privilege_review_evidence_workflow(
    assessment_id: str,
    actor_user_id: str,
    review_program_run_id: str,
) -> None:
    del review_program_run_id  # frozen in the assessment and used in the workflow ID
    try:
        activate_evidence_first_assessment(assessment_id, actor_user_id)
        activate_records(assessment_id)
        family_member_document_ids = load_family_member_ids(assessment_id, actor_user_id)
        plan = plan_provider_batches(assessment_id, family_member_document_ids, actor_user_id)
        fallback_document_ids = list(plan["realtime_document_ids"])
        submitted_batch_ids = []
        # Submit every provider batch before any workflow starts waiting on
        # Google.  The provider queue limits local polling/finalization work;
        # it must not serialize admission into the remote batch service.
        for batch_id in plan["provider_batch_ids"]:
            try:
                status = submit_provider_batch(batch_id, actor_user_id)
                if status in TERMINAL_PROVIDER_STATES and status not in SUCCESS_PROVIDER_STATES:
                    fallback_document_ids.extend(
                        fallback_provider_batch(
                            batch_id,
                            f"Privilege evidence provider batch ended in {status}",
                        )
                    )
                    continue
                submitted_batch_ids.append(batch_id)
            except Exception as exc:
                logger.exception("Privilege evidence provider batch submission failed batch_id=%s", batch_id)
                fallback_document_ids.extend(
                    fallback_provider_batch(
                        batch_id,
                        " ".join(str(exc).split()) or type(exc).__name__,
                    )
                )
        provider_handles = []
        poll_seconds = get_settings().definition_assessment_provider_batch_poll_seconds
        for batch_id in submitted_batch_ids:
            with SetWorkflowID(f"privilege-evidence-v4-provider-batch:{batch_id}"):
                provider_handles.append(
                    PRIVILEGE_EVIDENCE_PROVIDER_QUEUE.enqueue(
                        provider_batch_workflow,
                        batch_id,
                        actor_user_id,
                        poll_seconds,
                    )
                )
        for handle in provider_handles:
            result = handle.get_result()
            fallback_document_ids.extend(result.get("fallback_document_ids") or [])
        fallback_document_ids = list(dict.fromkeys(fallback_document_ids))
        checkpoint_chunk_without_stopping(assessment_id, 0)
        chunk_handles = []
        for offset in range(0, len(fallback_document_ids), DOCUMENT_CHUNK_SIZE):
            chunk_ordinal = (offset // DOCUMENT_CHUNK_SIZE) + 1
            with SetWorkflowID(f"privilege-evidence-v4-fallback-chunk:{assessment_id}:{chunk_ordinal}"):
                chunk_handles.append(
                    PRIVILEGE_EVIDENCE_CHUNK_QUEUE.enqueue(
                        chunk_workflow,
                        assessment_id,
                        actor_user_id,
                        fallback_document_ids[offset : offset + DOCUMENT_CHUNK_SIZE],
                        chunk_ordinal,
                    )
                )
        for handle in chunk_handles:
            handle.get_result()
        merge_family_evidence(assessment_id, actor_user_id)
        checkpoint_chunk_without_stopping(assessment_id, len(chunk_handles) + 1)
        complete(assessment_id)
    except Exception as exc:
        logger.exception("Privilege Review evidence failed assessment_id=%s", assessment_id)
        fail(assessment_id, " ".join(str(exc).split()) or type(exc).__name__)
        raise
