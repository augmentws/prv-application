import hashlib
import io
import json
import math
import tempfile
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, BinaryIO

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.artifact_gateway import (
    ChunkVectorArtifactSnapshot,
    WorkflowArtifactReference,
    copy_workflow_artifact_content,
    load_current_chunk_vector_artifacts,
    store_classification_workflow_artifact,
)
from app.audit import record_audit
from app.classification_datasets import ClassificationDatasetConflictError
from app.models import (
    ClassificationDatasetSnapshot,
    ClassificationModelConfiguration,
    Matter,
    MatterEmbeddingJob,
)
from app.schemas import ClassificationFeatureSplitRequest
from artifact_service.schemas import WorkflowArtifactLineageInput
from classification_service.schemas import (
    ArtifactType,
    ClassificationStrategy,
    FeatureContract,
    FeatureDataType,
    FeatureNormalization,
)

PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
FEATURE_SCHEMA_VERSION = 1
SPLIT_SCHEMA_VERSION = 1
POOLING_ALGORITHM_VERSION = 1
SPLIT_ALGORITHM_NAME = "greedy-stratified-connected-groups"
SPLIT_ALGORITHM_VERSION = 2
FEATURE_LOAD_BATCH_SIZE = 1000


class ClassificationFeatureSplitError(ClassificationDatasetConflictError):
    def __init__(self, message: str, *, code: str = "FEATURE_SPLIT_VALIDATION_FAILED") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FeatureRow:
    matter_document_id: uuid.UUID
    collection_item_id: uuid.UUID
    scope_ordinal: int
    status: str
    failure_reason: str | None
    source_vector_artifact_id: uuid.UUID | None
    source_vector_content_hash: str | None
    chunk_count: int
    embedding: tuple[float, ...] | None


@dataclass(frozen=True)
class SplitRow:
    matter_document_id: uuid.UUID
    group_id: str
    family_id: str | None
    exact_duplicate_group_id: str | None
    near_duplicate_group_id: str | None
    feature_status: str
    label_status: str
    training_eligible: bool
    exclusion_reason: str | None
    fold_index: int | None
    source_weight: float | None
    normalized_training_weights: tuple[float | None, ...]


@dataclass(frozen=True)
class PreparedFeatureSplit:
    feature_rows: list[FeatureRow]
    split_rows: list[SplitRow]
    feature_specification: dict[str, Any]
    feature_specification_hash: str
    split_specification: dict[str, Any]
    split_specification_hash: str
    feature_bytes: bytes
    feature_content_hash: str
    split_bytes: bytes
    split_content_hash: str
    training_aggregate_hash: str
    feature_ready_count: int
    feature_missing_count: int
    feature_failed_count: int
    training_eligible_count: int
    trained_class_keys: list[str]
    excluded_classes: list[dict[str, Any]]


@dataclass(frozen=True)
class PreparedScoringFeatures:
    feature_stream: BinaryIO
    feature_row_count: int
    feature_byte_length: int
    feature_specification: dict[str, Any]
    feature_specification_hash: str
    feature_content_hash: str
    feature_ready_count: int
    feature_missing_count: int
    feature_failed_count: int


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_seekable_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_length = 0
    stream.seek(0)
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
        byte_length += len(chunk)
    stream.seek(0)
    return digest.hexdigest(), byte_length


def _metadata(values: dict[str, Any]) -> dict[bytes, bytes]:
    result: dict[bytes, bytes] = {}
    for key, value in sorted(values.items()):
        if value is None:
            continue
        rendered = (
            _canonical_json(value).decode()
            if isinstance(value, (dict, list))
            else str(value)
        )
        result[f"pvr.{key}".encode()] = rendered.encode()
    return result


def _read_workflow_bytes(
    artifact_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID,
    snapshot: ClassificationDatasetSnapshot,
    client_id: uuid.UUID,
) -> bytes:
    destination = io.BytesIO()
    copy_workflow_artifact_content(
        artifact_id=artifact_id,
        destination=destination,
        actor_user_id=actor_user_id,
        tenant_id=snapshot.tenant_id,
        client_id=client_id,
    )
    return destination.getvalue()


def _validate_base_artifact(
    content: bytes,
    *,
    expected_hash: str | None,
    expected_schema: str,
    columns: list[str] | None = None,
) -> pa.Table:
    if expected_hash is None or _sha256(content) != expected_hash:
        raise ClassificationFeatureSplitError(
            f"{expected_schema} artifact content does not match its frozen hash",
            code="BASE_ARTIFACT_HASH_MISMATCH",
        )
    try:
        table = pq.read_table(io.BytesIO(content), columns=columns)
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise ClassificationFeatureSplitError(
            f"{expected_schema} artifact is not readable Parquet",
            code="BASE_ARTIFACT_INVALID",
        ) from exc
    metadata = table.schema.metadata or {}
    if metadata.get(b"pvr.schema", b"").decode() != expected_schema:
        raise ClassificationFeatureSplitError(
            f"Expected {expected_schema} artifact schema",
            code="BASE_ARTIFACT_INVALID",
        )
    return table


def _feature_specification(
    contract: FeatureContract,
    job: MatterEmbeddingJob,
) -> tuple[dict[str, Any], str]:
    embedding = (job.configuration or {}).get("embedding") or {}
    expected_normalized = contract.normalization == FeatureNormalization.L2
    mismatches = []
    if job.embedding_model != contract.embedding_model:
        mismatches.append("model")
    if job.embedding_model_revision != contract.embedding_model_revision:
        mismatches.append("model revision")
    if job.embedding_dimensions != contract.dimensions:
        mismatches.append("dimensions")
    if job.embedding_normalized != expected_normalized:
        mismatches.append("normalization")
    if embedding.get("model") not in {None, job.embedding_model}:
        mismatches.append("frozen embedding model")
    if embedding.get("model_revision") != job.embedding_model_revision:
        mismatches.append("frozen embedding revision")
    if embedding.get("dimensions") not in {None, job.embedding_dimensions}:
        mismatches.append("frozen embedding dimensions")
    if mismatches:
        raise ClassificationFeatureSplitError(
            "Embedding job is incompatible with the feature contract: " + ", ".join(mismatches),
            code="EMBEDDING_JOB_INCOMPATIBLE",
        )
    if contract.text_source_policy != "active_text":
        raise ClassificationFeatureSplitError(
            "DOCUMENT_EMBEDDING_POOLED_V1 currently requires the active_text source policy",
            code="FEATURE_POLICY_UNSUPPORTED",
        )
    specification = {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_contract": contract.model_dump(mode="json"),
        "embedding_job_id": str(job.id),
        "embedding_configuration_hash": job.configuration_hash,
        "embedding_configuration": job.configuration,
        "pooling_algorithm": {
            "name": "arithmetic_mean",
            "version": POOLING_ALGORITHM_VERSION,
        },
        "missing_feature_policy": "EXCLUDE",
    }
    return specification, _sha256(_canonical_json(specification))


def _schema_metadata_text(table: pa.Table, key: str) -> str | None:
    value = (table.schema.metadata or {}).get(f"pvr.{key}".encode())
    return value.decode() if value is not None else None


