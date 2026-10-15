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

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent_models import resolve_agent_model
from app.analysis_input_manifests import (
    AnalysisInputContract,
    AnalysisInputDocument,
    AnalysisInputExtensionField,
    AnalysisInputManifestError,
    AnalysisInputManifestReader,
    prepare_analysis_input_manifest,
    publish_analysis_input_manifest,
)
from app.artifact_gateway import (
    AnalysisInputItemSnapshot,
    TextSourceReference,
    get_collection_item_analysis_input_snapshots,
    read_text_source,
)
from app.audit import record_audit
from app.authorization import ResourceRef, user_can
from app.config import Settings, get_settings
from app.database import SessionLocal
from app.document_evidence import PARAGRAPH_MAP_VERSION, render_model_document
from app.embeddings.configuration import canonical_hash
from app.fact_consolidation import (
    ConsolidationSettings,
    consolidation_validator,
    decide,
    late_candidates,
    lock_issue_partitions,
)
from app.fact_development_skills import FACT_CONSOLIDATION_ROLE, FACT_EXTRACTION_ROLE
from app.fact_evidence import DocumentText, document_text_from_source
from app.fact_extraction import (
    CreateDecision,
    ExtractionContext,
    ExtractionOperation,
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
    AnalysisInputManifest,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskRun,
    MatterAnalysisTaskRunChunk,
    MatterAnalysisTaskVersion,
    MatterDocument,
    MatterDocumentCustodian,
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
INPUT_MANIFEST_OWNER_TYPE = "MATTER_ANALYSIS_TASK_RUN"
INPUT_CONTRACT_KEY = "fact-development.population"
INPUT_CONTRACT_VERSION = 1
INPUT_RESOLVER_VERSION = "fact-development-population-resolver-v1"
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
    "merged_into_existing",
    "blocked_by_rejected",
    "refined",
    "suggestions_created",
    "duplicate_check_unavailable",
    "duplicate_rechecks",
)
MAX_DUPLICATE_RECHECKS = 2


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
    consolidation_version = resolved[FACT_CONSOLIDATION_ROLE][2]
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
        "chunk_window": settings.fact_development_chunk_window,
        "duplicate_threshold": settings.fact_duplicate_threshold,
        "skill_definition_version_id": str(skill_version.id),
        "resolved_model": resolve_agent_model(skill_version.model_key, settings),
        "consolidation_skill_definition_version_id": str(consolidation_version.id),
        "consolidation_model": resolve_agent_model(consolidation_version.model_key, settings),
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
        configuration_snapshot={
            "resolved_models": {
                FACT_EXTRACTION_ROLE: configuration["resolved_model"],
                FACT_CONSOLIDATION_ROLE: configuration["consolidation_model"],
            }
        },
        retry_of=retry_of,
    )
    db.add_all(
        [
            WorkflowStepRun(
                workflow_run_id=execution.id,
                role_key=FACT_EXTRACTION_ROLE,
                ordinal=1,
                status="QUEUED",
                total_count=run.population_document_count,
            ),
            WorkflowStepRun(
                workflow_run_id=execution.id,
                role_key=FACT_CONSOLIDATION_ROLE,
                ordinal=2,
                status="QUEUED",
                total_count=0,
            ),
        ]
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


def _outcome_chunk(chunk: MatterAnalysisTaskRunChunk, rows: list[dict[str, Any]]) -> OutcomeChunk:
    return OutcomeChunk(
        chunk_id=str(chunk.id),
        chunk_number=chunk.chunk_number,
        ordinal_start=chunk.ordinal_start,
        document_ids=tuple(row["matter_document_id"] for row in rows),
    )


def _manifest_reader(db: Session, run: MatterAnalysisTaskRun) -> AnalysisInputManifestReader:
    manifest = db.get(AnalysisInputManifest, run.analysis_input_manifest_id) if run.analysis_input_manifest_id else None
    if (
        manifest is None
        or manifest.owner_type != INPUT_MANIFEST_OWNER_TYPE
        or manifest.owner_id != run.id
        or manifest.matter_id != run.matter_id
        or manifest.item_count != run.population_document_count
        or manifest.scope_type != "REVIEW_BATCH"
        or manifest.scope_id != run.scope_id
    ):
        raise FactDevelopmentRunError(
            "The run's input manifest does not match the run",
            code="INPUT_MANIFEST_IDENTITY_MISMATCH",
        )
    contract = manifest.input_contract or {}
    if (
        contract.get("key") != INPUT_CONTRACT_KEY
        or contract.get("version") != INPUT_CONTRACT_VERSION
        or manifest.resolver_version != INPUT_RESOLVER_VERSION
    ):
        raise FactDevelopmentRunError(
            "This worker does not support the run's input contract version",
            code="INPUT_CONTRACT_UNSUPPORTED",
        )
    try:
        return AnalysisInputManifestReader(manifest, actor_user_id=run.created_by_user_id)
    except AnalysisInputManifestError as exc:
        raise FactDevelopmentRunError(str(exc), code="INPUT_MANIFEST_INVALID") from exc


def _chunk_rows(reader: AnalysisInputManifestReader, chunk: MatterAnalysisTaskRunChunk) -> list[dict[str, Any]]:
    try:
        return reader.rows(chunk.ordinal_start, chunk.item_count)
    except AnalysisInputManifestError as exc:
        raise FactDevelopmentRunError(str(exc), code="INPUT_MANIFEST_INVALID") from exc


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
    """Freeze the population into the run's input manifest and plan chunks; return chunk IDs still to process."""

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
    if run.analysis_input_manifest_id is None:
        # Release the run lock while the batch is resolved so a cancel request is not blocked behind the freeze.
        db.commit()
        run = db.get(MatterAnalysisTaskRun, run_id)
        _freeze_input_manifest(db, run)
        db.commit()
        run = _lock_run(db, run_id)
        if _cancel_requested(db, run.id):
            return []
    chunks = _chunks(db, run.id)
    if not chunks:
        chunks = _plan_chunks(db, run)
    _refresh_progress(db, run, stage="EXTRACTING")
    ensure_outcome_manifest(db, _outcome_scope(db, run))
    db.commit()
    # Chunks are returned in manifest (chronological) order; the workflow dispatches them in this order.
    return [str(chunk.id) for chunk in chunks if chunk.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}]


