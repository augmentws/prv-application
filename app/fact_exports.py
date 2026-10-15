"""CSV export of filtered facts as a durable job.

Core freezes the filter, CSV policy, and requesting user. The worker re-authorizes that user, reads every matching
fact from one consistent database snapshot in date order (the same order the fact list uses), and writes two CSV
files: one row per fact and one row per active quote. The Artifact Service stores both files; downloads always
stream through Core after fresh authorization. No per-fact rows are written to Core for an export.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import tempfile
import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from typing import IO, Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from app.artifact_gateway import (
    WorkflowArtifactReference,
    find_workflow_artifact_reference,
    store_workflow_artifact,
)
from app.audit import record_audit
from app.authorization import ResourceRef, user_can
from app.config import Settings
from app.fact_queries import FACT_DATE_ORDER, FactFilters, fact_filter_statement
from app.fact_specifications import FactSpecificationError, parse_fact_development_specification
from app.job_executions import (
    MATTER_FACT_EXPORT_JOB_TYPE,
    JobExecutionConflict,
    JobScope,
    create_job_execution,
    current_job_execution,
    execution_cancel_requested,
    transition_job_execution,
)
from app.models import (
    DocumentMetadataCurrent,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    MatterEntity,
    MatterFact,
    MatterFactEntity,
    MatterFactEvidence,
    MatterFactExportJob,
    MatterFactIssue,
    MetadataDefinition,
    User,
    WorkflowRun,
)
from classification_service.schemas import ArtifactType

logger = logging.getLogger(__name__)
CSV_POLICY_VERSION = "fact-export-v1"
EXPORT_PERMISSIONS = ("fact.export", "fact.view", "document.view")
READ_CHUNK_SIZE = 500
FACT_COLUMNS = (
    "Fact ID",
    "Status",
    "Event date",
    "Event end date",
    "Date precision",
    "Date basis",
    "Summary",
    "Owner",
    "Owner type",
    "Actors",
    "Recipients",
    "Issues",
    "Issue keys",
    "Key fact",
    "Has contradiction",
    "Evidence count",
    "Document count",
    "Confirmed by",
    "Confirmed at",
    "Revision",
    "Last updated",
)
EVIDENCE_COLUMNS = (
    "Fact ID",
    "Evidence ID",
    "Control number",
    "Matter document ID",
    "Paragraph",
    "Start offset",
    "End offset",
    "Stance",
    "Quote",
    "Added at",
)


class FactExportTransientError(RuntimeError):
    """A dependency produced a transient response that can be retried safely."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_stem(value: str) -> str:
    name = PurePath(value.replace("\\", "/")).name.replace("\r", "").replace("\n", "").strip()
    stem = name[:-4] if name.lower().endswith(".csv") else name
    return stem.strip()[:150] or "facts"


def export_filenames(stem: str, snapshot_started_at: datetime) -> tuple[str, str]:
    stamp = as_utc(snapshot_started_at).strftime("%Y%m%dT%H%M%SZ")
    return f"{stem}-facts-as-of-{stamp}.csv", f"{stem}-evidence-as-of-{stamp}.csv"


def _cell(value: Any, *, spreadsheet_safe: bool) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return as_utc(value).isoformat()
    text = str(value)
    if spreadsheet_safe and text[:1] in {"=", "+", "-", "@"}:
        return f"'{text}"
    return text


def require_export_permissions(db: Session, user: User, matter_id: uuid.UUID) -> None:
    for permission_key in EXPORT_PERMISSIONS:
        if not user_can(db, user, permission_key, ResourceRef.matter(matter_id)):
            raise PermissionError(f"Fact export requires {permission_key}")


