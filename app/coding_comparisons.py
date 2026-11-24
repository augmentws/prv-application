from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.artifact_gateway import copy_workflow_artifact_content, store_workflow_artifact
from app.audit import record_audit
from app.job_executions import (
    CODING_COMPARISON_JOB_TYPE,
    JobExecutionConflict,
    JobScope,
    create_job_execution,
    current_job_execution,
    transition_job_execution,
)
from app.models import (
    ClassificationScoringRun,
    DocumentMetadataCurrent,
    Matter,
    MatterCodingComparison,
    MatterDocument,
    MetadataDefinition,
    ReviewBatch,
    ReviewBatchDocument,
    ReviewBatchReviewerCoding,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewBatchRunValue,
)
from app.reviewer_coding import resolve_reviewer_coding_as_of
from app.schemas import MatterCodingComparisonCreate
from artifact_service.schemas import WorkflowArtifactLineageInput
from artifact_service.workflow_artifact_types import ArtifactType

SCHEMA_VERSION = 1
PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
TERMINAL_SCORING_STATUSES = {"COMPLETED", "COMPLETED_WITH_ERRORS"}
SUMMARY_VALUE_LIMIT = 200
SUMMARY_CONFUSION_LIMIT = 400


@dataclass(frozen=True)
class ClassifierRunSnapshot:
    id: uuid.UUID
    tenant_id: uuid.UUID
    model_version_id: uuid.UUID
    prediction_artifact_id: uuid.UUID | None
    prediction_content_hash: str | None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _typed_value(record: Any) -> Any:
    for name in (
        "value_text",
        "value_long",
        "value_float",
        "value_boolean",
        "value_date",
        "value_datetime",
        "value_json",
    ):
        value = getattr(record, name, None)
        if value is not None:
            return value
    return None