def run_ordered_window(
    chunk_ids: list[str],
    window: int,
    start: Callable[[str], Any],
    wait: Callable[[Any], Any],
) -> None:
    """Start chunks in order with at most ``window`` in flight; the next starts when the oldest in flight finishes.

    Earlier (chronologically older) chunks therefore always finish before much later ones begin, so later documents
    can recall facts created from earlier ones, without a barrier that idles workers between groups.
    """

    handles = [start(chunk_id) for chunk_id in chunk_ids[:window]]
    for position in range(len(chunk_ids)):
        wait(handles[position])
        following = position + window
        if following < len(chunk_ids):
            handles.append(start(chunk_ids[following]))


def chunk_window(db: Session, run_id: uuid.UUID) -> int:
    run = db.get(MatterAnalysisTaskRun, run_id)
    if run is None:
        raise FactDevelopmentRunError("Fact-development run was not found")
    return max(1, int(run.configuration_snapshot.get("chunk_window", 1)))


def extraction_fingerprint(
    *,
    matter_document_id: str,
    source_content_hash: str,
    specification_hash: str,
    issue_keys: list[str],
) -> str:
    """What makes a document's extraction reusable: the document, its exact text, the catalog and its coded issues.

    The skill version and model are deliberately excluded so a platform prompt or model update does not re-extract
    (and duplicate) reviewed work across a matter.
    """

    return canonical_hash(
        {
            "contract": "fact-extraction-fingerprint-v2",
            "matter_document_id": matter_document_id,
            "source_content_hash": source_content_hash,
            "paragraph_map_version": PARAGRAPH_MAP_VERSION,
            "specification_hash": specification_hash,
            "issue_keys": sorted(issue_keys),
        }
    )


