from __future__ import annotations

import csv
import hashlib
import json
import logging
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import PurePath
from types import SimpleNamespace
from typing import Any

import httpx
from sqlalchemy import select
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
from app.embedding_gateway import get_query_embedding_gateway
from app.job_executions import (
    MATTER_SEARCH_EXPORT_JOB_TYPE,
    JobExecutionConflict,
    JobScope,
    create_job_execution,
    current_job_execution,
    execution_cancel_requested,
    transition_job_execution,
)
from app.models import (
    Matter,
    MatterSearchExportJob,
    MetadataDefinition,
    MetadataGroup,
    MetadataGroupField,
    SearchIndexGeneration,
    User,
    WorkflowRun,
)
from app.schemas import MatterSearchExportCreate, MatterSearchRequest, SearchExportFieldRead
from app.search.client import OpenSearchClient, is_retryable_opensearch_error
from app.search.query import RRF_SEARCH_PIPELINE, compile_search_export_request
from classification_service.schemas import ArtifactType

logger = logging.getLogger(__name__)
CSV_POLICY_VERSION = "search-result-export-v1"
PIT_KEEP_ALIVE = "10m"


class SearchExportTransientError(RuntimeError):
    """A dependency produced a transient response that can be retried safely."""


_BUILTIN_FIELDS: tuple[dict[str, Any], ...] = (
    {"id": "document_id", "display_name": "Matter document ID", "value_type": "TEXT"},
    {"id": "collection_item_id", "display_name": "Collection item ID", "value_type": "TEXT"},
    {"id": "source_collection_id", "display_name": "Source collection ID", "value_type": "TEXT"},
    {"id": "created_at", "display_name": "Date added to matter", "value_type": "DATETIME"},
    {"id": "record_type", "display_name": "Record type", "value_type": "TEXT"},
    {"id": "processing_status", "display_name": "Processing status", "value_type": "TEXT"},
    {"id": "original_filename", "display_name": "Original filename", "value_type": "TEXT"},
    {"id": "source_path", "display_name": "Source path", "value_type": "TEXT"},
    {"id": "family_id", "display_name": "Family ID", "value_type": "TEXT"},
    {"id": "custodian_ids", "display_name": "Custodian IDs", "value_type": "TEXT", "cardinality": "MULTIPLE"},
    {"id": "custodian_names", "display_name": "Custodian names", "value_type": "TEXT", "cardinality": "MULTIPLE"},
    {"id": "email_from", "display_name": "Email from", "value_type": "TEXT"},
    {"id": "email_to", "display_name": "Email to", "value_type": "TEXT", "cardinality": "MULTIPLE"},
    {"id": "email_cc", "display_name": "Email cc", "value_type": "TEXT", "cardinality": "MULTIPLE"},
    {"id": "email_bcc", "display_name": "Email bcc", "value_type": "TEXT", "cardinality": "MULTIPLE"},
    {"id": "email_subject", "display_name": "Email subject", "value_type": "TEXT"},
    {"id": "body_text", "display_name": "Document text", "value_type": "LONG_TEXT", "large_value": True},
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _metadata_group_lookup(db: Session, matter_id: uuid.UUID) -> dict[uuid.UUID, tuple[uuid.UUID, str]]:
    rows = db.execute(
        select(MetadataGroupField.metadata_definition_id, MetadataGroup.id, MetadataGroup.display_name)
        .join(MetadataGroup, MetadataGroup.id == MetadataGroupField.metadata_group_id)
        .where(MetadataGroup.matter_id == matter_id, MetadataGroup.status == "ACTIVE")
        .order_by(MetadataGroup.sort_order, MetadataGroupField.sort_order, MetadataGroup.id)
    )
    result: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}
    for definition_id, group_id, display_name in rows:
        result.setdefault(definition_id, (group_id, display_name))
    return result


