from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.models import (
    AnalysisTaskBatchSet,
    ClassificationScoringRun,
    ClassificationTrainingJob,
    Matter,
    MatterAnalysisTaskRun,
    MatterDocumentImportJob,
    MatterEmbeddingJob,
    MatterSearchExportJob,
    ReviewBatch,
    ReviewBatchRun,
    ReviewBatchRunPromotion,
    WorkflowRun,
)

CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE = "CLASSIFICATION_ANALYSIS_TASK"
ANALYSIS_TASK_BATCH_RUN_JOB_TYPE = "ANALYSIS_TASK_BATCH_RUN"
MATTER_EMBEDDING_JOB_TYPE = "MATTER_EMBEDDING_JOB"
MATTER_DOCUMENT_IMPORT_JOB_TYPE = "MATTER_DOCUMENT_IMPORT"
REVIEW_BATCH_MATERIALIZATION_JOB_TYPE = "REVIEW_BATCH_MATERIALIZATION"
CLASSIFICATION_TRAINING_JOB_TYPE = "CLASSIFICATION_TRAINING_JOB"
CLASSIFICATION_SCORING_RUN_JOB_TYPE = "CLASSIFICATION_SCORING_RUN"
CLASSIFICATION_PROJECTION_JOB_TYPE = "CLASSIFICATION_PROJECTION"
REVIEW_BATCH_CODING_PROMOTION_JOB_TYPE = "REVIEW_BATCH_CODING_PROMOTION"
MATTER_SEARCH_EXPORT_JOB_TYPE = "MATTER_SEARCH_EXPORT"
FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE = "FACT_DEVELOPMENT_ANALYSIS_TASK"
ANALYSIS_TASK_BATCH_SET_JOB_TYPE = "ANALYSIS_TASK_BATCH_SET"

EXECUTION_TERMINAL_STATUSES = {
    "COMPLETED",
    "COMPLETED_WITH_ERRORS",
    "FAILED",
    "CANCELED",
}
EXECUTION_STATUSES = {
    "QUEUED",
    "RUNNING",
    "WAITING",
    *EXECUTION_TERMINAL_STATUSES,
}

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "QUEUED": {"RUNNING", "FAILED", "CANCELED"},
    "RUNNING": {"WAITING", "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"},
    "WAITING": {"RUNNING", "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"},
    "COMPLETED": set(),
    "COMPLETED_WITH_ERRORS": set(),
    "FAILED": set(),
    "CANCELED": set(),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobExecutionError(RuntimeError):
    pass


class JobExecutionNotFound(JobExecutionError):
    pass


class JobExecutionConflict(JobExecutionError):
    pass


@dataclass(frozen=True)
class JobScope:
    tenant_id: uuid.UUID
    client_id: uuid.UUID | None
    matter_id: uuid.UUID | None


class JobExecutionAdapter(Protocol):
    job_type: str
    supports_retry: bool

    def load(self, db: Session, job_id: uuid.UUID, *, for_update: bool = False) -> Any | None: ...

    def scope(self, db: Session, job: Any) -> tuple[JobScope, Matter | None]: ...

    def project(self, job: Any, execution: WorkflowRun) -> None: ...