def _input_contract(run: MatterAnalysisTaskRun) -> AnalysisInputContract:
    configuration = run.configuration_snapshot
    return AnalysisInputContract(
        key=INPUT_CONTRACT_KEY,
        version=INPUT_CONTRACT_VERSION,
        resolver_version=INPUT_RESOLVER_VERSION,
        required_input_paths=["issue_keys", "fingerprint", "metadata"],
        source_artifact_required=True,
        extension_fields=[
            AnalysisInputExtensionField(name="family_id", type="STRING"),
            AnalysisInputExtensionField(name="file_date", type="STRING"),
        ],
        options={
            "specification_hash": configuration["specification_hash"],
            "coding_source": configuration["coding_source"],
            "reviewer_coding_revision": configuration.get("reviewer_coding_revision"),
            "paragraph_map_version": PARAGRAPH_MAP_VERSION,
        },
    )


def _finished_fingerprints(db: Session, run: MatterAnalysisTaskRun) -> set[str]:
    """Fingerprints of documents that earlier runs of the same task finished."""

    finished: set[str] = set()
    for fingerprints in db.scalars(
        select(MatterAnalysisTaskRunChunk.fingerprints)
        .join(MatterAnalysisTaskRun, MatterAnalysisTaskRun.id == MatterAnalysisTaskRunChunk.run_id)
        .where(
            MatterAnalysisTaskRun.matter_analysis_task_id == run.matter_analysis_task_id,
            MatterAnalysisTaskRun.run_kind == "FACT_DEVELOPMENT",
            MatterAnalysisTaskRun.id != run.id,
        )
    ):
        finished.update(fingerprints or [])
    return finished


def _document_metadata(
    snapshot: AnalysisInputItemSnapshot,
    custodian_ids: list[str],
) -> dict[str, Any]:
    return {
        "file_date": snapshot.file_date.isoformat() if snapshot.file_date else None,
        "original_filename": snapshot.original_filename,
        "record_type": snapshot.record_type,
        "family_id": str(snapshot.family_id) if snapshot.family_id else None,
        "custodian_ids": custodian_ids,
        "email_from": snapshot.email_sender,
        "email_to": snapshot.email_recipients.get("TO", []),
        "email_cc": snapshot.email_recipients.get("CC", []),
        "email_bcc": snapshot.email_recipients.get("BCC", []),
        "email_subject": snapshot.email_subject,
        "email_sent_at": snapshot.email_sent_at.isoformat() if snapshot.email_sent_at else None,
    }


def _chronological_key(
    sequence: int,
    snapshot: AnalysisInputItemSnapshot | None,
    item_id: uuid.UUID,
) -> tuple[Any, ...]:
    # Oldest File Date first and undated last. Attachments take their parent's File Date, so grouping ties by
    # family keeps a family together; batch order breaks any remaining tie.
    file_date = snapshot.file_date if snapshot is not None else None
    family = str(snapshot.family_id) if snapshot is not None and snapshot.family_id else str(item_id)
    return (file_date is None, file_date.isoformat() if file_date else "", family, sequence)