def search_export_field_catalog(
    db: Session,
    matter_id: uuid.UUID,
    generation: SearchIndexGeneration,
) -> list[SearchExportFieldRead]:
    properties = generation.schema_snapshot.get("mappings", {}).get("properties", {})
    metadata_properties = properties.get("metadata", {}).get("properties", {})
    fields = [
        SearchExportFieldRead(
            id=item["id"],
            stable_key=item["id"],
            display_name=item["display_name"],
            group_display_name="Document fields",
            source_path=item["id"],
            value_type=item["value_type"],
            cardinality=item.get("cardinality", "SINGLE"),
            selectable=item["id"] in properties,
            unavailable_reason=None if item["id"] in properties else "Not projected in this search index",
            large_value=item.get("large_value", False),
        )
        for item in _BUILTIN_FIELDS
    ]
    groups = _metadata_group_lookup(db, matter_id)
    definitions = db.scalars(
        select(MetadataDefinition)
        .where(MetadataDefinition.matter_id == matter_id, MetadataDefinition.status == "ACTIVE")
        .order_by(MetadataDefinition.display_name, MetadataDefinition.key)
    ).all()
    for definition in definitions:
        group = groups.get(definition.id)
        available = definition.searchable and definition.key in metadata_properties and definition.type != "JSON"
        enum_values = [
            {"key": str(item["key"]), "label": str(item.get("label") or item["key"])}
            for item in definition.allowed_values or []
            if item.get("active", True)
        ]
        fields.append(
            SearchExportFieldRead(
                id=str(definition.id),
                stable_key=definition.key,
                display_name=definition.display_name,
                group_id=group[0] if group else None,
                group_display_name=group[1] if group else "Matter metadata",
                source_path=f"metadata.{definition.key}",
                value_type=definition.type,
                cardinality=definition.cardinality,
                selectable=available,
                unavailable_reason=None if available else "Not projected in this search index",
                large_value=definition.type == "LONG_TEXT",
                enum_values=enum_values,
            )
        )
    return fields


def _canonical_search(search: MatterSearchRequest) -> dict[str, Any]:
    update: dict[str, Any] = {"offset": 0, "size": 1, "facets": []}
    if search.search_mode != "KEYWORD" and search.candidate_limit is None:
        update["candidate_limit"] = 100 if search.search_mode == "HYBRID" else 10_000
    return search.model_copy(update=update).model_dump(mode="json")


def _search_definition_snapshot(definitions: list[MetadataDefinition]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(definition.id),
            "key": definition.key,
            "type": definition.type,
            "cardinality": definition.cardinality,
            "allowed_values": definition.allowed_values,
            "searchable": definition.searchable,
            "facetable": definition.facetable,
            "normalize_to_lowercase": definition.normalize_to_lowercase,
            "hierarchy_separator": definition.hierarchy_separator,
            "status": definition.status,
        }
        for definition in definitions
    ]


def _restore_search_definitions(snapshot: dict[str, Any]) -> list[Any]:
    definitions = snapshot.get("metadata_definitions")
    if not isinstance(definitions, list):
        raise TypeError("Pinned search field definitions are unavailable")
    return [SimpleNamespace(**definition) for definition in definitions if isinstance(definition, dict)]


def _safe_filename(value: str) -> str:
    filename = PurePath(value.replace("\\", "/")).name.replace("\r", "").replace("\n", "").strip()
    stem = filename[:-4] if filename.lower().endswith(".csv") else filename
    stem = stem.strip()[:251] or "search-results"
    return f"{stem}.csv"


