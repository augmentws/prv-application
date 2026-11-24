"""Durable document adjudication for Privilege Review programs."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from dbos import DBOS, Queue, SetWorkflowID
from sqlalchemy import delete, func, select, update

from app.assessment_provider_batches import SUCCESS_PROVIDER_STATES, TERMINAL_PROVIDER_STATES
from app.config import get_settings
from app.database import SessionLocal
from app.document_metadata import value_columns
from app.models import (
    MetadataDefinition,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewBatchRunValue,
    ReviewProgramChild,
    ReviewProgramRun,
    WorkflowRun,
    utcnow,
)
from app.privilege_adjudication_batches import (
    fail_privilege_adjudication_batch_to_fallback,
    finalize_privilege_adjudication_batch,
    plan_privilege_adjudication_batches,
    poll_privilege_adjudication_batch,
    submit_privilege_adjudication_batch,
)
from app.privilege_evidence_context import load_privilege_evidence_packets
from app.review_programs import project_privilege_adjudication_terminal
from app.search.service import sync_review_batch_search
from app.workflows.evidence_assessments import (
    EVIDENCE_ADJUDICATION_QUEUE,
    evidence_adjudication_workflow,
)

logger = logging.getLogger(__name__)

PRIVILEGE_ADJUDICATION_QUEUE = Queue("privilege-review-adjudication", global_concurrency=2)
PRIVILEGE_ADJUDICATION_CHUNK_QUEUE = Queue(
    "privilege-review-adjudication-chunks",
    global_concurrency=4,
)
PRIVILEGE_ADJUDICATION_PROVIDER_QUEUE = Queue(
    "privilege-review-adjudication-provider-batches",
    global_concurrency=4,
)
PRIVILEGE_FAMILY_ADJUDICATION_EXECUTION_VERSION = "v3"
DOCUMENT_CHUNK_SIZE = 50


@DBOS.step(name="plan_privilege_adjudication_provider_batches", retries_allowed=True, max_attempts=3)
def plan_provider_batches(
    review_batch_run_id: str,
    documents: list[dict[str, Any]],
    actor_user_id: str,
) -> dict[str, Any]:
    with SessionLocal() as db:
        return plan_privilege_adjudication_batches(
            db,
            review_batch_run_id=uuid.UUID(review_batch_run_id),
            documents=documents,
            actor_user_id=uuid.UUID(actor_user_id),
            settings=get_settings(),
        )


@DBOS.step(name="submit_privilege_adjudication_provider_batch", retries_allowed=True, max_attempts=3)
def submit_provider_batch(batch_id: str, actor_user_id: str) -> str:
    with SessionLocal() as db:
        return submit_privilege_adjudication_batch(
            db,
            batch_id=uuid.UUID(batch_id),
            actor_user_id=uuid.UUID(actor_user_id),
        )


@DBOS.step(name="poll_privilege_adjudication_provider_batch", retries_allowed=True, max_attempts=10)
def poll_provider_batch(batch_id: str) -> str:
    with SessionLocal() as db:
        return poll_privilege_adjudication_batch(db, uuid.UUID(batch_id))


@DBOS.step(name="finalize_privilege_adjudication_provider_batch", retries_allowed=True, max_attempts=3)
def finalize_provider_batch(batch_id: str, actor_user_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        return finalize_privilege_adjudication_batch(
            db,
            batch_id=uuid.UUID(batch_id),
            actor_user_id=uuid.UUID(actor_user_id),
        )


@DBOS.step(name="fallback_privilege_adjudication_provider_batch", retries_allowed=True, max_attempts=3)
def fallback_provider_batch(batch_id: str, message: str) -> list[str]:
    with SessionLocal() as db:
        return fail_privilege_adjudication_batch_to_fallback(
            db,
            batch_id=uuid.UUID(batch_id),
            message=message,
        )


def _publish_provider_batch_result(
    review_batch_run_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    for completed in result.get("completed_families") or []:
        document = completed["document"]
        for document_id in document.get("matter_document_ids") or [document["matter_document_id"]]:
            record_document(review_batch_run_id, document_id, completed["result"])
    checkpoint_without_stopping(review_batch_run_id, 0)
    return {**result, "results_recorded": True}


@DBOS.workflow(name="privilege_review_adjudication_provider_batch_v1")
def provider_batch_workflow(
    batch_id: str,
    actor_user_id: str,
    poll_seconds: float,
    review_batch_run_id: str | None = None,
) -> dict[str, Any]:
    try:
        status = submit_provider_batch(batch_id, actor_user_id)
        while status not in TERMINAL_PROVIDER_STATES:
            DBOS.sleep(poll_seconds)
            status = poll_provider_batch(batch_id)
        if status not in SUCCESS_PROVIDER_STATES:
            return {
                "completed_families": [],
                "fallback_anchor_document_ids": fallback_provider_batch(
                    batch_id,
                    f"Privilege adjudication provider batch ended in {status}",
                ),
            }
        result = finalize_provider_batch(batch_id, actor_user_id)
        if review_batch_run_id is not None:
            return _publish_provider_batch_result(review_batch_run_id, result)
        return result
    except Exception as exc:
        logger.exception("Privilege adjudication provider batch failed batch_id=%s", batch_id)
        message = " ".join(str(exc).split()) or type(exc).__name__
        return {
            "completed_families": [],
            "fallback_anchor_document_ids": fallback_provider_batch(batch_id, message),
        }


def _records(db, review_batch_run_id: uuid.UUID) -> tuple[ReviewBatchRun, WorkflowRun]:
    review_run = db.get(ReviewBatchRun, review_batch_run_id)
    if review_run is None or review_run.workflow_run_record_id is None:
        raise ValueError("Privilege adjudication run is unavailable")
    workflow = db.get(WorkflowRun, review_run.workflow_run_record_id)
    if workflow is None:
        raise ValueError("Privilege adjudication workflow is unavailable")
    return review_run, workflow


@DBOS.step(name="activate_privilege_review_adjudication", retries_allowed=True, max_attempts=3)
def activate(review_batch_run_id: str) -> None:
    with SessionLocal() as db:
        review_run, workflow = _records(db, uuid.UUID(review_batch_run_id))
        now = utcnow()
        review_run.status = "RUNNING"
        review_run.started_at = review_run.started_at or now
        workflow.status = "RUNNING"
        workflow.current_stage = "PRIVILEGE_ADJUDICATION"
        workflow.started_at = workflow.started_at or now
        db.commit()


@DBOS.step(
    name="sync_privilege_review_adjudication_batch_search",
    retries_allowed=True,
    max_attempts=3,
)
def sync_batch_search(review_batch_run_id: str) -> None:
    """Make the generated recommendation batch available in the review workspace."""

    with SessionLocal() as db:
        review_run, _workflow = _records(db, uuid.UUID(review_batch_run_id))
        batch_id = review_run.review_batch_id
    sync_review_batch_search(batch_id, get_settings())


def _observations_artifact_id(progress: dict[str, Any]) -> str | None:
    documents = progress.get("documents")
    if not isinstance(documents, list) or len(documents) != 1 or not isinstance(documents[0], dict):
        return None
    value = documents[0].get("observations_artifact_id")
    return str(value) if value else None


@DBOS.step(name="load_privilege_review_adjudication_documents", retries_allowed=True, max_attempts=3)
def load_documents(review_batch_run_id: str) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        review_run, _workflow = _records(db, uuid.UUID(review_batch_run_id))
        configuration = review_run.configuration_snapshot or {}
        source_run_id = uuid.UUID(str(configuration["source_evidence_review_batch_run_id"]))
        source_assessment_id = str(configuration["source_evidence_assessment_id"])
        queued = list(
            db.scalars(
                select(ReviewBatchRunDocument)
                .where(
                    ReviewBatchRunDocument.review_batch_run_id == review_run.id,
                    ReviewBatchRunDocument.status == "QUEUED",
                )
                .order_by(ReviewBatchRunDocument.matter_document_id)
            )
        )
        source_rows = {
            row.matter_document_id: row
            for row in db.scalars(
                select(ReviewBatchRunDocument).where(
                    ReviewBatchRunDocument.review_batch_run_id == source_run_id,
                    ReviewBatchRunDocument.matter_document_id.in_([row.matter_document_id for row in queued]),
                )
            )
        }
        workflow_ids = {
            row.last_workflow_run_id for row in source_rows.values() if row.last_workflow_run_id is not None
        }
        source_workflows = {
            workflow.id: workflow
            for workflow in db.scalars(select(WorkflowRun).where(WorkflowRun.id.in_(workflow_ids)))
        }
        prepared_documents: list[dict[str, Any]] = []
        for target in queued:
            source = source_rows.get(target.matter_document_id)
            source_workflow = (
                source_workflows.get(source.last_workflow_run_id)
                if source is not None and source.last_workflow_run_id is not None
                else None
            )
            artifact_id = (
                _observations_artifact_id(source_workflow.progress or {}) if source_workflow is not None else None
            )
            if source is None or source.status != "COMPLETED" or artifact_id is None:
                target.status = "FAILED"
                target.failure_code = "EVIDENCE_ARTIFACT_UNAVAILABLE"
                target.failure_message = "Completed cited evidence is unavailable for privilege adjudication"
                target.failure_retriable = True
                target.completed_at = utcnow()
                continue
            prepared_documents.append(
                {
                    "matter_document_id": str(target.matter_document_id),
                    "assessment_id": source_assessment_id,
                    "observations_artifact_id": artifact_id,
                }
            )
        db.commit()
        return prepared_documents


def _failure(document_id: str, exc: BaseException) -> dict[str, Any]:
    message = " ".join(str(exc).split()) or type(exc).__name__
    return {
        "matter_document_id": document_id,
        "status": "FAILED",
        "failure_code": f"ADJUDICATION_{type(exc).__name__}"[:100],
        "failure_message": message[:4_000],
        "failure_retriable": True,
    }


def _recommendation(result: dict[str, Any]) -> str:
    determination = str(result.get("determination") or "UNCLEAR")
    decision_status = str(result.get("document_decision_status") or "")
    if (
        decision_status != "COMPLETED"
        or result.get("evidence_complete") is not True
        or result.get("adjudication_complete") is not True
        or determination == "UNCLEAR"
    ):
        return "needs_review"
    disposition_counts = result.get("disposition_counts")
    counts = disposition_counts if isinstance(disposition_counts, dict) else {}
    if result.get("unresolved_criterion_keys") or result.get("fallback_criterion_keys"):
        return "needs_review"
    if determination == "RESPONSIVE":
        return "privileged" if int(counts.get("MATCH") or 0) > 0 else "needs_review"
    if determination == "NON_RESPONSIVE":
        if int(counts.get("NEAR_MISS") or 0) > 0:
            # Screening is intentionally less demanding than the final legal
            # determination. A near miss routes to human review only when the
            # extractor found affirmative support for the same criterion. Legal
            # adjacency alone (draft status, a standard dispute clause, or the
            # mere presence of counsel) is not enough to hold the document.
            dispositions = result.get("criterion_dispositions")
            signal_counts = result.get("criterion_signal_counts")
            if not isinstance(dispositions, dict) or not isinstance(signal_counts, dict):
                # Results produced before screening-policy v2 remain conservative.
                return "needs_review"
            near_miss_criteria = {
                str(key)
                for key, value in dispositions.items()
                if str(value) == "NEAR_MISS"
            }
            support_count = sum(
                int((signal_counts.get(key) or {}).get("SUPPORT") or 0)
                for key in near_miss_criteria
                if isinstance(signal_counts.get(key), dict)
            )
            return "needs_review" if support_count > 0 else "not_privileged"
        total = sum(int(value or 0) for value in counts.values())
        if total == 0 or int(counts.get("NO_MATCH") or 0) != total:
            return "needs_review"
        return "not_privileged"
    return "needs_review"


def _family_conflict_document_ids(
    packets: dict[str, Any],
    recommendations: dict[str, str],
) -> set[str]:
    """Route negative members of a mixed positive/negative family to human review."""

    family_documents: dict[str, set[str]] = {}
    for packet in packets.get("packets", []):
        if not isinstance(packet, dict):
            continue
        family_key = str(packet.get("family_key") or "")
        if not family_key:
            continue
        family_documents.setdefault(family_key, set()).update(
            str(value) for value in packet.get("review_target_document_ids", [])
        )
    routed: set[str] = set()
    for document_ids in family_documents.values():
        values = {recommendations.get(document_id) for document_id in document_ids}
        if "privileged" not in values or "not_privileged" not in values:
            continue
        routed.update(
            document_id for document_id in document_ids if recommendations.get(document_id) == "not_privileged"
        )
    return routed


def _route_family_conflicts(db, *, review_run: ReviewBatchRun, workflow: WorkflowRun) -> int:
    reference = review_run.configuration_snapshot.get("privilege_evidence_packets")
    raw_program_id = review_run.configuration_snapshot.get("review_program_id")
    raw_definition_id = review_run.configuration_snapshot.get("target_metadata_definition_id")
    if not isinstance(reference, dict) or raw_program_id is None or raw_definition_id is None:
        return 0
    packets = load_privilege_evidence_packets(
        packet_reference=reference,
        matter_id=workflow.matter_id,
        program_id=uuid.UUID(str(raw_program_id)),
        actor_user_id=review_run.initiated_by_user_id,
        tenant_id=workflow.tenant_id,
        client_id=workflow.client_id,
    )
    definition_id = uuid.UUID(str(raw_definition_id))
    recommendations = {
        str(document_id): str(value)
        for document_id, value in db.execute(
            select(ReviewBatchRunValue.matter_document_id, ReviewBatchRunValue.value_text).where(
                ReviewBatchRunValue.review_batch_run_id == review_run.id,
                ReviewBatchRunValue.metadata_definition_id == definition_id,
                ReviewBatchRunValue.value_ordinal == 0,
            )
        )
        if value is not None
    }
    routed = _family_conflict_document_ids(packets, recommendations)
    if not routed:
        return 0
    routed_ids = [uuid.UUID(value) for value in routed]
    db.execute(
        update(ReviewBatchRunValue)
        .where(
            ReviewBatchRunValue.review_batch_run_id == review_run.id,
            ReviewBatchRunValue.metadata_definition_id == definition_id,
            ReviewBatchRunValue.value_ordinal == 0,
            ReviewBatchRunValue.matter_document_id.in_(routed_ids),
            ReviewBatchRunValue.value_text == "not_privileged",
        )
        .values(
            value_text="needs_review",
            question_key="privilege_review_family_conflict",
        )
    )
    return len(routed_ids)


@DBOS.step(name="record_privilege_review_adjudication_document", retries_allowed=True, max_attempts=5)
def record_document(
    review_batch_run_id: str,
    document_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    with SessionLocal() as db:
        review_run, _workflow = _records(db, uuid.UUID(review_batch_run_id))
        row = db.scalar(
            select(ReviewBatchRunDocument)
            .where(
                ReviewBatchRunDocument.review_batch_run_id == review_run.id,
                ReviewBatchRunDocument.matter_document_id == uuid.UUID(document_id),
            )
            .with_for_update()
        )
        if row is None:
            raise ValueError("Privilege adjudication document is unavailable")
        if result.get("status") == "FAILED":
            row.status = "FAILED"
            row.failure_code = str(result.get("failure_code") or "ADJUDICATION_FAILED")[:100]
            row.failure_message = str(result.get("failure_message") or "Privilege adjudication failed")[:4_000]
            row.failure_retriable = bool(result.get("failure_retriable", True))
        else:
            definition_id = uuid.UUID(str(review_run.configuration_snapshot["target_metadata_definition_id"]))
            definition = db.get(MetadataDefinition, definition_id)
            if definition is None:
                raise ValueError("Operational privilege field is unavailable")
            recommendation = _recommendation(result)
            db.execute(
                delete(ReviewBatchRunValue).where(
                    ReviewBatchRunValue.review_batch_run_id == review_run.id,
                    ReviewBatchRunValue.matter_document_id == row.matter_document_id,
                    ReviewBatchRunValue.metadata_definition_id == definition.id,
                )
            )
            confidence_raw = result.get("decision_confidence")
            confidence = (
                float(confidence_raw)
                if isinstance(confidence_raw, (int, float)) and not isinstance(confidence_raw, bool)
                else None
            )
            db.add(
                ReviewBatchRunValue(
                    review_batch_run_id=review_run.id,
                    matter_document_id=row.matter_document_id,
                    metadata_definition_id=definition.id,
                    value_ordinal=0,
                    confidence=confidence,
                    confidence_kind="DERIVED_PROBABILITY" if confidence is not None else "NONE",
                    review_decision_result_id=None,
                    question_key="privilege_review_adjudication",
                    **value_columns(recommendation, definition),
                )
            )
            row.status = "COMPLETED"
            row.failure_code = None
            row.failure_message = None
            row.failure_retriable = False
        workflow_run_id = result.get("workflow_run_id")
        row.last_workflow_run_id = uuid.UUID(str(workflow_run_id)) if workflow_run_id else None
        row.completed_at = utcnow()
        db.commit()
        return {**result, "recommendation": None if result.get("status") == "FAILED" else _recommendation(result)}


@DBOS.workflow(name="privilege_review_adjudication_chunk_v1")
def adjudication_chunk(
    review_batch_run_id: str,
    actor_user_id: str,
    documents: list[dict[str, Any]],
    chunk_ordinal: int,
) -> dict[str, Any]:
    handles: list[tuple[dict[str, Any], Any]] = []
    for document in documents:
        with SetWorkflowID(
            f"privilege-document-evidence-adjudication-{PRIVILEGE_FAMILY_ADJUDICATION_EXECUTION_VERSION}:"
            f"{document['assessment_id']}:{document['observations_artifact_id']}:"
            f"{document['matter_document_id']}"
        ):
            handles.append(
                (
                    document,
                    EVIDENCE_ADJUDICATION_QUEUE.enqueue(
                        evidence_adjudication_workflow,
                        document["assessment_id"],
                        document["observations_artifact_id"],
                        actor_user_id,
                        document["matter_document_id"],
                    ),
                )
            )
    for document, handle in handles:
        try:
            result = handle.get_result()
        except Exception as exc:
            logger.exception(
                "Privilege adjudication failed review_batch_run_id=%s document_id=%s",
                review_batch_run_id,
                document["matter_document_id"],
            )
            result = _failure(document["matter_document_id"], exc)
        record_document(review_batch_run_id, document["matter_document_id"], result)
    return checkpoint_without_stopping(review_batch_run_id, chunk_ordinal)


@DBOS.step(name="checkpoint_privilege_review_adjudication", retries_allowed=True, max_attempts=5)
def checkpoint(review_batch_run_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        review_run, workflow = _records(db, uuid.UUID(review_batch_run_id))
        counts = {
            status: int(count)
            for status, count in db.execute(
                select(ReviewBatchRunDocument.status, func.count())
                .where(ReviewBatchRunDocument.review_batch_run_id == review_run.id)
                .group_by(ReviewBatchRunDocument.status)
            ).all()
        }
        distribution = {
            str(value): int(count)
            for value, count in db.execute(
                select(ReviewBatchRunValue.value_text, func.count())
                .where(ReviewBatchRunValue.review_batch_run_id == review_run.id)
                .group_by(ReviewBatchRunValue.value_text)
            ).all()
            if value is not None
        }
        completed = counts.get("COMPLETED", 0)
        failed = counts.get("FAILED", 0)
        workflow.progress_completed = completed + failed
        workflow.progress = {"completed": completed, "failed": failed, "distribution": distribution}
        review_run.processed_document_count = completed + failed
        child = db.scalar(
            select(ReviewProgramChild).where(
                ReviewProgramChild.child_type == "PRIVILEGE_ADJUDICATION_RUN",
                ReviewProgramChild.child_id == review_run.id,
            )
        )
        program_run = db.get(ReviewProgramRun, child.run_id) if child is not None else None
        if program_run is not None and program_run.current_stage == "PRIVILEGE_ADJUDICATION":
            current = dict((program_run.progress_summary or {}).get("privilege_adjudication") or {})
            program_run.progress_summary = {
                **(program_run.progress_summary or {}),
                "privilege_adjudication": {
                    **current,
                    "state": "RUNNING",
                    "completed_document_count": completed,
                    "failed_document_count": failed,
                    "recommendation_counts": distribution,
                },
            }
            child.status = "RUNNING"
        db.commit()
        return {"completed": completed, "failed": failed, "distribution": distribution}


def checkpoint_without_stopping(review_batch_run_id: str, chunk_ordinal: int) -> dict[str, Any]:
    try:
        return checkpoint(review_batch_run_id)
    except Exception as exc:  # noqa: BLE001 - progress is not correctness.
        logger.warning(
            "Privilege adjudication checkpoint failed review_batch_run_id=%s chunk_ordinal=%s error=%s",
            review_batch_run_id,
            chunk_ordinal,
            " ".join(str(exc).split()) or type(exc).__name__,
        )
        return {"checkpoint_failed": True}


@DBOS.step(name="complete_privilege_review_adjudication", retries_allowed=True, max_attempts=5)
def complete(review_batch_run_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        review_run, workflow = _records(db, uuid.UUID(review_batch_run_id))
        counts = {
            status: int(count)
            for status, count in db.execute(
                select(ReviewBatchRunDocument.status, func.count())
                .where(ReviewBatchRunDocument.review_batch_run_id == review_run.id)
                .group_by(ReviewBatchRunDocument.status)
            ).all()
        }
        completed = counts.get("COMPLETED", 0)
        failed = counts.get("FAILED", 0)
        total = int(workflow.progress_total or 0)
        if completed + failed != total:
            raise ValueError("Privilege adjudication has non-terminal documents")
        family_conflict_routed_count = 0
        if (
            review_run.configuration_snapshot.get("family_recommendation_policy")
            == "SHARED_FAMILY_DECISION_V1"
        ):
            family_conflict_routed_count = _route_family_conflicts(
                db,
                review_run=review_run,
                workflow=workflow,
            )
        distribution = {
            str(value): int(count)
            for value, count in db.execute(
                select(ReviewBatchRunValue.value_text, func.count())
                .where(ReviewBatchRunValue.review_batch_run_id == review_run.id)
                .group_by(ReviewBatchRunValue.value_text)
            ).all()
            if value is not None
        }
        status = "COMPLETED" if failed == 0 else "COMPLETED_WITH_ERRORS"
        completed_at = datetime.now(timezone.utc)
        result = {
            "status": status,
            "document_count": total,
            "completed_document_count": completed,
            "failed_document_count": failed,
            "recommendation_counts": distribution,
            "family_conflict_routed_count": family_conflict_routed_count,
        }
        review_run.status = status
        review_run.processed_document_count = completed + failed
        review_run.result_summary = result
        review_run.completed_at = completed_at
        workflow.status = status
        workflow.current_stage = "COMPLETED"
        workflow.progress_completed = completed + failed
        workflow.progress = {
            "completed": completed,
            "failed": failed,
            "distribution": distribution,
            "family_conflict_routed_count": family_conflict_routed_count,
        }
        workflow.completed_at = completed_at
        project_privilege_adjudication_terminal(db, review_run=review_run, workflow=workflow)
        db.commit()
        return result


@DBOS.step(name="fail_privilege_review_adjudication", retries_allowed=True, max_attempts=3)
def fail(review_batch_run_id: str, message: str) -> None:
    with SessionLocal() as db:
        review_run, workflow = _records(db, uuid.UUID(review_batch_run_id))
        completed_at = utcnow()
        review_run.status = "FAILED"
        review_run.error_message = message[:4_000]
        review_run.completed_at = completed_at
        workflow.status = "FAILED"
        workflow.current_stage = "COMPLETED"
        workflow.failure_code = "PRIVILEGE_ADJUDICATION_FAILED"
        workflow.failure_summary = message[:4_000]
        workflow.failure_retriable = True
        workflow.completed_at = completed_at
        project_privilege_adjudication_terminal(db, review_run=review_run, workflow=workflow)
        db.commit()


@DBOS.workflow(name="privilege_review_adjudication_v5")
def privilege_review_adjudication_workflow(
    review_batch_run_id: str,
    actor_user_id: str,
) -> None:
    try:
        activate(review_batch_run_id)
        try:
            sync_batch_search(review_batch_run_id)
        except Exception:
            logger.exception(
                "Privilege recommendation batch search projection failed review_batch_run_id=%s",
                review_batch_run_id,
            )
        documents = load_documents(review_batch_run_id)
        by_anchor = {str(value["matter_document_id"]): value for value in documents}
        plan = plan_provider_batches(review_batch_run_id, documents, actor_user_id)
        fallback_anchor_ids = list(plan["realtime_anchor_document_ids"])
        submitted_batch_ids = []
        # Submit every provider batch before any workflow starts waiting on
        # Google.  The provider queue limits local polling/finalization work;
        # it must not serialize admission into the remote batch service.
        for batch_id in plan["provider_batch_ids"]:
            try:
                status = submit_provider_batch(batch_id, actor_user_id)
                if status in TERMINAL_PROVIDER_STATES and status not in SUCCESS_PROVIDER_STATES:
                    fallback_anchor_ids.extend(
                        fallback_provider_batch(
                            batch_id,
                            f"Privilege adjudication provider batch ended in {status}",
                        )
                    )
                    continue
                submitted_batch_ids.append(batch_id)
            except Exception as exc:
                logger.exception("Privilege adjudication provider batch submission failed batch_id=%s", batch_id)
                fallback_anchor_ids.extend(
                    fallback_provider_batch(
                        batch_id,
                        " ".join(str(exc).split()) or type(exc).__name__,
                    )
                )
        provider_handles = []
        poll_seconds = get_settings().definition_assessment_provider_batch_poll_seconds
        for batch_id in submitted_batch_ids:
            with SetWorkflowID(f"privilege-adjudication-v5-provider-batch:{batch_id}"):
                provider_handles.append(
                    PRIVILEGE_ADJUDICATION_PROVIDER_QUEUE.enqueue(
                        provider_batch_workflow,
                        batch_id,
                        actor_user_id,
                        poll_seconds,
                        review_batch_run_id,
                    )
                )
        for handle in provider_handles:
            result = handle.get_result()
            # Provider children publish successful outcomes as soon as their
            # own batch completes.  Preserve compatibility with child runs
            # created before that behavior was introduced.
            if not result.get("results_recorded"):
                _publish_provider_batch_result(review_batch_run_id, result)
            fallback_anchor_ids.extend(result.get("fallback_anchor_document_ids") or [])
        fallback_documents = [by_anchor[value] for value in dict.fromkeys(fallback_anchor_ids) if value in by_anchor]
        handles = []
        for offset in range(0, len(fallback_documents), DOCUMENT_CHUNK_SIZE):
            chunk_ordinal = (offset // DOCUMENT_CHUNK_SIZE) + 1
            with SetWorkflowID(f"privilege-adjudication-v5-fallback:{review_batch_run_id}:{chunk_ordinal}"):
                handles.append(
                    PRIVILEGE_ADJUDICATION_CHUNK_QUEUE.enqueue(
                        adjudication_chunk,
                        review_batch_run_id,
                        actor_user_id,
                        fallback_documents[offset : offset + DOCUMENT_CHUNK_SIZE],
                        chunk_ordinal,
                    )
                )
        for handle in handles:
            handle.get_result()
        complete(review_batch_run_id)
    except Exception as exc:
        logger.exception("Privilege Review adjudication failed review_batch_run_id=%s", review_batch_run_id)
        fail(review_batch_run_id, " ".join(str(exc).split()) or type(exc).__name__)
        raise