def _pool_vector_artifact(
    artifact: ChunkVectorArtifactSnapshot,
    *,
    job: MatterEmbeddingJob,
    contract: FeatureContract,
) -> tuple[tuple[float, ...], int]:
    if _sha256(artifact.content) != artifact.content_hash:
        raise ValueError("Chunk vector artifact hash mismatch")
    table = pq.read_table(io.BytesIO(artifact.content), columns=["embedding"])
    if _schema_metadata_text(table, "schema") != "chunk-vector-set":
        raise ValueError("Chunk vector artifact schema mismatch")
    if _schema_metadata_text(table, "configuration_hash") != job.configuration_hash:
        raise ValueError("Chunk vector artifact configuration mismatch")
    dimensions = int(_schema_metadata_text(table, "dimensions") or 0)
    if dimensions != contract.dimensions:
        raise ValueError("Chunk vector artifact dimensions mismatch")
    values = table.column("embedding").to_pylist()
    if not values:
        raise ValueError("Chunk vector artifact contains no vectors")
    if any(vector is None or len(vector) != contract.dimensions for vector in values):
        raise ValueError("Chunk vector artifact has an invalid vector shape")
    pooled = [
        math.fsum(float(vector[index]) for vector in values) / len(values)
        for index in range(contract.dimensions)
    ]
    if any(not math.isfinite(value) for value in pooled):
        raise ValueError("Pooled feature contains a non-finite value")
    if contract.normalization == FeatureNormalization.L2:
        norm = math.sqrt(math.fsum(value * value for value in pooled))
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("Pooled feature cannot be L2 normalized")
        pooled = [value / norm for value in pooled]
    return tuple(pooled), len(values)


def _iter_feature_row_batches(
    population: list[dict[str, Any]],
    labels: Iterable[dict[str, Any]],
    *,
    job: MatterEmbeddingJob,
    contract: FeatureContract,
    actor_user_id: uuid.UUID,
    snapshot: ClassificationDatasetSnapshot,
    client_id: uuid.UUID,
) -> Iterator[tuple[list[FeatureRow], int, int, int, int]]:
    processed = 0
    status_counts: Counter[str] = Counter()
    labeled_sources = [
        source
        for source, label in zip(population, labels, strict=True)
        if label["resolution_status"] == "LABELED"
    ]
    for offset in range(0, len(labeled_sources), FEATURE_LOAD_BATCH_SIZE):
        sources = labeled_sources[offset : offset + FEATURE_LOAD_BATCH_SIZE]
        rows: list[FeatureRow] = []
        item_ids = [uuid.UUID(source["collection_item_id"]) for source in sources]
        artifacts = load_current_chunk_vector_artifacts(
            collection_item_ids=item_ids,
            configuration_hash=job.configuration_hash,
            actor_user_id=actor_user_id,
            tenant_id=snapshot.tenant_id,
            client_id=client_id,
        )
        for source, collection_item_id in zip(sources, item_ids, strict=True):
            document_id = uuid.UUID(source["matter_document_id"])
            try:
                artifact = artifacts.get(collection_item_id)
                if artifact is None:
                    rows.append(
                        FeatureRow(
                            document_id,
                            collection_item_id,
                            source["scope_ordinal"],
                            "MISSING",
                            "CHUNK_VECTOR_SET_NOT_FOUND",
                            None,
                            None,
                            0,
                            None,
                        )
                    )
                    status_counts["MISSING"] += 1
                    continue
                vector, chunk_count = _pool_vector_artifact(artifact, job=job, contract=contract)
                rows.append(
                    FeatureRow(
                        document_id,
                        collection_item_id,
                        source["scope_ordinal"],
                        "READY",
                        None,
                        artifact.artifact_id,
                        artifact.content_hash,
                        chunk_count,
                        vector,
                    )
                )
                status_counts["READY"] += 1
            except Exception:  # noqa: BLE001 - row-level failures are data, not job crashes
                rows.append(
                    FeatureRow(
                        document_id,
                        collection_item_id,
                        source["scope_ordinal"],
                        "FAILED",
                        "CHUNK_VECTOR_SET_INVALID",
                        None,
                        None,
                        0,
                        None,
                    )
                )
                status_counts["FAILED"] += 1
        processed += len(rows)
        yield (
            rows,
            processed,
            status_counts["READY"],
            status_counts["MISSING"],
            status_counts["FAILED"],
        )


def _build_feature_rows(
    population: list[dict[str, Any]],
    labels: Iterable[dict[str, Any]],
    *,
    job: MatterEmbeddingJob,
    contract: FeatureContract,
    actor_user_id: uuid.UUID,
    snapshot: ClassificationDatasetSnapshot,
    client_id: uuid.UUID,
    progress_callback: Callable[[int, int, int, int], None] | None = None,
) -> list[FeatureRow]:
    rows: list[FeatureRow] = []
    for batch, processed, ready, missing, failed in _iter_feature_row_batches(
        population,
        labels,
        job=job,
        contract=contract,
        actor_user_id=actor_user_id,
        snapshot=snapshot,
        client_id=client_id,
    ):
        rows.extend(batch)
        if progress_callback is not None:
            progress_callback(
                processed,
                ready,
                missing,
                failed,
            )
    return rows


def _feature_parquet_schema(
    *,
    dimensions: int,
    data_type: FeatureDataType,
    specification: dict[str, Any],
    specification_hash: str,
    snapshot: ClassificationDatasetSnapshot,
) -> pa.Schema:
    scalar_type = pa.float32() if data_type == FeatureDataType.FLOAT32 else pa.float64()
    return pa.schema(
        [
            ("matter_document_id", pa.string()),
            ("collection_item_id", pa.string()),
            ("scope_ordinal", pa.int64()),
            ("feature_status", pa.string()),
            ("failure_reason", pa.string()),
            ("source_vector_artifact_id", pa.string()),
            ("source_vector_content_hash", pa.string()),
            ("chunk_count", pa.int32()),
            ("feature_dimension", pa.int32()),
            ("embedding", pa.list_(scalar_type)),
        ],
        metadata=_metadata(
            {
                "schema": "classification-features",
                "schema_version": FEATURE_SCHEMA_VERSION,
                "snapshot_id": snapshot.id,
                "snapshot_aggregate_hash": snapshot.aggregate_hash,
                "feature_specification_hash": specification_hash,
                "feature_specification": specification,
            }
        ),
    )


def _feature_rows_table(rows: list[FeatureRow], *, dimensions: int, schema: pa.Schema) -> pa.Table:
    return pa.Table.from_pylist(
        [
            {
                "matter_document_id": str(row.matter_document_id),
                "collection_item_id": str(row.collection_item_id),
                "scope_ordinal": row.scope_ordinal,
                "feature_status": row.status,
                "failure_reason": row.failure_reason,
                "source_vector_artifact_id": (
                    str(row.source_vector_artifact_id) if row.source_vector_artifact_id else None
                ),
                "source_vector_content_hash": row.source_vector_content_hash,
                "chunk_count": row.chunk_count,
                "feature_dimension": dimensions if row.embedding is not None else None,
                "embedding": list(row.embedding) if row.embedding is not None else None,
            }
            for row in rows
        ],
        schema=schema,
    )


