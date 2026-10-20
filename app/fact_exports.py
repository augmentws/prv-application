"""CSV export of filtered facts, produced within the request.

Core reads every matching fact from one consistent database snapshot in date order (the same order the fact list
uses) and writes two CSV files: one row per fact and one row per active quote. The Artifact Service stores both
files; Core then records a completed export with the filter, CSV policy, counts, and file references, plus an audit
event. Downloads always stream through Core after fresh authorization. No per-fact rows are written to Core for an
export, and no background job is involved: a request that fails leaves no export record behind.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import tempfile
import threading
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from typing import IO, Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.artifact_gateway import (
    WorkflowArtifactReference,
    find_workflow_artifact_reference,
    store_workflow_artifact,
)
from app.audit import record_audit
from app.config import Settings
from app.fact_queries import FACT_DATE_ORDER, FactFilters, fact_filter_statement
from app.fact_specifications import FactSpecificationError, parse_fact_development_specification
from app.matter_entities import entity_display_label
from app.models import (
    DocumentMetadataCurrent,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    MatterEntity,
    MatterFact,
    MatterFactEntity,
    MatterFactEvidence,
    MatterFactExport,
    MatterFactIssue,
    MetadataDefinition,
    User,
)
from classification_service.schemas import ArtifactType

logger = logging.getLogger(__name__)
CSV_POLICY_VERSION = "fact-export-v1"
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


class FactExportConflict(RuntimeError):
    """The idempotency key was already used for a different export."""


class FactExportTooLarge(ValueError):
    """The export would exceed the configured output size limit."""


class FactExportBusy(RuntimeError):
    """Another export of the same matter is in progress."""


@dataclass(frozen=True)
class _ExportScope:
    matter_id: uuid.UUID
    tenant_id: uuid.UUID
    client_id: uuid.UUID


_LOCAL_EXPORTS: set[uuid.UUID] = set()
_LOCAL_EXPORTS_LOCK = threading.Lock()


@contextmanager
def _matter_export_guard(db: Session, matter_id: uuid.UUID) -> Iterator[None]:
    """Allow one export per matter at a time, without holding a transaction open while it runs.

    PostgreSQL uses a session advisory lock on its own autocommit connection; other databases (tests) use an
    in-process guard.
    """

    if db.get_bind().dialect.name != "postgresql":
        with _LOCAL_EXPORTS_LOCK:
            if matter_id in _LOCAL_EXPORTS:
                raise FactExportBusy("Another export of this matter is in progress. Try again when it finishes.")
            _LOCAL_EXPORTS.add(matter_id)
        try:
            yield
        finally:
            with _LOCAL_EXPORTS_LOCK:
                _LOCAL_EXPORTS.discard(matter_id)
        return
    digest = hashlib.sha256(f"matter-fact-export:{matter_id}".encode()).digest()
    lock_key = int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)
    with db.get_bind().connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        if not connection.execute(text("SELECT pg_try_advisory_lock(:lock_key)"), {"lock_key": lock_key}).scalar():
            raise FactExportBusy("Another export of this matter is in progress. Try again when it finishes.")
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": lock_key})


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
    text_value = str(value)
    if spreadsheet_safe and text_value[:1] in {"=", "+", "-", "@"}:
        return f"'{text_value}"
    return text_value


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
        return entity_display_label(canonical), canonical.entity_type

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
    export_id: uuid.UUID,
    matter_id: uuid.UUID,
    file_kind: str,
    filter_hash: str,
) -> WorkflowArtifactReference:
    metadata = artifact.metadata
    valid = (
        artifact.artifact_type == ArtifactType.FACT_EXPORT
        and artifact.core_scope_kind == "MATTER"
        and artifact.core_scope_id == matter_id
        and artifact.producer_kind == "CORE"
        and artifact.producer_run_id == export_id
        and str(metadata.get("export_id")) == str(export_id)
        and metadata.get("file_kind") == file_kind
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
    export_id: uuid.UUID,
    scope: _ExportScope,
    filter_hash: str,
    snapshot_started_at: datetime,
    row_count: int,
    settings: Settings,
    actor_user_id: uuid.UUID,
) -> WorkflowArtifactReference:
    byte_length = binary.seek(0, io.SEEK_END)
    if byte_length > settings.fact_export_max_bytes:
        raise FactExportTooLarge("Fact export exceeds the configured output size limit")
    binary.seek(0)
    digest = hashlib.sha256()
    while chunk := binary.read(1024 * 1024):
        digest.update(chunk)
    content_hash = digest.hexdigest()
    binary.seek(0)
    derivation_key = canonical_hash(
        {"contract": CSV_POLICY_VERSION, "export_id": str(export_id), "file_kind": file_kind, "filter_hash": filter_hash}
    )
    owner = {"actor_user_id": actor_user_id, "tenant_id": scope.tenant_id, "client_id": scope.client_id}
    verify = {"export_id": export_id, "matter_id": scope.matter_id, "file_kind": file_kind, "filter_hash": filter_hash}
    existing = find_workflow_artifact_reference(
        artifact_type=ArtifactType.FACT_EXPORT, derivation_key=derivation_key, **owner
    )
    if existing is not None:
        return _verified_artifact(existing, **verify)
    artifact, _ = store_workflow_artifact(
        content=binary,
        artifact_type=ArtifactType.FACT_EXPORT,
        core_scope_kind="MATTER",
        core_scope_id=scope.matter_id,
        schema_version=1,
        derivation_key=derivation_key,
        content_hash=content_hash,
        byte_length=byte_length,
        media_type="text/csv",
        producer_kind="CORE",
        producer_run_id=export_id,
        artifact_metadata={
            "contract_version": CSV_POLICY_VERSION,
            "export_id": str(export_id),
            "matter_id": str(scope.matter_id),
            "file_kind": file_kind,
            "filter_hash": filter_hash,
            "row_count": row_count,
            "snapshot_started_at": snapshot_started_at.isoformat(),
            "filename": filename,
            "content_hash": content_hash,
            "byte_length": byte_length,
        },
        lineage=[],
        filename=filename,
        **owner,
    )
    return _verified_artifact(artifact, **verify)


def create_fact_export(
    db: Session,
    matter: Matter,
    *,
    filters: FactFilters,
    requested_filename: str,
    idempotency_key: str,
    spreadsheet_safe: bool,
    actor_user_id: uuid.UUID,
    settings: Settings,
) -> tuple[MatterFactExport, bool]:
    """Write both CSV files, store them, and record the completed export; repeat requests return the first export.

    The caller has already authorized the user and commits the returned record with its audit event.
    """

    fact_filter_statement(db, matter_id=matter.id, filters=filters)  # rejects an invalid filter before any work
    filter_snapshot = filters.snapshot()
    filter_hash = canonical_hash(filter_snapshot)
    policy = {
        "version": CSV_POLICY_VERSION,
        "encoding": "utf-8",
        "delimiter": ",",
        "line_terminator": "CRLF",
        "spreadsheet_safe": spreadsheet_safe,
    }
    stem = _safe_stem(requested_filename)
    existing = _existing_export(db, matter.id, actor_user_id, idempotency_key, filter_hash, policy, stem)
    if existing is not None:
        return existing, False
    scope = _ExportScope(matter_id=matter.id, tenant_id=matter.client.tenant_id, client_id=matter.client_id)
    with _matter_export_guard(db, matter.id):
        # Exports of a matter are serialized, so a repeat of this request that finished meanwhile is found here.
        existing = _existing_export(db, matter.id, actor_user_id, idempotency_key, filter_hash, policy, stem)
        if existing is not None:
            return existing, False
        # No transaction stays open on the request connection while the files are written and uploaded.
        db.rollback()
        return _write_export(
            db,
            scope,
            filters=filters,
            filter_snapshot=filter_snapshot,
            filter_hash=filter_hash,
            policy=policy,
            stem=stem,
            idempotency_key=idempotency_key,
            spreadsheet_safe=spreadsheet_safe,
            actor_user_id=actor_user_id,
            settings=settings,
        ), True


def _existing_export(
    db: Session,
    matter_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    idempotency_key: str,
    filter_hash: str,
    policy: dict[str, Any],
    stem: str,
) -> MatterFactExport | None:
    existing = db.scalar(
        select(MatterFactExport).where(
            MatterFactExport.matter_id == matter_id,
            MatterFactExport.created_by_user_id == actor_user_id,
            MatterFactExport.idempotency_key == idempotency_key,
        )
    )
    if existing is not None and (
        existing.filter_hash != filter_hash or existing.csv_policy_snapshot != policy or existing.requested_filename != stem
    ):
        raise FactExportConflict("Idempotency key was already used for a different export")
    return existing


def _write_export(
    db: Session,
    scope: _ExportScope,
    *,
    filters: FactFilters,
    filter_snapshot: dict[str, Any],
    filter_hash: str,
    policy: dict[str, Any],
    stem: str,
    idempotency_key: str,
    spreadsheet_safe: bool,
    actor_user_id: uuid.UUID,
    settings: Settings,
) -> MatterFactExport:
    export_id = uuid.uuid4()
    fact_count = 0
    evidence_count = 0
    with tempfile.TemporaryFile(mode="w+b") as facts_binary, tempfile.TemporaryFile(mode="w+b") as evidence_binary:
        facts_text = io.TextIOWrapper(facts_binary, encoding="utf-8", newline="", write_through=True)
        evidence_text = io.TextIOWrapper(evidence_binary, encoding="utf-8", newline="", write_through=True)
        facts_writer = csv.writer(facts_text, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
        evidence_writer = csv.writer(evidence_text, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
        facts_writer.writerow(FACT_COLUMNS)
        evidence_writer.writerow(EVIDENCE_COLUMNS)

        # A separate session so the whole read sees one snapshot, independent of the request's own transaction.
        with Session(bind=db.get_bind(), autoflush=False) as read_db:
            _begin_snapshot(read_db)
            snapshot_started_at = utcnow()
            reader = _ExportReader(read_db, scope.matter_id)
            statement = fact_filter_statement(read_db, matter_id=scope.matter_id, filters=filters)
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
                    raise FactExportTooLarge("Fact export exceeds the configured output size limit")

            for fact in read_db.scalars(statement.order_by(*FACT_DATE_ORDER).execution_options(yield_per=READ_CHUNK_SIZE)):
                chunk.append(fact)
                if len(chunk) == READ_CHUNK_SIZE:
                    flush(chunk)
                    chunk = []
            if chunk:
                flush(chunk)

        facts_filename, evidence_filename = export_filenames(stem, snapshot_started_at)
        common = {
            "export_id": export_id,
            "scope": scope,
            "filter_hash": filter_hash,
            "snapshot_started_at": snapshot_started_at,
            "settings": settings,
            "actor_user_id": actor_user_id,
        }
        facts_artifact = _store_csv(facts_binary, file_kind="FACTS", filename=facts_filename, row_count=fact_count, **common)
        evidence_artifact = _store_csv(
            evidence_binary, file_kind="EVIDENCE", filename=evidence_filename, row_count=evidence_count, **common
        )

    export = MatterFactExport(
        id=export_id,
        tenant_id=scope.tenant_id,
        matter_id=scope.matter_id,
        filter_snapshot=filter_snapshot,
        filter_hash=filter_hash,
        csv_policy_snapshot=policy,
        idempotency_key=idempotency_key,
        requested_filename=stem,
        fact_count=fact_count,
        evidence_count=evidence_count,
        facts_artifact_id=facts_artifact.artifact_id,
        facts_content_hash=facts_artifact.content_hash,
        facts_byte_length=facts_artifact.byte_length,
        facts_filename=facts_filename,
        evidence_artifact_id=evidence_artifact.artifact_id,
        evidence_content_hash=evidence_artifact.content_hash,
        evidence_byte_length=evidence_artifact.byte_length,
        evidence_filename=evidence_filename,
        snapshot_started_at=snapshot_started_at,
        expires_at=utcnow() + timedelta(days=settings.fact_export_retention_days),
        created_by_user_id=actor_user_id,
    )
    db.add(export)
    db.flush()
    record_audit(
        db,
        tenant_id=export.tenant_id,
        actor_user_id=actor_user_id,
        action="fact_export.created",
        target_type="matter_fact_export",
        target_id=export.id,
        details={
            "matter_id": str(scope.matter_id),
            "filters": filter_snapshot,
            "spreadsheet_safe": spreadsheet_safe,
            "fact_count": fact_count,
            "evidence_count": evidence_count,
            "snapshot_started_at": snapshot_started_at.isoformat(),
            "facts_artifact_id": str(facts_artifact.artifact_id),
            "facts_content_hash": facts_artifact.content_hash,
            "evidence_artifact_id": str(evidence_artifact.artifact_id),
            "evidence_content_hash": evidence_artifact.content_hash,
        },
    )
    return export


def record_fact_export_failure(
    db: Session,
    matter: Matter,
    *,
    actor_user_id: uuid.UUID,
    filters: FactFilters,
    error: BaseException,
) -> None:
    """Audit an export attempt that produced no export, so failed attempts are visible alongside completed ones."""

    record_audit(
        db,
        tenant_id=matter.client.tenant_id,
        actor_user_id=actor_user_id,
        action="fact_export.failed",
        target_type="matter",
        target_id=matter.id,
        details={
            "filters": filters.snapshot(),
            "error": type(error).__name__,
            # Only this module's own messages are recorded; dependency errors may carry URLs or response bodies.
            "message": str(error)[:1000] if isinstance(error, FactExportTooLarge | FactExportBusy) else None,
        },
    )
