"""Periodic, idempotent projection of terminal DBOS state into Core jobs."""

from __future__ import annotations

import logging
from datetime import datetime

from dbos import DBOS
from sqlalchemy import select

from app.assessment_workflow_reconciliation import reconcile_terminal_assessment_workflows
from app.config import get_settings
from app.database import SessionLocal
from app.job_executions import (
    EXECUTION_TERMINAL_STATUSES,
    get_job_execution_adapter,
    registered_job_types,
    transition_job_execution,
)
from app.models import MatterDefinitionAssessmentRun, WorkflowRun
from app.workflows.dispatcher import get_dbos_client

logger = logging.getLogger(__name__)

_DURABLE_TERMINAL_STATUSES = {
    "SUCCESS",
    "ERROR",
    "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
    "CANCELLED",
}


def reconcile_terminal_job_executions(*, limit: int = 500) -> int:
    """Repair common jobs whose durable workflow terminated outside its body."""

    with SessionLocal() as db:
        active = list(
            db.execute(
                select(
                    WorkflowRun.id,
                    WorkflowRun.job_type,
                    WorkflowRun.job_id,
                    WorkflowRun.dbos_workflow_id,
                )
                .where(
                    WorkflowRun.job_type.in_(registered_job_types()),
                    WorkflowRun.job_id.is_not(None),
                    WorkflowRun.status.not_in(EXECUTION_TERMINAL_STATUSES),
                )
                .order_by(WorkflowRun.created_at)
                .limit(limit)
            )
        )
    if not active:
        return 0

    durable = get_dbos_client().list_workflows(
        workflow_ids=[row.dbos_workflow_id for row in active],
        status=list(_DURABLE_TERMINAL_STATUSES),
        load_input=False,
        load_output=True,
    )
    durable_by_id = {row.workflow_id: row for row in durable}
    reconciled = 0
    for row in active:
        terminal = durable_by_id.get(row.dbos_workflow_id)
        if terminal is None or row.job_type is None or row.job_id is None:
            continue
        if terminal.status == "CANCELLED":
            status = "CANCELED"
            failure_code = "DURABLE_WORKFLOW_CANCELED"
            message = "Durable workflow was canceled before Core finalized the job"
            retryable = False
        else:
            status = "FAILED"
            failure_code = "DURABLE_WORKFLOW_TERMINAL_MISMATCH"
            if terminal.status == "SUCCESS":
                message = "Durable workflow completed without persisting a terminal Core job state"
            else:
                message = str(terminal.error or f"Durable workflow ended with status {terminal.status}")
            retryable = get_job_execution_adapter(row.job_type).supports_retry
        try:
            with SessionLocal() as db:
                current = db.get(WorkflowRun, row.id)
                if current is None or current.status in EXECUTION_TERMINAL_STATUSES:
                    continue
                transition_job_execution(
                    db,
                    job_type=row.job_type,
                    job_id=row.job_id,
                    status=status,
                    current_stage=status,
                    failure_code=failure_code,
                    failure_summary=message,
                    failure_retriable=retryable,
                )
                db.commit()
            reconciled += 1
        except Exception:
            logger.warning(
                "Could not reconcile terminal workflow core_workflow_run_id=%s dbos_workflow_id=%s",
                row.id,
                row.dbos_workflow_id,
                exc_info=True,
            )
    return reconciled


@DBOS.step(name="reconcile_terminal_job_executions")
def reconcile_step() -> int:
    reconciled = reconcile_terminal_job_executions()
    with SessionLocal() as db:
        assessments = list(
            db.scalars(
                select(MatterDefinitionAssessmentRun)
                .where(
                    MatterDefinitionAssessmentRun.status.not_in(
                        {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}
                    )
                )
                .order_by(MatterDefinitionAssessmentRun.created_at)
                .limit(500)
            )
        )
        reconciled += len(
            reconcile_terminal_assessment_workflows(
                db,
                assessments,
                settings=get_settings(),
            )
        )
        db.commit()
    return reconciled


@DBOS.scheduled("0 * * * * *")
@DBOS.workflow(name="periodic_job_execution_reconciliation_v1")
def scheduled_job_execution_reconciliation(_scheduled_at: datetime, _started_at: datetime) -> None:
    reconcile_step()