class ClassificationAnalysisTaskExecutionAdapter:
    job_type = CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE
    supports_retry = True

    def load(
        self,
        db: Session,
        job_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> MatterAnalysisTaskRun | None:
        statement = select(MatterAnalysisTaskRun).where(
            MatterAnalysisTaskRun.id == job_id,
            MatterAnalysisTaskRun.run_kind == "CLASSIFICATION",
        )
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)

    def scope(
        self,
        db: Session,
        job: MatterAnalysisTaskRun,
    ) -> tuple[JobScope, Matter | None]:
        matter = db.get(Matter, job.matter_id)
        if matter is None:
            raise JobExecutionNotFound("Analysis Task matter was not found")
        return (
            JobScope(
                tenant_id=job.tenant_id,
                client_id=matter.client_id,
                matter_id=job.matter_id,
            ),
            matter,
        )

    def project(self, job: MatterAnalysisTaskRun, execution: WorkflowRun) -> None:
        job.current_workflow_run_id = execution.id
        job.current_workflow_run = execution
        job.workflow_id = execution.dbos_workflow_id
        job.current_stage = execution.current_stage or execution.status
        job.progress_completed = min(
            int(execution.progress_completed),
            job.progress_total,
        )
        job.failure_code = execution.failure_code
        job.failure_summary = execution.failure_summary
        job.failure_retriable = execution.failure_retriable
        job.started_at = execution.started_at
        job.completed_at = execution.completed_at

        if execution.status == "QUEUED":
            job.status = "QUEUED"
        elif execution.status in {"RUNNING", "WAITING"}:
            job.status = {
                "FREEZING_POPULATION": "SNAPSHOTTING",
                "MATERIALIZING_FEATURES": "PREPARING_FEATURES",
                "LAUNCHING_SCORING": "SCORING",
                "SCORING_DOCUMENTS": "SCORING",
            }.get(execution.current_stage or "", job.status)
        else:
            job.status = execution.status
        if execution.status == "CANCELED":
            job.canceled_at = execution.completed_at or utcnow()
        else:
            job.canceled_at = None


class AnalysisTaskBatchRunExecutionAdapter:
    job_type = ANALYSIS_TASK_BATCH_RUN_JOB_TYPE
    supports_retry = False

    def load(
        self,
        db: Session,
        job_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> ReviewBatchRun | None:
        statement = select(ReviewBatchRun).where(
            ReviewBatchRun.id == job_id,
            ReviewBatchRun.run_type == "WORKFLOW",
        )
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)

    def scope(
        self,
        db: Session,
        job: ReviewBatchRun,
    ) -> tuple[JobScope, Matter | None]:
        batch = db.get(ReviewBatch, job.review_batch_id)
        matter = db.get(Matter, batch.matter_id) if batch is not None else None
        if batch is None or matter is None:
            raise JobExecutionNotFound("Analysis Task batch or matter was not found")
        return (
            JobScope(
                tenant_id=matter.client.tenant_id,
                client_id=matter.client_id,
                matter_id=matter.id,
            ),
            matter,
        )

    def project(self, job: ReviewBatchRun, execution: WorkflowRun) -> None:
        job.workflow_run_record_id = execution.id
        job.current_workflow_run_id = execution.id
        job.current_workflow_run = execution
        job.dbos_workflow_id = execution.dbos_workflow_id
        job.status = execution.status
        job.started_at = execution.started_at
        job.completed_at = execution.completed_at
        job.error_message = execution.failure_summary


class MatterEmbeddingJobExecutionAdapter:
    job_type = MATTER_EMBEDDING_JOB_TYPE
    supports_retry = False

    def load(
        self,
        db: Session,
        job_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> MatterEmbeddingJob | None:
        statement = select(MatterEmbeddingJob).where(MatterEmbeddingJob.id == job_id)
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)

    def scope(
        self,
        db: Session,
        job: MatterEmbeddingJob,
    ) -> tuple[JobScope, Matter | None]:
        matter = db.get(Matter, job.matter_id)
        if matter is None:
            raise JobExecutionNotFound("Embedding job matter was not found")
        return (
            JobScope(
                tenant_id=matter.client.tenant_id,
                client_id=matter.client_id,
                matter_id=matter.id,
            ),
            matter,
        )

    def project(self, job: MatterEmbeddingJob, execution: WorkflowRun) -> None:
        job.current_workflow_run_id = execution.id
        job.current_workflow_run = execution
        job.workflow_id = execution.dbos_workflow_id
        job.processed_count = int(execution.progress_completed)
        job.error_message = execution.failure_summary
        job.started_at = execution.started_at
        job.completed_at = execution.completed_at

        if execution.status == "QUEUED":
            job.status = "QUEUED"
        elif execution.status in {"RUNNING", "WAITING"}:
            job.status = "PLANNING" if execution.current_stage == "PLANNING" else "RUNNING"
        else:
            job.status = execution.status
        job.canceled_at = execution.completed_at or utcnow() if execution.status == "CANCELED" else None


