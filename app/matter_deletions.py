from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import Base
from app.models import (
    AnalysisInputManifest,
    ClassificationDatasetSnapshot,
    ClassificationModel,
    ClassificationModelConfiguration,
    ClassificationScoringRun,
    ClassificationTrainingRun,
    Matter,
    MatterDeletionJob,
    MatterDocument,
    MatterDocumentImportJob,
    ReviewBatch,
    SearchProjectionOperation,
    WorkflowRun,
)
from app.workflows.dispatcher import get_dbos_client
from artifact_service.schemas import WorkflowArtifactScopeDelete

logger = logging.getLogger(__name__)

_DBOS_TERMINAL_STATUSES = {
    "SUCCESS",
    "ERROR",
    "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
    "CANCELLED",
}

ACTIVE_DELETION_STATUSES = (
    "QUEUED",
    "VALIDATING",
    "DELETING_SEARCH",
    "DELETING_ARTIFACTS",
    "DELETING_DATABASE_ROWS",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MatterDeletionValidationUnavailable(RuntimeError):
    """Raised when deletion safety cannot be verified against durable workflows."""


@dataclass(frozen=True)
class MatterDeletionBlockers:
    active_workflow_count: int
    active_search_operation_count: int
    active_import_count: int

    @property
    def blocked(self) -> bool:
        return any(
            (
                self.active_workflow_count,
                self.active_search_operation_count,
                self.active_import_count,
            )
        )

    def message(self) -> str:
        reasons: list[str] = []
        if self.active_workflow_count:
            reasons.append(f"{self.active_workflow_count} active workflow(s)")
        if self.active_search_operation_count:
            reasons.append(f"{self.active_search_operation_count} active search operation(s)")
        if self.active_import_count:
            reasons.append(f"{self.active_import_count} active document import(s)")
        return "Matter deletion is blocked by " + ", ".join(reasons)


def _active_search_operation_count(db: Session, matter_id: uuid.UUID) -> int:
    operations = list(
        db.scalars(
            select(SearchProjectionOperation).where(
                SearchProjectionOperation.matter_id == matter_id,
                SearchProjectionOperation.status.in_(("QUEUED", "RUNNING")),
            )
        )
    )
    settings = get_settings()
    if not operations or not settings.dbos_enabled:
        return len(operations)

    try:
        workflows = get_dbos_client().list_workflows(
            workflow_ids=[operation.workflow_id for operation in operations],
            load_input=False,
            load_output=True,
        )
    except Exception as exc:
        logger.exception(
            "Could not verify durable search workflows before matter deletion matter_id=%s",
            matter_id,
        )
        raise MatterDeletionValidationUnavailable(
            "Durable search workflow status is temporarily unavailable"
        ) from exc

    durable_by_id = {workflow.workflow_id: workflow for workflow in workflows}
    active_count = 0
    for operation in operations:
        durable = durable_by_id.get(operation.workflow_id)
        if durable is None or durable.status not in _DBOS_TERMINAL_STATUSES:
            active_count += 1
            continue

        operation.completed_at = (
            datetime.fromtimestamp(durable.completed_at / 1000, tz=timezone.utc)
            if durable.completed_at is not None
            else utcnow()
        )
        if durable.status == "SUCCESS":
            operation.status = "COMPLETED"
            operation.error_message = None
        else:
            operation.status = "FAILED"
            durable_error = str(durable.error) if durable.error is not None else None
            operation.error_message = (
                durable_error or f"Durable workflow ended with status {durable.status}"
            )[:4000]

    db.flush()
    return active_count


def matter_deletion_blockers(db: Session, matter_id: uuid.UUID) -> MatterDeletionBlockers:
    active_workflow_count = int(
        db.scalar(
            select(func.count())
            .select_from(WorkflowRun)
            .where(
                WorkflowRun.matter_id == matter_id,
                WorkflowRun.status.in_(("QUEUED", "RUNNING", "WAITING")),
            )
        )
        or 0
    )
    active_search_operation_count = _active_search_operation_count(db, matter_id)
    active_import_count = int(
        db.scalar(
            select(func.count())
            .select_from(MatterDocumentImportJob)
            .where(
                MatterDocumentImportJob.matter_id == matter_id,
                MatterDocumentImportJob.status.in_(("QUEUED", "SNAPSHOTTING", "RUNNING")),
            )
        )
        or 0
    )
    return MatterDeletionBlockers(
        active_workflow_count=active_workflow_count,
        active_search_operation_count=active_search_operation_count,
        active_import_count=active_import_count,
    )


def matter_artifact_scopes(db: Session, matter_id: uuid.UUID) -> list[WorkflowArtifactScopeDelete]:
    scopes = [WorkflowArtifactScopeDelete(core_scope_kind="MATTER", core_scope_id=matter_id)]
    mappings = (
        ("REVIEW_BATCH", ReviewBatch),
        ("ANALYSIS_INPUT_MANIFEST", AnalysisInputManifest),
        ("CLASSIFICATION_MODEL", ClassificationModel),
        ("CLASSIFICATION_DATASET_SNAPSHOT", ClassificationDatasetSnapshot),
        ("CLASSIFICATION_TRAINING_RUN", ClassificationTrainingRun),
        ("CLASSIFICATION_SCORING_RUN", ClassificationScoringRun),
    )
    for scope_kind, model in mappings:
        scopes.extend(
            WorkflowArtifactScopeDelete(core_scope_kind=scope_kind, core_scope_id=value)
            for value in db.scalars(select(model.id).where(model.matter_id == matter_id))
        )
    configuration_ids = db.scalars(
        select(ClassificationModelConfiguration.id)
        .join(
            ClassificationModel,
            ClassificationModel.id == ClassificationModelConfiguration.classification_model_id,
        )
        .where(ClassificationModel.matter_id == matter_id)
    )
    scopes.extend(
        WorkflowArtifactScopeDelete(
            core_scope_kind="CLASSIFICATION_CONFIGURATION",
            core_scope_id=value,
        )
        for value in configuration_ids
    )
    return scopes


def create_matter_deletion_job(
    db: Session,
    matter: Matter,
    *,
    requested_by_user_id: uuid.UUID,
    artifact_count: int,
    scopes: list[WorkflowArtifactScopeDelete],
) -> MatterDeletionJob:
    existing = db.scalar(
        select(MatterDeletionJob)
        .where(
            MatterDeletionJob.matter_id == matter.id,
            MatterDeletionJob.status.in_(ACTIVE_DELETION_STATUSES),
        )
        .order_by(MatterDeletionJob.created_at.desc())
    )
    if existing is not None:
        return existing
    if matter.status == "DELETING":
        failed = db.scalar(
            select(MatterDeletionJob)
            .where(MatterDeletionJob.matter_id == matter.id)
            .order_by(MatterDeletionJob.created_at.desc())
        )
        if failed is not None:
            return failed
        raise ValueError("Matter is already being deleted")
    job_id = uuid.uuid4()
    job = MatterDeletionJob(
        id=job_id,
        matter_id=matter.id,
        tenant_id=matter.client.tenant_id,
        client_id=matter.client_id,
        matter_name=matter.name,
        previous_matter_status=matter.status,
        status="QUEUED",
        workflow_id=f"matter-delete:{job_id}:attempt:1",
        attempt_count=1,
        document_count=int(
            db.scalar(
                select(func.count()).select_from(MatterDocument).where(MatterDocument.matter_id == matter.id)
            )
            or 0
        ),
        artifact_count=artifact_count,
        artifact_scopes=[scope.model_dump(mode="json") for scope in scopes],
        requested_by_user_id=requested_by_user_id,
    )
    matter.status = "DELETING"
    db.add(job)
    db.flush()
    return job


def retry_matter_deletion_job(db: Session, job_id: uuid.UUID) -> MatterDeletionJob:
    job = db.scalar(select(MatterDeletionJob).where(MatterDeletionJob.id == job_id).with_for_update())
    if job is None:
        raise ValueError("Matter deletion job not found")
    if job.status != "FAILED":
        raise ValueError("Only failed matter deletions can be retried")
    matter = db.get(Matter, job.matter_id)
    if matter is not None:
        matter.status = "DELETING"
    job.attempt_count += 1
    job.workflow_id = f"matter-delete:{job.id}:attempt:{job.attempt_count}"
    job.status = "QUEUED"
    job.error_message = None
    job.completed_at = None
    db.flush()
    return job


def mark_matter_deletion_stage(db: Session, job_id: uuid.UUID, stage: str) -> MatterDeletionJob:
    job = db.get(MatterDeletionJob, job_id)
    if job is None:
        raise ValueError("Matter deletion job not found")
    if job.status == "COMPLETED":
        return job
    job.status = stage
    job.started_at = job.started_at or utcnow()
    job.error_message = None
    db.commit()
    return job


def _matter_owned_tables() -> list:
    tables = []
    for table in Base.metadata.sorted_tables:
        if "matter_id" not in table.c:
            continue
        if any(foreign_key.target_fullname == "matter.id" for foreign_key in table.c.matter_id.foreign_keys):
            tables.append(table)
    return tables


def delete_matter_database_rows(db: Session, job_id: uuid.UUID) -> int:
    job = db.get(MatterDeletionJob, job_id)
    if job is None:
        raise ValueError("Matter deletion job not found")
    matter = db.get(Matter, job.matter_id)
    if matter is None:
        return 0

    # Break the two optional classification registry pointers that form cycles
    # with matter-owned configuration/version rows.
    classification_model = Base.metadata.tables.get("classification_model")
    if classification_model is not None:
        db.execute(
            update(classification_model)
            .where(classification_model.c.matter_id == job.matter_id)
            .values(current_configuration_id=None, active_model_version_id=None)
        )

    deleted = 0
    for table in reversed(_matter_owned_tables()):
        result = db.execute(delete(table).where(table.c.matter_id == job.matter_id))
        deleted += max(result.rowcount or 0, 0)
    result = db.execute(delete(Matter).where(Matter.id == job.matter_id))
    deleted += max(result.rowcount or 0, 0)
    db.commit()
    return deleted


def complete_matter_deletion(db: Session, job_id: uuid.UUID) -> MatterDeletionJob:
    job = db.get(MatterDeletionJob, job_id)
    if job is None:
        raise ValueError("Matter deletion job not found")
    if db.get(Matter, job.matter_id) is not None:
        raise ValueError("Matter database rows remain")
    job.status = "COMPLETED"
    job.completed_at = utcnow()
    job.error_message = None
    db.commit()
    return job


def fail_matter_deletion(db: Session, job_id: uuid.UUID, message: str) -> MatterDeletionJob | None:
    job = db.get(MatterDeletionJob, job_id)
    if job is None:
        return None
    job.status = "FAILED"
    job.error_message = message[:4000]
    job.completed_at = utcnow()
    matter = db.get(Matter, job.matter_id)
    if (
        matter is not None
        and job.deleted_index_count == 0
        and job.deleted_artifact_count == 0
        and job.deleted_blob_count == 0
    ):
        matter.status = job.previous_matter_status
    db.commit()
    return job