def create_fact_export_job(
    db: Session,
    matter: Matter,
    *,
    filters: FactFilters,
    requested_filename: str,
    idempotency_key: str,
    spreadsheet_safe: bool,
    actor_user_id: uuid.UUID,
) -> tuple[MatterFactExportJob, bool]:
    fact_filter_statement(db, matter_id=matter.id, filters=filters)  # validates the filter before freezing it
    filter_snapshot = filters.snapshot()
    filter_hash = canonical_hash(filter_snapshot)
    policy = {
        "version": CSV_POLICY_VERSION,
        "encoding": "utf-8",
        "delimiter": ",",
        "line_terminator": "CRLF",
        "spreadsheet_safe": spreadsheet_safe,
    }
    existing = db.scalar(
        select(MatterFactExportJob).where(
            MatterFactExportJob.matter_id == matter.id,
            MatterFactExportJob.created_by_user_id == actor_user_id,
            MatterFactExportJob.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.filter_hash != filter_hash or existing.csv_policy_snapshot != policy:
            raise JobExecutionConflict("Idempotency key was already used for a different export")
        return existing, False
    job = MatterFactExportJob(
        tenant_id=matter.client.tenant_id,
        matter_id=matter.id,
        filter_snapshot=filter_snapshot,
        filter_hash=filter_hash,
        csv_policy_snapshot=policy,
        idempotency_key=idempotency_key,
        requested_filename=_safe_stem(requested_filename),
        status="QUEUED",
        workflow_id=f"matter-fact-export:{uuid.uuid4()}",
        current_stage="QUEUED",
        created_by_user_id=actor_user_id,
    )
    db.add(job)
    db.flush()
    create_fact_export_execution(db, job, retry_of=None)
    record_audit(
        db,
        tenant_id=job.tenant_id,
        actor_user_id=actor_user_id,
        action="fact_export.requested",
        target_type="matter_fact_export_job",
        target_id=job.id,
        details={"matter_id": str(matter.id), "filters": filter_snapshot},
    )
    return job, True


def create_fact_export_execution(
    db: Session,
    job: MatterFactExportJob,
    *,
    retry_of: WorkflowRun | None,
) -> WorkflowRun:
    attempt = retry_of.attempt_number + 1 if retry_of is not None else 1
    workflow_id = job.workflow_id if retry_of is None else f"matter-fact-export:{job.id}:attempt:{attempt}"
    execution = create_job_execution(
        db,
        job_type=MATTER_FACT_EXPORT_JOB_TYPE,
        job_id=job.id,
        scope=JobScope(job.tenant_id, job.matter.client_id, job.matter_id),
        workflow_key="matter_fact_export_v1",
        code_version="1",
        dbos_workflow_id=workflow_id,
        initiated_by_user_id=job.created_by_user_id,
        idempotency_key=f"matter-fact-export:{job.id}:attempt:{attempt}",
        current_stage="QUEUED",
        progress_unit="facts",
        input_snapshot={"matter_id": str(job.matter_id), "filter_hash": job.filter_hash, "filters": job.filter_snapshot},
        configuration_snapshot={"csv_policy": job.csv_policy_snapshot},
        retry_of=retry_of,
    )
    job.workflow_id = execution.dbos_workflow_id
    return execution


def retry_fact_export_job(db: Session, job: MatterFactExportJob, *, actor_user_id: uuid.UUID) -> WorkflowRun:
    current = current_job_execution(db, job_type=MATTER_FACT_EXPORT_JOB_TYPE, job_id=job.id, for_update=True)
    if current is None or current.status not in {"FAILED", "CANCELED"}:
        raise JobExecutionConflict("Only failed or canceled fact exports can be retried")
    if current.status == "FAILED" and not current.failure_retriable:
        raise JobExecutionConflict("This fact export failure is not retryable")
    job.status = "QUEUED"
    job.current_stage = "QUEUED"
    job.fact_count = 0
    job.evidence_count = 0
    job.failure_code = None
    job.failure_summary = None
    job.failure_retriable = False
    job.completed_at = None
    job.canceled_at = None
    execution = create_fact_export_execution(db, job, retry_of=current)
    record_audit(
        db,
        tenant_id=job.tenant_id,
        actor_user_id=actor_user_id,
        action="fact_export.retried",
        target_type="matter_fact_export_job",
        target_id=job.id,
        details={"matter_id": str(job.matter_id), "attempt_number": execution.attempt_number},
    )
    return execution


class _ExportReader:
    """Batch-loads the labels a chunk of facts needs, caching across chunks."""

    def __init__(self, db: Session, matter_id: uuid.UUID) -> None:
        self.db = db
        self.matter_id = matter_id
        self.entities: dict[uuid.UUID, MatterEntity] = {}
        self.issue_names: dict[tuple[uuid.UUID, str], str] = {}
        self.loaded_tasks: set[uuid.UUID] = set()
        self.users: dict[uuid.UUID, str] = {}
        self.control_definition_id = db.scalar(
            select(MetadataDefinition.id).where(
                MetadataDefinition.matter_id == matter_id,
                MetadataDefinition.key == "control_number",
            )
        )

    def _load_entities(self, ids: Iterable[uuid.UUID]) -> None:
        missing = set(ids) - set(self.entities)
        if missing:
            self.entities.update(
                {row.id: row for row in self.db.scalars(select(MatterEntity).where(MatterEntity.id.in_(missing)))}
            )
        parents = {row.parent_id for row in self.entities.values() if row.parent_id} - set(self.entities)
        if parents:
            self.entities.update(
                {row.id: row for row in self.db.scalars(select(MatterEntity).where(MatterEntity.id.in_(parents)))}
            )

    def canonical_name(self, entity_id: uuid.UUID | None) -> tuple[str | None, str | None]:
        if entity_id is None or entity_id not in self.entities:
            return None, None
        row = self.entities[entity_id]
        canonical = self.entities.get(row.parent_id, row) if row.parent_id else row
        return canonical.display_name, canonical.entity_type

    def _load_issue_names(self, task_ids: Iterable[uuid.UUID]) -> None:
        for task_id in set(task_ids) - self.loaded_tasks:
            self.loaded_tasks.add(task_id)
            task = self.db.get(MatterAnalysisTask, task_id)
            if task is None or task.published_version is None:
                continue
            version = self.db.scalar(
                select(MatterAnalysisTaskVersion).where(
                    MatterAnalysisTaskVersion.matter_analysis_task_id == task.id,
                    MatterAnalysisTaskVersion.version == task.published_version,
                )
            )
            try:
                specification = parse_fact_development_specification(
                    version.fact_development_specification if version is not None else None
                )
            except FactSpecificationError:
                logger.warning("Fact export could not read the issue catalog of task %s", task_id)
                continue
            for issue in specification.issues:
                self.issue_names[(task.id, issue.key)] = issue.name

    def user_name(self, user_id: uuid.UUID | None) -> str | None:
        if user_id is None:
            return None
        if user_id not in self.users:
            user = self.db.get(User, user_id)
            self.users[user_id] = user.display_name if user is not None else str(user_id)
        return self.users[user_id]

    def chunk_context(self, facts: list[MatterFact]) -> dict[str, Any]:
        fact_ids = [fact.id for fact in facts]
        issues = list(
            self.db.scalars(
                select(MatterFactIssue)
                .where(MatterFactIssue.fact_id.in_(fact_ids))
                .order_by(MatterFactIssue.fact_id, MatterFactIssue.issue_key)
            )
        )
        participants = list(
            self.db.scalars(
                select(MatterFactEntity)
                .where(MatterFactEntity.fact_id.in_(fact_ids))
                .order_by(MatterFactEntity.fact_id, MatterFactEntity.role, MatterFactEntity.entity_id)
            )
        )
        evidence = list(
            self.db.scalars(
                select(MatterFactEvidence)
                .where(MatterFactEvidence.fact_id.in_(fact_ids), MatterFactEvidence.status == "ACTIVE")
                .order_by(MatterFactEvidence.fact_id, MatterFactEvidence.created_at, MatterFactEvidence.id)
            )
        )
        self._load_entities({row.entity_id for row in participants} | {f.owner_entity_id for f in facts if f.owner_entity_id})
        self._load_issue_names({row.matter_analysis_task_id for row in issues})
        control_numbers: dict[uuid.UUID, str] = {}
        document_ids = {row.matter_document_id for row in evidence}
        if self.control_definition_id is not None and document_ids:
            control_numbers = dict(
                self.db.execute(
                    select(DocumentMetadataCurrent.matter_document_id, DocumentMetadataCurrent.value_text).where(
                        DocumentMetadataCurrent.metadata_definition_id == self.control_definition_id,
                        DocumentMetadataCurrent.matter_document_id.in_(document_ids),
                    )
                ).all()
            )
        by_fact: dict[str, dict[uuid.UUID, list[Any]]] = {
            "issues": defaultdict(list),
            "participants": defaultdict(list),
            "evidence": defaultdict(list),
        }
        for row in issues:
            by_fact["issues"][row.fact_id].append(row)
        for row in participants:
            by_fact["participants"][row.fact_id].append(row)
        for row in evidence:
            by_fact["evidence"][row.fact_id].append(row)
        return {**by_fact, "control_numbers": control_numbers}

    def fact_row(self, fact: MatterFact, context: dict[str, Any]) -> list[Any]:
        owner_name, owner_type = self.canonical_name(fact.owner_entity_id)
        participants = context["participants"].get(fact.id, [])
        issues = context["issues"].get(fact.id, [])
        return [
            fact.id,
            fact.status,
            fact.event_date_start.isoformat() if fact.event_date_start else None,
            fact.event_date_end.isoformat() if fact.event_date_end else None,
            fact.date_precision,
            fact.date_basis,
            fact.summary,
            owner_name,
            owner_type,
            "; ".join(
                self.canonical_name(row.entity_id)[0] or "" for row in participants if row.role == "PARTICIPANT_ACTOR"
            ),
            "; ".join(
                self.canonical_name(row.entity_id)[0] or ""
                for row in participants
                if row.role == "PARTICIPANT_RECIPIENT"
            ),
            "; ".join(self.issue_names.get((row.matter_analysis_task_id, row.issue_key), row.issue_key) for row in issues),
            "; ".join(row.issue_key for row in issues),
            fact.key_fact,
            fact.has_contradiction,
            fact.evidence_count,
            fact.document_count,
            self.user_name(fact.confirmed_by_user_id),
            fact.confirmed_at,
            fact.current_revision,
            fact.updated_at,
        ]

    @staticmethod
    def evidence_rows(fact: MatterFact, context: dict[str, Any]) -> list[list[Any]]:
        return [
            [
                fact.id,
                row.id,
                context["control_numbers"].get(row.matter_document_id),
                row.matter_document_id,
                row.paragraph_id,
                row.char_start,
                row.char_end,
                row.stance,
                row.quote,
                row.created_at,
            ]
            for row in context["evidence"].get(fact.id, [])
        ]


def _begin_snapshot(db: Session) -> None:
    """Read the whole export from one consistent snapshot where the database supports it."""

    if db.get_bind().dialect.name == "postgresql":
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})