class MatterDocumentImportExecutionAdapter:
    job_type = MATTER_DOCUMENT_IMPORT_JOB_TYPE
    supports_retry = False

    def load(
        self,
        db: Session,
        job_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> MatterDocumentImportJob | None:
        statement = select(MatterDocumentImportJob).where(MatterDocumentImportJob.id == job_id)
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)

    def scope(
        self,
        db: Session,
        job: MatterDocumentImportJob,
    ) -> tuple[JobScope, Matter | None]:
        matter = db.get(Matter, job.matter_id)
        if matter is None:
            raise JobExecutionNotFound("Document import matter was not found")
        return (
            JobScope(
                tenant_id=matter.client.tenant_id,
                client_id=matter.client_id,
                matter_id=matter.id,
            ),
            matter,
        )

    def project(self, job: MatterDocumentImportJob, execution: WorkflowRun) -> None:
        job.current_workflow_run_id = execution.id
        job.current_workflow_run = execution
        job.workflow_id = execution.dbos_workflow_id
        job.processed_count = int(execution.progress_completed)
        job.error_message = execution.failure_summary
        job.started_at = execution.started_at
        job.completed_at = execution.completed_at

        if execution.status == "QUEUED":
            job.status = "QUEUED"
        elif execution.status in {"RUNNING", "WAITING"}:
            job.status = "SNAPSHOTTING" if execution.current_stage == "FREEZING_SELECTION" else "RUNNING"
        else:
            job.status = "COMPLETED" if execution.status == "COMPLETED_WITH_ERRORS" else execution.status
        job.canceled_at = execution.completed_at or utcnow() if execution.status == "CANCELED" else None


class ReviewBatchMaterializationExecutionAdapter:
    job_type = REVIEW_BATCH_MATERIALIZATION_JOB_TYPE
    supports_retry = True

    def load(
        self,
        db: Session,
        job_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> ReviewBatch | None:
        statement = select(ReviewBatch).where(
            ReviewBatch.id == job_id,
            ReviewBatch.selection_type != "DEFINITION_ASSESSMENT",
        )
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)

    def scope(
        self,
        db: Session,
        job: ReviewBatch,
    ) -> tuple[JobScope, Matter | None]:
        matter = db.get(Matter, job.matter_id)
        if matter is None:
            raise JobExecutionNotFound("Review batch matter was not found")
        return (
            JobScope(
                tenant_id=matter.client.tenant_id,
                client_id=matter.client_id,
                matter_id=matter.id,
            ),
            matter,
        )

    def project(self, job: ReviewBatch, execution: WorkflowRun) -> None:
        job.current_workflow_run_id = execution.id
        job.current_workflow_run = execution
        job.workflow_id = execution.dbos_workflow_id
        if execution.progress_total is not None:
            job.document_count = int(execution.progress_completed)
        job.completed_at = execution.completed_at

        if execution.status == "QUEUED":
            job.status = "QUEUED"
            job.error_message = None
        elif execution.status in {"RUNNING", "WAITING"}:
            job.status = (
                "READY"
                if execution.current_stage == "SYNCING_SEARCH"
                and execution.progress_total is not None
                and execution.progress_completed >= execution.progress_total
                else "BUILDING"
            )
            job.error_message = None
        elif execution.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
            job.status = "READY"
            job.error_message = None
        elif execution.status == "FAILED":
            job.status = "FAILED"
            job.error_message = execution.failure_summary
        elif execution.status == "CANCELED":
            job.status = "CANCELED"
            job.error_message = None


class AnalysisTaskBatchSetExecutionAdapter:
    job_type = ANALYSIS_TASK_BATCH_SET_JOB_TYPE
    supports_retry = False

    def load(self, db: Session, job_id: uuid.UUID, *, for_update: bool = False) -> AnalysisTaskBatchSet | None:
        statement = select(AnalysisTaskBatchSet).where(AnalysisTaskBatchSet.id == job_id)
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)

    def scope(self, db: Session, job: AnalysisTaskBatchSet) -> tuple[JobScope, Matter | None]:
        matter = db.get(Matter, job.matter_id)
        if matter is None:
            raise JobExecutionNotFound("Analysis Task Batch Set matter was not found")
        return JobScope(job.tenant_id, matter.client_id, matter.id), matter

    def project(self, job: AnalysisTaskBatchSet, execution: WorkflowRun) -> None:
        job.current_workflow_run_id = execution.id
        job.current_workflow_run = execution
        job.workflow_id = execution.dbos_workflow_id
        if execution.status == "QUEUED":
            job.status = "QUEUED"
        elif execution.status in {"RUNNING", "WAITING"}:
            job.status = "BUILDING"
        elif execution.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
            job.status = "READY"
        elif execution.status == "FAILED":
            job.status = "FAILED"
            job.error_message = execution.failure_summary
        elif execution.status == "CANCELED":
            job.status = "CANCELED"