def create_search_export_job(
    db: Session,
    matter: Matter,
    payload: MatterSearchExportCreate,
    *,
    actor_user_id: uuid.UUID,
    settings: Settings,
    required_filters: list[dict[str, Any]] | None = None,
    scope_snapshot: dict[str, Any] | None = None,
) -> tuple[MatterSearchExportJob, bool]:
    generation = db.scalar(
        select(SearchIndexGeneration).where(
            SearchIndexGeneration.matter_id == matter.id,
            SearchIndexGeneration.status == "ACTIVE",
        )
    )
    if generation is None:
        raise JobExecutionConflict("Matter search index is not ready")
    catalog = {item.id: item for item in search_export_field_catalog(db, matter.id, generation)}
    selected: list[dict[str, Any]] = []
    used_headers: dict[str, int] = {}
    for field_id in payload.field_ids:
        field = catalog.get(field_id)
        if field is None:
            raise JobExecutionConflict(f"Unknown export field: {field_id}")
        if not field.selectable:
            raise JobExecutionConflict(f"Export field is unavailable: {field.display_name}")
        snapshot = field.model_dump(mode="json")
        header = field.display_name
        used_headers[header] = used_headers.get(header, 0) + 1
        if used_headers[header] > 1:
            header = f"{header} ({field.stable_key})"
        snapshot["header"] = header
        selected.append(snapshot)

    search_definition = _canonical_search(payload.search)
    frozen_required_filters = list(required_filters or [])
    search_hash = canonical_hash(
        {"search": search_definition, "required_filters": frozen_required_filters}
    )
    field_hash = canonical_hash(selected)
    definitions = list(
        db.scalars(
            select(MetadataDefinition)
            .where(MetadataDefinition.matter_id == matter.id, MetadataDefinition.status == "ACTIVE")
            .order_by(MetadataDefinition.key, MetadataDefinition.id)
        )
    )
    existing = db.scalar(
        select(MatterSearchExportJob).where(
            MatterSearchExportJob.matter_id == matter.id,
            MatterSearchExportJob.created_by_user_id == actor_user_id,
            MatterSearchExportJob.idempotency_key == payload.idempotency_key,
        )
    )
    if existing is not None:
        same_contract = (
            existing.search_definition_hash == search_hash
            and existing.field_snapshot_hash == field_hash
            and bool(existing.csv_policy_snapshot.get("spreadsheet_safe", True)) == payload.spreadsheet_safe
        )
        if not same_contract:
            raise JobExecutionConflict("Idempotency key was already used for a different export")
        return existing, False

    job = MatterSearchExportJob(
        tenant_id=matter.client.tenant_id,
        matter_id=matter.id,
        search_index_generation_id=generation.id,
        search_definition=search_definition,
        search_definition_hash=search_hash,
        search_index_snapshot={
            "generation": generation.generation,
            "index_name": generation.index_name,
            "schema_hash": generation.schema_hash,
            "activated_at": generation.activated_at.isoformat() if generation.activated_at else None,
            "metadata_definitions": _search_definition_snapshot(definitions),
            "required_filters": frozen_required_filters,
            "scope": dict(scope_snapshot or {"kind": "MATTER"}),
        },
        field_snapshot=selected,
        field_snapshot_hash=field_hash,
        csv_policy_snapshot={
            "version": CSV_POLICY_VERSION,
            "encoding": "utf-8",
            "delimiter": ",",
            "line_terminator": "CRLF",
            "spreadsheet_safe": payload.spreadsheet_safe,
        },
        idempotency_key=payload.idempotency_key,
        requested_filename=_safe_filename(payload.requested_filename),
        status="QUEUED",
        workflow_id=f"matter-search-export:{uuid.uuid4()}",
        current_stage="QUEUED",
        created_by_user_id=actor_user_id,
        retry_until=utcnow() + timedelta(hours=settings.search_export_retry_hours),
    )
    db.add(job)
    db.flush()
    create_search_export_execution(db, job, retry_of=None)
    record_audit(
        db,
        tenant_id=job.tenant_id,
        actor_user_id=actor_user_id,
        action="search_export.requested",
        target_type="matter_search_export_job",
        target_id=job.id,
        details={
            "matter_id": str(matter.id),
            "field_ids": payload.field_ids,
            "field_count": len(selected),
            "search_definition_hash": search_hash,
            "search_index_generation": generation.generation,
            "scope": dict(scope_snapshot or {"kind": "MATTER"}),
        },
    )
    return job, True


def create_search_export_execution(
    db: Session,
    job: MatterSearchExportJob,
    *,
    retry_of: WorkflowRun | None,
) -> WorkflowRun:
    attempt = retry_of.attempt_number + 1 if retry_of is not None else 1
    workflow_id = job.workflow_id if retry_of is None else f"matter-search-export:{job.id}:attempt:{attempt}"
    execution = create_job_execution(
        db,
        job_type=MATTER_SEARCH_EXPORT_JOB_TYPE,
        job_id=job.id,
        scope=JobScope(job.tenant_id, job.matter.client_id, job.matter_id),
        workflow_key="matter_search_export_v1",
        code_version="1",
        dbos_workflow_id=workflow_id,
        initiated_by_user_id=job.created_by_user_id,
        idempotency_key=f"matter-search-export:{job.id}:attempt:{attempt}",
        current_stage="QUEUED",
        progress_unit="documents",
        input_snapshot={
            "matter_id": str(job.matter_id),
            "search_definition_hash": job.search_definition_hash,
            "field_snapshot_hash": job.field_snapshot_hash,
            "search_index_snapshot": job.search_index_snapshot,
        },
        configuration_snapshot={"csv_policy": job.csv_policy_snapshot},
        retry_of=retry_of,
    )
    job.workflow_id = execution.dbos_workflow_id
    return execution