def _verified_artifact(
    artifact: WorkflowArtifactReference,
    *,
    job_id: uuid.UUID,
    matter_id: uuid.UUID,
    file_kind: str,
    attempt_number: int,
    filter_hash: str,
) -> WorkflowArtifactReference:
    metadata = artifact.metadata
    valid = (
        artifact.artifact_type == ArtifactType.FACT_EXPORT
        and artifact.core_scope_kind == "MATTER"
        and artifact.core_scope_id == matter_id
        and artifact.producer_kind == "CORE"
        and artifact.producer_run_id == job_id
        and str(metadata.get("export_job_id")) == str(job_id)
        and metadata.get("file_kind") == file_kind
        and int(metadata.get("attempt_number") or 0) == attempt_number
        and metadata.get("filter_hash") == filter_hash
        and metadata.get("content_hash") == artifact.content_hash
        and int(metadata.get("byte_length") or -1) == artifact.byte_length
        and artifact.media_type == "text/csv"
    )
    if not valid:
        raise ValueError("Fact export artifact provenance is invalid")
    return artifact


def _store_csv(
    binary: IO[bytes],
    *,
    file_kind: str,
    filename: str,
    job_id: uuid.UUID,
    matter_id: uuid.UUID,
    attempt_number: int,
    filter_hash: str,
    snapshot_started_at: datetime,
    row_count: int,
    settings: Settings,
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
) -> WorkflowArtifactReference:
    byte_length = binary.seek(0, io.SEEK_END)
    if byte_length > settings.fact_export_max_bytes:
        raise ValueError("Fact export exceeds the configured output size limit")
    binary.seek(0)
    digest = hashlib.sha256()
    while chunk := binary.read(1024 * 1024):
        digest.update(chunk)
    content_hash = digest.hexdigest()
    binary.seek(0)
    derivation_key = canonical_hash(
        {
            "contract": CSV_POLICY_VERSION,
            "job_id": str(job_id),
            "attempt_number": attempt_number,
            "file_kind": file_kind,
            "filter_hash": filter_hash,
        }
    )
    existing = find_workflow_artifact_reference(
        artifact_type=ArtifactType.FACT_EXPORT,
        derivation_key=derivation_key,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
    )
    verify = {
        "job_id": job_id,
        "matter_id": matter_id,
        "file_kind": file_kind,
        "attempt_number": attempt_number,
        "filter_hash": filter_hash,
    }
    if existing is not None:
        return _verified_artifact(existing, **verify)
    artifact, _ = store_workflow_artifact(
        content=binary,
        artifact_type=ArtifactType.FACT_EXPORT,
        core_scope_kind="MATTER",
        core_scope_id=matter_id,
        schema_version=1,
        derivation_key=derivation_key,
        content_hash=content_hash,
        byte_length=byte_length,
        media_type="text/csv",
        producer_kind="CORE",
        producer_run_id=job_id,
        artifact_metadata={
            "contract_version": CSV_POLICY_VERSION,
            "export_job_id": str(job_id),
            "matter_id": str(matter_id),
            "file_kind": file_kind,
            "filter_hash": filter_hash,
            "attempt_number": attempt_number,
            "row_count": row_count,
            "snapshot_started_at": snapshot_started_at.isoformat(),
            "filename": filename,
            "content_hash": content_hash,
            "byte_length": byte_length,
        },
        lineage=[],
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
        filename=filename,
    )
    return _verified_artifact(artifact, **verify)