class ClassificationTrainingJobExecutionAdapter:
    job_type = CLASSIFICATION_TRAINING_JOB_TYPE
    supports_retry = True

    def load(
        self,
        db: Session,
        job_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> ClassificationTrainingJob | None:
        statement = select(ClassificationTrainingJob).where(ClassificationTrainingJob.id == job_id)
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)

    def scope(
        self,
        db: Session,
        job: ClassificationTrainingJob,
    ) -> tuple[JobScope, Matter | None]:
        matter = db.get(Matter, job.matter_id)
        if matter is None:
            raise JobExecutionNotFound("Classification training matter was not found")
        return (
            JobScope(
                tenant_id=job.tenant_id,
                client_id=matter.client_id,
                matter_id=matter.id,
            ),
            matter,
        )

    def project(self, job: ClassificationTrainingJob, execution: WorkflowRun) -> None:
        job.current_workflow_run_id = execution.id
        job.current_workflow_run = execution
        job.workflow_id = execution.dbos_workflow_id
        job.current_stage = execution.current_stage or execution.status
        job.progress_completed = min(int(execution.progress_completed), job.progress_total)
        job.failure_code = execution.failure_code
        job.failure_summary = execution.failure_summary
        job.failure_retriable = execution.failure_retriable
        job.started_at = execution.started_at
        job.completed_at = execution.completed_at
        if execution.status == "QUEUED":
            job.status = "QUEUED"
        elif execution.status in {"RUNNING", "WAITING"}:
            job.status = {
                "RESOLVING_LABELS_AND_SNAPSHOTTING": "SNAPSHOTTING",
                "PREPARING_FEATURES_AND_SPLITS": "PREPARING_FEATURES",
                "LAUNCHING_TRAINING": "LAUNCHING",
                "TRAINING_MODEL": "TRAINING",
                "FINALIZING": "FINALIZING",
            }.get(execution.current_stage or "", job.status)
        else:
            job.status = "COMPLETED" if execution.status == "COMPLETED_WITH_ERRORS" else execution.status
        job.canceled_at = execution.completed_at or utcnow() if execution.status == "CANCELED" else None


class ClassificationScoringRunExecutionAdapter:
    job_type = CLASSIFICATION_SCORING_RUN_JOB_TYPE
    supports_retry = True

    def load(self, db: Session, job_id: uuid.UUID, *, for_update: bool = False) -> ClassificationScoringRun | None:
        statement = select(ClassificationScoringRun).where(ClassificationScoringRun.id == job_id)
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)

    def scope(self, db: Session, job: ClassificationScoringRun) -> tuple[JobScope, Matter | None]:
        matter = db.get(Matter, job.matter_id)
        if matter is None:
            raise JobExecutionNotFound("Classification scoring matter was not found")
        return JobScope(job.tenant_id, matter.client_id, matter.id), matter

    def project(self, job: ClassificationScoringRun, execution: WorkflowRun) -> None:
        job.current_workflow_run_id = execution.id
        job.current_workflow_run = execution
        job.workflow_id = execution.dbos_workflow_id
        job.current_stage = execution.current_stage or execution.status
        job.progress_completed = min(int(execution.progress_completed), job.progress_total)
        job.failure_code = execution.failure_code
        job.failure_summary = execution.failure_summary
        job.failure_retriable = execution.failure_retriable
        job.started_at = execution.started_at
        job.completed_at = execution.completed_at
        if execution.status == "QUEUED":
            job.status = "QUEUED"
        elif execution.status in {"RUNNING", "WAITING"}:
            job.status = {
                "VALIDATING_PACKAGE": "WAITING_FOR_RUNTIME",
                "SCORING_DOCUMENTS": "SCORING",
                "PUBLISHING_RESULTS": "PUBLISHING",
                "FINALIZING": "FINALIZING",
                "FINALIZING_RECONCILED_OUTPUTS": "RECONCILING",
            }.get(execution.current_stage or "", job.status)
        else:
            job.status = execution.status
        job.canceled_at = execution.completed_at if execution.status == "CANCELED" else None


