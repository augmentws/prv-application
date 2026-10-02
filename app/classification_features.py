import hashlib
import io
import json
import math
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.artifact_gateway import (
    ChunkVectorArtifactSnapshot,
    WorkflowArtifactReference,
    copy_workflow_artifact_content,
    load_current_chunk_vector_artifact,
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
SPLIT_ALGORITHM_VERSION = 1


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


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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
) -> pa.Table:
    if expected_hash is None or _sha256(content) != expected_hash:
        raise ClassificationFeatureSplitError(
            f"{expected_schema} artifact content does not match its frozen hash",
            code="BASE_ARTIFACT_HASH_MISMATCH",
        )
    try:
        table = pq.read_table(io.BytesIO(content))
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


def _build_feature_rows(
    population: list[dict[str, Any]],
    *,
    job: MatterEmbeddingJob,
    contract: FeatureContract,
    actor_user_id: uuid.UUID,
    snapshot: ClassificationDatasetSnapshot,
    client_id: uuid.UUID,
) -> list[FeatureRow]:
    rows: list[FeatureRow] = []
    for source in population:
        document_id = uuid.UUID(source["matter_document_id"])
        collection_item_id = uuid.UUID(source["collection_item_id"])
        try:
            artifact = load_current_chunk_vector_artifact(
                collection_item_id=collection_item_id,
                configuration_hash=job.configuration_hash,
                actor_user_id=actor_user_id,
                tenant_id=snapshot.tenant_id,
                client_id=client_id,
            )
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
    return rows


def _write_feature_parquet(
    rows: list[FeatureRow],
    *,
    dimensions: int,
    data_type: FeatureDataType,
    specification: dict[str, Any],
    specification_hash: str,
    snapshot: ClassificationDatasetSnapshot,
) -> bytes:
    scalar_type = pa.float32() if data_type == FeatureDataType.FLOAT32 else pa.float64()
    schema = pa.schema(
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
    table = pa.Table.from_pylist(
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
        if is_observed and positive
    )


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
                (fold_sizes[fold] + len(eligible_groups[group_id])) / len(eligible_indices),
                _sha256(f"{seed}:{group_id}:{fold}".encode()),
            ),
        )
        group_fold[group_id] = selected
        fold_counts[selected].update(group_counts[group_id])
        fold_sizes[selected] += len(eligible_groups[group_id])

    for fold, support in enumerate(fold_counts):
        deficient = [key for key in class_keys if support[key] < minimum_per_class]
        if deficient:
            raise ClassificationFeatureSplitError(
                f"Fold {fold} lacks minimum support for: {', '.join(deficient)}",
                code="INSUFFICIENT_CLASS_SUPPORT_PER_FOLD",
            )
    assignments = {
        index: group_fold[group_id]
        for group_id, indices in eligible_groups.items()
        for index in indices
    }
    return assignments, [dict(sorted(support.items())) for support in fold_counts]


def _build_split_rows(
    population: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    features: list[FeatureRow],
    *,
    configuration: ClassificationModelConfiguration,
    request: ClassificationFeatureSplitRequest,
) -> tuple[list[SplitRow], dict[str, Any]]:
    population_ids = [row["matter_document_id"] for row in population]
    label_ids = [row["matter_document_id"] for row in labels]
    feature_ids = [str(row.matter_document_id) for row in features]
    if population_ids != label_ids or population_ids != feature_ids:
        raise ClassificationFeatureSplitError(
            "Population, label, and feature rows must align exactly by document ID and order",
            code="DATASET_ROW_ALIGNMENT_FAILED",
        )
    groups, index_to_group = _connected_groups(population)
    strategy = ClassificationStrategy(configuration.classification_strategy)
    class_keys = list(configuration.ordered_class_keys)
    eligible_indices = {
        index
        for index, (label, feature) in enumerate(zip(labels, features, strict=True))
        if label["resolution_status"] == "LABELED" and feature.status == "READY"
    }
    if len(eligible_indices) < request.validation_policy.minimum_labeled_count:
        raise ClassificationFeatureSplitError(
            "Feature-ready labeled rows do not meet the minimum labeled count",
            code="INSUFFICIENT_LABELED_FEATURES",
        )
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
    for index, (population_row, label, feature) in enumerate(
        zip(population, labels, features, strict=True)
    ):
        eligible = index in eligible_indices
        if label["resolution_status"] != "LABELED":
            exclusion_reason = label["reason_code"]
        elif feature.status != "READY":
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
                matter_document_id=feature.matter_document_id,
                group_id=index_to_group[index],
                family_id=population_row.get("family_id"),
                exact_duplicate_group_id=population_row.get("exact_duplicate_group_id"),
                near_duplicate_group_id=None,
                feature_status=feature.status,
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
    }
    return split_rows, summary


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


def prepare_feature_split(
    db: Session,
    *,
    snapshot: ClassificationDatasetSnapshot,
    request: ClassificationFeatureSplitRequest,
    actor_user_id: uuid.UUID,
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
    if job.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        raise ClassificationFeatureSplitError(
            "Embedding job must be complete before features can be materialized",
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
        job=job,
        contract=contract,
        actor_user_id=actor_user_id,
        snapshot=snapshot,
        client_id=matter.client_id,
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
    split_rows, split_summary = _build_split_rows(
        population,
        labels,
        feature_rows,
        configuration=configuration,
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
    )


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
            "row_count": snapshot.population_count,
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
    snapshot.warnings = warnings
    snapshot.status = "READY_FOR_TRAINING"
    snapshot.failure_code = None
    snapshot.failure_summary = None
    snapshot.completed_at = datetime.now(timezone.utc)