def _cancel_if_requested(db: Session, job: MatterFactExportJob, fact_count: int) -> bool:
    if not execution_cancel_requested(db, job_type=MATTER_FACT_EXPORT_JOB_TYPE, job_id=job.id):
        return False
    transition_job_execution(
        db,
        job_type=MATTER_FACT_EXPORT_JOB_TYPE,
        job_id=job.id,
        status="CANCELED",
        current_stage="CANCELED",
        failure_retriable=False,
    )
    record_audit(
        db,
        tenant_id=job.tenant_id,
        actor_user_id=job.created_by_user_id,
        action="fact_export.canceled",
        target_type="matter_fact_export_job",
        target_id=job.id,
        details={"matter_id": str(job.matter_id), "fact_count": fact_count},
    )
    return True


def run_fact_export(job_id: uuid.UUID, settings: Settings, *, session_factory: Any) -> None:
    with session_factory() as db:
        job = db.get(MatterFactExportJob, job_id)
        if job is None:
            raise ValueError("Fact export job was not found")
        matter = db.get(Matter, job.matter_id)
        actor = db.get(User, job.created_by_user_id)
        if matter is None or actor is None:
            raise ValueError("Fact export scope is unavailable")
        require_export_permissions(db, actor, matter.id)
        execution = current_job_execution(db, job_type=MATTER_FACT_EXPORT_JOB_TYPE, job_id=job.id, for_update=True)
        if execution is None:
            raise ValueError("Fact export execution is unavailable")
        # A DBOS step retry rebuilds the unpublished files from the beginning.
        execution.progress_completed = 0
        execution.progress_total = None
        transition_job_execution(
            db, job_type=MATTER_FACT_EXPORT_JOB_TYPE, job_id=job.id, status="RUNNING", current_stage="EXPORTING"
        )
        db.commit()
        context = {
            "matter_id": matter.id,
            "tenant_id": job.tenant_id,
            "client_id": matter.client_id,
            "actor_user_id": actor.id,
            "attempt_number": execution.attempt_number,
            "filter_hash": job.filter_hash,
            "filters": FactFilters.from_snapshot(job.filter_snapshot),
            "spreadsheet_safe": bool(job.csv_policy_snapshot.get("spreadsheet_safe", True)),
            "stem": job.requested_filename,
        }

    spreadsheet_safe = context["spreadsheet_safe"]
    fact_count = 0
    evidence_count = 0
    with tempfile.TemporaryFile(mode="w+b") as facts_binary, tempfile.TemporaryFile(mode="w+b") as evidence_binary:
        facts_text = io.TextIOWrapper(facts_binary, encoding="utf-8", newline="", write_through=True)
        evidence_text = io.TextIOWrapper(evidence_binary, encoding="utf-8", newline="", write_through=True)
        facts_writer = csv.writer(facts_text, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
        evidence_writer = csv.writer(evidence_text, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
        facts_writer.writerow(FACT_COLUMNS)
        evidence_writer.writerow(EVIDENCE_COLUMNS)

        with session_factory() as read_db:
            _begin_snapshot(read_db)
            snapshot_started_at = utcnow()
            reader = _ExportReader(read_db, context["matter_id"])
            statement = fact_filter_statement(read_db, matter_id=context["matter_id"], filters=context["filters"])
            total = read_db.scalar(select(func.count()).select_from(statement.subquery())) or 0
            chunk: list[MatterFact] = []

            def flush(facts: list[MatterFact]) -> None:
                nonlocal fact_count, evidence_count
                chunk_context = reader.chunk_context(facts)
                for fact in facts:
                    facts_writer.writerow(
                        [_cell(value, spreadsheet_safe=spreadsheet_safe) for value in reader.fact_row(fact, chunk_context)]
                    )
                    for row in reader.evidence_rows(fact, chunk_context):
                        evidence_writer.writerow([_cell(value, spreadsheet_safe=spreadsheet_safe) for value in row])
                        evidence_count += 1
                    fact_count += 1
                if facts_binary.tell() + evidence_binary.tell() > settings.fact_export_max_bytes:
                    raise ValueError("Fact export exceeds the configured output size limit")

            for fact in read_db.scalars(statement.order_by(*FACT_DATE_ORDER).execution_options(yield_per=READ_CHUNK_SIZE)):
                chunk.append(fact)
                if len(chunk) < READ_CHUNK_SIZE:
                    continue
                flush(chunk)
                chunk = []
                with session_factory() as progress_db:
                    current = progress_db.get(MatterFactExportJob, job_id)
                    if current is None:
                        raise ValueError("Fact export job was not found")
                    transition_job_execution(
                        progress_db,
                        job_type=MATTER_FACT_EXPORT_JOB_TYPE,
                        job_id=job_id,
                        current_stage="EXPORTING",
                        progress_completed=fact_count,
                        progress_total=total,
                    )
                    canceled = _cancel_if_requested(progress_db, current, fact_count)
                    progress_db.commit()
                    if canceled:
                        return
            if chunk:
                flush(chunk)

        with session_factory() as db:
            current = db.get(MatterFactExportJob, job_id)
            if current is None:
                raise ValueError("Fact export job was not found")
            canceled = _cancel_if_requested(db, current, fact_count)
            if not canceled:
                transition_job_execution(
                    db,
                    job_type=MATTER_FACT_EXPORT_JOB_TYPE,
                    job_id=job_id,
                    current_stage="UPLOADING",
                    progress_completed=fact_count,
                    progress_total=total,
                )
            db.commit()
            if canceled:
                return

        facts_filename, evidence_filename = export_filenames(context["stem"], snapshot_started_at)
        common = {
            "job_id": job_id,
            "matter_id": context["matter_id"],
            "attempt_number": context["attempt_number"],
            "filter_hash": context["filter_hash"],
            "snapshot_started_at": snapshot_started_at,
            "settings": settings,
            "actor_user_id": context["actor_user_id"],
            "tenant_id": context["tenant_id"],
            "client_id": context["client_id"],
        }
        facts_artifact = _store_csv(
            facts_binary, file_kind="FACTS", filename=facts_filename, row_count=fact_count, **common
        )
        evidence_artifact = _store_csv(
            evidence_binary, file_kind="EVIDENCE", filename=evidence_filename, row_count=evidence_count, **common
        )

    with session_factory() as db:
        current = db.get(MatterFactExportJob, job_id)
        if current is None:
            raise ValueError("Fact export job was not found")
        current.facts_artifact_id = facts_artifact.artifact_id
        current.facts_content_hash = facts_artifact.content_hash
        current.facts_byte_length = facts_artifact.byte_length
        current.facts_filename = facts_filename
        current.evidence_artifact_id = evidence_artifact.artifact_id
        current.evidence_content_hash = evidence_artifact.content_hash
        current.evidence_byte_length = evidence_artifact.byte_length
        current.evidence_filename = evidence_filename
        current.evidence_count = evidence_count
        current.snapshot_started_at = snapshot_started_at
        current.expires_at = utcnow() + timedelta(days=settings.fact_export_retention_days)
        transition_job_execution(
            db,
            job_type=MATTER_FACT_EXPORT_JOB_TYPE,
            job_id=job_id,
            status="COMPLETED",
            current_stage="COMPLETED",
            progress_completed=fact_count,
            progress_total=fact_count,
            progress={"evidence_count": evidence_count, "snapshot_started_at": snapshot_started_at.isoformat()},
            failure_retriable=False,
        )
        record_audit(
            db,
            tenant_id=current.tenant_id,
            actor_user_id=current.created_by_user_id,
            action="fact_export.completed",
            target_type="matter_fact_export_job",
            target_id=current.id,
            details={
                "matter_id": str(current.matter_id),
                "fact_count": fact_count,
                "evidence_count": evidence_count,
                "facts_content_hash": facts_artifact.content_hash,
                "evidence_content_hash": evidence_artifact.content_hash,
            },
        )
        db.commit()


def _fact_export_failure(error: BaseException) -> tuple[str, bool]:
    if isinstance(error, FactExportTransientError):
        return "FACT_EXPORT_DEPENDENCY_UNAVAILABLE", True
    if isinstance(error, httpx.HTTPStatusError):
        return "FACT_EXPORT_DEPENDENCY_FAILED", error.response.status_code in {408, 429} or error.response.status_code >= 500
    if isinstance(error, httpx.TransportError | ConnectionError | TimeoutError):
        return "FACT_EXPORT_DEPENDENCY_UNAVAILABLE", True
    if isinstance(error, OperationalError | ProgrammingError):
        return "FACT_EXPORT_STORAGE_UNAVAILABLE", True
    if isinstance(error, PermissionError):
        return "FACT_EXPORT_AUTHORIZATION_REVOKED", False
    if isinstance(error, TypeError | ValueError):
        return "FACT_EXPORT_INVALID", False
    return "FACT_EXPORT_FAILED", False


def record_fact_export_attempt_failure(db: Session, job_id: uuid.UUID, error: BaseException) -> None:
    """Retain the original step error classification across DBOS retry wrapping."""

    job = db.get(MatterFactExportJob, job_id)
    if job is None:
        return
    code, retriable = _fact_export_failure(error)
    job.failure_code = code
    job.failure_summary = str(error)[:4000]
    job.failure_retriable = retriable


def fail_fact_export_job(db: Session, job_id: uuid.UUID, error: BaseException) -> None:
    job = db.get(MatterFactExportJob, job_id)
    if job is None:
        return
    execution = current_job_execution(db, job_type=MATTER_FACT_EXPORT_JOB_TYPE, job_id=job_id)
    if execution is None or execution.status in {"COMPLETED", "FAILED", "CANCELED"}:
        return
    fallback_code, fallback_retriable = _fact_export_failure(error)
    retriable = job.failure_retriable or fallback_retriable
    transition_job_execution(
        db,
        job_type=MATTER_FACT_EXPORT_JOB_TYPE,
        job_id=job_id,
        status="FAILED",
        current_stage="FAILED",
        failure_code=job.failure_code or fallback_code,
        failure_summary=job.failure_summary or str(error)[:4000],
        failure_retriable=retriable,
    )
    record_audit(
        db,
        tenant_id=job.tenant_id,
        actor_user_id=job.created_by_user_id,
        action="fact_export.failed",
        target_type="matter_fact_export_job",
        target_id=job.id,
        details={"matter_id": str(job.matter_id), "retriable": retriable},
    )