class ClassificationProjectionExecutionAdapter:
    job_type = CLASSIFICATION_PROJECTION_JOB_TYPE
    supports_retry = True

    def load(self, db: Session, job_id: uuid.UUID, *, for_update: bool = False) -> ReviewBatchRun | None:
        statement = select(ReviewBatchRun).where(
            ReviewBatchRun.id == job_id,
            ReviewBatchRun.run_type == "CLASSIFIER",
        )
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)

    def scope(self, db: Session, job: ReviewBatchRun) -> tuple[JobScope, Matter | None]:
        batch = db.get(ReviewBatch, job.review_batch_id)
        matter = db.get(Matter, batch.matter_id) if batch is not None else None
        if batch is None or matter is None:
            raise JobExecutionNotFound("Classifier projection batch or matter was not found")
        return JobScope(matter.client.tenant_id, matter.client_id, matter.id), matter

    def project(self, job: ReviewBatchRun, execution: WorkflowRun) -> None:
        job.current_workflow_run_id = execution.id
        job.current_workflow_run = execution
        job.dbos_workflow_id = execution.dbos_workflow_id
        job.processed_document_count = int(execution.progress_completed)
        job.started_at = execution.started_at
        job.completed_at = execution.completed_at
        job.error_message = execution.failure_summary
        job.status = execution.status if execution.status not in {"WAITING"} else "RUNNING"


class ReviewBatchCodingPromotionExecutionAdapter:
    job_type = REVIEW_BATCH_CODING_PROMOTION_JOB_TYPE
    supports_retry = False

    def load(self, db: Session, job_id: uuid.UUID, *, for_update: bool = False) -> ReviewBatchRunPromotion | None:
        statement = select(ReviewBatchRunPromotion).where(ReviewBatchRunPromotion.id == job_id)
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)

    def scope(self, db: Session, job: ReviewBatchRunPromotion) -> tuple[JobScope, Matter | None]:
        matter = db.get(Matter, job.matter_id)
        if matter is None:
            raise JobExecutionNotFound("Coding promotion matter was not found")
        return JobScope(matter.client.tenant_id, matter.client_id, matter.id), matter

    def project(self, job: ReviewBatchRunPromotion, execution: WorkflowRun) -> None:
        job.current_workflow_run_id = execution.id
        job.current_workflow_run = execution
        job.dbos_workflow_id = execution.dbos_workflow_id
        job.processed_document_count = int(execution.progress_completed)
        job.started_at = execution.started_at
        job.completed_at = execution.completed_at
        job.error_message = execution.failure_summary
        if execution.status == "QUEUED":
            job.status = "QUEUED"
        elif execution.status in {"RUNNING", "WAITING"}:
            job.status = "RUNNING"
        elif execution.status == "COMPLETED_WITH_ERRORS":
            job.status = "COMPLETED"
        elif execution.status != "CANCELED":
            job.status = execution.status


class MatterSearchExportExecutionAdapter:
    job_type = MATTER_SEARCH_EXPORT_JOB_TYPE
    supports_retry = True

    def load(
        self,
        db: Session,
        job_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> MatterSearchExportJob | None:
        statement = select(MatterSearchExportJob).where(MatterSearchExportJob.id == job_id)
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)

    def scope(
        self,
        db: Session,
        job: MatterSearchExportJob,
    ) -> tuple[JobScope, Matter | None]:
        matter = db.get(Matter, job.matter_id)
        if matter is None:
            raise JobExecutionNotFound("Search export matter was not found")
        return JobScope(job.tenant_id, matter.client_id, matter.id), matter

    def project(self, job: MatterSearchExportJob, execution: WorkflowRun) -> None:
        job.current_workflow_run_id = execution.id
        job.current_workflow_run = execution
        job.workflow_id = execution.dbos_workflow_id
        job.current_stage = execution.current_stage or execution.status
        job.failure_code = execution.failure_code
        job.failure_summary = execution.failure_summary
        job.failure_retriable = execution.failure_retriable
        job.started_at = execution.started_at
        job.completed_at = execution.completed_at
        if execution.progress_total is not None:
            job.matched_count = int(execution.progress_total)
        job.exported_count = int(execution.progress_completed)
        if execution.status == "QUEUED":
            job.status = "QUEUED"
        elif execution.status in {"RUNNING", "WAITING"}:
            job.status = "RUNNING"
        else:
            job.status = "COMPLETED" if execution.status == "COMPLETED_WITH_ERRORS" else execution.status
        job.canceled_at = execution.completed_at if execution.status == "CANCELED" else None