def _freeze_input_manifest(db: Session, run: MatterAnalysisTaskRun) -> AnalysisInputManifest:
    """Resolve every batch document once, in chronological order, into the run's immutable input manifest."""

    configuration = run.configuration_snapshot
    matter = db.get(Matter, run.matter_id)
    task_version = db.get(MatterAnalysisTaskVersion, run.matter_analysis_task_version_id)
    if matter is None or task_version is None:
        raise FactDevelopmentRunError("The pinned fact-development task version is unavailable")
    try:
        specification = parse_fact_development_specification(task_version.fact_development_specification)
    except FactSpecificationError as exc:
        raise FactDevelopmentRunError(str(exc), code="FACT_SPECIFICATION_INVALID") from exc
    if canonical_hash(specification.model_dump(mode="json")) != configuration["specification_hash"]:
        raise FactDevelopmentRunError("The pinned issue catalog changed after the run was launched")
    members = db.execute(
        select(MatterDocument, ReviewBatchDocument.sequence_number)
        .join(ReviewBatchDocument, ReviewBatchDocument.matter_document_id == MatterDocument.id)
        .where(ReviewBatchDocument.review_batch_id == run.scope_id, MatterDocument.matter_id == run.matter_id)
        .order_by(ReviewBatchDocument.sequence_number)
    ).all()
    if len(members) != run.population_document_count:
        raise FactDevelopmentRunError("The review batch's documents changed after the run was launched")
    document_ids = [document.id for document, _ in members]
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
    try:
        snapshots = get_collection_item_analysis_input_snapshots(
            item_ids=[document.collection_item_id for document, _ in members],
            actor_user_id=run.created_by_user_id,
            tenant_id=run.tenant_id,
            client_id=matter.client_id,
        )
    except (httpx.HTTPError, PermissionError, ValueError) as exc:
        raise FactDevelopmentRunError(f"Document inputs are unavailable: {exc}", code="INPUT_RESOLUTION_FAILED") from exc
    custodians: dict[uuid.UUID, list[str]] = {}
    for document_id, custodian_id in db.execute(
        select(MatterDocumentCustodian.matter_document_id, MatterDocumentCustodian.custodian_id)
        .where(MatterDocumentCustodian.matter_document_id.in_(document_ids))
        .order_by(MatterDocumentCustodian.matter_document_id, MatterDocumentCustodian.custodian_id)
    ):
        custodians.setdefault(document_id, []).append(str(custodian_id))
    finished = _finished_fingerprints(db, run)
    ordered = sorted(
        members,
        key=lambda member: _chronological_key(
            member[1], snapshots.get(member[0].collection_item_id), member[0].collection_item_id
        ),
    )
    rows: list[AnalysisInputDocument] = []
    status_counts: Counter[str] = Counter()
    reused = 0
    for position, (document, _) in enumerate(ordered, start=1):
        snapshot = snapshots.get(document.collection_item_id)
        issue_keys = membership.get(document.id, [])
        extensions = {
            "family_id": str(snapshot.family_id) if snapshot is not None and snapshot.family_id else None,
            "file_date": snapshot.file_date.isoformat() if snapshot is not None and snapshot.file_date else None,
        }
        base = {
            "matter_document_id": document.id,
            "collection_item_id": document.collection_item_id,
            "source_collection_id": document.source_collection_id,
            "scope_ordinal": position,
            "extensions": extensions,
        }
        if not issue_keys:
            row = AnalysisInputDocument(**base, input_status="EXCLUDED", failure_code="fact.no_matching_issue")
        elif snapshot is None:
            row = AnalysisInputDocument(
                **base,
                input_status="MISSING",
                failure_code="fact.item_unavailable",
                resolved_inputs={"issue_keys": issue_keys},
            )
        elif snapshot.text_source is None:
            row = AnalysisInputDocument(
                **base,
                input_status="MISSING",
                failure_code="fact.no_text_source",
                resolved_inputs={"issue_keys": issue_keys},
            )
        else:
            fingerprint = extraction_fingerprint(
                matter_document_id=str(document.id),
                source_content_hash=snapshot.text_source.content_hash,
                specification_hash=configuration["specification_hash"],
                issue_keys=issue_keys,
            )
            prior_extraction = fingerprint in finished
            reused += prior_extraction
            row = AnalysisInputDocument(
                **base,
                source_artifact_id=snapshot.text_source.artifact_id,
                source_content_hash=snapshot.text_source.content_hash,
                resolved_inputs={
                    "issue_keys": issue_keys,
                    "fingerprint": fingerprint,
                    "prior_extraction": prior_extraction,
                    "metadata": _document_metadata(snapshot, custodians.get(document.id, [])),
                    "text_source": {
                        "role": snapshot.text_source.role,
                        "media_type": snapshot.text_source.media_type,
                        "original_filename": snapshot.text_source.original_filename,
                        "byte_length": snapshot.text_source.byte_length,
                        "record_type": snapshot.record_type,
                    },
                },
            )
        status_counts[row.input_status] += 1
        rows.append(row)
    db.expire_all()  # Observe a cancel committed while the batch was being resolved.
    if _cancel_requested(db, run.id):
        # Resolution can take a while for a large batch; do not publish a manifest for a canceled run.
        raise FactDevelopmentRunError("The run was canceled while its population was being frozen", code="CANCELED")
    try:
        prepared = prepare_analysis_input_manifest(
            rows,
            contract=_input_contract(run),
            scope_type="REVIEW_BATCH",
            scope_id=run.scope_id,
        )
        manifest, _ = publish_analysis_input_manifest(
            db,
            prepared=prepared,
            tenant_id=run.tenant_id,
            client_id=matter.client_id,
            matter_id=run.matter_id,
            owner_type=INPUT_MANIFEST_OWNER_TYPE,
            owner_id=run.id,
            producer_kind="CORE",
            producer_run_id=run.id,
            actor_user_id=run.created_by_user_id,
        )
    except AnalysisInputManifestError as exc:
        raise FactDevelopmentRunError(str(exc), code="INPUT_MANIFEST_INVALID") from exc
    run.analysis_input_manifest_id = manifest.id
    issue_counts: Counter[str] = Counter(key for keys in membership.values() for key in keys)
    run.report = {
        "matched_document_count": len(membership),
        "unmatched_document_count": len(document_ids) - len(membership),
        "input_status_counts": dict(status_counts),
        "previously_extracted_count": reused,
        "issue_document_counts": [
            {"issue_key": issue.key, "name": issue.name, "document_count": issue_counts.get(issue.key, 0)}
            for issue in specification.issues
        ],
    }
    db.flush()
    return manifest


