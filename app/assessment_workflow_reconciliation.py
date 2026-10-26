import logging
import uuid
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.assessment_execution import fail_assessment
from app.config import Settings
from app.models import MatterDefinitionAssessmentRun, ReviewBatchRunDocument, WorkflowRun
from app.workflows.dispatcher import get_dbos_client

logger = logging.getLogger(__name__)

_DBOS_TERMINAL_STATUSES = {
    "SUCCESS",
    "ERROR",
    "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
    "CANCELLED",
}
_ASSESSMENT_TERMINAL_STATUSES = {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}
_DOCUMENT_TERMINAL_STATUSES = {"COMPLETED", "SKIPPED", "FAILED"}


def reconcile_terminal_assessment_workflows(
    db: Session,
    assessments: Iterable[MatterDefinitionAssessmentRun],
    *,
    settings: Settings,
) -> set[uuid.UUID]:
    """Project terminal DBOS state when an assessment failed outside its workflow body.

    Import and dispatch failures can terminate a durable workflow before its own
    exception handler runs. In that case the Core assessment otherwise remains in
    a running state indefinitely.
    """

    if not settings.dbos_enabled:
        return set()

    active: list[tuple[MatterDefinitionAssessmentRun, WorkflowRun]] = []
    for assessment in assessments:
        if assessment.status in _ASSESSMENT_TERMINAL_STATUSES:
            continue
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        if workflow is None or workflow.status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}:
            continue
        active.append((assessment, workflow))
    if not active:
        return set()

    try:
        client = get_dbos_client()
        durable_workflows = client.list_workflows(
            workflow_ids=[workflow.dbos_workflow_id for _, workflow in active],
            load_input=False,
            load_output=True,
        )
    except Exception:
        logger.warning("Could not reconcile durable Matter Definition assessment workflows", exc_info=True)
        return set()

    terminal = {workflow.workflow_id: workflow for workflow in durable_workflows}
    run_ids = [assessment.review_batch_run_id for assessment, _ in active if assessment.review_batch_run_id]
    document_counts: dict[uuid.UUID, dict[str, int]] = {}
    if run_ids:
        for run_id, document_status, count in db.execute(
            select(
                ReviewBatchRunDocument.review_batch_run_id,
                ReviewBatchRunDocument.status,
                func.count(),
            )
            .where(ReviewBatchRunDocument.review_batch_run_id.in_(run_ids))
            .group_by(ReviewBatchRunDocument.review_batch_run_id, ReviewBatchRunDocument.status)
        ):
            document_counts.setdefault(run_id, {})[document_status] = int(count)
    reconciled: set[uuid.UUID] = set()
    for assessment, workflow in active:
        durable = terminal.get(workflow.dbos_workflow_id)
        if durable is None:
            continue
        if durable.status == "PENDING" and assessment.review_batch_run_id is not None:
            counts = document_counts.get(assessment.review_batch_run_id, {})
            document_total = sum(counts.values())
            terminal_total = sum(count for status, count in counts.items() if status in _DOCUMENT_TERMINAL_STATUSES)
            resumed_workflows = list(
                assessment.configuration_snapshot.get("reconciled_durable_workflow_ids", [])
            )
            if (
                assessment.synthesis_result is None
                and document_total > 0
                and terminal_total == document_total
                and workflow.dbos_workflow_id not in resumed_workflows
            ):
                try:
                    client.resume_workflow(
                        workflow.dbos_workflow_id,
                        queue_name="definition-assessments",
                    )
                except Exception:
                    logger.warning(
                        "Could not resume durable Matter Definition assessment workflow %s",
                        workflow.dbos_workflow_id,
                        exc_info=True,
                    )
                else:
                    assessment.configuration_snapshot = {
                        **assessment.configuration_snapshot,
                        "reconciled_durable_workflow_ids": [
                            *resumed_workflows,
                            workflow.dbos_workflow_id,
                        ],
                    }
                    workflow.configuration_snapshot = assessment.configuration_snapshot
                    db.flush()
                    reconciled.add(assessment.id)
            continue
        if durable.status not in _DBOS_TERMINAL_STATUSES:
            continue
        durable_error = str(durable.error) if durable.error is not None else None
        if durable.status == "SUCCESS":
            message = (
                "Durable Matter Definition assessment workflow completed without persisting "
                "a terminal assessment state"
            )
        elif durable.status == "CANCELLED":
            message = "Durable Matter Definition assessment workflow was cancelled before finalization"
        else:
            message = durable_error or (
                f"Durable Matter Definition assessment workflow ended with status {durable.status} "
                "before finalization"
            )
        try:
            fail_assessment(db, assessment.id, message)
        except Exception:
            db.rollback()
            logger.warning(
                "Could not reconcile Matter Definition assessment %s from durable status %s",
                assessment.id,
                durable.status,
                exc_info=True,
            )
            continue
        reconciled.add(assessment.id)
    return reconciled