def _value_key(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return str(value)


def _mapped_values(raw_values: list[str], mapping: dict[str, str]) -> tuple[str, list[str]]:
    if not raw_values:
        return "EVALUATED", []
    mapped: list[str] = []
    for value in raw_values:
        target = mapping.get(value, value if not mapping else None)
        if target is None:
            return "UNMAPPED", []
        mapped.append(target)
    return "EVALUATED", sorted(set(mapped))


def create_comparison(
    db: Session,
    matter: Matter,
    payload: MatterCodingComparisonCreate,
    *,
    actor_user_id: uuid.UUID,
) -> tuple[MatterCodingComparison, bool]:
    configuration = payload.model_dump(mode="json", exclude={"name", "idempotency_key"})
    configuration_hash = canonical_hash(configuration)
    existing = db.scalar(
        select(MatterCodingComparison).where(
            MatterCodingComparison.matter_id == matter.id,
            MatterCodingComparison.created_by_user_id == actor_user_id,
            MatterCodingComparison.idempotency_key == payload.idempotency_key,
        )
    )
    if existing is not None:
        if existing.configuration_hash != configuration_hash:
            raise JobExecutionConflict("Idempotency key already belongs to a different coding comparison")
        return existing, False

    # Load the exact set so a missing or foreign batch cannot be hidden by a partial match.
    batches = list(
        db.scalars(
            select(ReviewBatch).where(
                ReviewBatch.id.in_(payload.review_batch_ids),
                ReviewBatch.matter_id == matter.id,
                ReviewBatch.status == "READY",
            )
        )
    )
    if len(batches) != len(payload.review_batch_ids):
        raise JobExecutionConflict("Every selected review batch must exist in this matter and be ready")
    _validate_sources(db, matter.id, payload)

    job_id = uuid.uuid4()
    job = MatterCodingComparison(
        id=job_id,
        tenant_id=matter.client.tenant_id,
        matter_id=matter.id,
        name=payload.name,
        idempotency_key=payload.idempotency_key,
        configuration=configuration,
        configuration_hash=configuration_hash,
        status="QUEUED",
        workflow_id=f"matter-coding-comparison:{job_id}:attempt:1",
        current_stage="QUEUED",
        created_by_user_id=actor_user_id,
    )
    db.add(job)
    db.flush()
    create_job_execution(
        db,
        job_type=CODING_COMPARISON_JOB_TYPE,
        job_id=job.id,
        scope=JobScope(job.tenant_id, matter.client_id, matter.id),
        workflow_key="matter_coding_comparison_v1",
        code_version="1",
        dbos_workflow_id=job.workflow_id,
        initiated_by_user_id=actor_user_id,
        idempotency_key=f"coding-comparison:{job.id}:attempt:1",
        current_stage="QUEUED",
        progress_unit="documents",
        configuration_snapshot={"configuration_hash": configuration_hash},
    )
    record_audit(
        db,
        tenant_id=job.tenant_id,
        actor_user_id=actor_user_id,
        action="coding_comparison.requested",
        target_type="matter_coding_comparison",
        target_id=job.id,
        details={
            "matter_id": str(matter.id),
            "review_batch_count": len(payload.review_batch_ids),
            "source_count": len(payload.sources),
            "configuration_hash": configuration_hash,
        },
    )
    return job, True


def _validate_sources(db: Session, matter_id: uuid.UUID, payload: MatterCodingComparisonCreate) -> None:
    definition_ids = {source.metadata_definition_id for source in payload.sources}
    definitions = set(
        db.scalars(
            select(MetadataDefinition.id).where(
                MetadataDefinition.id.in_(definition_ids),
                MetadataDefinition.matter_id == matter_id,
                MetadataDefinition.status == "ACTIVE",
            )
        )
    )
    if definitions != definition_ids:
        raise JobExecutionConflict("Every comparison field must be active in this matter")
    for source in payload.sources:
        if source.source_type == "QA_RUN_GROUP":
            runs = list(
                db.scalars(
                    select(ReviewBatchRun).where(
                        ReviewBatchRun.id.in_(source.run_ids),
                        ReviewBatchRun.review_batch_id.in_(payload.review_batch_ids),
                        ReviewBatchRun.status.in_(("COMPLETED", "COMPLETED_WITH_ERRORS")),
                    )
                )
            )
            if len(runs) != len(source.run_ids):
                raise JobExecutionConflict(f"Source {source.label} contains a run outside the selected ready batches")
        elif source.source_type == "REVIEWER_CODING_GROUP":
            snapshots = {snapshot.id: snapshot.revision for snapshot in source.reviewer_codings}
            codings = list(
                db.scalars(
                    select(ReviewBatchReviewerCoding).where(
                        ReviewBatchReviewerCoding.id.in_(snapshots),
                        ReviewBatchReviewerCoding.review_batch_id.in_(payload.review_batch_ids),
                    )
                )
            )
            if len(codings) != len(snapshots) or any(
                coding.revision != snapshots[coding.id] for coding in codings
            ):
                raise JobExecutionConflict(
                    f"Reviewer source {source.label} changed or is outside the selected ready batches; refresh and retry"
                )
        elif source.source_type == "CLASSIFIER_SCORING_RUN":
            run = db.get(ClassificationScoringRun, source.scoring_run_id)
            if run is None or run.matter_id != matter_id or run.status not in TERMINAL_SCORING_STATUSES:
                raise JobExecutionConflict(f"Classifier source {source.label} is not a completed run for this matter")
            target_id = run.classification_model.target_metadata_definition_id
            if target_id != source.metadata_definition_id:
                raise JobExecutionConflict(f"Classifier source {source.label} does not target the selected field")


def _population(db: Session, matter_id: uuid.UUID, batch_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
    rows = db.execute(
        select(
            ReviewBatchDocument.matter_document_id,
            MatterDocument.collection_item_id,
            ReviewBatchDocument.review_batch_id,
        )
        .join(MatterDocument, MatterDocument.id == ReviewBatchDocument.matter_document_id)
        .where(
            ReviewBatchDocument.review_batch_id.in_(batch_ids),
            MatterDocument.matter_id == matter_id,
        )
        .order_by(ReviewBatchDocument.matter_document_id, ReviewBatchDocument.review_batch_id)
    )
    grouped: dict[uuid.UUID, dict[str, Any]] = {}
    for document_id, collection_item_id, batch_id in rows:
        entry = grouped.setdefault(
            document_id,
            {
                "matter_document_id": document_id,
                "collection_item_id": collection_item_id,
                "batch_ids": [],
            },
        )
        entry["batch_ids"].append(batch_id)
    return [{**entry, "ordinal": ordinal} for ordinal, entry in enumerate(grouped.values(), start=1)]


def _qa_states(
    db: Session,
    document_ids: set[uuid.UUID],
    source: dict[str, Any],
) -> dict[uuid.UUID, dict[str, Any]]:
    run_ids = [uuid.UUID(value) for value in source["run_ids"]]
    definition_id = uuid.UUID(source["metadata_definition_id"])
    status_rows = db.execute(
        select(
            ReviewBatchRunDocument.matter_document_id,
            ReviewBatchRunDocument.review_batch_run_id,
            ReviewBatchRunDocument.status,
        ).where(
            ReviewBatchRunDocument.review_batch_run_id.in_(run_ids),
            ReviewBatchRunDocument.matter_document_id.in_(document_ids),
        )
    )
    statuses: dict[uuid.UUID, list[str]] = defaultdict(list)
    for document_id, _, status in status_rows:
        statuses[document_id].append(status)
    value_rows = db.scalars(
        select(ReviewBatchRunValue).where(
            ReviewBatchRunValue.review_batch_run_id.in_(run_ids),
            ReviewBatchRunValue.metadata_definition_id == definition_id,
            ReviewBatchRunValue.matter_document_id.in_(document_ids),
        )
    )
    values_by_document_run: dict[tuple[uuid.UUID, uuid.UUID], list[str]] = defaultdict(list)
    for row in value_rows:
        values_by_document_run[(row.matter_document_id, row.review_batch_run_id)].append(_value_key(_typed_value(row)))

    result: dict[uuid.UUID, dict[str, Any]] = {}
    mapping = source.get("value_mapping") or {}
    for document_id in document_ids:
        doc_statuses = statuses.get(document_id, [])
        run_value_sets = {
            tuple(sorted(set(values)))
            for (value_document_id, _), values in values_by_document_run.items()
            if value_document_id == document_id
        }
        if len(run_value_sets) > 1:
            result[document_id] = {"state": "CONFLICTED", "raw_value_keys": [], "value_keys": []}
            continue
        raw = list(next(iter(run_value_sets))) if run_value_sets else []
        if raw and "COMPLETED" in doc_statuses:
            state, mapped = _mapped_values(raw, mapping)
        elif raw:
            state, mapped = "INCOMPLETE", []
        elif "FAILED" in doc_statuses:
            state, mapped = "FAILED", []
        elif "SKIPPED" in doc_statuses:
            state, mapped = "SKIPPED", []
        elif "COMPLETED" in doc_statuses:
            state, mapped = "EVALUATED", []
        elif doc_statuses:
            state, mapped = "INCOMPLETE", []
        else:
            state, mapped = "MISSING", []
        result[document_id] = {"state": state, "raw_value_keys": raw, "value_keys": mapped}
    return result


def _matter_field_states(
    db: Session,
    document_ids: set[uuid.UUID],
    source: dict[str, Any],
) -> dict[uuid.UUID, dict[str, Any]]:
    definition_id = uuid.UUID(source["metadata_definition_id"])
    rows = db.scalars(
        select(DocumentMetadataCurrent).where(
            DocumentMetadataCurrent.metadata_definition_id == definition_id,
            DocumentMetadataCurrent.matter_document_id.in_(document_ids),
        )
    )
    grouped: dict[uuid.UUID, list[DocumentMetadataCurrent]] = defaultdict(list)
    for row in rows:
        grouped[row.matter_document_id].append(row)
    result: dict[uuid.UUID, dict[str, Any]] = {}
    mapping = source.get("value_mapping") or {}
    for document_id in document_ids:
        values = grouped.get(document_id, [])
        if not values:
            result[document_id] = {"state": "MISSING", "raw_value_keys": [], "value_keys": []}
            continue
        if any(value.resolution_state == "CONFLICTED" for value in values):
            result[document_id] = {"state": "CONFLICTED", "raw_value_keys": [], "value_keys": []}
            continue
        if any(value.resolution_state == "PENDING" for value in values):
            result[document_id] = {"state": "INCOMPLETE", "raw_value_keys": [], "value_keys": []}
            continue
        raw = sorted({_value_key(_typed_value(value)) for value in values if value.resolution_state == "VALUE"})
        state, mapped = _mapped_values(raw, mapping)
        result[document_id] = {"state": state, "raw_value_keys": raw, "value_keys": mapped}
    return result


def _classifier_states(
    run: ClassifierRunSnapshot,
    client_id: uuid.UUID,
    document_ids: set[uuid.UUID],
    source: dict[str, Any],
    *,
    actor_user_id: uuid.UUID,
) -> tuple[dict[uuid.UUID, dict[str, Any]], uuid.UUID]:
    if run.prediction_artifact_id is None or run.prediction_content_hash is None:
        raise JobExecutionConflict("Classifier prediction artifact is unavailable")
    with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as artifact_file:
        copy_workflow_artifact_content(
            artifact_id=run.prediction_artifact_id,
            destination=artifact_file,
            actor_user_id=actor_user_id,
            tenant_id=run.tenant_id,
            client_id=client_id,
        )
        artifact_file.seek(0)
        digest = hashlib.sha256()
        while chunk := artifact_file.read(1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != run.prediction_content_hash:
            raise JobExecutionConflict("Classifier prediction artifact hash does not match the scoring run")
        artifact_file.seek(0)
        parquet = pq.ParquetFile(artifact_file)
        metadata = parquet.schema_arrow.metadata or {}
        required_columns = {
            "matter_document_id",
            "result_status",
            "failure_reason",
            "predicted_class_keys",
            "abstained",
        }
        if (
            metadata.get(b"pvr.schema") != b"classification-scoring-predictions"
            or metadata.get(b"pvr.scoring_run_id") != str(run.id).encode()
            or metadata.get(b"pvr.model_version_id") != str(run.model_version_id).encode()
            or not required_columns.issubset(parquet.schema_arrow.names)
        ):
            raise JobExecutionConflict("Classifier prediction artifact identity or schema is invalid")
        by_document: dict[uuid.UUID, dict[str, Any]] = {}
        for row_group_index in range(parquet.num_row_groups):
            table = parquet.read_row_group(row_group_index, columns=sorted(required_columns))
            for row in table.to_pylist():
                document_id = uuid.UUID(str(row["matter_document_id"]))
                if document_id in document_ids:
                    by_document[document_id] = row
    mapping = source.get("value_mapping") or {}
    result: dict[uuid.UUID, dict[str, Any]] = {}
    for document_id in document_ids:
        row = by_document.get(document_id)
        if row is None:
            result[document_id] = {"state": "MISSING", "raw_value_keys": [], "value_keys": []}
            continue
        if row["result_status"] != "SUCCESS":
            state = "FAILED" if row["result_status"] == "FAILED" else "SKIPPED"
            result[document_id] = {"state": state, "raw_value_keys": [], "value_keys": []}
            continue
        if row.get("abstained"):
            result[document_id] = {"state": "INCOMPLETE", "raw_value_keys": [], "value_keys": []}
            continue
        raw = sorted(str(value) for value in (row.get("predicted_class_keys") or []))
        state, mapped = _mapped_values(raw, mapping)
        result[document_id] = {"state": state, "raw_value_keys": raw, "value_keys": mapped}
    return result, run.prediction_artifact_id


def _agreement(sources: list[dict[str, Any]]) -> str:
    if any(source["state"] != "EVALUATED" for source in sources):
        return "INCOMPLETE"
    values = [set(source["value_keys"]) for source in sources]
    if all(value == values[0] for value in values[1:]):
        return "EXACT"
    if values and set.intersection(*values):
        return "PARTIAL"
    return "DISAGREEMENT"


def _reviewer_states(
    db: Session,
    document_ids: set[uuid.UUID],
    source: dict[str, Any],
) -> dict[uuid.UUID, dict[str, Any]]:
    definition_id = uuid.UUID(source["metadata_definition_id"])
    mapping = source.get("value_mapping") or {}
    candidates: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    for snapshot in source["reviewer_codings"]:
        resolved = resolve_reviewer_coding_as_of(
            db,
            reviewer_coding_id=uuid.UUID(snapshot["id"]),
            revision=int(snapshot["revision"]),
            document_ids=list(document_ids),
            metadata_definition_ids=[definition_id],
        )
        values: dict[uuid.UUID, list[str]] = defaultdict(list)
        for value in resolved.values:
            values[value.matter_document_id].append(_value_key(_typed_value(value)))
        for document in resolved.documents:
            if document.status == "SKIPPED":
                candidates[document.matter_document_id].append(
                    {"state": "SKIPPED", "raw_value_keys": [], "value_keys": []}
                )
                continue
            if document.status != "COMPLETED":
                candidates[document.matter_document_id].append(
                    {"state": "INCOMPLETE", "raw_value_keys": [], "value_keys": []}
                )
                continue
            raw = sorted(set(values.get(document.matter_document_id, [])))
            state, mapped = _mapped_values(raw, mapping)
            candidates[document.matter_document_id].append(
                {"state": state, "raw_value_keys": raw, "value_keys": mapped}
            )

    result: dict[uuid.UUID, dict[str, Any]] = {}
    for document_id in document_ids:
        document_candidates = candidates.get(document_id, [])
        if not document_candidates:
            result[document_id] = {"state": "MISSING", "raw_value_keys": [], "value_keys": []}
        elif all(candidate == document_candidates[0] for candidate in document_candidates[1:]):
            result[document_id] = document_candidates[0]
        else:
            result[document_id] = {"state": "CONFLICTED", "raw_value_keys": [], "value_keys": []}
    return result


def _summary(rows: list[dict[str, Any]], sources: list[dict[str, Any]]) -> dict[str, Any]:
    source_summary: list[dict[str, Any]] = []
    for source in sources:
        states = Counter()
        values = Counter()
        no_value_count = 0
        for row in rows:
            item = next(value for value in row["sources"] if value["source_id"] == source["source_id"])
            states[item["state"]] += 1
            values.update(item["value_keys"])
            if item["state"] == "EVALUATED" and not item["value_keys"]:
                no_value_count += 1
        source_summary.append(
            {
                "source_id": source["source_id"],
                "label": source["label"],
                "source_type": source["source_type"],
                "state_counts": dict(sorted(states.items())),
                "value_counts": dict(
                    sorted(values.items(), key=lambda item: (-item[1], item[0]))[:SUMMARY_VALUE_LIMIT]
                ),
                "value_counts_truncated": len(values) > SUMMARY_VALUE_LIMIT,
                "evaluated_document_count": states["EVALUATED"],
                "no_value_document_count": no_value_count,
            }
        )
    pairwise: list[dict[str, Any]] = []
    for left_index, left in enumerate(sources):
        for right in sources[left_index + 1 :]:
            counts = Counter()
            matrix = Counter()
            jaccards: list[float] = []
            for row in rows:
                left_value = next(value for value in row["sources"] if value["source_id"] == left["source_id"])
                right_value = next(value for value in row["sources"] if value["source_id"] == right["source_id"])
                status = _agreement([left_value, right_value])
                counts[status] += 1
                if status != "INCOMPLETE":
                    left_values = set(left_value["value_keys"])
                    right_values = set(right_value["value_keys"])
                    union = left_values | right_values
                    jaccards.append((len(left_values & right_values) / len(union)) if union else 1.0)
                    matrix[
                        (
                            " | ".join(left_value["value_keys"]) or "__NO_VALUE__",
                            " | ".join(right_value["value_keys"]) or "__NO_VALUE__",
                        )
                    ] += 1
            compared = sum(counts[key] for key in ("EXACT", "PARTIAL", "DISAGREEMENT"))
            pairwise.append(
                {
                    "left_source_id": left["source_id"],
                    "right_source_id": right["source_id"],
                    "compared_document_count": compared,
                    "incomplete_document_count": counts["INCOMPLETE"],
                    "agreement_counts": dict(sorted(counts.items())),
                    "exact_agreement": (counts["EXACT"] / compared) if compared else None,
                    "mean_jaccard": (sum(jaccards) / len(jaccards)) if jaccards else None,
                    "confusion_matrix": [
                        {"left_value": key[0], "right_value": key[1], "document_count": count}
                        for key, count in sorted(matrix.items(), key=lambda item: (-item[1], item[0]))[
                            :SUMMARY_CONFUSION_LIMIT
                        ]
                    ],
                    "confusion_matrix_truncated": len(matrix) > SUMMARY_CONFUSION_LIMIT,
                }
            )

    fully_evaluated_rows = [
        row for row in rows if all(item["state"] == "EVALUATED" for item in row["sources"])
    ]
    canonical_value_counts = Counter(
        value_key
        for row in fully_evaluated_rows
        for item in row["sources"]
        for value_key in item["value_keys"]
    )
    canonical_values = [
        value_key
        for value_key, _count in sorted(
            canonical_value_counts.items(), key=lambda item: (-item[1], item[0])
        )[:SUMMARY_VALUE_LIMIT]
    ]
    class_agreement: list[dict[str, Any]] = []
    for value_key in canonical_values:
        source_counts = [
            {
                "source_id": source["source_id"],
                "document_count": sum(
                    value_key
                    in next(item for item in row["sources"] if item["source_id"] == source["source_id"])[
                        "value_keys"
                    ]
                    for row in fully_evaluated_rows
                ),
            }
            for source in sources
        ]
        all_selected = sum(
            all(value_key in item["value_keys"] for item in row["sources"]) for row in fully_evaluated_rows
        )
        any_selected = sum(
            any(value_key in item["value_keys"] for item in row["sources"]) for row in fully_evaluated_rows
        )
        class_agreement.append(
            {
                "value_key": value_key,
                "source_counts": source_counts,
                "all_selected_count": all_selected,
                "any_selected_count": any_selected,
                "jaccard": (all_selected / any_selected) if any_selected else None,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "document_count": len(rows),
        "agreement_counts": dict(sorted(Counter(row["agreement_status"] for row in rows).items())),
        "sources": source_summary,
        "pairwise": pairwise,
        "multi_source": {
            "compared_document_count": len(fully_evaluated_rows),
            "incomplete_document_count": len(rows) - len(fully_evaluated_rows),
            "class_agreement": class_agreement,
            "class_agreement_truncated": len(canonical_value_counts) > SUMMARY_VALUE_LIMIT,
        },
    }


def _write_parquet(rows: list[dict[str, Any]], *, kind: str, comparison_id: uuid.UUID) -> tempfile.SpooledTemporaryFile:
    if kind == "population":
        payload = {
            "ordinal": [row["ordinal"] for row in rows],
            "matter_document_id": [str(row["matter_document_id"]) for row in rows],
            "collection_item_id": [str(row["collection_item_id"]) for row in rows],
            "batch_ids_json": [json.dumps([str(value) for value in row["batch_ids"]]) for row in rows],
        }
    else:
        payload = {
            "ordinal": [row["ordinal"] for row in rows],
            "matter_document_id": [str(row["matter_document_id"]) for row in rows],
            "collection_item_id": [str(row["collection_item_id"]) for row in rows],
            "batch_ids_json": [json.dumps([str(value) for value in row["batch_ids"]]) for row in rows],
            "agreement_status": [row["agreement_status"] for row in rows],
            "sources_json": [json.dumps(row["sources"], sort_keys=True, separators=(",", ":")) for row in rows],
        }
    table = pa.table(payload).replace_schema_metadata(
        {
            b"pvr.schema": f"matter-coding-comparison-{kind}".encode(),
            b"pvr.schema_version": str(SCHEMA_VERSION).encode(),
            b"pvr.comparison_id": str(comparison_id).encode(),
        }
    )
    output = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)  # noqa: SIM115 - caller owns stream
    pq.write_table(table, output, compression="zstd", row_group_size=5000)
    output.seek(0)
    return output


def _file_identity(stream: Any) -> tuple[str, int]:
    stream.seek(0)
    digest = hashlib.sha256()
    length = 0
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
        length += len(chunk)
    stream.seek(0)
    return digest.hexdigest(), length


def run_comparison(job_id: uuid.UUID, *, session_factory: sessionmaker[Session]) -> None:
    with session_factory() as db:
        job = db.get(MatterCodingComparison, job_id)
        if job is None:
            raise ValueError("Coding comparison not found")
        execution = current_job_execution(db, job_type=CODING_COMPARISON_JOB_TYPE, job_id=job.id)
        if execution is None:
            raise ValueError("Coding comparison execution not found")
        actor_user_id = job.created_by_user_id
        tenant_id = job.tenant_id
        matter_id = job.matter_id
        client_id = job.matter.client_id
        producer_run_id = execution.id
        configuration = dict(job.configuration)
        transition_job_execution(
            db,
            job_type=CODING_COMPARISON_JOB_TYPE,
            job_id=job.id,
            status="RUNNING",
            current_stage="FREEZING_POPULATION",
        )
        db.commit()

    batch_ids = [uuid.UUID(value) for value in configuration["review_batch_ids"]]
    with session_factory() as db:
        population = _population(db, matter_id, batch_ids)
    if not population:
        raise JobExecutionConflict("Selected review batches contain no documents")
    with session_factory() as db:
        transition_job_execution(
            db,
            job_type=CODING_COMPARISON_JOB_TYPE,
            job_id=job_id,
            current_stage="RESOLVING_SOURCES",
            progress_total=len(population),
        )
        job = db.get(MatterCodingComparison, job_id)
        assert job is not None
        job.population_document_count = len(population)
        db.commit()

    population_file = _write_parquet(population, kind="population", comparison_id=job_id)
    population_hash, population_length = _file_identity(population_file)
    population_artifact, _ = store_workflow_artifact(
        content=population_file,
        artifact_type=ArtifactType.CODING_COMPARISON_POPULATION,
        core_scope_kind="MATTER",
        core_scope_id=matter_id,
        schema_version=SCHEMA_VERSION,
        derivation_key=canonical_hash({"comparison_id": str(job_id), "kind": "population", "hash": population_hash}),
        content_hash=population_hash,
        byte_length=population_length,
        media_type=PARQUET_MEDIA_TYPE,
        producer_kind="CORE",
        producer_run_id=producer_run_id,
        artifact_metadata={"comparison_id": str(job_id), "document_count": len(population)},
        lineage=[],
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
        filename=f"coding-comparison-{job_id}-population.parquet",
    )
    population_file.close()

    document_ids = {row["matter_document_id"] for row in population}
    source_states: dict[str, dict[uuid.UUID, dict[str, Any]]] = {}
    classifier_artifact_ids: set[uuid.UUID] = set()
    classifier_sources: list[tuple[dict[str, Any], ClassifierRunSnapshot]] = []
    with session_factory() as db:
        for source in configuration["sources"]:
            if source["source_type"] == "QA_RUN_GROUP":
                source_states[source["source_id"]] = _qa_states(db, document_ids, source)
            elif source["source_type"] == "REVIEWER_CODING_GROUP":
                source_states[source["source_id"]] = _reviewer_states(db, document_ids, source)
            elif source["source_type"] == "MATTER_FIELD":
                source_states[source["source_id"]] = _matter_field_states(db, document_ids, source)
            else:
                scoring_run = db.get(ClassificationScoringRun, uuid.UUID(source["scoring_run_id"]))
                if scoring_run is None:
                    raise JobExecutionConflict("Classifier scoring run is unavailable")
                classifier_sources.append(
                    (
                        source,
                        ClassifierRunSnapshot(
                            id=scoring_run.id,
                            tenant_id=scoring_run.tenant_id,
                            model_version_id=scoring_run.model_version_id,
                            prediction_artifact_id=scoring_run.prediction_artifact_id,
                            prediction_content_hash=scoring_run.prediction_content_hash,
                        ),
                    )
                )
    for source, scoring_run in classifier_sources:
        states, artifact_id = _classifier_states(
            scoring_run,
            client_id,
            document_ids,
            source,
            actor_user_id=actor_user_id,
        )
        source_states[source["source_id"]] = states
        classifier_artifact_ids.add(artifact_id)

    rows: list[dict[str, Any]] = []
    for document in population:
        sources = [
            {"source_id": source["source_id"], **source_states[source["source_id"]][document["matter_document_id"]]}
            for source in configuration["sources"]
        ]
        rows.append({**document, "agreement_status": _agreement(sources), "sources": sources})
    summary = _summary(rows, configuration["sources"])

    result_file = _write_parquet(rows, kind="result", comparison_id=job_id)
    result_hash, result_length = _file_identity(result_file)
    lineage = [
        WorkflowArtifactLineageInput(
            source_kind="WORKFLOW_ARTIFACT",
            source_artifact_id=population_artifact.artifact_id,
        )
    ]
    lineage.extend(
        WorkflowArtifactLineageInput(source_kind="WORKFLOW_ARTIFACT", source_artifact_id=artifact_id)
        for artifact_id in sorted(classifier_artifact_ids, key=str)
    )
    result_artifact, _ = store_workflow_artifact(
        content=result_file,
        artifact_type=ArtifactType.CODING_COMPARISON_RESULT,
        core_scope_kind="MATTER",
        core_scope_id=matter_id,
        schema_version=SCHEMA_VERSION,
        derivation_key=canonical_hash({"comparison_id": str(job_id), "kind": "result", "hash": result_hash}),
        content_hash=result_hash,
        byte_length=result_length,
        media_type=PARQUET_MEDIA_TYPE,
        producer_kind="CORE",
        producer_run_id=producer_run_id,
        artifact_metadata={
            "comparison_id": str(job_id),
            "document_count": len(rows),
            "classifier_prediction_artifact_ids": sorted(str(value) for value in classifier_artifact_ids),
        },
        lineage=lineage,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
        filename=f"coding-comparison-{job_id}-result.parquet",
    )
    result_file.close()

    with session_factory() as db:
        job = db.get(MatterCodingComparison, job_id)
        assert job is not None
        job.population_artifact_id = population_artifact.artifact_id
        job.population_content_hash = population_artifact.content_hash
        job.population_byte_length = population_artifact.byte_length
        job.result_artifact_id = result_artifact.artifact_id
        job.result_content_hash = result_artifact.content_hash
        job.result_byte_length = result_artifact.byte_length
        job.summary = summary
        job.compared_document_count = len(rows)
        transition_job_execution(
            db,
            job_type=CODING_COMPARISON_JOB_TYPE,
            job_id=job_id,
            status="COMPLETED",
            current_stage="COMPLETED",
            progress_completed=len(rows),
            progress_total=len(rows),
        )
        db.commit()


def fail_comparison(db: Session, job_id: uuid.UUID, exc: Exception) -> None:
    transition_job_execution(
        db,
        job_type=CODING_COMPARISON_JOB_TYPE,
        job_id=job_id,
        status="FAILED",
        current_stage="FAILED",
        failure_code=type(exc).__name__.upper()[:100],
        failure_summary=str(exc)[:4000],
        failure_retriable=False,
    )


def result_page(
    job: MatterCodingComparison,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
    offset: int,
    limit: int,
    agreement_status: str | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    if job.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"} or job.result_artifact_id is None:
        raise JobExecutionConflict("Coding comparison result is not ready")
    with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as artifact_file:
        copy_workflow_artifact_content(
            artifact_id=job.result_artifact_id,
            destination=artifact_file,
            actor_user_id=actor_user_id,
            tenant_id=job.tenant_id,
            client_id=matter.client_id,
        )
        artifact_file.seek(0)
        digest = hashlib.sha256()
        while chunk := artifact_file.read(1024 * 1024):
            digest.update(chunk)
        if job.result_content_hash is None or digest.hexdigest() != job.result_content_hash:
            raise JobExecutionConflict("Coding comparison result artifact hash is invalid")
        artifact_file.seek(0)
        parquet = pq.ParquetFile(artifact_file)
        metadata = parquet.schema_arrow.metadata or {}
        required_columns = {
            "ordinal",
            "matter_document_id",
            "collection_item_id",
            "batch_ids_json",
            "agreement_status",
            "sources_json",
        }
        if (
            metadata.get(b"pvr.schema") != b"matter-coding-comparison-result"
            or metadata.get(b"pvr.comparison_id") != str(job.id).encode()
            or not required_columns.issubset(parquet.schema_arrow.names)
        ):
            raise JobExecutionConflict("Coding comparison result artifact identity or schema is invalid")
        total = 0
        selected_rows: list[dict[str, Any]] = []
        for row_group_index in range(parquet.num_row_groups):
            table = parquet.read_row_group(row_group_index, columns=sorted(required_columns))
            for row in table.to_pylist():
                if agreement_status and row["agreement_status"] != agreement_status:
                    continue
                if total >= offset and len(selected_rows) < limit:
                    selected_rows.append(row)
                total += 1
    result = []
    for row in selected_rows:
        result.append(
            {
                "ordinal": row["ordinal"],
                "matter_document_id": row["matter_document_id"],
                "collection_item_id": row["collection_item_id"],
                "batch_ids": json.loads(row["batch_ids_json"]),
                "agreement_status": row["agreement_status"],
                "sources": json.loads(row["sources_json"]),
            }
        )
    return total, result