def _plan_chunks(db: Session, run: MatterAnalysisTaskRun) -> list[MatterAnalysisTaskRunChunk]:
    chunk_size = int(run.configuration_snapshot["chunk_size"])
    chunks = []
    for number, start in enumerate(range(0, run.population_document_count, chunk_size)):
        chunk = MatterAnalysisTaskRunChunk(
            run_id=run.id,
            chunk_number=number,
            ordinal_start=start,
            item_count=min(chunk_size, run.population_document_count - start),
            fingerprints=[],
            status="QUEUED",
            processed_count=0,
            failed_count=0,
            counts={},
        )
        db.add(chunk)
        chunks.append(chunk)
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
    consolidation_step: WorkflowStepRun
    consolidation_skill_version: SkillDefinitionVersion
    consolidation_model: Any

    @property
    def configuration(self) -> dict[str, Any]:
        return self.run.configuration_snapshot


def _run_context(
    db: Session,
    run: MatterAnalysisTaskRun,
    model: Any | None,
    consolidation_model: Any | None = None,
) -> _RunContext:
    matter = db.get(Matter, run.matter_id)
    task_version = db.get(MatterAnalysisTaskVersion, run.matter_analysis_task_version_id)
    workflow = current_job_execution(db, job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE, job_id=run.id)
    if matter is None or task_version is None or workflow is None:
        raise FactDevelopmentRunError("The run's matter, task version or execution is unavailable")
    step = db.scalar(
        select(WorkflowStepRun).where(WorkflowStepRun.workflow_run_id == workflow.id, WorkflowStepRun.ordinal == 1)
    )
    skill_version = db.get(SkillDefinitionVersion, uuid.UUID(run.configuration_snapshot["skill_definition_version_id"]))
    consolidation_step = db.scalar(
        select(WorkflowStepRun).where(WorkflowStepRun.workflow_run_id == workflow.id, WorkflowStepRun.ordinal == 2)
    )
    consolidation_version = db.get(
        SkillDefinitionVersion,
        uuid.UUID(run.configuration_snapshot["consolidation_skill_definition_version_id"]),
    )
    if step is None or skill_version is None or consolidation_step is None or consolidation_version is None:
        raise FactDevelopmentRunError("The pinned fact-extraction skills are unavailable")
    return _RunContext(
        run=run,
        matter=matter,
        specification=parse_fact_development_specification(task_version.fact_development_specification),
        workflow=workflow,
        step=step,
        skill_version=skill_version,
        model=model if model is not None else run.configuration_snapshot["resolved_model"],
        consolidation_step=consolidation_step,
        consolidation_skill_version=consolidation_version,
        consolidation_model=(
            consolidation_model if consolidation_model is not None else run.configuration_snapshot["consolidation_model"]
        ),
    )