class FactDevelopmentAnalysisTaskExecutionAdapter:
    job_type = FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE
    supports_retry = True

    def load(
        self,
        db: Session,
        job_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> MatterAnalysisTaskRun | None:
        statement = select(MatterAnalysisTaskRun).where(
            MatterAnalysisTaskRun.id == job_id,
            MatterAnalysisTaskRun.run_kind == "FACT_DEVELOPMENT",
        )
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)

    def scope(
        self,
        db: Session,
        job: MatterAnalysisTaskRun,
    ) -> tuple[JobScope, Matter | None]:
        matter = db.get(Matter, job.matter_id)
        if matter is None:
            raise JobExecutionNotFound("Analysis Task matter was not found")
        return JobScope(job.tenant_id, matter.client_id, matter.id), matter

    def project(self, job: MatterAnalysisTaskRun, execution: WorkflowRun) -> None:
        job.current_workflow_run_id = execution.id
        job.current_workflow_run = execution
        job.workflow_id = execution.dbos_workflow_id
        job.current_stage = execution.current_stage or execution.status
        job.progress_completed = int(execution.progress_completed)
        if execution.progress_total is not None:
            job.progress_total = int(execution.progress_total)
        job.failure_code = execution.failure_code
        job.failure_summary = execution.failure_summary
        job.failure_retriable = execution.failure_retriable
        job.started_at = execution.started_at
        job.completed_at = execution.completed_at
        if execution.status == "QUEUED":
            job.status = "QUEUED"
        elif execution.status in {"RUNNING", "WAITING"}:
            job.status = {"PREPARING": "PREPARING", "EXTRACTING": "EXTRACTING", "FINALIZING": "EXTRACTING"}.get(
                execution.current_stage or "",
                job.status,
            )
        else:
            job.status = execution.status
        job.canceled_at = (execution.completed_at or utcnow()) if execution.status == "CANCELED" else None


_ADAPTERS: dict[str, JobExecutionAdapter] = {
    ANALYSIS_TASK_BATCH_SET_JOB_TYPE: AnalysisTaskBatchSetExecutionAdapter(),
    ANALYSIS_TASK_BATCH_RUN_JOB_TYPE: AnalysisTaskBatchRunExecutionAdapter(),
    CLASSIFICATION_ANALYSIS_TASK_JOB_TYPE: ClassificationAnalysisTaskExecutionAdapter(),
    MATTER_DOCUMENT_IMPORT_JOB_TYPE: MatterDocumentImportExecutionAdapter(),
    MATTER_EMBEDDING_JOB_TYPE: MatterEmbeddingJobExecutionAdapter(),
    REVIEW_BATCH_MATERIALIZATION_JOB_TYPE: ReviewBatchMaterializationExecutionAdapter(),
    CLASSIFICATION_TRAINING_JOB_TYPE: ClassificationTrainingJobExecutionAdapter(),
    CLASSIFICATION_SCORING_RUN_JOB_TYPE: ClassificationScoringRunExecutionAdapter(),
    CLASSIFICATION_PROJECTION_JOB_TYPE: ClassificationProjectionExecutionAdapter(),
    REVIEW_BATCH_CODING_PROMOTION_JOB_TYPE: ReviewBatchCodingPromotionExecutionAdapter(),
    MATTER_SEARCH_EXPORT_JOB_TYPE: MatterSearchExportExecutionAdapter(),
    FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE: FactDevelopmentAnalysisTaskExecutionAdapter(),
}


def registered_job_types() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))