def _write_feature_parquet(
    rows: list[FeatureRow],
    *,
    dimensions: int,
    data_type: FeatureDataType,
    specification: dict[str, Any],
    specification_hash: str,
    snapshot: ClassificationDatasetSnapshot,
) -> bytes:
    schema = _feature_parquet_schema(
        dimensions=dimensions,
        data_type=data_type,
        specification=specification,
        specification_hash=specification_hash,
        snapshot=snapshot,
    )
    table = _feature_rows_table(rows, dimensions=dimensions, schema=schema)
    destination = io.BytesIO()
    pq.write_table(
        table,
        destination,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
        data_page_version="2.0",
    )
    return destination.getvalue()


class _DisjointSet:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def _connected_groups(population: list[dict[str, Any]]) -> tuple[dict[str, list[int]], dict[int, str]]:
    sets = _DisjointSet(len(population))
    owners: dict[str, int] = {}
    for index, row in enumerate(population):
        keys = []
        if row.get("family_id"):
            keys.append(f"family:{row['family_id']}")
        if row.get("exact_duplicate_group_id"):
            keys.append(f"exact:{row['exact_duplicate_group_id']}")
        for key in keys:
            if key in owners:
                sets.union(index, owners[key])
            else:
                owners[key] = index
    members: dict[int, list[int]] = defaultdict(list)
    for index in range(len(population)):
        members[sets.find(index)].append(index)
    groups: dict[str, list[int]] = {}
    index_to_group: dict[int, str] = {}
    for indices in members.values():
        identifiers = sorted(population[index]["matter_document_id"] for index in indices)
        group_id = _sha256(_canonical_json(identifiers))
        groups[group_id] = indices
        for index in indices:
            index_to_group[index] = group_id
    return groups, index_to_group


def _row_class_memberships(
    label: dict[str, Any],
    *,
    strategy: ClassificationStrategy,
    class_keys: list[str],
    selected_class_keys: set[str] | None = None,
) -> tuple[str, ...]:
    if strategy == ClassificationStrategy.BINARY:
        value = label.get("binary_label")
        return (class_keys[value],) if value in {0, 1} else ()
    if strategy == ClassificationStrategy.MULTICLASS:
        value = label.get("multiclass_key")
        return (value,) if value in class_keys else ()
    labels = label.get("one_vs_rest_labels") or []
    observed = label.get("one_vs_rest_observed_mask") or []
    return tuple(
        key
        for key, positive, is_observed in zip(class_keys, labels, observed, strict=True)
        if is_observed and positive and (selected_class_keys is None or key in selected_class_keys)
    )


def _row_observes_any_class(
    label: dict[str, Any],
    *,
    observed_class_keys: list[str],
    selected_class_keys: set[str],
) -> bool:
    observed = label.get("one_vs_rest_observed_mask") or []
    return any(
        is_observed and key in selected_class_keys
        for key, is_observed in zip(observed_class_keys, observed, strict=True)
    )


def _row_observes_class(
    label: dict[str, Any],
    *,
    observed_class_keys: list[str],
    class_key: str,
) -> bool:
    observed = label.get("one_vs_rest_observed_mask") or []
    return any(
        key == class_key and is_observed
        for key, is_observed in zip(observed_class_keys, observed, strict=True)
    )


def _allocate_folds(
    *,
    eligible_groups: dict[str, list[int]],
    memberships: dict[int, tuple[str, ...]],
    class_keys: list[str],
    fold_count: int,
    seed: int,
) -> tuple[dict[int, int], list[Counter[str]]]:
    group_counts: dict[str, Counter[str]] = {
        group_id: Counter(key for index in indices for key in memberships[index])
        for group_id, indices in eligible_groups.items()
    }
    total_counts = Counter(key for indices in eligible_groups.values() for index in indices for key in memberships[index])
    ordered_groups = sorted(
        eligible_groups,
        key=lambda group_id: (
            -max(group_counts[group_id].values(), default=0),
            -sum(group_counts[group_id].values()),
            -len(eligible_groups[group_id]),
            _sha256(f"{seed}:{group_id}".encode()),
        ),
    )
    fold_counts = [Counter() for _ in range(fold_count)]
    fold_sizes = [0 for _ in range(fold_count)]
    group_fold: dict[str, int] = {}
    row_count = sum(len(indices) for indices in eligible_groups.values())
    for group_id in ordered_groups:
        empty = [fold for fold, size in enumerate(fold_sizes) if size == 0]
        candidates = empty or list(range(fold_count))
        selected = min(
            candidates,
            key=lambda fold: (
                sum(
                    ((fold_counts[fold][key] + group_counts[group_id][key]) / max(total_counts[key], 1))
                    ** 2
                    for key in class_keys
                ),
                (fold_sizes[fold] + len(eligible_groups[group_id])) / max(row_count, 1),
                _sha256(f"{seed}:{group_id}:{fold}".encode()),
            ),
        )
        group_fold[group_id] = selected
        fold_counts[selected].update(group_counts[group_id])
        fold_sizes[selected] += len(eligible_groups[group_id])
    assignments = {
        index: group_fold[group_id]
        for group_id, indices in eligible_groups.items()
        for index in indices
    }
    return assignments, fold_counts


def _assign_folds(
    *,
    groups: dict[str, list[int]],
    eligible_indices: set[int],
    memberships: dict[int, tuple[str, ...]],
    class_keys: list[str],
    fold_count: int,
    minimum_per_class: int,
    seed: int,
) -> tuple[dict[int, int], list[dict[str, int]]]:
    eligible_groups = {
        group_id: [index for index in indices if index in eligible_indices]
        for group_id, indices in groups.items()
    }
    eligible_groups = {key: value for key, value in eligible_groups.items() if value}
    if len(eligible_groups) < fold_count:
        raise ClassificationFeatureSplitError(
            "Group-aware validation requires at least one eligible connected group per fold",
            code="INSUFFICIENT_GROUPS_FOR_FOLDS",
        )
    group_counts: dict[str, Counter[str]] = {
        group_id: Counter(key for index in indices for key in memberships[index])
        for group_id, indices in eligible_groups.items()
    }
    total_counts = Counter(key for index in eligible_indices for key in memberships[index])
    for key in class_keys:
        supporting_groups = sum(group_counts[group_id][key] > 0 for group_id in eligible_groups)
        if total_counts[key] < minimum_per_class * fold_count or supporting_groups < fold_count:
            raise ClassificationFeatureSplitError(
                f"Class {key} cannot meet minimum support in every group-aware fold",
                code="INSUFFICIENT_CLASS_SUPPORT_PER_FOLD",
            )

    assignments, fold_counts = _allocate_folds(
        eligible_groups=eligible_groups,
        memberships=memberships,
        class_keys=class_keys,
        fold_count=fold_count,
        seed=seed,
    )

    for fold, support in enumerate(fold_counts):
        deficient = [key for key in class_keys if support[key] < minimum_per_class]
        if deficient:
            raise ClassificationFeatureSplitError(
                f"Fold {fold} lacks minimum support for: {', '.join(deficient)}",
                code="INSUFFICIENT_CLASS_SUPPORT_PER_FOLD",
            )
    return assignments, [dict(sorted(support.items())) for support in fold_counts]