def _record_fingerprint(chunk: MatterAnalysisTaskRunChunk, fingerprint: str) -> None:
    if fingerprint not in (chunk.fingerprints or []):
        chunk.fingerprints = [*(chunk.fingerprints or []), fingerprint]


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


def _ask_consolidator(
    db: Session,
    context: _RunContext,
    document_id: str,
    proposed: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    output, _ = asyncio.run(
        execute_skill_run(
            db,
            workflow=context.workflow,
            step=context.consolidation_step,
            skill_version=context.consolidation_skill_version,
            scope_type="MATTER_DOCUMENT",
            scope_id=uuid.UUID(document_id),
            stable_context={},
            dynamic_input={"proposed": proposed, "candidates": candidates},
            cache_identity={
                "tenant_id": str(context.run.tenant_id),
                "matter_id": str(context.matter.id),
                "skill_version_id": str(context.consolidation_skill_version.id),
            },
            output_validators=(consolidation_validator,),
            usage_job_type=FACT_DEVELOPMENT_ANALYSIS_TASK_JOB_TYPE,
            model=context.consolidation_model,
        )
    )
    return output


def _consolidate_creates(
    db: Session,
    context: _RunContext,
    document_id: str,
    operations: list[ExtractionOperation],
    issues: list[str],
    counts: Counter[str],
) -> dict[int, CreateDecision]:
    """Rule on every CREATE, then lock its issues and recheck for facts created concurrently since the ruling.

    Model calls happen without locks held. If a close fact appeared after a ruling, the locks are released and that
    CREATE is ruled again; after the bounded retries it is ruled while the locks are held. The locks stay held until
    the caller commits the document's writes.
    """

    settings = ConsolidationSettings(
        matter_id=context.matter.id,
        task_id=context.run.matter_analysis_task_id,
        threshold=float(context.configuration.get("duplicate_threshold", 0.85)),
        window_days=int(context.configuration["recall_window_days"]),
    )
    # Only CREATEs that could apply are ruled on; ones outside the document's coding fail validation anyway.
    creates = {
        index: operation
        for index, operation in enumerate(operations)
        if operation.op == "CREATE"
        and operation.summary
        and operation.evidence
        and operation.issue_keys
        and set(operation.issue_keys) <= set(issues)
    }
    if not creates:
        return {}

    def ask(proposed: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
        return _ask_consolidator(db, context, document_id, proposed, candidates)

    def rule(operation: ExtractionOperation) -> CreateDecision:
        try:
            return decide(db, settings, operation, ask)
        except Exception as exc:  # noqa: BLE001 - an unavailable duplicate check must not lose the fact
            logger.warning("Duplicate check failed run_id=%s document_id=%s: %s", context.run.id, document_id, exc)
            db.rollback()
            counts["duplicate_check_unavailable"] += 1
            return CreateDecision("CREATE", rationale="Duplicate check unavailable")

    decided_at = utcnow()
    decisions = {index: rule(operation) for index, operation in creates.items()}
    db.commit()  # Keep consolidation skill runs; nothing else is pending.
    for attempt in range(MAX_DUPLICATE_RECHECKS + 1):
        to_create = {index: creates[index] for index, decision in decisions.items() if decision.action == "CREATE"}
        lock_issue_partitions(db, settings, (key for operation in to_create.values() for key in operation.issue_keys))
        late = [
            index
            for index, operation in to_create.items()
            if late_candidates(db, settings, operation, decisions[index], decided_at=decided_at)
        ]
        if not late:
            break
        counts["duplicate_rechecks"] += 1
        if attempt == MAX_DUPLICATE_RECHECKS:
            for index in late:
                decisions[index] = rule(creates[index])
            break
        db.commit()  # Release the locks before calling the model again.
        decided_at = utcnow()
        for index in late:
            decisions[index] = rule(creates[index])
        db.commit()
    return decisions


def _process_document(
    db: Session,
    context: _RunContext,
    chunk: MatterAnalysisTaskRunChunk,
    row: dict[str, Any],
    counts: Counter[str],
) -> JobItemOutcome:
    started_at = utcnow()
    document_id = row["matter_document_id"]
    inputs = row["resolved_inputs"]
    if row["input_status"] == "EXCLUDED":
        return _outcome(document_id, "EXCLUDED", row["failure_code"], "The document is not coded to any issue")
    if row["input_status"] != "READY":
        return _outcome(document_id, "SKIPPED", row["failure_code"], "The document had no usable text when frozen")
    issues = list(inputs["issue_keys"])
    fingerprint = inputs["fingerprint"]
    if inputs.get("prior_extraction") or fingerprint in (chunk.fingerprints or []):
        # The same document, text, catalog and coding were already extracted by an earlier run or attempt.
        _record_fingerprint(chunk, fingerprint)
        counts["reused"] += 1
        return _outcome(
            document_id,
            "REUSED",
            "fact.reused_prior_extraction",
            "This document was already extracted with the same text, issue catalog and coding",
            input_fingerprint=fingerprint,
        )
    source = inputs["text_source"]
    max_characters = int(context.configuration["max_document_characters"])
    try:
        raw_text = read_text_source(
            TextSourceReference(
                artifact_id=uuid.UUID(row["source_artifact_id"]),
                role=source["role"],
                media_type=source["media_type"],
                original_filename=source["original_filename"],
                content_hash=row["source_content_hash"],
                byte_length=int(source["byte_length"]),
            ),
            record_type=source["record_type"],
            actor_user_id=context.run.created_by_user_id,
            tenant_id=context.run.tenant_id,
            client_id=context.matter.client_id,
        )
    except (httpx.HTTPError, PermissionError, ValueError) as exc:
        return _outcome(document_id, "FAILED", "fact.text_unavailable", str(exc)[:1000], input_fingerprint=fingerprint)
    if raw_text is None:
        return _outcome(document_id, "SKIPPED", "fact.no_text_source", "The pinned text source has no text")
    if len(raw_text) > max_characters:
        return _outcome(
            document_id,
            "SKIPPED",
            "fact.text_too_large",
            f"The document text has {len(raw_text):,} characters; the limit is {max_characters:,}",
            metrics={"characters": len(raw_text)},
        )
    text = document_text_from_source(
        matter_document_id=uuid.UUID(document_id),
        source_artifact_id=uuid.UUID(row["source_artifact_id"]),
        source_content_hash=row["source_content_hash"],
        text=raw_text,
    )
    parsed_id = uuid.UUID(document_id)
    metadata = inputs["metadata"]
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
    decisions = _consolidate_creates(db, context, document_id, extraction.operations, issues, counts)
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
        decisions,
    )
    if result.applied_count:
        status, reason, message = "SUCCEEDED", "fact.facts_proposed", None
    elif result.invalid_count and not result.no_fact_issue_keys and not result.blocked_count:
        status, reason, message = "FAILED", "fact.operations_invalid", "Every proposed operation failed validation"
    else:
        status, reason, message = "SUCCEEDED", "fact.no_fact", None
    if status == "SUCCEEDED":
        # Recorded in the same transaction as the document's facts, so a crash can never separate them.
        _record_fingerprint(chunk, fingerprint)
    db.commit()
    counts["facts_created"] += len(result.created_fact_ids)
    counts["evidence_added"] += len(result.evidence_fact_ids)
    counts["no_fact"] += len(result.no_fact_issue_keys)
    counts["invalid_operations"] += result.invalid_count
    counts["proposed_entities"] += len(result.proposed_entity_ids)
    counts["merged_into_existing"] += len(result.merged_fact_ids)
    counts["blocked_by_rejected"] += result.blocked_count
    counts["refined"] += len(result.refined_fact_ids)
    counts["suggestions_created"] += len(result.suggestion_ids)
    if result.applied_count:
        counts["documents_with_facts"] += 1
    metrics = {
        "operation_count": len(extraction.operations),
        "facts_created": len(result.created_fact_ids),
        "evidence_added": len(result.evidence_fact_ids),
        "invalid_operations": result.invalid_count,
        "proposed_entities": len(result.proposed_entity_ids),
        "merged_into_existing": len(result.merged_fact_ids),
        "blocked_by_rejected": result.blocked_count,
        "refined": len(result.refined_fact_ids),
        "suggestions_created": len(result.suggestion_ids),
        "recalled_facts": len(recalled),
        "recall_unavailable": recall_unavailable,
    }
    payload = {
        "skill_run_id": str(skill_run.id),
        "created_fact_ids": [str(value) for value in result.created_fact_ids[:REPORT_SAMPLE_LIMIT]],
        "evidence_fact_ids": [str(value) for value in result.evidence_fact_ids[:REPORT_SAMPLE_LIMIT]],
        "merged_fact_ids": [str(value) for value in result.merged_fact_ids[:REPORT_SAMPLE_LIMIT]],
        "refined_fact_ids": [str(value) for value in result.refined_fact_ids[:REPORT_SAMPLE_LIMIT]],
        "suggestion_ids": [str(value) for value in result.suggestion_ids[:REPORT_SAMPLE_LIMIT]],
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
    consolidation_model: Any | None = None,
) -> dict[str, Any]:
    with session_factory() as db:
        chunk = db.get(MatterAnalysisTaskRunChunk, chunk_id)
        if chunk is None:
            raise FactDevelopmentRunError("Fact-development chunk was not found")
        run = db.get(MatterAnalysisTaskRun, chunk.run_id)
        if run is None:
            raise FactDevelopmentRunError("Fact-development run was not found")
        scope = _outcome_scope(db, run)
        rows = _chunk_rows(_manifest_reader(db, run), chunk)
        outcome_chunk = _outcome_chunk(chunk, rows)
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

        context = _run_context(db, run, model, consolidation_model)
        counts: Counter[str] = Counter()
        outcomes: list[JobItemOutcome] = []
        canceled = False
        for row in rows:
            if canceled or _cancel_requested(db, run.id):
                canceled = True
                outcomes.append(
                    _outcome(
                        row["matter_document_id"],
                        "CANCELED",
                        "fact.run_canceled",
                        "The run was canceled before this document",
                    )
                )
                continue
            outcome = _process_document(db, context, chunk, row, counts)
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


def _canceled_outcomes(rows: list[dict[str, Any]]) -> list[JobItemOutcome]:
    return [
        _outcome(row["matter_document_id"], "CANCELED", "fact.run_canceled", "The run was canceled before this document")
        for row in rows
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
    reader = _manifest_reader(db, run)
    manifest = finalize_outcome_manifest(
        db,
        scope=_outcome_scope(db, run),
        chunks=[_outcome_chunk(chunk, _chunk_rows(reader, chunk)) for chunk in chunks],
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
    if run.analysis_input_manifest_id is None or not chunks:
        return  # Canceled before the population was frozen: there are no documents to report.
    manifest = ensure_outcome_manifest(db, scope)
    if manifest.manifest_status == "FINALIZED":
        return
    reader = _manifest_reader(db, run)
    outcome_chunks = []
    for chunk in chunks:
        rows = _chunk_rows(reader, chunk)
        outcome_chunk = _outcome_chunk(chunk, rows)
        outcome_chunks.append(outcome_chunk)
        if find_outcome_fragment(scope, outcome_chunk) is not None:
            continue
        publish_outcome_fragment(db, scope=scope, chunk=outcome_chunk, outcomes=_canceled_outcomes(rows))
        chunk.status = "CANCELED"
    manifest = finalize_outcome_manifest(db, scope=scope, chunks=outcome_chunks)
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