def get_job_execution_adapter(job_type: str) -> JobExecutionAdapter:
    adapter = _ADAPTERS.get(job_type.upper())
    if adapter is None:
        raise JobExecutionNotFound(f"Unsupported job type: {job_type}")
    return adapter


def load_job_and_scope(
    db: Session,
    *,
    job_type: str,
    job_id: uuid.UUID,
    for_update: bool = False,
) -> tuple[JobExecutionAdapter, Any, JobScope, Matter | None]:
    adapter = get_job_execution_adapter(job_type)
    job = adapter.load(db, job_id, for_update=for_update)
    if job is None:
        raise JobExecutionNotFound("Job was not found")
    scope, matter = adapter.scope(db, job)
    return adapter, job, scope, matter


def current_job_execution(
    db: Session,
    *,
    job_type: str,
    job_id: uuid.UUID,
    for_update: bool = False,
) -> WorkflowRun | None:
    statement = (
        select(WorkflowRun)
        .where(
            WorkflowRun.job_type == job_type,
            WorkflowRun.job_id == job_id,
        )
        .order_by(WorkflowRun.attempt_number.desc())
        .limit(1)
    )
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


def job_execution_history(
    db: Session,
    *,
    job_type: str,
    job_id: uuid.UUID,
) -> list[WorkflowRun]:
    return list(
        db.scalars(
            select(WorkflowRun)
            .where(
                WorkflowRun.job_type == job_type,
                WorkflowRun.job_id == job_id,
            )
            .order_by(WorkflowRun.attempt_number.desc())
        )
    )


def create_job_execution(
    db: Session,
    *,
    job_type: str,
    job_id: uuid.UUID,
    scope: JobScope,
    workflow_key: str,
    code_version: str,
    dbos_workflow_id: str,
    initiated_by_user_id: uuid.UUID,
    idempotency_key: str | None = None,
    current_stage: str = "QUEUED",
    progress_total: int | None = None,
    progress_unit: str | None = None,
    input_snapshot: dict[str, Any] | None = None,
    binding_snapshot: dict[str, Any] | None = None,
    configuration_snapshot: dict[str, Any] | None = None,
    retry_of: WorkflowRun | None = None,
) -> WorkflowRun:
    adapter = get_job_execution_adapter(job_type)
    job = adapter.load(db, job_id, for_update=True)
    if job is None:
        raise JobExecutionNotFound("Job was not found")
    existing = current_job_execution(
        db,
        job_type=adapter.job_type,
        job_id=job_id,
        for_update=True,
    )
    if retry_of is None and existing is not None:
        return existing
    if retry_of is not None and existing is not None and existing.id != retry_of.id:
        raise JobExecutionConflict("The requested retry source is not the current execution")
    attempt_number = (existing.attempt_number + 1) if existing is not None else 1
    execution = WorkflowRun(
        tenant_id=scope.tenant_id,
        client_id=scope.client_id,
        matter_id=scope.matter_id,
        job_type=adapter.job_type,
        job_id=job_id,
        attempt_number=attempt_number,
        retry_of_workflow_run_id=retry_of.id if retry_of is not None else None,
        workflow_key=workflow_key,
        code_version=code_version,
        dbos_workflow_id=dbos_workflow_id,
        idempotency_key=idempotency_key,
        status="QUEUED",
        current_stage=current_stage,
        progress_completed=0,
        progress_total=progress_total,
        progress_unit=progress_unit,
        input_snapshot=input_snapshot or {},
        binding_snapshot=binding_snapshot or {},
        configuration_snapshot=configuration_snapshot or {},
        progress={},
        initiated_by_user_id=initiated_by_user_id,
    )
    db.add(execution)
    db.flush()
    adapter.project(job, execution)
    return execution


