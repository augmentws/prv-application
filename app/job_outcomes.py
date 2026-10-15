"""Reusable, Artifact-backed item outcomes for document-oriented jobs."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import tempfile
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, BinaryIO, Literal

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.artifact_gateway import (
    WorkflowArtifactReference,
    copy_workflow_artifact_content,
    find_workflow_artifact_reference,
    get_workflow_artifact_reference,
    store_workflow_artifact,
)
from app.audit import record_audit
from app.embeddings.configuration import canonical_hash
from app.job_executions import MATTER_EMBEDDING_JOB_TYPE, current_job_execution
from app.models import JobOutcomeManifest, Matter, MatterEmbeddingBatch, MatterEmbeddingJob, WorkflowRun
from artifact_service.schemas import WorkflowArtifactLineageInput
from artifact_service.workflow_artifact_types import ArtifactType

OUTCOME_SCHEMA = "priv-view.job-item-outcomes"
OUTCOME_SCHEMA_VERSION = 1
OUTCOME_ROW_GROUP_SIZE = 10_000
OUTCOME_STATUSES = ("SUCCEEDED", "REUSED", "SKIPPED", "FAILED", "EXCLUDED", "CANCELED")
OUTCOME_REASON_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
OutcomeStatus = Literal["SUCCEEDED", "REUSED", "SKIPPED", "FAILED", "EXCLUDED", "CANCELED"]

OUTCOME_ARROW_SCHEMA = pa.schema(
    [
        pa.field("ordinal", pa.int64(), nullable=False),
        pa.field("item_kind", pa.string(), nullable=False),
        pa.field("item_id", pa.string(), nullable=False),
        pa.field("outcome_status", pa.string(), nullable=False),
        pa.field("reason_code", pa.string(), nullable=False),
        pa.field("message", pa.string()),
        pa.field("batch_id", pa.string()),
        pa.field("attempt_number", pa.int32(), nullable=False),
        pa.field("input_fingerprint", pa.string()),
        pa.field("output_references_json", pa.string(), nullable=False),
        pa.field("metrics_json", pa.string(), nullable=False),
        pa.field("payload_json", pa.string(), nullable=False),
        pa.field("started_at", pa.timestamp("us", tz="UTC")),
        pa.field("completed_at", pa.timestamp("us", tz="UTC")),
    ]
)


class JobOutcomeError(ValueError):
    pass


@dataclass(frozen=True)
class JobItemOutcome:
    item_id: str
    outcome_status: OutcomeStatus
    reason_code: str
    message: str | None = None
    input_fingerprint: str | None = None
    output_references: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class JobOutcomeAdapter:
    job_type: str
    outcome_schema: str
    item_kind: str

    def load(self, db: Session, job_id: uuid.UUID) -> MatterEmbeddingJob | None:
        if self.job_type == MATTER_EMBEDDING_JOB_TYPE:
            return db.get(MatterEmbeddingJob, job_id)
        raise JobOutcomeError(f"Unsupported outcome job type: {self.job_type}")

    def matter(self, db: Session, job: MatterEmbeddingJob) -> Matter | None:
        return db.get(Matter, job.matter_id)


_ADAPTERS = {
    MATTER_EMBEDDING_JOB_TYPE: JobOutcomeAdapter(
        job_type=MATTER_EMBEDDING_JOB_TYPE,
        outcome_schema=OUTCOME_SCHEMA,
        item_kind="MATTER_DOCUMENT",
    )
}


def get_job_outcome_adapter(job_type: str) -> JobOutcomeAdapter:
    try:
        return _ADAPTERS[job_type]
    except KeyError as exc:
        raise JobOutcomeError(f"Job type {job_type} does not expose item outcomes") from exc


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _bounded_canonical_json(value: Any, *, field_name: str) -> str:
    encoded = _canonical_json(value)
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise JobOutcomeError(f"Outcome {field_name} exceeds the 64 KiB row limit")
    return encoded


def _sanitize_message(message: str | None) -> str | None:
    if not message:
        return None
    single_line = " ".join(str(message).replace("\x00", "").split())
    return single_line[:1000] or None


def _schema_with_metadata(metadata: dict[str, Any]) -> pa.Schema:
    return OUTCOME_ARROW_SCHEMA.with_metadata(
        {f"priv_view.{key}".encode(): str(value).encode() for key, value in metadata.items()}
    )


def _row(
    outcome: JobItemOutcome,
    *,
    ordinal: int,
    item_kind: str,
    batch_id: uuid.UUID,
    attempt_number: int,
) -> dict[str, Any]:
    if outcome.outcome_status not in OUTCOME_STATUSES:
        raise JobOutcomeError(f"Unsupported outcome status: {outcome.outcome_status}")
    if len(outcome.reason_code) > 200 or OUTCOME_REASON_PATTERN.fullmatch(outcome.reason_code) is None:
        raise JobOutcomeError("Outcome reason codes must be lowercase namespaced values up to 200 characters")
    return {
        "ordinal": ordinal,
        "item_kind": item_kind,
        "item_id": outcome.item_id,
        "outcome_status": outcome.outcome_status,
        "reason_code": outcome.reason_code,
        "message": _sanitize_message(outcome.message),
        "batch_id": str(batch_id),
        "attempt_number": attempt_number,
        "input_fingerprint": outcome.input_fingerprint,
        "output_references_json": _bounded_canonical_json(
            outcome.output_references,
            field_name="output references",
        ),
        "metrics_json": _bounded_canonical_json(outcome.metrics, field_name="metrics"),
        "payload_json": _bounded_canonical_json(outcome.payload, field_name="payload"),
        "started_at": outcome.started_at,
        "completed_at": outcome.completed_at,
    }


def _hash_file(content: BinaryIO) -> tuple[str, int]:
    content.seek(0)
    digest = hashlib.sha256()
    length = 0
    while chunk := content.read(1024 * 1024):
        digest.update(chunk)
        length += len(chunk)
    content.seek(0)
    return digest.hexdigest(), length


def _manifest_derivation(job: MatterEmbeddingJob) -> str:
    return canonical_hash(
        {
            "contract": f"{OUTCOME_SCHEMA}-v{OUTCOME_SCHEMA_VERSION}",
            "job_type": MATTER_EMBEDDING_JOB_TYPE,
            "job_id": str(job.id),
            "configuration_hash": job.configuration_hash,
            "population": [job.total_count, job.batch_count],
        }
    )


def ensure_embedding_outcome_manifest(db: Session, job: MatterEmbeddingJob) -> JobOutcomeManifest:
    execution = current_job_execution(db, job_type=MATTER_EMBEDDING_JOB_TYPE, job_id=job.id)
    if execution is None:
        raise JobOutcomeError("Embedding job has no common execution attempt")
    manifest = db.scalar(
        select(JobOutcomeManifest).where(
            JobOutcomeManifest.job_type == MATTER_EMBEDDING_JOB_TYPE,
            JobOutcomeManifest.job_id == job.id,
        )
    )
    derivation = _manifest_derivation(job)
    if manifest is None:
        manifest = JobOutcomeManifest(
            tenant_id=job.matter.client.tenant_id,
            client_id=job.matter.client_id,
            matter_id=job.matter_id,
            job_type=MATTER_EMBEDDING_JOB_TYPE,
            job_id=job.id,
            manifest_status="BUILDING",
            outcome_schema=OUTCOME_SCHEMA,
            outcome_schema_version=OUTCOME_SCHEMA_VERSION,
            derivation_key=derivation,
            item_count=job.total_count,
            status_counts={},
            reason_counts={},
            producer_kind="CORE",
            producer_run_id=execution.id,
        )
        db.add(manifest)
        db.flush()
    elif (
        manifest.derivation_key != derivation
        or manifest.item_count != job.total_count
    ):
        raise JobOutcomeError("Embedding outcome manifest identity does not match its frozen job")
    return manifest


def embedding_fragment_derivation(job: MatterEmbeddingJob, batch: MatterEmbeddingBatch) -> str:
    return canonical_hash(
        {
            "contract": f"{OUTCOME_SCHEMA}-fragment-v{OUTCOME_SCHEMA_VERSION}",
            "job_id": str(job.id),
            "batch_id": str(batch.id),
            "batch_number": batch.batch_number,
            "configuration_hash": job.configuration_hash,
            "document_ids": batch.document_ids,
        }
    )


def publish_embedding_outcome_fragment(
    db: Session,
    *,
    job: MatterEmbeddingJob,
    batch: MatterEmbeddingBatch,
    outcomes: list[JobItemOutcome],
) -> WorkflowArtifactReference:
    manifest = ensure_embedding_outcome_manifest(db, job)
    if manifest.manifest_status == "FINALIZED":
        raise JobOutcomeError("Embedding outcome manifest is already finalized")
    if len(outcomes) != len(batch.document_ids):
        raise JobOutcomeError("Embedding batch outcomes do not cover the frozen batch")
    if [outcome.item_id for outcome in outcomes] != batch.document_ids:
        raise JobOutcomeError("Embedding batch outcomes do not preserve frozen document order")
    ordinal_start = int(
        db.scalar(
            select(func.coalesce(func.sum(MatterEmbeddingBatch.item_count), 0)).where(
                MatterEmbeddingBatch.job_id == job.id,
                MatterEmbeddingBatch.batch_number < batch.batch_number,
            )
        )
        or 0
    )
    execution = db.get(WorkflowRun, manifest.producer_run_id)
    if execution is None:
        raise JobOutcomeError("Embedding job has no common execution attempt")
    fragment_derivation = embedding_fragment_derivation(job, batch)
    metadata = {
        "outcome_schema": OUTCOME_SCHEMA,
        "outcome_schema_version": OUTCOME_SCHEMA_VERSION,
        "job_type": MATTER_EMBEDDING_JOB_TYPE,
        "job_id": str(job.id),
        "batch_id": str(batch.id),
        "batch_number": batch.batch_number,
        "ordinal_start": ordinal_start,
        "item_count": len(outcomes),
        "producer_run_id": str(execution.id),
        "tenant_id": str(job.matter.client.tenant_id),
        "client_id": str(job.matter.client_id),
        "matter_id": str(job.matter_id),
        "configuration_hash": job.configuration_hash,
        "derivation_key": fragment_derivation,
    }
    rows = [
        _row(
            outcome,
            ordinal=ordinal_start + index,
            item_kind="MATTER_DOCUMENT",
            batch_id=batch.id,
            attempt_number=execution.attempt_number,
        )
        for index, outcome in enumerate(outcomes)
    ]
    content = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")  # noqa: SIM115
    try:
        pq.write_table(
            pa.Table.from_pylist(rows, schema=_schema_with_metadata(metadata)),
            content,
            compression="zstd",
            row_group_size=OUTCOME_ROW_GROUP_SIZE,
            use_dictionary=["item_kind", "outcome_status", "reason_code"],
        )
        content_hash, byte_length = _hash_file(content)
        reference, _ = store_workflow_artifact(
            content=content,
            artifact_type=ArtifactType.JOB_ITEM_OUTCOME_FRAGMENT,
            core_scope_kind="MATTER",
            core_scope_id=job.matter_id,
            schema_version=OUTCOME_SCHEMA_VERSION,
            derivation_key=fragment_derivation,
            content_hash=content_hash,
            byte_length=byte_length,
            media_type="application/vnd.apache.parquet",
            producer_kind="CORE",
            producer_run_id=execution.id,
            artifact_metadata=metadata,
            lineage=[],
            actor_user_id=job.created_by_user_id,
            tenant_id=job.matter.client.tenant_id,
            client_id=job.matter.client_id,
            filename=f"embedding-outcomes-{batch.id}.parquet",
        )
        return reference
    finally:
        content.close()


def _copy_verified(reference: WorkflowArtifactReference, job: MatterEmbeddingJob) -> BinaryIO:
    output = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024, mode="w+b")  # noqa: SIM115
    copy_workflow_artifact_content(
        artifact_id=reference.artifact_id,
        destination=output,
        actor_user_id=job.created_by_user_id,
        tenant_id=job.matter.client.tenant_id,
        client_id=job.matter.client_id,
    )
    content_hash, byte_length = _hash_file(output)
    if content_hash != reference.content_hash or byte_length != reference.byte_length:
        output.close()
        raise JobOutcomeError("Job outcome artifact content does not match its verified reference")
    return output


def _validate_fragment(
    parquet: pq.ParquetFile,
    *,
    job: MatterEmbeddingJob,
    batch: MatterEmbeddingBatch,
    expected_ordinal: int,
) -> list[dict[str, Any]]:
    if parquet.schema_arrow.remove_metadata() != OUTCOME_ARROW_SCHEMA:
        raise JobOutcomeError("Job outcome fragment has an incompatible Parquet schema")
    metadata = {
        key.decode().removeprefix("priv_view."): value.decode()
        for key, value in (parquet.schema_arrow.metadata or {}).items()
        if key.decode().startswith("priv_view.")
    }
    expected_metadata = {
        "outcome_schema": OUTCOME_SCHEMA,
        "outcome_schema_version": str(OUTCOME_SCHEMA_VERSION),
        "job_type": MATTER_EMBEDDING_JOB_TYPE,
        "job_id": str(job.id),
        "batch_id": str(batch.id),
        "batch_number": str(batch.batch_number),
        "ordinal_start": str(expected_ordinal),
        "item_count": str(batch.item_count),
        "tenant_id": str(job.matter.client.tenant_id),
        "client_id": str(job.matter.client_id),
        "matter_id": str(job.matter_id),
        "configuration_hash": job.configuration_hash,
        "derivation_key": embedding_fragment_derivation(job, batch),
    }
    if any(metadata.get(key) != value for key, value in expected_metadata.items()):
        raise JobOutcomeError("Job outcome fragment metadata does not match its frozen batch")
    rows: list[dict[str, Any]] = []
    for record_batch in parquet.iter_batches(batch_size=OUTCOME_ROW_GROUP_SIZE):
        rows.extend(pa.Table.from_batches([record_batch]).to_pylist())
    if len(rows) != batch.item_count:
        raise JobOutcomeError("Job outcome fragment row count does not match its batch")
    if [row["ordinal"] for row in rows] != list(range(expected_ordinal, expected_ordinal + batch.item_count)):
        raise JobOutcomeError("Job outcome fragment ordinals are missing, duplicated, or out of order")
    if [row["item_id"] for row in rows] != batch.document_ids:
        raise JobOutcomeError("Job outcome fragment items do not match the frozen batch")
    if any(row["outcome_status"] not in OUTCOME_STATUSES for row in rows):
        raise JobOutcomeError("Job outcome fragment contains an unsupported status")
    return rows


def finalize_embedding_outcome_manifest(db: Session, job: MatterEmbeddingJob) -> JobOutcomeManifest:
    manifest = ensure_embedding_outcome_manifest(db, job)
    if manifest.manifest_status == "FINALIZED":
        return manifest
    batches = list(
        db.scalars(
            select(MatterEmbeddingBatch)
            .where(MatterEmbeddingBatch.job_id == job.id)
            .order_by(MatterEmbeddingBatch.batch_number)
        )
    )
    execution = db.get(WorkflowRun, manifest.producer_run_id)
    if execution is None:
        raise JobOutcomeError("Embedding job has no common execution attempt")
    references: list[WorkflowArtifactReference] = []
    status_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    final_metadata = {
        "outcome_schema": OUTCOME_SCHEMA,
        "outcome_schema_version": OUTCOME_SCHEMA_VERSION,
        "job_type": MATTER_EMBEDDING_JOB_TYPE,
        "job_id": str(job.id),
        "item_count": job.total_count,
        "producer_run_id": str(execution.id),
        "tenant_id": str(job.matter.client.tenant_id),
        "client_id": str(job.matter.client_id),
        "matter_id": str(job.matter_id),
        "configuration_hash": job.configuration_hash,
    }
    output = tempfile.SpooledTemporaryFile(max_size=128 * 1024 * 1024, mode="w+b")  # noqa: SIM115
    writer = pq.ParquetWriter(
        output,
        _schema_with_metadata(final_metadata),
        compression="zstd",
        use_dictionary=["item_kind", "outcome_status", "reason_code"],
    )
    ordinal = 0
    try:
        for batch in batches:
            reference = find_workflow_artifact_reference(
                artifact_type=ArtifactType.JOB_ITEM_OUTCOME_FRAGMENT,
                derivation_key=embedding_fragment_derivation(job, batch),
                actor_user_id=job.created_by_user_id,
                tenant_id=job.matter.client.tenant_id,
                client_id=job.matter.client_id,
            )
            if reference is None:
                raise JobOutcomeError(f"Embedding outcome fragment is missing for batch {batch.batch_number}")
            if (
                reference.tenant_id != job.matter.client.tenant_id
                or reference.client_id != job.matter.client_id
                or reference.artifact_type != ArtifactType.JOB_ITEM_OUTCOME_FRAGMENT
                or reference.schema_version != OUTCOME_SCHEMA_VERSION
                or reference.derivation_key != embedding_fragment_derivation(job, batch)
                or reference.core_scope_kind != "MATTER"
                or reference.core_scope_id != job.matter_id
                or reference.producer_kind != "CORE"
                or reference.producer_run_id != execution.id
            ):
                raise JobOutcomeError("Embedding outcome fragment scope or producer does not match the job")
            fragment_file = _copy_verified(reference, job)
            try:
                rows = _validate_fragment(
                    pq.ParquetFile(fragment_file),
                    job=job,
                    batch=batch,
                    expected_ordinal=ordinal,
                )
            finally:
                fragment_file.close()
            table = pa.Table.from_pylist(rows, schema=OUTCOME_ARROW_SCHEMA)
            writer.write_table(table, row_group_size=OUTCOME_ROW_GROUP_SIZE)
            status_counts.update(row["outcome_status"] for row in rows)
            reason_counts.update(row["reason_code"] for row in rows)
            ordinal += len(rows)
            references.append(reference)
    finally:
        writer.close()
    if ordinal != job.total_count:
        output.close()
        raise JobOutcomeError("Final job outcome row count does not match the frozen population")
    if sum(status_counts.values()) != job.total_count or any(
        status not in OUTCOME_STATUSES for status in status_counts
    ):
        output.close()
        raise JobOutcomeError("Final job outcome status counts do not reconcile")
    if len(reason_counts) > 1_000 or len(_canonical_json(dict(reason_counts)).encode()) > 64 * 1024:
        output.close()
        raise JobOutcomeError("Final job outcome reason distribution exceeds its bounded summary")
    final_metadata["status_counts"] = _canonical_json(dict(sorted(status_counts.items())))
    final_metadata["reason_counts"] = _canonical_json(dict(sorted(reason_counts.items())))
    fragment_hashes = [reference.content_hash for reference in references]
    final_derivation = canonical_hash(
        {
            "manifest_derivation": manifest.derivation_key,
            "fragment_hashes": fragment_hashes,
        }
    )
    final_metadata["derivation_key"] = final_derivation
    final_metadata["summary_hash"] = canonical_hash(
        {
            "item_count": job.total_count,
            "status_counts": dict(sorted(status_counts.items())),
            "reason_counts": dict(sorted(reason_counts.items())),
        }
    )
    # Rewrite only the Parquet footer metadata while retaining bounded row groups.
    output.seek(0)
    parquet = pq.ParquetFile(output)
    rewritten = tempfile.SpooledTemporaryFile(max_size=128 * 1024 * 1024, mode="w+b")  # noqa: SIM115
    final_writer = pq.ParquetWriter(
        rewritten,
        _schema_with_metadata(final_metadata),
        compression="zstd",
        use_dictionary=["item_kind", "outcome_status", "reason_code"],
    )
    try:
        for index in range(parquet.num_row_groups):
            final_writer.write_table(parquet.read_row_group(index), row_group_size=OUTCOME_ROW_GROUP_SIZE)
    finally:
        final_writer.close()
        output.close()
    content_hash, byte_length = _hash_file(rewritten)
    try:
        final_reference, _ = store_workflow_artifact(
            content=rewritten,
            artifact_type=ArtifactType.JOB_ITEM_OUTCOMES,
            core_scope_kind="MATTER",
            core_scope_id=job.matter_id,
            schema_version=OUTCOME_SCHEMA_VERSION,
            derivation_key=final_derivation,
            content_hash=content_hash,
            byte_length=byte_length,
            media_type="application/vnd.apache.parquet",
            producer_kind="CORE",
            producer_run_id=execution.id,
            artifact_metadata=final_metadata,
            lineage=[
                WorkflowArtifactLineageInput(
                    source_kind="WORKFLOW_ARTIFACT",
                    source_artifact_id=reference.artifact_id,
                )
                for reference in references
            ],
            actor_user_id=job.created_by_user_id,
            tenant_id=job.matter.client.tenant_id,
            client_id=job.matter.client_id,
            filename=f"embedding-job-outcomes-{job.id}.parquet",
        )
    finally:
        rewritten.close()
    verified = get_workflow_artifact_reference(
        artifact_id=final_reference.artifact_id,
        actor_user_id=job.created_by_user_id,
        tenant_id=job.matter.client.tenant_id,
        client_id=job.matter.client_id,
    )
    if (
        verified.tenant_id != job.matter.client.tenant_id
        or verified.client_id != job.matter.client_id
        or verified.core_scope_kind != "MATTER"
        or verified.core_scope_id != job.matter_id
        or verified.artifact_type != ArtifactType.JOB_ITEM_OUTCOMES
        or verified.schema_version != OUTCOME_SCHEMA_VERSION
        or verified.derivation_key != final_derivation
        or verified.content_hash != content_hash
        or verified.byte_length != byte_length
        or verified.producer_kind != "CORE"
        or verified.producer_run_id != execution.id
        or verified.metadata != final_metadata
    ):
        raise JobOutcomeError("Final job outcome artifact verification failed")
    manifest.artifact_id = verified.artifact_id
    manifest.artifact_content_hash = verified.content_hash
    manifest.artifact_byte_length = verified.byte_length
    manifest.status_counts = dict(sorted(status_counts.items()))
    manifest.reason_counts = dict(sorted(reason_counts.items()))
    manifest.manifest_status = "FINALIZED"
    manifest.finalized_at = datetime.now(timezone.utc)
    manifest.failure_summary = None
    record_audit(
        db,
        tenant_id=manifest.tenant_id,
        actor_user_id=job.created_by_user_id,
        action="job_outcome_manifest.finalized",
        target_type="job_outcome_manifest",
        target_id=manifest.id,
        details={
            "job_type": manifest.job_type,
            "job_id": str(job.id),
            "artifact_id": str(verified.artifact_id),
            "item_count": manifest.item_count,
        },
    )
    db.flush()
    return manifest


def finalize_canceled_embedding_outcome_manifest(
    db: Session,
    job: MatterEmbeddingJob,
) -> JobOutcomeManifest | None:
    """Complete a planned manifest after cancellation without inventing legacy data."""
    manifest = db.scalar(
        select(JobOutcomeManifest).where(
            JobOutcomeManifest.job_type == MATTER_EMBEDDING_JOB_TYPE,
            JobOutcomeManifest.job_id == job.id,
        )
    )
    if manifest is None or manifest.manifest_status == "FINALIZED":
        return manifest
    batches = list(
        db.scalars(
            select(MatterEmbeddingBatch)
            .where(MatterEmbeddingBatch.job_id == job.id)
            .order_by(MatterEmbeddingBatch.batch_number)
        )
    )
    for batch in batches:
        reference = find_workflow_artifact_reference(
            artifact_type=ArtifactType.JOB_ITEM_OUTCOME_FRAGMENT,
            derivation_key=embedding_fragment_derivation(job, batch),
            actor_user_id=job.created_by_user_id,
            tenant_id=job.matter.client.tenant_id,
            client_id=job.matter.client_id,
        )
        if reference is not None:
            continue
        publish_embedding_outcome_fragment(
            db,
            job=job,
            batch=batch,
            outcomes=[
                JobItemOutcome(
                    item_id=document_id,
                    outcome_status="CANCELED",
                    reason_code="embedding.job_canceled",
                    message="Embedding was canceled before this document completed",
                    payload={"matter_document_id": document_id},
                )
                for document_id in batch.document_ids
            ],
        )
        batch.status = "CANCELED"
    return finalize_embedding_outcome_manifest(db, job)


def _verified_manifest_file(
    manifest: JobOutcomeManifest,
    *,
    actor_user_id: uuid.UUID,
) -> tuple[BinaryIO, WorkflowArtifactReference]:
    if manifest.manifest_status != "FINALIZED" or manifest.artifact_id is None:
        raise JobOutcomeError("Job item outcomes are not finalized")
    if manifest.client_id is None:
        raise JobOutcomeError("Job outcome manifest has no client scope")
    try:
        reference = get_workflow_artifact_reference(
            artifact_id=manifest.artifact_id,
            actor_user_id=actor_user_id,
            tenant_id=manifest.tenant_id,
            client_id=manifest.client_id,
        )
    except (PermissionError, ValueError) as exc:
        raise JobOutcomeError("Job outcome artifact is unavailable") from exc
    if (
        reference.artifact_type != ArtifactType.JOB_ITEM_OUTCOMES
        or reference.content_hash != manifest.artifact_content_hash
        or reference.byte_length != manifest.artifact_byte_length
        or reference.core_scope_kind != "MATTER"
        or reference.core_scope_id != manifest.matter_id
        or reference.producer_kind != manifest.producer_kind
        or reference.producer_run_id != manifest.producer_run_id
    ):
        raise JobOutcomeError("Job outcome manifest artifact identity does not match Core")
    expected_metadata = {
        "outcome_schema": manifest.outcome_schema,
        "outcome_schema_version": manifest.outcome_schema_version,
        "job_type": manifest.job_type,
        "job_id": str(manifest.job_id),
        "item_count": manifest.item_count,
        "producer_run_id": str(manifest.producer_run_id),
        "tenant_id": str(manifest.tenant_id),
        "client_id": str(manifest.client_id),
        "matter_id": str(manifest.matter_id),
        "derivation_key": reference.derivation_key,
    }
    if any(reference.metadata.get(key) != value for key, value in expected_metadata.items()):
        raise JobOutcomeError("Job outcome artifact metadata does not match Core")
    file = tempfile.SpooledTemporaryFile(max_size=128 * 1024 * 1024, mode="w+b")  # noqa: SIM115
    try:
        copy_workflow_artifact_content(
            artifact_id=reference.artifact_id,
            destination=file,
            actor_user_id=actor_user_id,
            tenant_id=manifest.tenant_id,
            client_id=manifest.client_id,
        )
    except (PermissionError, ValueError) as exc:
        file.close()
        raise JobOutcomeError("Job outcome artifact content is unavailable") from exc
    content_hash, byte_length = _hash_file(file)
    if content_hash != manifest.artifact_content_hash or byte_length != manifest.artifact_byte_length:
        file.close()
        raise JobOutcomeError("Job outcome artifact hash verification failed")
    return file, reference


def _validate_final_parquet(parquet: pq.ParquetFile, manifest: JobOutcomeManifest) -> None:
    if parquet.schema_arrow.remove_metadata() != OUTCOME_ARROW_SCHEMA:
        raise JobOutcomeError("Job outcome artifact has an incompatible schema")
    metadata = {
        key.decode().removeprefix("priv_view."): value.decode()
        for key, value in (parquet.schema_arrow.metadata or {}).items()
        if key.decode().startswith("priv_view.")
    }
    expected = {
        "outcome_schema": manifest.outcome_schema,
        "outcome_schema_version": str(manifest.outcome_schema_version),
        "job_type": manifest.job_type,
        "job_id": str(manifest.job_id),
        "item_count": str(manifest.item_count),
        "producer_run_id": str(manifest.producer_run_id),
        "tenant_id": str(manifest.tenant_id),
        "client_id": str(manifest.client_id),
        "matter_id": str(manifest.matter_id),
        "status_counts": _canonical_json(manifest.status_counts),
        "reason_counts": _canonical_json(manifest.reason_counts),
        "summary_hash": canonical_hash(
            {
                "item_count": manifest.item_count,
                "status_counts": manifest.status_counts,
                "reason_counts": manifest.reason_counts,
            }
        ),
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise JobOutcomeError("Job outcome Parquet metadata does not match Core")


def page_job_outcomes(
    manifest: JobOutcomeManifest,
    *,
    actor_user_id: uuid.UUID,
    status: str | None,
    reason_code: str | None,
    offset: int,
    limit: int,
) -> tuple[int, list[dict[str, Any]]]:
    if status is not None and status not in OUTCOME_STATUSES:
        raise JobOutcomeError(f"Unsupported outcome status: {status}")
    file, _ = _verified_manifest_file(manifest, actor_user_id=actor_user_id)
    try:
        parquet = pq.ParquetFile(file)
        _validate_final_parquet(parquet, manifest)
        total = 0
        page: list[dict[str, Any]] = []
        for row_group_index in range(parquet.num_row_groups):
            table = parquet.read_row_group(row_group_index)
            mask = None
            if status is not None:
                mask = pc.equal(table["outcome_status"], pa.scalar(status))
            if reason_code is not None:
                reason_mask = pc.equal(table["reason_code"], pa.scalar(reason_code))
                mask = reason_mask if mask is None else pc.and_(mask, reason_mask)
            if mask is not None:
                table = table.filter(mask)
            rows = table.to_pylist()
            for row in rows:
                if total >= offset and len(page) < limit:
                    row["output_references"] = json.loads(row.pop("output_references_json"))
                    row["metrics"] = json.loads(row.pop("metrics_json"))
                    row["payload"] = json.loads(row.pop("payload_json"))
                    page.append(row)
                total += 1
        return total, page
    finally:
        file.close()


def iter_job_outcome_artifact(
    manifest: JobOutcomeManifest,
    *,
    actor_user_id: uuid.UUID,
) -> Iterator[bytes]:
    file, _ = _verified_manifest_file(manifest, actor_user_id=actor_user_id)

    def generate() -> Iterator[bytes]:
        try:
            while chunk := file.read(1024 * 1024):
                yield chunk
        finally:
            file.close()

    return generate()


def iter_job_outcomes_csv(
    manifest: JobOutcomeManifest,
    *,
    actor_user_id: uuid.UUID,
) -> Iterator[bytes]:
    file, _ = _verified_manifest_file(manifest, actor_user_id=actor_user_id)
    parquet = pq.ParquetFile(file)
    _validate_final_parquet(parquet, manifest)

    def generate() -> Iterator[bytes]:
        try:
            fields = [field.name for field in OUTCOME_ARROW_SCHEMA]
            header = io.StringIO(newline="")
            csv.DictWriter(header, fieldnames=fields).writeheader()
            yield header.getvalue().encode("utf-8")
            for batch in parquet.iter_batches(batch_size=OUTCOME_ROW_GROUP_SIZE):
                buffer = io.StringIO(newline="")
                writer = csv.DictWriter(buffer, fieldnames=fields)
                for row in pa.Table.from_batches([batch]).to_pylist():
                    writer.writerow(row)
                yield buffer.getvalue().encode("utf-8")
        finally:
            file.close()

    return generate()
