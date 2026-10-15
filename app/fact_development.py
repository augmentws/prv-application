"""Fact-development runs: launch, prepare, per-chunk extraction, and finalization.

A run is a ``matter_analysis_task_run`` with ``run_kind = FACT_DEVELOPMENT`` and the
``FACT_DEVELOPMENT_ANALYSIS_TASK`` job type, so status, progress, cancellation and retry go through the unified job
execution platform. Preparation freezes the review batch's documents and the issues each one is coded to into
fixed-size ``matter_analysis_task_run_chunk`` rows. Each chunk reads document text and File Date / email headers
through Core's Artifact gateway (the same reads the search projection uses), asks the ``fact_extraction`` skill for
operations, applies the valid ones as the agent, and publishes its per-document outcomes as a job-outcome fragment.
No per-document rows are kept in Core.

This release runs a single chronological pass with no duplicate consolidation; consolidation and waves follow.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent_models import resolve_agent_model
from app.artifact_gateway import get_search_item_snapshot
from app.audit import record_audit
from app.authorization import ResourceRef, user_can
from app.config import Settings, get_settings
from app.database import SessionLocal
from app.document_evidence import render_model_document
from app.embeddings.configuration import canonical_hash
from app.fact_development_skills import FACT_EXTRACTION_ROLE
from app.fact_evidence import DocumentText, FactEvidenceError, load_document_text
from app.fact_extraction import (
    ExtractionContext,
    apply_extraction,
    extraction_output_validator,
    parse_extraction_output,
)
from app.fact_issue_membership import CodingSource, FactIssueMembershipError, evaluate_issue_membership
from app.fact_recall import recall_facts
from app.fact_specifications import (
    FactDevelopmentSpecification,
    FactSpecificationError,
    parse_fact_development_specification,
)
from app.facts import Actor
from app.job_executions import (
    FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
    JobExecutionConflict,
    JobScope,
    cancel_job_execution,
    create_job_execution,
    current_job_execution,
    execution_cancel_requested,
    next_attempt_number,
    transition_job_execution,
)
from app.job_outcomes import (
    JobItemOutcome,
    OutcomeChunk,
    OutcomeScope,
    ensure_outcome_manifest,
    finalize_outcome_manifest,
    find_outcome_fragment,
    publish_outcome_fragment,
    register_job_outcome_adapter,
)
from app.models import (
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskRun,
    MatterAnalysisTaskRunChunk,
    MatterAnalysisTaskVersion,
    MatterDocument,
    MatterEntity,
    MatterFact,
    MatterFactIssue,
    ReviewBatch,
    ReviewBatchDocument,
    ReviewBatchReviewerCoding,
    SkillDefinitionVersion,
    User,
    WorkflowRun,
    WorkflowStepRun,
)
from app.skill_execution import execute_skill_run
from app.workflow_specs import FACT_DEVELOPMENT_SPEC, binding_snapshot, resolve_workflow_skill_bindings

logger = logging.getLogger(__name__)

OUTCOME_FILENAME_PREFIX = "fact-development"
REPORT_SAMPLE_LIMIT = 50
RECALL_TEXT_CHARACTERS = 4_000
MAX_RECALLED_FACTS = 100
MAX_KNOWN_ENTITIES = 200
COUNT_KEYS = (
    "documents_with_facts",
    "facts_created",
    "evidence_added",
    "no_fact",
    "invalid_operations",
    "proposed_entities",
    "recall_unavailable",
    "reused",
)


class FactDevelopmentRunError(ValueError):
    def __init__(self, message: str, *, code: str = "FACT_DEVELOPMENT_RUN_INVALID") -> None:
        super().__init__(message)
        self.code = code


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Launch, retry, cancel


def _published_version(db: Session, task: MatterAnalysisTask) -> tuple[MatterAnalysisTaskVersion, FactDevelopmentSpecification]:
    if task.task_type != "FACT_DEVELOPMENT" or task.status != "ACTIVE":
        raise FactDevelopmentRunError("An active fact-development Analysis Task is required")
    if task.published_version is None:
        raise FactDevelopmentRunError("Publish the fact-development task before running it")
    version = db.scalar(
        select(MatterAnalysisTaskVersion).where(
            MatterAnalysisTaskVersion.matter_analysis_task_id == task.id,
            MatterAnalysisTaskVersion.version == task.published_version,
        )
    )
    if version is None or version.status != "PUBLISHED":
        raise FactDevelopmentRunError("The published fact-development task version is unavailable")
    try:
        specification = parse_fact_development_specification(version.fact_development_specification)
    except FactSpecificationError as exc:
        raise FactDevelopmentRunError(str(exc)) from exc
    return version, specification


def create_fact_development_run(
    db: Session,
    *,
    matter: Matter,
    task: MatterAnalysisTask,
    review_batch_id: uuid.UUID,
    coding_source: CodingSource,
    reviewer_coding_id: uuid.UUID | None,
    idempotency_key: str,
    actor_user_id: uuid.UUID,
    settings: Settings | None = None,
) -> tuple[MatterAnalysisTaskRun, bool]:
    settings = settings or get_settings()
    existing = db.scalar(
        select(MatterAnalysisTaskRun).where(
            MatterAnalysisTaskRun.matter_analysis_task_id == task.id,
            MatterAnalysisTaskRun.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.run_kind != "FACT_DEVELOPMENT":
            raise FactDevelopmentRunError("The idempotency key belongs to another kind of run")
        return existing, False
    task_version, specification = _published_version(db, task)
    batch = db.scalar(select(ReviewBatch).where(ReviewBatch.id == review_batch_id, ReviewBatch.matter_id == matter.id))
    if batch is None or batch.status != "READY":
        raise FactDevelopmentRunError("A ready review batch in this matter is required")
    reviewer_coding_revision: int | None = None
    if coding_source == "REVIEWER_CODING":
        coding = db.get(ReviewBatchReviewerCoding, reviewer_coding_id) if reviewer_coding_id is not None else None
        if coding is None or coding.review_batch_id != batch.id:
            raise FactDevelopmentRunError("Reviewer coding was not found for this review batch")
        reviewer_coding_revision = coding.revision
    elif reviewer_coding_id is not None:
        raise FactDevelopmentRunError("reviewer_coding_id applies only to the REVIEWER_CODING source")
    document_count = int(
        db.scalar(select(func.count()).where(ReviewBatchDocument.review_batch_id == batch.id)) or 0
    )
    if document_count == 0:
        raise FactDevelopmentRunError("The review batch has no documents")
    try:
        resolved = resolve_workflow_skill_bindings(
            db,
            workflow_key=FACT_DEVELOPMENT_SPEC.key,
            tenant_id=matter.client.tenant_id,
        )
    except ValueError as exc:
        raise FactDevelopmentRunError(f"Fact extraction is not configured: {exc}") from exc
    skill_version = resolved[FACT_EXTRACTION_ROLE][2]
    configuration = {
        "task_version_id": str(task_version.id),
        "task_version": task_version.version,
        "specification_hash": canonical_hash(specification.model_dump(mode="json")),
        "review_batch_id": str(batch.id),
        "coding_source": coding_source,
        "reviewer_coding_id": str(reviewer_coding_id) if reviewer_coding_id else None,
        "reviewer_coding_revision": reviewer_coding_revision,
        "chunk_size": settings.fact_development_chunk_size,
        "max_document_characters": settings.fact_development_max_document_characters,
        "recall_window_days": settings.fact_development_recall_window_days,
        "skill_definition_version_id": str(skill_version.id),
        "resolved_model": resolve_agent_model(skill_version.model_key, settings),
        "binding_snapshot": binding_snapshot(resolved),
    }
    configuration["configuration_hash"] = canonical_hash(
        {key: value for key, value in configuration.items() if key != "binding_snapshot"}
    )
    run_id = uuid.uuid4()
    run = MatterAnalysisTaskRun(
        id=run_id,
        tenant_id=matter.client.tenant_id,
        matter_id=matter.id,
        matter_analysis_task_id=task.id,
        matter_analysis_task_version_id=task_version.id,
        run_kind="FACT_DEVELOPMENT",
        scope_type="REVIEW_BATCH",
        scope_id=batch.id,
        configuration_snapshot=configuration,
        report={},
        idempotency_key=idempotency_key,
        workflow_id=f"fact-development:{run_id}",
        status="QUEUED",
        current_stage="QUEUED",
        progress_completed=0,
        progress_total=document_count,
        population_document_count=document_count,
        created_by_user_id=actor_user_id,
    )
    db.add(run)
    db.flush()
    _create_execution(db, run, matter=matter, actor_user_id=actor_user_id, idempotency_key=idempotency_key)
    record_audit(
        db,
        tenant_id=run.tenant_id,
        actor_user_id=actor_user_id,
        action="matter_analysis_task.fact_run.created",
        target_type="matter_analysis_task_run",
        target_id=run.id,
        details={
            "matter_id": str(matter.id),
            "task_id": str(task.id),
            "task_version_id": str(task_version.id),
            "review_batch_id": str(batch.id),
            "coding_source": coding_source,
            "reviewer_coding_revision": reviewer_coding_revision,
            "document_count": document_count,
        },
    )
    return run, True


def _create_execution(
    db: Session,
    run: MatterAnalysisTaskRun,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    idempotency_key: str | None = None,
    retry_of: WorkflowRun | None = None,
) -> WorkflowRun:
    configuration = run.configuration_snapshot
    execution = create_job_execution(
        db,
        job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
        job_id=run.id,
        scope=JobScope(tenant_id=run.tenant_id, client_id=matter.client_id, matter_id=matter.id),
        workflow_key=FACT_DEVELOPMENT_SPEC.key,
        code_version=FACT_DEVELOPMENT_SPEC.code_version,
        dbos_workflow_id=run.workflow_id,
        initiated_by_user_id=actor_user_id,
        idempotency_key=idempotency_key,
        current_stage="QUEUED",
        progress_total=run.population_document_count,
        progress_unit="documents",
        input_snapshot={
            "matter_analysis_task_id": str(run.matter_analysis_task_id),
            "matter_analysis_task_version_id": str(run.matter_analysis_task_version_id),
            "review_batch_id": configuration["review_batch_id"],
            "coding_source": configuration["coding_source"],
            "reviewer_coding_revision": configuration["reviewer_coding_revision"],
        },
        binding_snapshot=configuration["binding_snapshot"],
        configuration_snapshot={"resolved_models": {FACT_EXTRACTION_ROLE: configuration["resolved_model"]}},
        retry_of=retry_of,
    )
    db.add(
        WorkflowStepRun(
            workflow_run_id=execution.id,
            role_key=FACT_EXTRACTION_ROLE,
            ordinal=1,
            status="QUEUED",
            total_count=run.population_document_count,
        )
    )
    db.flush()
    return execution


def retry_fact_development_run(db: Session, run: MatterAnalysisTaskRun, *, actor_user_id: uuid.UUID) -> WorkflowRun:
    """Resume a failed run. Chunks whose outcomes were already published are not processed again.

    A canceled run is final: cancellation publishes CANCELED outcomes for every unfinished chunk.
    """

    current = current_job_execution(db, job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE, job_id=run.id, for_update=True)
    if current is None or current.status != "FAILED":
        raise JobExecutionConflict("Only failed fact-development runs can be retried")
    if not current.failure_retriable:
        raise JobExecutionConflict("This fact-development failure is not retryable")
    matter = db.get(Matter, run.matter_id)
    if matter is None:
        raise JobExecutionConflict("Analysis Task matter was not found")
    attempt = next_attempt_number(db, job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE, job_id=run.id)
    run.workflow_id = f"fact-development:{run.id}:attempt:{attempt}"
    run.failure_code = None
    run.failure_summary = None
    run.failure_retriable = False
    run.completed_at = None
    run.canceled_at = None
    db.execute(
        MatterAnalysisTaskRunChunk.__table__.update()
        .where(
            MatterAnalysisTaskRunChunk.run_id == run.id,
            MatterAnalysisTaskRunChunk.status == "RUNNING",
        )
        .values(status="QUEUED")
    )
    execution = _create_execution(db, run, matter=matter, actor_user_id=actor_user_id, retry_of=current)
    record_audit(
        db,
        tenant_id=run.tenant_id,
        actor_user_id=actor_user_id,
        action="matter_analysis_task.fact_run.retried",
        target_type="matter_analysis_task_run",
        target_id=run.id,
        details={"matter_id": str(run.matter_id), "attempt_number": execution.attempt_number},
    )
    return execution


def cancel_fact_development_run(
    db: Session,
    run: MatterAnalysisTaskRun,
    *,
    actor_user_id: uuid.UUID,
) -> tuple[WorkflowRun, bool]:
    execution, canceled = cancel_job_execution(
        db,
        job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
        job_id=run.id,
        actor_user_id=actor_user_id,
    )
    if canceled:
        db.execute(
            MatterAnalysisTaskRunChunk.__table__.update()
            .where(MatterAnalysisTaskRunChunk.run_id == run.id, MatterAnalysisTaskRunChunk.status == "QUEUED")
            .values(status="CANCELED")
        )
        record_audit(
            db,
            tenant_id=run.tenant_id,
            actor_user_id=actor_user_id,
            action="matter_analysis_task.fact_run.canceled",
            target_type="matter_analysis_task_run",
            target_id=run.id,
            details={"matter_id": str(run.matter_id)},
        )
    return execution, canceled


# Outcomes


def _outcome_scope(db: Session, run: MatterAnalysisTaskRun) -> OutcomeScope:
    matter = db.get(Matter, run.matter_id)
    if matter is None:
        raise FactDevelopmentRunError("Analysis Task matter was not found")
    configuration = run.configuration_snapshot
    return OutcomeScope(
        job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
        job_id=run.id,
        tenant_id=run.tenant_id,
        client_id=matter.client_id,
        matter_id=run.matter_id,
        actor_user_id=run.created_by_user_id,
        configuration_hash=configuration["configuration_hash"],
        total_count=run.population_document_count,
        chunk_count=math.ceil(run.population_document_count / configuration["chunk_size"]),
        item_kind="MATTER_DOCUMENT",
        filename_prefix=OUTCOME_FILENAME_PREFIX,
    )


def _outcome_chunk(chunk: MatterAnalysisTaskRunChunk) -> OutcomeChunk:
    return OutcomeChunk(
        chunk_id=str(chunk.id),
        chunk_number=chunk.chunk_number,
        ordinal_start=chunk.ordinal_start,
        document_ids=tuple(chunk.document_ids),
    )


def _chunks(db: Session, run_id: uuid.UUID) -> list[MatterAnalysisTaskRunChunk]:
    return list(
        db.scalars(
            select(MatterAnalysisTaskRunChunk)
            .where(MatterAnalysisTaskRunChunk.run_id == run_id)
            .order_by(MatterAnalysisTaskRunChunk.chunk_number)
        )
    )


class _FactDevelopmentOutcomeAdapter:
    job_type = FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE

    def load(self, db: Session, job_id: uuid.UUID) -> MatterAnalysisTaskRun | None:
        return db.scalar(
            select(MatterAnalysisTaskRun).where(
                MatterAnalysisTaskRun.id == job_id,
                MatterAnalysisTaskRun.run_kind == "FACT_DEVELOPMENT",
            )
        )

    def matter(self, db: Session, job: MatterAnalysisTaskRun) -> Matter | None:
        return db.get(Matter, job.matter_id)

    def can_read(self, db: Session, principal: Any, matter: Matter) -> bool:
        from app.authorization import can

        return can(db, principal, "analysis_task.run", ResourceRef.matter(matter.id))

    def legacy_summary(self, job: MatterAnalysisTaskRun) -> dict[str, Any] | None:
        # Every fact-development run publishes complete outcomes; there is no legacy shape.
        return None


register_job_outcome_adapter(_FactDevelopmentOutcomeAdapter())


# Prepare


def _lock_run(db: Session, run_id: uuid.UUID) -> MatterAnalysisTaskRun:
    run = db.scalar(
        select(MatterAnalysisTaskRun)
        .where(MatterAnalysisTaskRun.id == run_id, MatterAnalysisTaskRun.run_kind == "FACT_DEVELOPMENT")
        .with_for_update()
    )
    if run is None:
        raise FactDevelopmentRunError("Fact-development run was not found")
    return run


def _creator_authorized(db: Session, run: MatterAnalysisTaskRun) -> bool:
    user = db.get(User, run.created_by_user_id)
    if user is None or user.status != "ACTIVE":
        return False
    if user.tenant.is_root and user.tenant_role == "ADMIN":
        return True
    return user_can(db, user, "analysis_task.run", ResourceRef.matter(run.matter_id))


def _cancel_requested(db: Session, run_id: uuid.UUID) -> bool:
    return execution_cancel_requested(db, job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE, job_id=run_id)


def prepare_fact_development_run(db: Session, run_id: uuid.UUID) -> list[str]:
    """Freeze the documents and their matched issues into chunks; return chunk IDs that still need processing."""

    run = _lock_run(db, run_id)
    if _cancel_requested(db, run.id):
        return []
    if not _creator_authorized(db, run):
        raise FactDevelopmentRunError(
            "The run's creator is no longer authorized to run Analysis Tasks in this matter",
            code="ANALYSIS_TASK_AUTHORIZATION_REVOKED",
        )
    transition_job_execution(
        db,
        job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
        job_id=run.id,
        status="RUNNING",
        current_stage="PREPARING",
    )
    execution = current_job_execution(db, job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE, job_id=run.id)
    step = (
        db.scalar(
            select(WorkflowStepRun).where(WorkflowStepRun.workflow_run_id == execution.id, WorkflowStepRun.ordinal == 1)
        )
        if execution is not None
        else None
    )
    if step is not None and step.status == "QUEUED":
        step.status = "RUNNING"
        step.started_at = step.started_at or utcnow()
    chunks = _chunks(db, run.id)
    if not chunks:
        chunks = _freeze_chunks(db, run)
    _refresh_progress(db, run, stage="EXTRACTING")
    ensure_outcome_manifest(db, _outcome_scope(db, run))
    db.commit()
    return [str(chunk.id) for chunk in chunks if chunk.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}]


def _freeze_chunks(db: Session, run: MatterAnalysisTaskRun) -> list[MatterAnalysisTaskRunChunk]:
    configuration = run.configuration_snapshot
    task_version = db.get(MatterAnalysisTaskVersion, run.matter_analysis_task_version_id)
    if task_version is None:
        raise FactDevelopmentRunError("The pinned fact-development task version is unavailable")
    try:
        specification = parse_fact_development_specification(task_version.fact_development_specification)
    except FactSpecificationError as exc:
        raise FactDevelopmentRunError(str(exc), code="FACT_SPECIFICATION_INVALID") from exc
    if canonical_hash(specification.model_dump(mode="json")) != configuration["specification_hash"]:
        raise FactDevelopmentRunError("The pinned issue catalog changed after the run was launched")
    document_ids = list(
        db.scalars(
            select(ReviewBatchDocument.matter_document_id)
            .where(ReviewBatchDocument.review_batch_id == run.scope_id)
            .order_by(ReviewBatchDocument.sequence_number)
        )
    )
    if len(document_ids) != run.population_document_count:
        raise FactDevelopmentRunError("The review batch's documents changed after the run was launched")
    reviewer_coding_id = configuration.get("reviewer_coding_id")
    try:
        membership = evaluate_issue_membership(
            db,
            matter_id=run.matter_id,
            specification=specification,
            document_ids=document_ids,
            coding_source=configuration["coding_source"],
            reviewer_coding_id=uuid.UUID(reviewer_coding_id) if reviewer_coding_id else None,
            reviewer_coding_revision=configuration.get("reviewer_coding_revision"),
        )
    except FactIssueMembershipError as exc:
        raise FactDevelopmentRunError(str(exc), code="FACT_ISSUE_MEMBERSHIP_FAILED") from exc
    prior = _prior_fingerprints(db, run)
    chunk_size = int(configuration["chunk_size"])
    chunks: list[MatterAnalysisTaskRunChunk] = []
    for number, start in enumerate(range(0, len(document_ids), chunk_size)):
        slice_ids = document_ids[start : start + chunk_size]
        chunk = MatterAnalysisTaskRunChunk(
            run_id=run.id,
            chunk_number=number,
            ordinal_start=start,
            document_ids=[str(value) for value in slice_ids],
            issue_keys=[membership.get(value, []) for value in slice_ids],
            fingerprints=[None] * len(slice_ids),
            prior_fingerprints=[sorted(prior.get(str(value), ())) for value in slice_ids],
            item_count=len(slice_ids),
            status="QUEUED",
            processed_count=0,
            failed_count=0,
            counts={},
        )
        db.add(chunk)
        chunks.append(chunk)
    issue_counts: Counter[str] = Counter(key for keys in membership.values() for key in keys)
    run.report = {
        "matched_document_count": len(membership),
        "unmatched_document_count": len(document_ids) - len(membership),
        "issue_document_counts": [
            {"issue_key": issue.key, "name": issue.name, "document_count": issue_counts.get(issue.key, 0)}
            for issue in specification.issues
        ],
    }
    db.flush()
    return chunks


def _refresh_progress(db: Session, run: MatterAnalysisTaskRun, *, stage: str | None = None) -> None:
    processed = int(
        db.scalar(
            select(func.coalesce(func.sum(MatterAnalysisTaskRunChunk.processed_count), 0)).where(
                MatterAnalysisTaskRunChunk.run_id == run.id
            )
        )
        or 0
    )
    execution = current_job_execution(db, job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE, job_id=run.id)
    if execution is None or execution.status not in {"QUEUED", "RUNNING", "WAITING"}:
        return
    transition_job_execution(
        db,
        job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
        job_id=run.id,
        current_stage=stage,
        progress_completed=max(processed, execution.progress_completed),
    )


# Chunk processing


@dataclass
class _RunContext:
    run: MatterAnalysisTaskRun
    matter: Matter
    specification: FactDevelopmentSpecification
    workflow: WorkflowRun
    step: WorkflowStepRun
    skill_version: SkillDefinitionVersion
    model: Any

    @property
    def configuration(self) -> dict[str, Any]:
        return self.run.configuration_snapshot


def _run_context(db: Session, run: MatterAnalysisTaskRun, model: Any | None) -> _RunContext:
    matter = db.get(Matter, run.matter_id)
    task_version = db.get(MatterAnalysisTaskVersion, run.matter_analysis_task_version_id)
    workflow = current_job_execution(db, job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE, job_id=run.id)
    if matter is None or task_version is None or workflow is None:
        raise FactDevelopmentRunError("The run's matter, task version or execution is unavailable")
    step = db.scalar(
        select(WorkflowStepRun).where(WorkflowStepRun.workflow_run_id == workflow.id, WorkflowStepRun.ordinal == 1)
    )
    skill_version = db.get(SkillDefinitionVersion, uuid.UUID(run.configuration_snapshot["skill_definition_version_id"]))
    if step is None or skill_version is None:
        raise FactDevelopmentRunError("The pinned fact-extraction skill is unavailable")
    return _RunContext(
        run=run,
        matter=matter,
        specification=parse_fact_development_specification(task_version.fact_development_specification),
        workflow=workflow,
        step=step,
        skill_version=skill_version,
        model=model if model is not None else run.configuration_snapshot["resolved_model"],
    )


def _prior_fingerprints(db: Session, run: MatterAnalysisTaskRun) -> dict[str, set[str]]:
    """Fingerprints that earlier runs of the same task recorded for documents they finished, by document ID."""

    prior: dict[str, set[str]] = {}
    rows = db.execute(
        select(MatterAnalysisTaskRunChunk.document_ids, MatterAnalysisTaskRunChunk.fingerprints)
        .join(MatterAnalysisTaskRun, MatterAnalysisTaskRun.id == MatterAnalysisTaskRunChunk.run_id)
        .where(
            MatterAnalysisTaskRun.matter_analysis_task_id == run.matter_analysis_task_id,
            MatterAnalysisTaskRun.run_kind == "FACT_DEVELOPMENT",
            MatterAnalysisTaskRun.id != run.id,
        )
    )
    for document_ids, fingerprints in rows:
        for document_id, fingerprint in zip(document_ids, fingerprints or [], strict=False):
            if fingerprint:
                prior.setdefault(document_id, set()).add(fingerprint)
    return prior


def extraction_fingerprint(context: _RunContext, text: DocumentText, issues: list[str]) -> str:
    """What makes a document's extraction reusable: its exact text, paragraph map, issue catalog and coded issues.

    The skill version and model are deliberately excluded so a platform prompt or model update does not re-extract
    (and duplicate) reviewed work across a matter.
    """

    return canonical_hash(
        {
            "contract": "fact-extraction-fingerprint-v1",
            "source_content_hash": text.source_content_hash,
            "paragraph_map_version": text.paragraph_map.version,
            "specification_hash": context.configuration["specification_hash"],
            "issue_keys": sorted(issues),
        }
    )


@dataclass(frozen=True)
class _DocumentSlot:
    """One document's position in its chunk, for reading prior fingerprints and recording its own."""

    chunk: MatterAnalysisTaskRunChunk
    index: int

    def known(self) -> set[str]:
        known = set(self.chunk.prior_fingerprints[self.index]) if self.index < len(self.chunk.prior_fingerprints) else set()
        if self.index < len(self.chunk.fingerprints) and self.chunk.fingerprints[self.index]:
            known.add(self.chunk.fingerprints[self.index])
        return known

    def record(self, fingerprint: str) -> None:
        fingerprints = list(self.chunk.fingerprints or [])
        fingerprints.extend([None] * (len(self.chunk.document_ids) - len(fingerprints)))
        fingerprints[self.index] = fingerprint
        self.chunk.fingerprints = fingerprints