def transition_job_execution(
    db: Session,
    *,
    job_type: str,
    job_id: uuid.UUID,
    status: str | None = None,
    current_stage: str | None = None,
    progress_completed: int | None = None,
    progress_total: int | None = None,
    progress: dict[str, Any] | None = None,
    failure_code: str | None = None,
    failure_summary: str | None = None,
    failure_retriable: bool | None = None,
    heartbeat: bool = True,
) -> WorkflowRun:
    adapter, job, _, _ = load_job_and_scope(
        db,
        job_type=job_type,
        job_id=job_id,
        for_update=True,
    )
    execution = current_job_execution(
        db,
        job_type=adapter.job_type,
        job_id=job_id,
        for_update=True,
    )
    if execution is None:
        raise JobExecutionNotFound("Job execution was not found")
    if status is not None:
        if status not in EXECUTION_STATUSES:
            raise JobExecutionConflict(f"Unsupported execution status: {status}")
        if status != execution.status and status not in _ALLOWED_TRANSITIONS[execution.status]:
            raise JobExecutionConflict(f"Cannot transition execution from {execution.status} to {status}")
        execution.status = status
    if current_stage is not None:
        execution.current_stage = current_stage[:100]
    if progress_completed is not None:
        if progress_completed < execution.progress_completed:
            raise JobExecutionConflict("Execution progress cannot move backwards")
        execution.progress_completed = progress_completed
    if progress_total is not None:
        if progress_total < 0 or progress_total < execution.progress_completed:
            raise JobExecutionConflict("Execution progress total is invalid")
        execution.progress_total = progress_total
    if progress is not None:
        execution.progress = progress
    if failure_code is not None:
        execution.failure_code = failure_code[:100]
    if failure_summary is not None:
        execution.failure_summary = failure_summary[:4000]
        execution.error_message = execution.failure_summary
    if failure_retriable is not None:
        execution.failure_retriable = failure_retriable

    now = utcnow()
    if heartbeat:
        execution.last_heartbeat_at = now
    if execution.status in {"RUNNING", "WAITING"}:
        execution.started_at = execution.started_at or now
    if execution.status in EXECUTION_TERMINAL_STATUSES:
        execution.completed_at = execution.completed_at or now
    adapter.project(job, execution)
    return execution


def request_job_cancellation(
    db: Session,
    *,
    job_type: str,
    job_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowRun, bool]:
    adapter, job, scope, _ = load_job_and_scope(
        db,
        job_type=job_type,
        job_id=job_id,
        for_update=True,
    )
    execution = current_job_execution(
        db,
        job_type=adapter.job_type,
        job_id=job_id,
        for_update=True,
    )
    if execution is None:
        raise JobExecutionNotFound("Job execution was not found")
    if execution.status in EXECUTION_TERMINAL_STATUSES:
        return execution, False
    if execution.cancel_requested_at is not None:
        return execution, False
    now = utcnow()
    execution.cancel_requested_at = now
    execution.cancel_requested_by_user_id = actor_user_id
    execution.last_heartbeat_at = now
    record_audit(
        db,
        tenant_id=scope.tenant_id,
        actor_user_id=actor_user_id,
        action="job_execution.cancel_requested",
        target_type=adapter.job_type.lower(),
        target_id=job_id,
        details={
            "execution_id": str(execution.id),
            "attempt_number": execution.attempt_number,
            "matter_id": str(scope.matter_id) if scope.matter_id else None,
        },
    )
    adapter.project(job, execution)
    return execution, True


def cancel_job_execution(
    db: Session,
    *,
    job_type: str,
    job_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowRun, bool]:
    execution, requested = request_job_cancellation(
        db,
        job_type=job_type,
        job_id=job_id,
        actor_user_id=actor_user_id,
    )
    if execution.status in EXECUTION_TERMINAL_STATUSES:
        return execution, requested
    execution = transition_job_execution(
        db,
        job_type=job_type,
        job_id=job_id,
        status="CANCELED",
        current_stage="CANCELED",
        failure_retriable=False,
    )
    return execution, True


def execution_cancel_requested(
    db: Session,
    *,
    job_type: str,
    job_id: uuid.UUID,
) -> bool:
    execution = current_job_execution(db, job_type=job_type, job_id=job_id)
    return bool(execution is not None and (execution.cancel_requested_at is not None or execution.status == "CANCELED"))


def next_attempt_number(db: Session, *, job_type: str, job_id: uuid.UUID) -> int:
    current = db.scalar(
        select(func.max(WorkflowRun.attempt_number)).where(
            WorkflowRun.job_type == job_type,
            WorkflowRun.job_id == job_id,
        )
    )
    return int(current or 0) + 1