def retry_search_export_job(db: Session, job: MatterSearchExportJob, *, actor_user_id: uuid.UUID) -> WorkflowRun:
    current = current_job_execution(db, job_type=MATTER_SEARCH_EXPORT_JOB_TYPE, job_id=job.id, for_update=True)
    if current is None or current.status not in {"FAILED", "CANCELED"}:
        raise JobExecutionConflict("Only failed or canceled search exports can be retried")
    if current.status == "FAILED" and not current.failure_retriable:
        raise JobExecutionConflict("This search export failure is not retryable")
    if job.search_index_generation_id is None or (
        job.retry_until is not None and as_utc(job.retry_until) < utcnow()
    ):
        raise JobExecutionConflict("The pinned search generation is no longer available")
    job.status = "QUEUED"
    job.current_stage = "QUEUED"
    job.matched_count = 0
    job.exported_count = 0
    job.page_count = 0
    job.failure_code = None
    job.failure_summary = None
    job.failure_retriable = False
    job.completed_at = None
    job.canceled_at = None
    execution = create_search_export_execution(db, job, retry_of=current)
    record_audit(
        db,
        tenant_id=job.tenant_id,
        actor_user_id=actor_user_id,
        action="search_export.retried",
        target_type="matter_search_export_job",
        target_id=job.id,
        details={"matter_id": str(job.matter_id), "attempt_number": execution.attempt_number},
    )
    return execution


def _source_value(source: dict[str, Any], path: str) -> Any:
    value: Any = source
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _enum_value(value: Any, labels: dict[str, str]) -> Any:
    if isinstance(value, list):
        return [labels.get(str(item), str(item)) for item in value]
    return labels.get(str(value), str(value)) if value is not None else None


def _serialize_cell(value: Any, field: dict[str, Any], *, spreadsheet_safe: bool) -> str:
    if value is None:
        return ""
    if field.get("value_type") == "ENUM":
        labels = {str(item["key"]): str(item["label"]) for item in field.get("enum_values") or []}
        value = _enum_value(value, labels)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    text = str(value)
    if (
        spreadsheet_safe
        and field.get("value_type") in {"TEXT", "LONG_TEXT", "ENUM"}
        and text[:1] in {"=", "+", "-", "@"}
    ):
        return f"'{text}"
    return text


def _serialize_header(value: Any, *, spreadsheet_safe: bool) -> str:
    text = str(value)
    return f"'{text}" if spreadsheet_safe and text[:1] in {"=", "+", "-", "@"} else text