def _document_metadata(db: Session, context: _RunContext, document: MatterDocument) -> dict[str, Any]:
    """Read File Date and email headers through Core's Artifact gateway, as the search projection does."""

    snapshot = get_search_item_snapshot(
        collection_item_id=document.collection_item_id,
        actor_user_id=context.run.created_by_user_id,
        tenant_id=context.matter.client.tenant_id,
        client_id=context.matter.client_id,
    )
    return {
        "file_date": snapshot.file_date.isoformat() if snapshot.file_date else None,
        "original_filename": snapshot.original_filename,
        "record_type": snapshot.record_type,
        "family_id": str(snapshot.family_id) if snapshot.family_id else None,
        "custodian_ids": [str(value) for value in snapshot.custodian_ids],
        "email_from": snapshot.email_sender,
        "email_to": snapshot.email_recipients.get("TO", []),
        "email_cc": snapshot.email_recipients.get("CC", []),
        "email_bcc": snapshot.email_recipients.get("BCC", []),
        "email_subject": snapshot.email_subject,
        "email_sent_at": snapshot.email_sent_at.isoformat() if snapshot.email_sent_at else None,
    }


def _known_entities(db: Session, context: _RunContext, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    addresses = {
        value.strip().lower()
        for value in [metadata.get("email_from"), *metadata["email_to"], *metadata["email_cc"], *metadata["email_bcc"]]
        if isinstance(value, str) and value.strip()
    }
    if not addresses:
        return []
    rows = list(
        db.scalars(
            select(MatterEntity)
            .where(MatterEntity.matter_id == context.matter.id, MatterEntity.normalized_email.in_(addresses))
            .limit(MAX_KNOWN_ENTITIES)
        )
    )
    parents = {
        row.id: row
        for row in db.scalars(
            select(MatterEntity).where(MatterEntity.id.in_({row.parent_id for row in rows if row.parent_id}))
        )
    }
    known = []
    for row in rows:
        canonical = parents.get(row.parent_id, row) if row.parent_id else row
        if canonical.status == "REJECTED":
            continue
        known.append(
            {
                "name": canonical.display_name,
                "email": row.email,
                "entity_type": canonical.entity_type,
                "status": canonical.status,
            }
        )
    return known


def _recalled_facts(
    db: Session,
    context: _RunContext,
    text: DocumentText,
    issues: list[str],
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    file_date = datetime.fromisoformat(metadata["file_date"]).date() if metadata.get("file_date") else None
    recalled = recall_facts(
        db,
        matter_id=context.matter.id,
        text=text.text[:RECALL_TEXT_CHARACTERS],
        issue_keys=issues,
        event_date=file_date,
        window_days=int(context.configuration["recall_window_days"]),
        limit=min(context.specification.limits.recalled_facts_per_issue * len(issues), MAX_RECALLED_FACTS),
    )
    if not recalled:
        return []
    issue_rows = db.execute(
        select(MatterFactIssue.fact_id, MatterFactIssue.issue_key).where(
            MatterFactIssue.fact_id.in_([item.fact.id for item in recalled])
        )
    ).all()
    issues_by_fact: dict[uuid.UUID, list[str]] = {}
    for fact_id, issue_key in issue_rows:
        issues_by_fact.setdefault(fact_id, []).append(issue_key)
    return [
        {
            "fact_id": str(item.fact.id),
            "status": item.fact.status,
            "rejected": item.rejected,
            "summary": item.fact.summary,
            "date_precision": item.fact.date_precision,
            "event_date_start": item.fact.event_date_start.isoformat() if item.fact.event_date_start else None,
            "event_date_end": item.fact.event_date_end.isoformat() if item.fact.event_date_end else None,
            "issue_keys": sorted(issues_by_fact.get(item.fact.id, [])),
        }
        for item in recalled
    ]


def _specification_context(specification: FactDevelopmentSpecification) -> dict[str, Any]:
    return {
        "summary": specification.summary,
        "date_rules": specification.date_rules,
        "owner_rules": specification.owner_rules,
        "issues": [
            {"key": issue.key, "name": issue.name, "description": issue.description, "fact_guidance": issue.fact_guidance}
            for issue in specification.issues
        ],
    }


def _outcome(document_id: str, status: str, reason: str, message: str | None = None, **kwargs: Any) -> JobItemOutcome:
    return JobItemOutcome(
        item_id=document_id,
        outcome_status=status,  # type: ignore[arg-type]
        reason_code=reason,
        message=message,
        completed_at=utcnow(),
        **kwargs,
    )


def _process_document(
    db: Session,
    context: _RunContext,
    slot: _DocumentSlot,
    document_id: str,
    issues: list[str],
    counts: Counter[str],
) -> JobItemOutcome:
    started_at = utcnow()
    if not issues:
        return _outcome(document_id, "EXCLUDED", "fact.no_matching_issue", "The document is not coded to any issue")
    parsed_id = uuid.UUID(document_id)
    document = db.scalar(
        select(MatterDocument).where(MatterDocument.id == parsed_id, MatterDocument.matter_id == context.matter.id)
    )
    if document is None:
        return _outcome(document_id, "SKIPPED", "fact.document_unavailable", "The document is no longer in the matter")
    try:
        text = load_document_text(
            db,
            matter=context.matter,
            matter_document_id=parsed_id,
            actor_user_id=context.run.created_by_user_id,
        )
    except FactEvidenceError as exc:
        return _outcome(document_id, "SKIPPED", "fact.no_text_source", str(exc))
    fingerprint = extraction_fingerprint(context, text, issues)
    if fingerprint in slot.known():
        # The same text, catalog and coding were already extracted by an earlier run or attempt.
        slot.record(fingerprint)
        counts["reused"] += 1
        return _outcome(
            document_id,
            "REUSED",
            "fact.reused_prior_extraction",
            "This document was already extracted with the same text, issue catalog and coding",
            input_fingerprint=fingerprint,
        )
    max_characters = int(context.configuration["max_document_characters"])
    if len(text.text) > max_characters:
        return _outcome(
            document_id,
            "SKIPPED",
            "fact.text_too_large",
            f"The document text has {len(text.text):,} characters; the limit is {max_characters:,}",
            metrics={"characters": len(text.text)},
        )
    try:
        metadata = _document_metadata(db, context, document)
    except (ValueError, PermissionError) as exc:
        return _outcome(document_id, "FAILED", "fact.metadata_unavailable", str(exc))
    recall_unavailable = False
    try:
        recalled = _recalled_facts(db, context, text, issues, metadata)
    except Exception as exc:  # noqa: BLE001 - recall failure degrades the prompt, it must not fail the document
        logger.warning("Fact recall failed run_id=%s document_id=%s: %s", context.run.id, document_id, exc)
        db.rollback()
        recalled = []
        recall_unavailable = True
        counts["recall_unavailable"] += 1
    max_operations = context.specification.limits.max_operations_per_document
    try:
        output, skill_run = asyncio.run(
            execute_skill_run(
                db,
                workflow=context.workflow,
                step=context.step,
                skill_version=context.skill_version,
                scope_type="MATTER_DOCUMENT",
                scope_id=parsed_id,
                stable_context={"specification": _specification_context(context.specification)},
                dynamic_input={
                    "document": {
                        "matter_document_id": document_id,
                        "metadata": metadata,
                        "paragraph_map_version": text.paragraph_map.version,
                        "text": render_model_document(text.paragraph_map),
                    },
                    "matched_issues": issues,
                    "known_entities": _known_entities(db, context, metadata),
                    "recalled_facts": recalled,
                    "max_operations": max_operations,
                },
                cache_identity={
                    "tenant_id": str(context.run.tenant_id),
                    "matter_id": str(context.matter.id),
                    "task_version_id": str(context.run.matter_analysis_task_version_id),
                    "specification_hash": context.configuration["specification_hash"],
                    "skill_version_id": str(context.skill_version.id),
                },
                output_validators=(extraction_output_validator(max_operations=max_operations),),
                usage_job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
                model=context.model,
            )
        )
    except Exception as exc:  # noqa: BLE001 - one document's model failure is recorded, not fatal to the run
        logger.warning("Fact extraction failed run_id=%s document_id=%s: %s", context.run.id, document_id, exc)
        db.commit()  # Keep the failed skill run and its usage.
        return _outcome(
            document_id,
            "FAILED",
            "fact.extraction_failed",
            " ".join(str(exc).split())[:1000] or type(exc).__name__,
            started_at=started_at,
        )
    extraction = parse_extraction_output(output, max_operations=max_operations)
    result = apply_extraction(
        db,
        ExtractionContext(
            matter=context.matter,
            task_id=context.run.matter_analysis_task_id,
            document=text,
            matched_issues=tuple(issues),
            recalled_fact_ids=frozenset(uuid.UUID(item["fact_id"]) for item in recalled),
            actor=Actor(actor_type="AGENT", workflow_run_id=context.workflow.id),
        ),
        extraction,
    )
    if result.applied_count:
        status, reason, message = "SUCCEEDED", "fact.facts_proposed", None
    elif result.invalid_count and not result.no_fact_issue_keys:
        status, reason, message = "FAILED", "fact.operations_invalid", "Every proposed operation failed validation"
    else:
        status, reason, message = "SUCCEEDED", "fact.no_fact", None
    if status == "SUCCEEDED":
        # Recorded in the same transaction as the document's facts, so a crash can never separate them.
        slot.record(fingerprint)
    db.commit()
    counts["facts_created"] += len(result.created_fact_ids)
    counts["evidence_added"] += len(result.evidence_fact_ids)
    counts["no_fact"] += len(result.no_fact_issue_keys)
    counts["invalid_operations"] += result.invalid_count
    counts["proposed_entities"] += len(result.proposed_entity_ids)
    if result.applied_count:
        counts["documents_with_facts"] += 1
    metrics = {
        "operation_count": len(extraction.operations),
        "facts_created": len(result.created_fact_ids),
        "evidence_added": len(result.evidence_fact_ids),
        "invalid_operations": result.invalid_count,
        "proposed_entities": len(result.proposed_entity_ids),
        "recalled_facts": len(recalled),
        "recall_unavailable": recall_unavailable,
    }
    payload = {
        "skill_run_id": str(skill_run.id),
        "created_fact_ids": [str(value) for value in result.created_fact_ids[:REPORT_SAMPLE_LIMIT]],
        "evidence_fact_ids": [str(value) for value in result.evidence_fact_ids[:REPORT_SAMPLE_LIMIT]],
        "no_fact_issue_keys": result.no_fact_issue_keys,
        "errors": result.errors,
    }
    return _outcome(
        document_id,
        status,
        reason,
        message,
        input_fingerprint=fingerprint,
        metrics=metrics,
        payload=payload,
        started_at=started_at,
    )


def process_fact_development_chunk(
    chunk_id: uuid.UUID,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    model: Any | None = None,
) -> dict[str, Any]:
    with session_factory() as db:
        chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
        if chunk is None:
            raise FactDevelopmentRunError("Fact-development chunk was not found")
        run = db.get(MatterAnalysisTaskRun, chunk.run_id)
        if run is None:
            raise FactDevelopmentRunError("Fact-development run was not found")
        scope = _outcome_scope(db, run)
        outcome_chunk = _outcome_chunk(chunk)
        if find_outcome_fragment(scope, outcome_chunk) is not None:
            if chunk.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
                chunk.status = "COMPLETED_WITH_ERRORS" if chunk.failed_count else "COMPLETED"
                db.commit()
            return {"status": chunk.status, "processed": chunk.processed_count}
        if _cancel_requested(db, run.id):
            return {"status": "CANCELED", "processed": chunk.processed_count}
        chunk.status = "RUNNING"
        chunk.processed_count = 0
        chunk.failed_count = 0
        chunk.counts = {}
        db.commit()

        context = _run_context(db, run, model)
        counts: Counter[str] = Counter()
        outcomes: list[JobItemOutcome] = []
        canceled = False
        for index, (document_id, issues) in enumerate(zip(chunk.document_ids, chunk.issue_keys, strict=True)):
            if canceled or _cancel_requested(db, run.id):
                canceled = True
                outcomes.append(
                    _outcome(document_id, "CANCELED", "fact.run_canceled", "The run was canceled before this document")
                )
                continue
            outcome = _process_document(
                db,
                context,
                _DocumentSlot(chunk, index),
                document_id,
                list(issues),
                counts,
            )
            outcomes.append(outcome)
            chunk.processed_count += 1
            if outcome.outcome_status == "FAILED":
                chunk.failed_count += 1
            chunk.counts = dict(counts)
            _refresh_progress(db, run)
            db.commit()

        publish_outcome_fragment(db, scope=scope, chunk=outcome_chunk, outcomes=outcomes)
        if canceled:
            chunk.status = "CANCELED"
        else:
            chunk.status = "COMPLETED_WITH_ERRORS" if chunk.failed_count else "COMPLETED"
        db.commit()
        return {"status": chunk.status, "processed": chunk.processed_count, **dict(counts)}


# Finalize


def _canceled_outcomes(chunk: MatterAnalysisTaskRunChunk) -> list[JobItemOutcome]:
    return [
        _outcome(document_id, "CANCELED", "fact.run_canceled", "The run was canceled before this document")
        for document_id in chunk.document_ids
    ]


def _report(db: Session, run: MatterAnalysisTaskRun, context_execution_ids: list[uuid.UUID], manifest: Any) -> dict:
    chunks = _chunks(db, run.id)
    counts: Counter[str] = Counter()
    for chunk in chunks:
        counts.update({key: int(value) for key, value in (chunk.counts or {}).items()})
    facts_by_issue = dict(
        db.execute(
            select(MatterFactIssue.issue_key, func.count(MatterFactIssue.fact_id))
            .join(MatterFact, MatterFact.id == MatterFactIssue.fact_id)
            .where(MatterFact.origin_workflow_run_id.in_(context_execution_ids))
            .group_by(MatterFactIssue.issue_key)
        ).all()
    )
    pending_entities = int(
        db.scalar(
            select(func.count()).where(
                MatterEntity.created_by_workflow_run_id.in_(context_execution_ids),
                MatterEntity.parent_id.is_(None),
                MatterEntity.status == "PROPOSED",
            )
        )
        or 0
    )
    return {
        **run.report,
        "status_counts": dict(manifest.status_counts),
        "reason_counts": dict(manifest.reason_counts),
        "counts": {key: counts.get(key, 0) for key in COUNT_KEYS},
        "facts_by_issue": [{"issue_key": key, "fact_count": int(value)} for key, value in sorted(facts_by_issue.items())],
        "qc": {
            "documents_without_facts": int(manifest.status_counts.get("SUCCEEDED", 0)) - counts.get("documents_with_facts", 0),
            "invalid_operations": counts.get("invalid_operations", 0),
            "entities_awaiting_confirmation": pending_entities,
            "recall_unavailable": counts.get("recall_unavailable", 0),
        },
    }


def _execution_ids(db: Session, run_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        db.scalars(
            select(WorkflowRun.id).where(
                WorkflowRun.job_type == FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
                WorkflowRun.job_id == run_id,
            )
        )
    )


def finalize_fact_development_run(db: Session, run_id: uuid.UUID) -> MatterAnalysisTaskRun:
    run = _lock_run(db, run_id)
    if _cancel_requested(db, run.id):
        finalize_canceled_fact_development_run(db, run)
        return run
    chunks = _chunks(db, run.id)
    incomplete = [chunk.chunk_number for chunk in chunks if chunk.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}]
    if incomplete:
        raise FactDevelopmentRunError(f"Chunks did not finish: {incomplete[:10]}")
    transition_job_execution(
        db,
        job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
        job_id=run.id,
        current_stage="FINALIZING",
    )
    manifest = finalize_outcome_manifest(
        db,
        scope=_outcome_scope(db, run),
        chunks=[_outcome_chunk(chunk) for chunk in chunks],
    )
    run.report = _report(db, run, _execution_ids(db, run.id), manifest)
    failed = int(manifest.status_counts.get("FAILED", 0))
    final_status = "COMPLETED_WITH_ERRORS" if failed else "COMPLETED"
    execution = current_job_execution(db, job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE, job_id=run.id)
    step = (
        db.scalar(
            select(WorkflowStepRun).where(WorkflowStepRun.workflow_run_id == execution.id, WorkflowStepRun.ordinal == 1)
        )
        if execution is not None
        else None
    )
    if step is not None:
        step.status = final_status
        step.completed_count = run.population_document_count - failed
        step.failed_count = failed
        step.completed_at = utcnow()
    transition_job_execution(
        db,
        job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
        job_id=run.id,
        status=final_status,
        current_stage=final_status,
        progress_completed=run.population_document_count,
        progress={"counts": run.report["counts"], "status_counts": run.report["status_counts"]},
        failure_code="FACT_DEVELOPMENT_DOCUMENT_FAILURES" if failed else None,
        failure_summary=f"{failed} documents failed fact extraction" if failed else None,
    )
    db.commit()
    return run


def finalize_canceled_fact_development_run(db: Session, run: MatterAnalysisTaskRun) -> None:
    """Fill unpublished chunks with CANCELED outcomes and publish the final outcome manifest."""

    scope = _outcome_scope(db, run)
    chunks = _chunks(db, run.id)
    manifest = ensure_outcome_manifest(db, scope)
    if manifest.manifest_status == "FINALIZED":
        return
    for chunk in chunks:
        outcome_chunk = _outcome_chunk(chunk)
        if find_outcome_fragment(scope, outcome_chunk) is not None:
            continue
        publish_outcome_fragment(db, scope=scope, chunk=outcome_chunk, outcomes=_canceled_outcomes(chunk))
        chunk.status = "CANCELED"
    manifest = finalize_outcome_manifest(db, scope=scope, chunks=[_outcome_chunk(chunk) for chunk in chunks])
    run.report = _report(db, run, _execution_ids(db, run.id), manifest)
    db.commit()


def fail_fact_development_run(db: Session, run_id: uuid.UUID, message: str, *, code: str, retriable: bool) -> None:
    run = db.get(MatterAnalysisTaskRun, run_id)
    if run is None or run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}:
        return
    transition_job_execution(
        db,
        job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
        job_id=run.id,
        status="FAILED",
        current_stage="FAILED",
        failure_code=code,
        failure_summary=message,
        failure_retriable=retriable,
    )
    db.commit()