def _assign_one_vs_rest_folds(
    *,
    groups: dict[str, list[int]],
    candidate_indices: set[int],
    labels: list[dict[str, Any]],
    observed_class_keys: list[str],
    fold_count: int,
    minimum_labeled_count: int,
    minimum_per_class: int,
    seed: int,
) -> tuple[dict[int, int], list[dict[str, int]], set[int], list[str], list[dict[str, Any]]]:
    required_positive_count = fold_count * minimum_per_class
    candidate_memberships = {
        index: _row_class_memberships(
            labels[index],
            strategy=ClassificationStrategy.ONE_VS_REST,
            class_keys=observed_class_keys,
        )
        for index in candidate_indices
    }
    index_to_group = {
        index: group_id for group_id, indices in groups.items() for index in indices
    }
    exclusions: dict[str, dict[str, Any]] = {}
    trained = list(observed_class_keys)
    for class_key in observed_class_keys:
        positive_indices = [index for index in candidate_indices if class_key in candidate_memberships[index]]
        negative_indices = [
            index
            for index in candidate_indices
            if class_key not in candidate_memberships[index]
            and _row_observes_class(
                labels[index],
                observed_class_keys=observed_class_keys,
                class_key=class_key,
            )
        ]
        positive_groups = {index_to_group[index] for index in positive_indices}
        negative_groups = {index_to_group[index] for index in negative_indices}
        reason_code: str | None = None
        if len(positive_indices) < required_positive_count:
            reason_code = "INSUFFICIENT_POSITIVE_DOCUMENTS"
        elif len(positive_groups) < fold_count:
            reason_code = "INSUFFICIENT_POSITIVE_GROUPS"
        elif len(negative_indices) < required_positive_count:
            reason_code = "INSUFFICIENT_NEGATIVE_DOCUMENTS"
        elif len(negative_groups) < fold_count:
            reason_code = "INSUFFICIENT_NEGATIVE_GROUPS"
        if reason_code is not None:
            exclusions[class_key] = {
                "class_key": class_key,
                "reason_code": reason_code,
                "message": (
                    f"Class {class_key} has {len(positive_indices)} positive and "
                    f"{len(negative_indices)} negative feature-ready documents across "
                    f"{len(positive_groups)} positive and {len(negative_groups)} negative connected groups; "
                    f"training requires at least {required_positive_count} documents of each outcome "
                    f"across {fold_count} groups"
                ),
                "feature_ready_positive_count": len(positive_indices),
                "positive_group_count": len(positive_groups),
                "feature_ready_negative_count": len(negative_indices),
                "negative_group_count": len(negative_groups),
                "required_positive_count": required_positive_count,
                "required_group_count": fold_count,
                "fold_support": None,
                "fold_negative_support": None,
            }
            trained.remove(class_key)

    while True:
        if len(trained) < 2:
            exclusion_summary = ", ".join(
                f"{key} ({value['reason_code']})"
                for key, value in list(exclusions.items())[:10]
            )
            if len(exclusions) > 10:
                exclusion_summary += f", plus {len(exclusions) - 10} more"
            raise ClassificationFeatureSplitError(
                "Fewer than two one-vs-rest classes have enough leakage-safe support for training. "
                f"Excluded: {exclusion_summary}",
                code="INSUFFICIENT_TRAINABLE_CLASSES",
            )
        selected = set(trained)
        eligible_indices = {
            index
            for index in candidate_indices
            if _row_observes_any_class(
                labels[index],
                observed_class_keys=observed_class_keys,
                selected_class_keys=selected,
            )
        }
        if len(eligible_indices) < minimum_labeled_count:
            raise ClassificationFeatureSplitError(
                "Rows observing the remaining one-vs-rest classes do not meet the minimum labeled count",
                code="INSUFFICIENT_LABELED_FEATURES",
            )
        eligible_groups = {
            group_id: [index for index in indices if index in eligible_indices]
            for group_id, indices in groups.items()
        }
        eligible_groups = {key: value for key, value in eligible_groups.items() if value}
        if len(eligible_groups) < fold_count:
            raise ClassificationFeatureSplitError(
                "Group-aware validation requires at least one eligible connected group per fold",
                code="INSUFFICIENT_GROUPS_FOR_FOLDS",
            )
        memberships = {
            index: tuple(key for key in candidate_memberships[index] if key in selected)
            for index in eligible_indices
        }
        assignments, fold_counts = _allocate_folds(
            eligible_groups=eligible_groups,
            memberships=memberships,
            class_keys=trained,
            fold_count=fold_count,
            seed=seed,
        )
        fold_negative_counts = [
            {
                key: sum(
                    assignments[index] == fold
                    and _row_observes_class(
                        labels[index],
                        observed_class_keys=observed_class_keys,
                        class_key=key,
                    )
                    and key not in memberships[index]
                    for index in eligible_indices
                )
                for key in trained
            }
            for fold in range(fold_count)
        ]
        deficient = [
            key
            for key in trained
            if any(support[key] < minimum_per_class for support in fold_counts)
            or any(support[key] < minimum_per_class for support in fold_negative_counts)
        ]
        if not deficient:
            return (
                assignments,
                [dict(sorted(support.items())) for support in fold_counts],
                eligible_indices,
                trained,
                [exclusions[key] for key in observed_class_keys if key in exclusions],
            )
        # Remove one worst-supported class and recompute the complete allocation. This avoids
        # excluding classes that become feasible after a single constraining class is removed.
        class_key = min(
            deficient,
            key=lambda key: (
                min(
                    min(counts[key] for counts in fold_counts),
                    min(counts[key] for counts in fold_negative_counts),
                ),
                trained.index(key),
            ),
        )
        support = [counts[class_key] for counts in fold_counts]
        negative_support = [counts[class_key] for counts in fold_negative_counts]
        positive_count = sum(class_key in candidate_memberships[index] for index in candidate_indices)
        positive_groups = {
            index_to_group[index]
            for index in candidate_indices
            if class_key in candidate_memberships[index]
        }
        negative_indices = [
            index
            for index in candidate_indices
            if class_key not in candidate_memberships[index]
            and _row_observes_class(
                labels[index],
                observed_class_keys=observed_class_keys,
                class_key=class_key,
            )
        ]
        negative_groups = {index_to_group[index] for index in negative_indices}
        exclusions[class_key] = {
            "class_key": class_key,
            "reason_code": "FOLD_SUPPORT_INFEASIBLE",
            "message": (
                f"Class {class_key} could not be allocated with at least {minimum_per_class} "
                "positive documents in every leakage-safe fold"
            ),
            "feature_ready_positive_count": positive_count,
            "positive_group_count": len(positive_groups),
            "feature_ready_negative_count": len(negative_indices),
            "negative_group_count": len(negative_groups),
            "required_positive_count": required_positive_count,
            "required_group_count": fold_count,
            "fold_support": support,
            "fold_negative_support": negative_support,
        }
        trained.remove(class_key)