def _verified_export_artifact_metadata(
    artifact: WorkflowArtifactReference,
    *,
    job_id: uuid.UUID,
    matter_id: uuid.UUID,
    search_hash: str,
    field_hash: str,
    policy_hash: str,
    attempt_number: int,
    requested_filename: str,
) -> tuple[int, int, int, datetime]:
    metadata = artifact.metadata
    valid = (
        artifact.artifact_type == ArtifactType.SEARCH_RESULT_EXPORT
        and artifact.core_scope_kind == "MATTER"
        and artifact.core_scope_id == matter_id
        and artifact.producer_kind == "CORE"
        and artifact.producer_run_id == job_id
        and str(metadata.get("export_job_id")) == str(job_id)
        and metadata.get("search_definition_hash") == search_hash
        and metadata.get("field_snapshot_hash") == field_hash
        and metadata.get("csv_policy_hash") == policy_hash
        and int(metadata.get("attempt_number") or 0) == attempt_number
        and metadata.get("content_hash") == artifact.content_hash
        and int(metadata.get("byte_length") or -1) == artifact.byte_length
        and metadata.get("filename") == requested_filename
        and artifact.media_type == "text/csv"
    )
    if not valid:
        raise ValueError("Existing search export artifact provenance is invalid")
    try:
        matched_count = int(metadata["matched_count"])
        row_count = int(metadata["row_count"])
        page_count = int(metadata["page_count"])
        snapshot_started_at = datetime.fromisoformat(str(metadata["snapshot_started_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Existing search export artifact metadata is incomplete") from exc
    if min(matched_count, row_count, page_count) < 0 or row_count > matched_count:
        raise ValueError("Existing search export artifact counts are invalid")
    return matched_count, row_count, page_count, as_utc(snapshot_started_at)


def _complete_search_export(
    db: Session,
    *,
    job_id: uuid.UUID,
    artifact: WorkflowArtifactReference,
    requested_filename: str,
    matched_count: int,
    exported_count: int,
    page_count: int,
    snapshot_started_at: datetime,
    settings: Settings,
) -> None:
    current = db.get(MatterSearchExportJob, job_id)
    if current is None:
        raise ValueError("Search export job was not found")
    current.output_artifact_id = artifact.artifact_id
    current.output_content_hash = artifact.content_hash
    current.output_byte_length = artifact.byte_length
    current.output_filename = requested_filename
    current.snapshot_started_at = snapshot_started_at
    current.expires_at = utcnow() + timedelta(days=settings.search_export_retention_days)
    current.page_count = page_count
    transition_job_execution(
        db,
        job_type=MATTER_SEARCH_EXPORT_JOB_TYPE,
        job_id=job_id,
        status="COMPLETED",
        current_stage="COMPLETED",
        progress_completed=exported_count,
        progress_total=matched_count,
        progress={
            "page_count": page_count,
            "output_byte_length": artifact.byte_length,
            "artifact_id": str(artifact.artifact_id),
        },
        failure_retriable=False,
    )
    record_audit(
        db,
        tenant_id=current.tenant_id,
        actor_user_id=current.created_by_user_id,
        action="search_export.completed",
        target_type="matter_search_export_job",
        target_id=current.id,
        details={
            "matter_id": str(current.matter_id),
            "row_count": exported_count,
            "byte_length": artifact.byte_length,
            "content_hash": artifact.content_hash,
        },
    )


def run_search_export(job_id: uuid.UUID, settings: Settings, *, session_factory: Any) -> None:
    pit_id: str | None = None
    client = OpenSearchClient(settings)
    try:
        with session_factory() as db:
            job = db.get(MatterSearchExportJob, job_id)
            if job is None:
                raise ValueError("Search export job was not found")
            matter = db.get(Matter, job.matter_id)
            actor = db.get(User, job.created_by_user_id)
            if matter is None or actor is None:
                raise ValueError("Search export scope is unavailable")
            if not user_can(db, actor, "document.view", ResourceRef.matter(matter.id)) or not user_can(
                db, actor, "document.export", ResourceRef.matter(matter.id)
            ):
                raise PermissionError("Search export authorization was revoked")
            generation = db.get(SearchIndexGeneration, job.search_index_generation_id)
            if generation is None or generation.index_name != job.search_index_snapshot.get("index_name"):
                raise ValueError("Pinned search generation is unavailable")
            definitions = _restore_search_definitions(job.search_index_snapshot)
            execution = current_job_execution(
                db,
                job_type=MATTER_SEARCH_EXPORT_JOB_TYPE,
                job_id=job.id,
                for_update=True,
            )
            if execution is None:
                raise ValueError("Search export execution is unavailable")
            # A DBOS step retry rebuilds an unpublished CSV from the beginning.
            execution.progress_completed = 0
            execution.progress_total = None
            job.exported_count = 0
            job.matched_count = 0
            job.page_count = 0
            transition_job_execution(
                db,
                job_type=MATTER_SEARCH_EXPORT_JOB_TYPE,
                job_id=job.id,
                status="RUNNING",
                current_stage="PREPARING",
            )
            db.commit()
            index_name = generation.index_name
            tenant_id = job.tenant_id
            client_id = matter.client_id
            actor_user_id = actor.id
            field_snapshot = list(job.field_snapshot)
            search = MatterSearchRequest.model_validate(job.search_definition)
            requested_filename = job.requested_filename
            spreadsheet_safe = bool(job.csv_policy_snapshot.get("spreadsheet_safe", True))
            search_hash = job.search_definition_hash
            field_hash = job.field_snapshot_hash
            policy_hash = canonical_hash(job.csv_policy_snapshot)
            attempt_number = execution.attempt_number
            matter_id = job.matter_id
            search_index_snapshot = dict(job.search_index_snapshot)
            required_filters = search_index_snapshot.get("required_filters", [])
            if not isinstance(required_filters, list) or not all(
                isinstance(item, dict) for item in required_filters
            ):
                raise TypeError("Pinned search scope filters are invalid")

        derivation_key = canonical_hash(
            {
                "contract": CSV_POLICY_VERSION,
                "job_id": str(job_id),
                "attempt_number": attempt_number,
                "search_hash": search_hash,
                "field_hash": field_hash,
                "policy_hash": policy_hash,
            }
        )
        existing_artifact = find_workflow_artifact_reference(
            artifact_type=ArtifactType.SEARCH_RESULT_EXPORT,
            derivation_key=derivation_key,
            actor_user_id=actor_user_id,
            tenant_id=tenant_id,
            client_id=client_id,
        )
        if existing_artifact is not None:
            matched_count, exported_count, page_count, snapshot_started_at = _verified_export_artifact_metadata(
                existing_artifact,
                job_id=job_id,
                matter_id=matter_id,
                search_hash=search_hash,
                field_hash=field_hash,
                policy_hash=policy_hash,
                attempt_number=attempt_number,
                requested_filename=requested_filename,
            )
            with session_factory() as db:
                _complete_search_export(
                    db,
                    job_id=job_id,
                    artifact=existing_artifact,
                    requested_filename=requested_filename,
                    matched_count=matched_count,
                    exported_count=exported_count,
                    page_count=page_count,
                    snapshot_started_at=snapshot_started_at,
                    settings=settings,
                )
                db.commit()
            return

        query_vector = None
        if search.search_mode != "KEYWORD":
            query_vector = get_query_embedding_gateway().embed([search.query or ""], "query").embeddings[0]
        pit_id = client.open_point_in_time(index_name, keep_alive=PIT_KEEP_ALIVE)
        snapshot_started_at = utcnow()
        with session_factory() as db:
            job = db.get(MatterSearchExportJob, job_id)
            if job is None:
                raise ValueError("Search export job was not found")
            job.snapshot_started_at = snapshot_started_at
            transition_job_execution(
                db,
                job_type=MATTER_SEARCH_EXPORT_JOB_TYPE,
                job_id=job.id,
                current_stage="EXPORTING",
            )
            db.commit()

        source_paths = [str(field["source_path"]) for field in field_snapshot]
        matched_count = 0
        exported_count = 0
        page_count = 0
        search_after: list[Any] | None = None
        with tempfile.TemporaryFile(mode="w+b") as binary:
            import io

            text = io.TextIOWrapper(binary, encoding="utf-8", newline="", write_through=True)
            writer = csv.writer(text, delimiter=",", lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
            writer.writerow(
                [_serialize_header(field["header"], spreadsheet_safe=spreadsheet_safe) for field in field_snapshot]
            )
            while True:
                body = compile_search_export_request(
                    search,
                    definitions,
                    tenant_id=str(tenant_id),
                    matter_id=str(matter_id),
                    source_paths=source_paths,
                    page_size=settings.search_export_page_size,
                    query_vector=query_vector,
                    search_after=search_after,
                    required_filters=required_filters,
                )
                result = client.search_point_in_time(
                    pit_id,
                    body,
                    keep_alive=PIT_KEEP_ALIVE,
                    search_pipeline=RRF_SEARCH_PIPELINE if search.search_mode == "HYBRID" else None,
                )
                pit_id = str(result.get("pit_id") or pit_id)
                if result.get("timed_out") or result.get("_shards", {}).get("failed", 0):
                    raise SearchExportTransientError("OpenSearch returned a timeout or partial shard failure")
                hits = result.get("hits", {}).get("hits", [])
                if page_count == 0:
                    total = result.get("hits", {}).get("total", 0)
                    matched_count = int(total.get("value", 0) if isinstance(total, dict) else total)
                    if search.search_mode != "KEYWORD" and search.candidate_limit is not None:
                        matched_count = min(matched_count, search.candidate_limit)
                if not hits:
                    break
                remaining = matched_count - exported_count if matched_count else len(hits)
                page_hits = hits[:remaining] if remaining >= 0 else []
                for hit in page_hits:
                    source = hit.get("_source") or {}
                    writer.writerow(
                        [
                            _serialize_cell(
                                _source_value(source, str(field["source_path"])),
                                field,
                                spreadsheet_safe=spreadsheet_safe,
                            )
                            for field in field_snapshot
                        ]
                    )
                    if binary.tell() > settings.search_export_max_bytes:
                        raise ValueError("Search export exceeds the configured output size limit")
                exported_count += len(page_hits)
                page_count += 1
                search_after = hits[-1].get("sort")
                if search_after is None:
                    raise RuntimeError("OpenSearch export page did not return a search_after cursor")
                with session_factory() as db:
                    current = db.get(MatterSearchExportJob, job_id)
                    if current is None:
                        raise ValueError("Search export job was not found")
                    current.page_count = page_count
                    transition_job_execution(
                        db,
                        job_type=MATTER_SEARCH_EXPORT_JOB_TYPE,
                        job_id=job_id,
                        current_stage="EXPORTING",
                        progress_completed=exported_count,
                        progress_total=matched_count,
                        progress={"page_count": page_count, "snapshot_started_at": snapshot_started_at.isoformat()},
                    )
                    canceled = execution_cancel_requested(db, job_type=MATTER_SEARCH_EXPORT_JOB_TYPE, job_id=job_id)
                    if canceled:
                        transition_job_execution(
                            db,
                            job_type=MATTER_SEARCH_EXPORT_JOB_TYPE,
                            job_id=job_id,
                            status="CANCELED",
                            current_stage="CANCELED",
                            failure_retriable=False,
                        )
                        record_audit(
                            db,
                            tenant_id=current.tenant_id,
                            actor_user_id=current.created_by_user_id,
                            action="search_export.canceled",
                            target_type="matter_search_export_job",
                            target_id=current.id,
                            details={"matter_id": str(current.matter_id), "exported_count": exported_count},
                        )
                    db.commit()
                    if canceled:
                        return
                if exported_count >= matched_count:
                    break

            with session_factory() as db:
                current = db.get(MatterSearchExportJob, job_id)
                if current is None:
                    raise ValueError("Search export job was not found")
                canceled = execution_cancel_requested(db, job_type=MATTER_SEARCH_EXPORT_JOB_TYPE, job_id=job_id)
                if canceled:
                    transition_job_execution(
                        db,
                        job_type=MATTER_SEARCH_EXPORT_JOB_TYPE,
                        job_id=job_id,
                        status="CANCELED",
                        current_stage="CANCELED",
                        failure_retriable=False,
                    )
                    record_audit(
                        db,
                        tenant_id=current.tenant_id,
                        actor_user_id=current.created_by_user_id,
                        action="search_export.canceled",
                        target_type="matter_search_export_job",
                        target_id=current.id,
                        details={"matter_id": str(current.matter_id), "exported_count": exported_count},
                    )
                db.commit()
                if canceled:
                    return

            text.flush()
            byte_length = binary.tell()
            if byte_length > settings.search_export_max_bytes:
                raise ValueError("Search export exceeds the configured output size limit")
            binary.seek(0)
            digest = hashlib.sha256()
            while chunk := binary.read(1024 * 1024):
                digest.update(chunk)
            content_hash = digest.hexdigest()
            binary.seek(0)
            with session_factory() as db:
                current = db.get(MatterSearchExportJob, job_id)
                if current is None:
                    raise ValueError("Search export job was not found")
                transition_job_execution(
                    db,
                    job_type=MATTER_SEARCH_EXPORT_JOB_TYPE,
                    job_id=job_id,
                    current_stage="UPLOADING",
                )
                db.commit()
            artifact, _ = store_workflow_artifact(
                content=binary,
                artifact_type=ArtifactType.SEARCH_RESULT_EXPORT,
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
                    "search_index_generation": search_index_snapshot.get("generation"),
                    "search_index_schema_hash": search_index_snapshot.get("schema_hash"),
                    "search_definition_hash": search_hash,
                    "scope": search_index_snapshot.get("scope", {"kind": "MATTER"}),
                    "field_snapshot_hash": field_hash,
                    "csv_policy_version": CSV_POLICY_VERSION,
                    "csv_policy_hash": policy_hash,
                    "attempt_number": attempt_number,
                    "matched_count": matched_count,
                    "row_count": exported_count,
                    "page_count": page_count,
                    "column_count": len(field_snapshot),
                    "snapshot_started_at": snapshot_started_at.isoformat(),
                    "filename": requested_filename,
                    "content_hash": content_hash,
                    "byte_length": byte_length,
                },
                lineage=[],
                actor_user_id=actor_user_id,
                tenant_id=tenant_id,
                client_id=client_id,
                filename=requested_filename,
            )

            _verified_export_artifact_metadata(
                artifact,
                job_id=job_id,
                matter_id=matter_id,
                search_hash=search_hash,
                field_hash=field_hash,
                policy_hash=policy_hash,
                attempt_number=attempt_number,
                requested_filename=requested_filename,
            )

        with session_factory() as db:
            _complete_search_export(
                db,
                job_id=job_id,
                artifact=artifact,
                requested_filename=requested_filename,
                matched_count=matched_count,
                exported_count=exported_count,
                page_count=page_count,
                snapshot_started_at=snapshot_started_at,
                settings=settings,
            )
            db.commit()
    finally:
        if pit_id is not None:
            try:
                client.close_point_in_time(pit_id)
            except Exception:
                logger.warning("Could not close search export PIT job_id=%s", job_id, exc_info=True)
        client.close()


def _search_export_failure(error: BaseException) -> tuple[str, bool]:
    if isinstance(error, SearchExportTransientError):
        return "SEARCH_EXPORT_SEARCH_UNAVAILABLE", True
    if isinstance(error, httpx.HTTPStatusError):
        return "SEARCH_EXPORT_DEPENDENCY_FAILED", error.response.status_code in {408, 429} or error.response.status_code >= 500
    if isinstance(error, httpx.TransportError):
        return "SEARCH_EXPORT_DEPENDENCY_UNAVAILABLE", True
    if isinstance(error, (OperationalError, ProgrammingError)):
        return "SEARCH_EXPORT_STORAGE_UNAVAILABLE", True
    if is_retryable_opensearch_error(error) or isinstance(error, (ConnectionError, TimeoutError)):
        return "SEARCH_EXPORT_SEARCH_UNAVAILABLE", True
    if isinstance(error, PermissionError):
        return "SEARCH_EXPORT_AUTHORIZATION_REVOKED", False
    if isinstance(error, (TypeError, ValueError)):
        return "SEARCH_EXPORT_INVALID", False
    return "SEARCH_EXPORT_FAILED", False


def record_search_export_attempt_failure(db: Session, job_id: uuid.UUID, error: BaseException) -> None:
    """Retain the original step error classification across DBOS retry wrapping."""

    job = db.get(MatterSearchExportJob, job_id)
    if job is None:
        return
    code, retriable = _search_export_failure(error)
    job.failure_code = code
    job.failure_summary = str(error)[:4000]
    job.failure_retriable = retriable


def fail_search_export_job(db: Session, job_id: uuid.UUID, error: BaseException) -> None:
    job = db.get(MatterSearchExportJob, job_id)
    if job is None:
        return
    execution = current_job_execution(db, job_type=MATTER_SEARCH_EXPORT_JOB_TYPE, job_id=job_id)
    if execution is None or execution.status in {"COMPLETED", "FAILED", "CANCELED"}:
        return
    fallback_code, fallback_retriable = _search_export_failure(error)
    failure_code = job.failure_code or fallback_code
    failure_summary = job.failure_summary or str(error)[:4000]
    retriable = job.failure_retriable or fallback_retriable
    transition_job_execution(
        db,
        job_type=MATTER_SEARCH_EXPORT_JOB_TYPE,
        job_id=job_id,
        status="FAILED",
        current_stage="FAILED",
        failure_code=failure_code,
        failure_summary=failure_summary,
        failure_retriable=retriable,
    )
    record_audit(
        db,
        tenant_id=job.tenant_id,
        actor_user_id=job.created_by_user_id,
        action="search_export.failed",
        target_type="matter_search_export_job",
        target_id=job.id,
        details={"matter_id": str(job.matter_id), "retriable": retriable},
    )