def _build_split_rows(
    population: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    features: list[FeatureRow],
    *,
    configuration: ClassificationModelConfiguration,
    class_keys: list[str],
    request: ClassificationFeatureSplitRequest,
) -> tuple[list[SplitRow], dict[str, Any], list[str], list[dict[str, Any]]]:
    population_ids = [row["matter_document_id"] for row in population]
    label_ids = [row["matter_document_id"] for row in labels]
    feature_ids = [str(row.matter_document_id) for row in features]
    labeled_ids = [
        label["matter_document_id"]
        for label in labels
        if label["resolution_status"] == "LABELED"
    ]
    if (
        population_ids != label_ids
        or len(feature_ids) != len(set(feature_ids))
        or feature_ids != labeled_ids
    ):
        raise ClassificationFeatureSplitError(
            "Population and label rows, plus labeled feature rows, must align by document ID and order",
            code="DATASET_ROW_ALIGNMENT_FAILED",
        )
    features_by_document_id = {
        str(feature.matter_document_id): feature for feature in features
    }
    groups, index_to_group = _connected_groups(population)
    strategy = ClassificationStrategy(configuration.classification_strategy)
    candidate_indices = {
        index
        for index, label in enumerate(labels)
        if label["resolution_status"] == "LABELED"
        and features_by_document_id[label["matter_document_id"]].status == "READY"
    }
    if len(candidate_indices) < request.validation_policy.minimum_labeled_count:
        raise ClassificationFeatureSplitError(
            "Feature-ready labeled rows do not meet the minimum labeled count",
            code="INSUFFICIENT_LABELED_FEATURES",
        )
    if strategy == ClassificationStrategy.ONE_VS_REST:
        assignments, fold_support, eligible_indices, trained_class_keys, excluded_classes = (
            _assign_one_vs_rest_folds(
                groups=groups,
                candidate_indices=candidate_indices,
                labels=labels,
                observed_class_keys=class_keys,
                fold_count=request.validation_policy.fold_count,
                minimum_labeled_count=request.validation_policy.minimum_labeled_count,
                minimum_per_class=request.validation_policy.minimum_labeled_count_per_class,
                seed=request.random_seed,
            )
        )
    else:
        eligible_indices = candidate_indices
        trained_class_keys = list(class_keys)
        excluded_classes = []
        memberships = {
            index: _row_class_memberships(
                labels[index],
                strategy=strategy,
                class_keys=class_keys,
            )
            for index in eligible_indices
        }
        assignments, fold_support = _assign_folds(
            groups=groups,
            eligible_indices=eligible_indices,
            memberships=memberships,
            class_keys=class_keys,
            fold_count=request.validation_policy.fold_count,
            minimum_per_class=request.validation_policy.minimum_labeled_count_per_class,
            seed=request.random_seed,
        )
    fold_scales: list[float] = []
    for validation_fold in range(request.validation_policy.fold_count):
        weights = [
            float(labels[index]["source_weight"])
            for index in eligible_indices
            if assignments[index] != validation_fold
        ]
        if not weights or math.fsum(weights) <= 0:
            raise ClassificationFeatureSplitError(
                f"Fold {validation_fold} has no weighted training rows",
                code="EMPTY_TRAINING_FOLD",
            )
        fold_scales.append(len(weights) / math.fsum(weights))

    split_rows: list[SplitRow] = []
    for index, (population_row, label) in enumerate(zip(population, labels, strict=True)):
        feature = features_by_document_id.get(label["matter_document_id"])
        eligible = index in eligible_indices
        if label["resolution_status"] != "LABELED":
            exclusion_reason = label["reason_code"]
        elif feature is not None and feature.status != "READY":
            exclusion_reason = feature.failure_reason
        else:
            exclusion_reason = None
        source_weight = float(label["source_weight"]) if eligible else None
        fold_index = assignments.get(index)
        normalized = tuple(
            None
            if not eligible or fold_index == validation_fold
            else source_weight * fold_scales[validation_fold]
            for validation_fold in range(request.validation_policy.fold_count)
        )
        split_rows.append(
            SplitRow(
                matter_document_id=uuid.UUID(population_row["matter_document_id"]),
                group_id=index_to_group[index],
                family_id=population_row.get("family_id"),
                exact_duplicate_group_id=population_row.get("exact_duplicate_group_id"),
                near_duplicate_group_id=None,
                feature_status=feature.status if feature is not None else "NOT_REQUIRED",
                label_status=label["resolution_status"],
                training_eligible=eligible,
                exclusion_reason=exclusion_reason,
                fold_index=fold_index,
                source_weight=source_weight,
                normalized_training_weights=normalized,
            )
        )
    summary = {
        "eligible_group_count": len({row.group_id for row in split_rows if row.training_eligible}),
        "fold_sizes": [
            sum(row.fold_index == fold for row in split_rows)
            for fold in range(request.validation_policy.fold_count)
        ],
        "fold_class_support": fold_support,
        "observed_class_keys": class_keys,
        "trained_class_keys": trained_class_keys,
        "excluded_classes": excluded_classes,
    }
    return split_rows, summary, trained_class_keys, excluded_classes


def _write_split_parquet(
    rows: list[SplitRow],
    *,
    fold_count: int,
    specification: dict[str, Any],
    specification_hash: str,
    snapshot: ClassificationDatasetSnapshot,
    feature_content_hash: str,
) -> bytes:
    schema = pa.schema(
        [
            ("matter_document_id", pa.string()),
            ("group_id", pa.string()),
            ("family_id", pa.string()),
            ("exact_duplicate_group_id", pa.string()),
            ("near_duplicate_group_id", pa.string()),
            ("feature_status", pa.string()),
            ("label_status", pa.string()),
            ("training_eligible", pa.bool_()),
            ("exclusion_reason", pa.string()),
            ("fold_index", pa.int16()),
            ("source_weight", pa.float64()),
            ("normalized_training_weights", pa.list_(pa.float64(), fold_count)),
        ],
        metadata=_metadata(
            {
                "schema": "classification-splits",
                "schema_version": SPLIT_SCHEMA_VERSION,
                "snapshot_id": snapshot.id,
                "snapshot_aggregate_hash": snapshot.aggregate_hash,
                "feature_content_hash": feature_content_hash,
                "split_specification_hash": specification_hash,
                "split_specification": specification,
            }
        ),
    )
    table = pa.Table.from_pylist(
        [
            {
                "matter_document_id": str(row.matter_document_id),
                "group_id": row.group_id,
                "family_id": row.family_id,
                "exact_duplicate_group_id": row.exact_duplicate_group_id,
                "near_duplicate_group_id": row.near_duplicate_group_id,
                "feature_status": row.feature_status,
                "label_status": row.label_status,
                "training_eligible": row.training_eligible,
                "exclusion_reason": row.exclusion_reason,
                "fold_index": row.fold_index,
                "source_weight": row.source_weight,
                "normalized_training_weights": list(row.normalized_training_weights),
            }
            for row in rows
        ],
        schema=schema,
    )
    destination = io.BytesIO()
    pq.write_table(
        table,
        destination,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
        data_page_version="2.0",
    )
    return destination.getvalue()


def embedding_job_output_is_usable(job: MatterEmbeddingJob) -> bool:
    if job.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        return True
    return (
        job.status == "CANCELED"
        and job.total_count > 0
        and job.processed_count == job.total_count
        and job.chunk_count > 0
    )


def feature_specification_hash_for_job(
    configuration: ClassificationModelConfiguration,
    job: MatterEmbeddingJob,
) -> str:
    try:
        contract = FeatureContract.model_validate(configuration.feature_contract)
    except ValidationError as exc:
        raise ClassificationFeatureSplitError("Classification feature contract is invalid") from exc
    return _feature_specification(contract, job)[1]


def prepare_feature_split(
    db: Session,
    *,
    snapshot: ClassificationDatasetSnapshot,
    request: ClassificationFeatureSplitRequest,
    actor_user_id: uuid.UUID,
    progress_callback: Callable[[int, int, int, int], None] | None = None,
) -> PreparedFeatureSplit:
    if snapshot.status not in {"READY_FOR_FEATURES", "READY_FOR_TRAINING", "FAILED", "BUILDING"}:
        raise ClassificationFeatureSplitError("Dataset snapshot is not ready for feature materialization")
    if snapshot.population_artifact_id is None or snapshot.label_artifact_id is None:
        raise ClassificationFeatureSplitError("Dataset snapshot base artifacts are incomplete")
    configuration = db.get(ClassificationModelConfiguration, snapshot.configuration_id)
    if configuration is None or configuration.classification_model_id != snapshot.classification_model_id:
        raise ClassificationFeatureSplitError("Dataset snapshot configuration was not found")
    matter = db.get(Matter, snapshot.matter_id)
    if matter is None:
        raise ClassificationFeatureSplitError("Dataset snapshot matter was not found")
    job = db.get(MatterEmbeddingJob, request.embedding_job_id)
    if job is None or job.matter_id != snapshot.matter_id:
        raise ClassificationFeatureSplitError(
            "Embedding job was not found in the dataset matter",
            code="EMBEDDING_JOB_NOT_FOUND",
        )
    if not embedding_job_output_is_usable(job):
        raise ClassificationFeatureSplitError(
            "Embedding job must have complete reusable output before features can be materialized",
            code="EMBEDDING_JOB_NOT_COMPLETE",
        )
    try:
        contract = FeatureContract.model_validate(configuration.feature_contract)
    except ValidationError as exc:
        raise ClassificationFeatureSplitError("Classification feature contract is invalid") from exc
    feature_specification, feature_specification_hash = _feature_specification(contract, job)
    population_content = _read_workflow_bytes(
        snapshot.population_artifact_id,
        actor_user_id=actor_user_id,
        snapshot=snapshot,
        client_id=matter.client_id,
    )
    label_content = _read_workflow_bytes(
        snapshot.label_artifact_id,
        actor_user_id=actor_user_id,
        snapshot=snapshot,
        client_id=matter.client_id,
    )
    population_table = _validate_base_artifact(
        population_content,
        expected_hash=snapshot.population_content_hash,
        expected_schema="classification-population",
    )
    label_table = _validate_base_artifact(
        label_content,
        expected_hash=snapshot.label_content_hash,
        expected_schema="classification-labels",
        columns=[
            "matter_document_id",
            "binary_label",
            "multiclass_key",
            "one_vs_rest_labels",
            "one_vs_rest_observed_mask",
            "source_weight",
            "resolution_status",
            "reason_code",
        ],
    )
    population = population_table.to_pylist()
    labels = label_table.to_pylist()
    if len(population) != snapshot.population_count or len(labels) != snapshot.population_count:
        raise ClassificationFeatureSplitError(
            "Frozen artifact row counts do not match the dataset snapshot",
            code="DATASET_ROW_ALIGNMENT_FAILED",
        )
    feature_rows = _build_feature_rows(
        population,
        labels,
        job=job,
        contract=contract,
        actor_user_id=actor_user_id,
        snapshot=snapshot,
        client_id=matter.client_id,
        progress_callback=progress_callback,
    )
    feature_bytes = _write_feature_parquet(
        feature_rows,
        dimensions=contract.dimensions,
        data_type=contract.data_type,
        specification=feature_specification,
        specification_hash=feature_specification_hash,
        snapshot=snapshot,
    )
    feature_content_hash = _sha256(feature_bytes)
    split_rows, split_summary, trained_class_keys, excluded_classes = _build_split_rows(
        population,
        labels,
        feature_rows,
        configuration=configuration,
        class_keys=list(snapshot.target_field_snapshot.get("discovered_class_keys", [])),
        request=request,
    )
    split_specification = {
        "split_schema_version": SPLIT_SCHEMA_VERSION,
        "algorithm": {
            "name": SPLIT_ALGORITHM_NAME,
            "version": SPLIT_ALGORITHM_VERSION,
        },
        "random_seed": request.random_seed,
        "validation_policy": request.validation_policy.model_dump(mode="json"),
        "grouping_sources": ["FAMILY", "EXACT_DUPLICATE"],
        "near_duplicate_grouping_available": False,
        "feature_specification_hash": feature_specification_hash,
        "feature_content_hash": feature_content_hash,
        "summary": split_summary,
    }
    split_specification_hash = _sha256(_canonical_json(split_specification))
    split_bytes = _write_split_parquet(
        split_rows,
        fold_count=request.validation_policy.fold_count,
        specification=split_specification,
        specification_hash=split_specification_hash,
        snapshot=snapshot,
        feature_content_hash=feature_content_hash,
    )
    split_content_hash = _sha256(split_bytes)
    training_aggregate_hash = _sha256(
        _canonical_json(
            {
                "snapshot_aggregate_hash": snapshot.aggregate_hash,
                "feature_specification_hash": feature_specification_hash,
                "feature_content_hash": feature_content_hash,
                "split_specification_hash": split_specification_hash,
                "split_content_hash": split_content_hash,
            }
        )
    )
    status_counts = Counter(row.status for row in feature_rows)
    return PreparedFeatureSplit(
        feature_rows=feature_rows,
        split_rows=split_rows,
        feature_specification=feature_specification,
        feature_specification_hash=feature_specification_hash,
        split_specification=split_specification,
        split_specification_hash=split_specification_hash,
        feature_bytes=feature_bytes,
        feature_content_hash=feature_content_hash,
        split_bytes=split_bytes,
        split_content_hash=split_content_hash,
        training_aggregate_hash=training_aggregate_hash,
        feature_ready_count=status_counts["READY"],
        feature_missing_count=status_counts["MISSING"],
        feature_failed_count=status_counts["FAILED"],
        training_eligible_count=sum(row.training_eligible for row in split_rows),
        trained_class_keys=trained_class_keys,
        excluded_classes=excluded_classes,
    )


def prepare_scoring_features(
    db: Session,
    *,
    snapshot: ClassificationDatasetSnapshot,
    embedding_job_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    progress_callback: Callable[[int, int, int, int], None] | None = None,
) -> PreparedScoringFeatures:
    if snapshot.purpose != "SCORING":
        raise ClassificationFeatureSplitError("Only scoring snapshots can materialize scoring features")
    if snapshot.status not in {"READY_FOR_FEATURES", "READY_FOR_SCORING", "FAILED", "BUILDING"}:
        raise ClassificationFeatureSplitError("Scoring snapshot is not ready for feature materialization")
    if snapshot.population_artifact_id is None:
        raise ClassificationFeatureSplitError("Scoring population artifact is incomplete")
    configuration = db.get(ClassificationModelConfiguration, snapshot.configuration_id)
    if configuration is None or configuration.classification_model_id != snapshot.classification_model_id:
        raise ClassificationFeatureSplitError("Scoring snapshot configuration was not found")
    matter = db.get(Matter, snapshot.matter_id)
    if matter is None:
        raise ClassificationFeatureSplitError("Scoring snapshot matter was not found")
    job = db.get(MatterEmbeddingJob, embedding_job_id)
    if job is None or job.matter_id != snapshot.matter_id:
        raise ClassificationFeatureSplitError(
            "Embedding job was not found in the scoring matter",
            code="EMBEDDING_JOB_NOT_FOUND",
        )
    if not embedding_job_output_is_usable(job):
        raise ClassificationFeatureSplitError(
            "Embedding job must have complete reusable output before scoring",
            code="EMBEDDING_JOB_NOT_COMPLETE",
        )
    try:
        contract = FeatureContract.model_validate(configuration.feature_contract)
    except ValidationError as exc:
        raise ClassificationFeatureSplitError("Classification feature contract is invalid") from exc
    feature_specification, feature_specification_hash = _feature_specification(contract, job)
    population_content = _read_workflow_bytes(
        snapshot.population_artifact_id,
        actor_user_id=actor_user_id,
        snapshot=snapshot,
        client_id=matter.client_id,
    )
    population_table = _validate_base_artifact(
        population_content,
        expected_hash=snapshot.population_content_hash,
        expected_schema="classification-population",
    )
    population = population_table.to_pylist()
    if len(population) != snapshot.population_count:
        raise ClassificationFeatureSplitError(
            "Frozen population row count does not match the scoring snapshot",
            code="DATASET_ROW_ALIGNMENT_FAILED",
        )
    schema = _feature_parquet_schema(
        dimensions=contract.dimensions,
        data_type=contract.data_type,
        specification=feature_specification,
        specification_hash=feature_specification_hash,
        snapshot=snapshot,
    )
    feature_stream = tempfile.SpooledTemporaryFile(  # noqa: SIM115 - consumed by the materialization step
        max_size=64 * 1024 * 1024,
        mode="w+b",
    )
    writer = pq.ParquetWriter(
        feature_stream,
        schema,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
        data_page_version="2.0",
    )
    processed = ready = missing = failed = 0
    try:
        for batch, processed, ready, missing, failed in _iter_feature_row_batches(
            population,
            ({"resolution_status": "LABELED"} for _ in population),
            job=job,
            contract=contract,
            actor_user_id=actor_user_id,
            snapshot=snapshot,
            client_id=matter.client_id,
        ):
            writer.write_table(
                _feature_rows_table(batch, dimensions=contract.dimensions, schema=schema),
                row_group_size=FEATURE_LOAD_BATCH_SIZE,
            )
            if progress_callback is not None:
                progress_callback(processed, ready, missing, failed)
        writer.close()
        feature_content_hash, feature_byte_length = _hash_seekable_stream(feature_stream)
        return PreparedScoringFeatures(
            feature_stream=feature_stream,
            feature_row_count=processed,
            feature_byte_length=feature_byte_length,
            feature_specification=feature_specification,
            feature_specification_hash=feature_specification_hash,
            feature_content_hash=feature_content_hash,
            feature_ready_count=ready,
            feature_missing_count=missing,
            feature_failed_count=failed,
        )
    except Exception:
        writer.close()
        feature_stream.close()
        raise


def materialize_scoring_features(
    db: Session,
    *,
    snapshot: ClassificationDatasetSnapshot,
    prepared: PreparedScoringFeatures,
    actor_user_id: uuid.UUID,
) -> ClassificationDatasetSnapshot:
    matter = db.get(Matter, snapshot.matter_id)
    if matter is None:
        raise ClassificationFeatureSplitError("Scoring snapshot matter was not found")
    try:
        feature_reference, _ = store_classification_workflow_artifact(
            content=prepared.feature_stream,
            artifact_type=ArtifactType.CLASSIFICATION_FEATURES,
            core_scope_kind="CLASSIFICATION_DATASET_SNAPSHOT",
            core_scope_id=snapshot.id,
            schema_version=FEATURE_SCHEMA_VERSION,
            derivation_key=_derivation_key(
                snapshot_id=snapshot.id,
                artifact_type=ArtifactType.CLASSIFICATION_FEATURES,
                content_hash=prepared.feature_content_hash,
            ),
            content_hash=prepared.feature_content_hash,
            byte_length=prepared.feature_byte_length,
            media_type=PARQUET_MEDIA_TYPE,
            producer_kind="CORE",
            producer_run_id=snapshot.id,
            artifact_metadata={
                "snapshot_aggregate_hash": snapshot.aggregate_hash,
                "feature_specification_hash": prepared.feature_specification_hash,
                "row_count": prepared.feature_row_count,
                "population_row_count": snapshot.population_count,
                "ready_count": prepared.feature_ready_count,
                "missing_count": prepared.feature_missing_count,
                "failed_count": prepared.feature_failed_count,
                "purpose": "SCORING",
            },
            lineage=[
                WorkflowArtifactLineageInput(
                    source_kind="WORKFLOW_ARTIFACT",
                    source_artifact_id=snapshot.population_artifact_id,
                )
            ],
            actor_user_id=actor_user_id,
            tenant_id=snapshot.tenant_id,
            client_id=matter.client_id,
            filename="classification-scoring-features.parquet",
        )
    finally:
        prepared.feature_stream.close()
    snapshot.feature_artifact_id = feature_reference.artifact_id
    snapshot.feature_content_hash = feature_reference.content_hash
    snapshot.feature_specification = prepared.feature_specification
    snapshot.feature_specification_hash = prepared.feature_specification_hash
    snapshot.feature_ready_count = prepared.feature_ready_count
    snapshot.feature_missing_count = prepared.feature_missing_count
    snapshot.feature_failed_count = prepared.feature_failed_count
    snapshot.training_eligible_count = 0
    snapshot.status = "READY_FOR_SCORING"
    snapshot.failure_code = None
    snapshot.failure_summary = None
    snapshot.completed_at = datetime.now(timezone.utc)
    record_audit(
        db,
        tenant_id=snapshot.tenant_id,
        actor_user_id=actor_user_id,
        action="classification_dataset_snapshot.ready_for_scoring",
        target_type="classification_dataset_snapshot",
        target_id=snapshot.id,
        details={
            "matter_id": str(snapshot.matter_id),
            "classification_model_id": str(snapshot.classification_model_id),
            "feature_artifact_id": str(feature_reference.artifact_id),
            "population_count": snapshot.population_count,
            "feature_ready_count": prepared.feature_ready_count,
        },
    )
    db.flush()
    return snapshot


def request_matches_materialized_snapshot(
    snapshot: ClassificationDatasetSnapshot,
    request: ClassificationFeatureSplitRequest,
) -> bool:
    if snapshot.status != "READY_FOR_TRAINING":
        return False
    feature = snapshot.feature_specification or {}
    split = snapshot.split_specification or {}
    return (
        feature.get("embedding_job_id") == str(request.embedding_job_id)
        and split.get("random_seed") == request.random_seed
        and split.get("validation_policy") == request.validation_policy.model_dump(mode="json")
    )


def begin_feature_split(snapshot: ClassificationDatasetSnapshot) -> None:
    snapshot.status = "BUILDING"
    snapshot.failure_code = None
    snapshot.failure_summary = None
    snapshot.completed_at = None
    snapshot.feature_ready_count = 0
    snapshot.trained_class_keys = []
    snapshot.excluded_classes = []
    snapshot.feature_missing_count = 0
    snapshot.feature_failed_count = 0
    snapshot.training_eligible_count = 0


def _derivation_key(
    *,
    snapshot_id: uuid.UUID,
    artifact_type: ArtifactType,
    content_hash: str,
) -> str:
    return _sha256(
        _canonical_json(
            {
                "contract_version": 1,
                "snapshot_id": str(snapshot_id),
                "artifact_type": artifact_type.value,
                "content_hash": content_hash,
            }
        )
    )


def materialize_feature_split(
    db: Session,
    *,
    snapshot: ClassificationDatasetSnapshot,
    prepared: PreparedFeatureSplit,
    actor_user_id: uuid.UUID,
) -> ClassificationDatasetSnapshot:
    matter = db.get(Matter, snapshot.matter_id)
    if matter is None:
        raise ClassificationFeatureSplitError("Dataset snapshot matter was not found")
    client_id = matter.client_id
    feature_reference, _ = store_classification_workflow_artifact(
        content=io.BytesIO(prepared.feature_bytes),
        artifact_type=ArtifactType.CLASSIFICATION_FEATURES,
        core_scope_kind="CLASSIFICATION_DATASET_SNAPSHOT",
        core_scope_id=snapshot.id,
        schema_version=FEATURE_SCHEMA_VERSION,
        derivation_key=_derivation_key(
            snapshot_id=snapshot.id,
            artifact_type=ArtifactType.CLASSIFICATION_FEATURES,
            content_hash=prepared.feature_content_hash,
        ),
        content_hash=prepared.feature_content_hash,
        byte_length=len(prepared.feature_bytes),
        media_type=PARQUET_MEDIA_TYPE,
        producer_kind="CORE",
        producer_run_id=snapshot.id,
        artifact_metadata={
            "snapshot_aggregate_hash": snapshot.aggregate_hash,
            "feature_specification_hash": prepared.feature_specification_hash,
            "row_count": len(prepared.feature_rows),
            "population_row_count": snapshot.population_count,
            "ready_count": prepared.feature_ready_count,
            "missing_count": prepared.feature_missing_count,
            "failed_count": prepared.feature_failed_count,
        },
        lineage=[
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=snapshot.population_artifact_id,
            )
        ],
        actor_user_id=actor_user_id,
        tenant_id=snapshot.tenant_id,
        client_id=client_id,
        filename="classification-features.parquet",
    )
    split_reference, _ = store_classification_workflow_artifact(
        content=io.BytesIO(prepared.split_bytes),
        artifact_type=ArtifactType.CLASSIFICATION_SPLITS,
        core_scope_kind="CLASSIFICATION_DATASET_SNAPSHOT",
        core_scope_id=snapshot.id,
        schema_version=SPLIT_SCHEMA_VERSION,
        derivation_key=_derivation_key(
            snapshot_id=snapshot.id,
            artifact_type=ArtifactType.CLASSIFICATION_SPLITS,
            content_hash=prepared.split_content_hash,
        ),
        content_hash=prepared.split_content_hash,
        byte_length=len(prepared.split_bytes),
        media_type=PARQUET_MEDIA_TYPE,
        producer_kind="CORE",
        producer_run_id=snapshot.id,
        artifact_metadata={
            "snapshot_aggregate_hash": snapshot.aggregate_hash,
            "training_aggregate_hash": prepared.training_aggregate_hash,
            "split_specification_hash": prepared.split_specification_hash,
            "row_count": snapshot.population_count,
            "training_eligible_count": prepared.training_eligible_count,
        },
        lineage=[
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=snapshot.population_artifact_id,
            ),
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=snapshot.label_artifact_id,
            ),
            WorkflowArtifactLineageInput(
                source_kind="WORKFLOW_ARTIFACT",
                source_artifact_id=feature_reference.artifact_id,
            ),
        ],
        actor_user_id=actor_user_id,
        tenant_id=snapshot.tenant_id,
        client_id=client_id,
        filename="classification-splits.parquet",
    )
    _finalize_feature_split(
        snapshot,
        prepared=prepared,
        feature_reference=feature_reference,
        split_reference=split_reference,
    )
    record_audit(
        db,
        tenant_id=snapshot.tenant_id,
        actor_user_id=actor_user_id,
        action="classification_dataset_snapshot.ready_for_training",
        target_type="classification_dataset_snapshot",
        target_id=snapshot.id,
        details={
            "matter_id": str(snapshot.matter_id),
            "classification_model_id": str(snapshot.classification_model_id),
            "feature_artifact_id": str(feature_reference.artifact_id),
            "split_artifact_id": str(split_reference.artifact_id),
            "training_aggregate_hash": prepared.training_aggregate_hash,
            "training_eligible_count": prepared.training_eligible_count,
        },
    )
    db.flush()
    return snapshot


def _finalize_feature_split(
    snapshot: ClassificationDatasetSnapshot,
    *,
    prepared: PreparedFeatureSplit,
    feature_reference: WorkflowArtifactReference,
    split_reference: WorkflowArtifactReference,
) -> None:
    snapshot.feature_artifact_id = feature_reference.artifact_id
    snapshot.feature_content_hash = feature_reference.content_hash
    snapshot.feature_specification = prepared.feature_specification
    snapshot.feature_specification_hash = prepared.feature_specification_hash
    snapshot.split_artifact_id = split_reference.artifact_id
    snapshot.split_content_hash = split_reference.content_hash
    snapshot.split_specification = prepared.split_specification
    snapshot.split_specification_hash = prepared.split_specification_hash
    snapshot.training_aggregate_hash = prepared.training_aggregate_hash
    snapshot.feature_ready_count = prepared.feature_ready_count
    snapshot.feature_missing_count = prepared.feature_missing_count
    snapshot.feature_failed_count = prepared.feature_failed_count
    snapshot.training_eligible_count = prepared.training_eligible_count
    snapshot.trained_class_keys = prepared.trained_class_keys
    snapshot.excluded_classes = prepared.excluded_classes
    warnings = list(snapshot.warnings or [])
    warning_codes = {warning.get("code") for warning in warnings}
    if prepared.feature_missing_count and "MISSING_FEATURES_EXCLUDED" not in warning_codes:
        warnings.append(
            {
                "code": "MISSING_FEATURES_EXCLUDED",
                "message": "Documents without the pinned chunk-vector artifact are excluded from training",
                "count": prepared.feature_missing_count,
            }
        )
    if prepared.feature_failed_count and "FAILED_FEATURES_EXCLUDED" not in warning_codes:
        warnings.append(
            {
                "code": "FAILED_FEATURES_EXCLUDED",
                "message": "Documents with invalid pinned chunk-vector artifacts are excluded from training",
                "count": prepared.feature_failed_count,
            }
        )
    if "NEAR_DUPLICATE_GROUPING_UNAVAILABLE" not in warning_codes:
        warnings.append(
            {
                "code": "NEAR_DUPLICATE_GROUPING_UNAVAILABLE",
                "message": "Splits use family and exact-duplicate groups; near-duplicate groups are not yet available",
                "count": None,
            }
        )
    if prepared.excluded_classes and "CLASSES_EXCLUDED_FROM_TRAINING" not in warning_codes:
        warnings.append(
            {
                "code": "CLASSES_EXCLUDED_FROM_TRAINING",
                "message": "One-vs-rest classes without sufficient leakage-safe support were excluded",
                "count": len(prepared.excluded_classes),
            }
        )
    snapshot.warnings = warnings
    snapshot.status = "READY_FOR_TRAINING"
    snapshot.failure_code = None
    snapshot.failure_summary = None
    snapshot.completed_at = datetime.now(timezone.utc)
