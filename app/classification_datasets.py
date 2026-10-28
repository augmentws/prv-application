import hashlib
import io
import json
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analysis_input_manifests import (
    AnalysisInputContract,
    AnalysisInputDocument,
    AnalysisInputExtensionField,
    PreparedAnalysisInputManifest,
    prepare_analysis_input_manifest,
    publish_analysis_input_manifest,
)
from app.artifact_gateway import (
    CollectionItemGroupingSnapshot,
    WorkflowArtifactReference,
    get_collection_item_grouping_snapshots,
    store_classification_workflow_artifact,
)
from app.audit import record_audit
from app.classification_models import (
    CLASSIFICATION_TARGET_TYPES,
    active_value_keys,
    eligible_target_definition,
    get_classification_model,
    target_field_snapshot,
    target_schema_fingerprint,
)
from app.document_metadata import event_value
from app.metadata_hierarchy import parse_hierarchy_path
from app.models import (
    ClassificationDatasetSnapshot,
    ClassificationModel,
    ClassificationModelConfiguration,
    DocumentMetadataCurrent,
    Matter,
    MatterDocument,
    ReviewBatch,
    ReviewBatchDocument,
    ReviewBatchReviewerCoding,
    ReviewBatchReviewerRevision,
    ReviewBatchRun,
    ReviewBatchRunValue,
)
from app.schemas import (
    ClassificationDatasetIssue,
    ClassificationDatasetSnapshotPreview,
    ClassificationDatasetSnapshotRequest,
    ClassificationLabelSourceSummary,
    ClassificationTargetDistributionRead,
)
from artifact_service.schemas import WorkflowArtifactLineageInput
from classification_service.schemas import (
    ArtifactType,
    ClassificationStrategy,
    LabelAuthority,
    LabelConflictAction,
    LabelPolicy,
    LabelSourceKind,
    LabelSourceSpecification,
    MissingLabelAction,
    TargetCardinality,
    TargetFieldSnapshot,
    TargetMetadataType,
    TrainingScopeSpecification,
    TrainingScopeType,
)

PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
LABEL_SCHEMA_VERSION = 1
AUTHORITY_RANK = {
    LabelAuthority.SILVER: 1,
    LabelAuthority.GOLD: 2,
    LabelAuthority.ADJUDICATED: 3,
}


class ClassificationDatasetError(ValueError):
    pass


class ClassificationDatasetNotFoundError(ClassificationDatasetError):
    pass


class ClassificationDatasetConflictError(ClassificationDatasetError):
    pass


@dataclass(frozen=True)
class PopulationRow:
    matter_document_id: uuid.UUID
    collection_item_id: uuid.UUID
    source_collection_id: uuid.UUID
    scope_ordinal: int
    family_id: uuid.UUID | None
    exact_duplicate_group_id: str | None
    record_type: str | None


@dataclass(frozen=True)
class LabelClaim:
    policy_index: int
    source: LabelSourceSpecification
    raw_value_keys: tuple[str, ...]
    source_record_ids: tuple[str, ...]
    label_timestamp: datetime | None
    observed_class_keys: tuple[str, ...] | None
    target_conflict: bool = False


@dataclass(frozen=True)
class ResolvedLabel:
    matter_document_id: uuid.UUID
    raw_value_keys: tuple[str, ...]
    binary_label: int | None
    multiclass_key: str | None
    one_vs_rest_labels: tuple[int, ...]
    one_vs_rest_observed_mask: tuple[bool, ...]
    source_kind: str | None
    source_reference_id: uuid.UUID | None
    authority: str | None
    source_record_ids: tuple[str, ...]
    label_timestamp: datetime | None
    source_weight: float | None
    candidate_claims_json: str
    resolution_status: str
    reason_code: str
    class_keys: tuple[str, ...]


@dataclass(frozen=True)
class PreparedDatasetSnapshot:
    model: ClassificationModel
    configuration: ClassificationModelConfiguration
    label_policy: LabelPolicy
    request: ClassificationDatasetSnapshotRequest
    population_rows: list[PopulationRow]
    label_rows: list[ResolvedLabel]
    population_manifest: PreparedAnalysisInputManifest
    label_bytes: bytes | None
    preview: ClassificationDatasetSnapshotPreview


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _issue(code: str, message: str, count: int | None = None) -> ClassificationDatasetIssue:
    return ClassificationDatasetIssue(code=code, message=message, count=count)


def _append_issue(
    issues: list[ClassificationDatasetIssue],
    *,
    code: str,
    message: str,
    count: int | None = None,
) -> None:
    existing = next((value for value in issues if value.code == code), None)
    if existing is None:
        issues.append(_issue(code, message, count))
    elif count is not None:
        existing.count = (existing.count or 0) + count


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _timestamp_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat()


def _metadata(values: dict[str, Any]) -> dict[bytes, bytes]:
    return {
        f"pvr.{key}".encode(): str(value).encode()
        for key, value in sorted(values.items())
        if value is not None
    }


def _classification_input_contract(*, configuration_hash: str) -> AnalysisInputContract:
    return AnalysisInputContract(
        key="statistical-classification.population",
        version=1,
        resolver_version="classification-population-resolver-v1",
        extension_fields=[
            AnalysisInputExtensionField(name="family_id", type="STRING"),
            AnalysisInputExtensionField(name="exact_duplicate_group_id", type="STRING"),
            AnalysisInputExtensionField(name="record_type", type="STRING"),
        ],
        options={"configuration_hash": configuration_hash},
    )


def _write_population_parquet(
    rows: list[PopulationRow],
    *,
    configuration_hash: str,
    scope: TrainingScopeSpecification,
) -> PreparedAnalysisInputManifest:
    return prepare_analysis_input_manifest(
        [
            AnalysisInputDocument(
                matter_document_id=row.matter_document_id,
                collection_item_id=row.collection_item_id,
                source_collection_id=row.source_collection_id,
                scope_ordinal=row.scope_ordinal,
                extensions={
                    "family_id": str(row.family_id) if row.family_id else None,
                    "exact_duplicate_group_id": row.exact_duplicate_group_id,
                    "record_type": row.record_type,
                },
            )
            for row in rows
        ],
        contract=_classification_input_contract(configuration_hash=configuration_hash),
        scope_type=scope.scope_type.value,
        scope_id=scope.scope_id,
    )


def _write_label_parquet(
    rows: list[ResolvedLabel],
    *,
    configuration_hash: str,
    strategy: str,
    ordered_class_keys: list[str],
) -> bytes:
    schema = pa.schema(
        [
            ("matter_document_id", pa.string()),
            ("raw_value_keys", pa.list_(pa.string())),
            ("binary_label", pa.int8()),
            ("multiclass_key", pa.string()),
            ("one_vs_rest_labels", pa.list_(pa.int8())),
            ("one_vs_rest_observed_mask", pa.list_(pa.bool_())),
            ("class_keys", pa.list_(pa.string())),
            ("source_kind", pa.string()),
            ("source_reference_id", pa.string()),
            ("authority", pa.string()),
            ("source_record_ids", pa.list_(pa.string())),
            ("label_timestamp", pa.string()),
            ("source_weight", pa.float64()),
            ("normalized_training_weight", pa.float64()),
            ("candidate_claims_json", pa.large_string()),
            ("resolution_status", pa.string()),
            ("reason_code", pa.string()),
        ],
        metadata=_metadata(
            {
                "schema": "classification-labels",
                "schema_version": LABEL_SCHEMA_VERSION,
                "configuration_hash": configuration_hash,
                "classification_strategy": strategy,
                "ordered_class_keys": json.dumps(ordered_class_keys, ensure_ascii=False, separators=(",", ":")),
            }
        ),
    )
    table = pa.Table.from_pylist(
        [
            {
                "matter_document_id": str(row.matter_document_id),
                "raw_value_keys": list(row.raw_value_keys),
                "binary_label": row.binary_label,
                "multiclass_key": row.multiclass_key,
                "one_vs_rest_labels": list(row.one_vs_rest_labels),
                "one_vs_rest_observed_mask": list(row.one_vs_rest_observed_mask),
                "class_keys": list(row.class_keys),
                "source_kind": row.source_kind,
                "source_reference_id": (
                    str(row.source_reference_id) if row.source_reference_id else None
                ),
                "authority": row.authority,
                "source_record_ids": list(row.source_record_ids),
                "label_timestamp": _timestamp_text(row.label_timestamp),
                "source_weight": row.source_weight,
                "normalized_training_weight": None,
                "candidate_claims_json": row.candidate_claims_json,
                "resolution_status": row.resolution_status,
                "reason_code": row.reason_code,
            }
            for row in rows
        ],
        schema=schema,
    )
    output = io.BytesIO()
    pq.write_table(
        table,
        output,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
        data_page_version="2.0",
    )
    return output.getvalue()


def _configuration(
    db: Session,
    *,
    model: ClassificationModel,
    configuration_id: uuid.UUID,
) -> ClassificationModelConfiguration:
    configuration = db.scalar(
        select(ClassificationModelConfiguration).where(
            ClassificationModelConfiguration.id == configuration_id,
            ClassificationModelConfiguration.classification_model_id == model.id,
        )
    )
    if configuration is None:
        raise ClassificationDatasetNotFoundError("Classification model configuration not found")
    return configuration


def _scope_documents(
    db: Session,
    *,
    matter: Matter,
    scope: TrainingScopeSpecification,
    blocking_errors: list[ClassificationDatasetIssue],
) -> list[tuple[MatterDocument, int]]:
    if scope.scope_type == TrainingScopeType.MATTER:
        if scope.scope_id != matter.id:
            _append_issue(
                blocking_errors,
                code="MATTER_SCOPE_ID_MISMATCH",
                message="Matter scope ID must equal the model's matter ID",
            )
            return []
        documents = db.scalars(
            select(MatterDocument)
            .where(MatterDocument.matter_id == matter.id)
            .order_by(MatterDocument.id)
        ).all()
        return [(document, index) for index, document in enumerate(documents, start=1)]

    batch = db.get(ReviewBatch, scope.scope_id)
    if batch is None:
        _append_issue(
            blocking_errors,
            code="REVIEW_BATCH_NOT_FOUND",
            message="Training review batch was not found",
        )
        return []
    if batch.matter_id != matter.id:
        _append_issue(
            blocking_errors,
            code="REVIEW_BATCH_WRONG_MATTER",
            message="Training review batch does not belong to the model's matter",
        )
        return []
    if batch.status != "READY":
        _append_issue(
            blocking_errors,
            code="REVIEW_BATCH_NOT_READY",
            message="Training review batch must be ready before it can be snapshotted",
        )
        return []
    rows = db.execute(
        select(MatterDocument, ReviewBatchDocument.sequence_number)
        .join(
            ReviewBatchDocument,
            ReviewBatchDocument.matter_document_id == MatterDocument.id,
        )
        .where(
            ReviewBatchDocument.review_batch_id == batch.id,
            MatterDocument.matter_id == matter.id,
        )
        .order_by(MatterDocument.id)
    ).all()
    if len(rows) != batch.document_count:
        _append_issue(
            blocking_errors,
            code="REVIEW_BATCH_MEMBERSHIP_INCOMPLETE",
            message="Ready review-batch membership does not match its recorded document count",
        )
    return [(document, sequence_number) for document, sequence_number in rows]


def _population_rows(
    documents: list[tuple[MatterDocument, int]],
    *,
    grouping: dict[uuid.UUID, CollectionItemGroupingSnapshot],
) -> list[PopulationRow]:
    return [
        PopulationRow(
            matter_document_id=document.id,
            collection_item_id=document.collection_item_id,
            source_collection_id=document.source_collection_id,
            scope_ordinal=scope_ordinal,
            family_id=(grouping[document.collection_item_id].family_id if document.collection_item_id in grouping else None),
            exact_duplicate_group_id=(
                grouping[document.collection_item_id].native_content_hash
                if document.collection_item_id in grouping
                else None
            ),
            record_type=(
                grouping[document.collection_item_id].record_type
                if document.collection_item_id in grouping
                else None
            ),
        )
        for document, scope_ordinal in documents
    ]


def _target_value(value: Any, target: TargetFieldSnapshot) -> str | None:
    if target.metadata_type == TargetMetadataType.BOOLEAN:
        return str(value).lower() if isinstance(value, bool) else None
    if not isinstance(value, str) or not value.strip():
        return None
    if target.hierarchy_separator is None or target.hierarchy_level is None:
        return value
    try:
        segments = parse_hierarchy_path(value, target.hierarchy_separator)
    except (TypeError, ValueError):
        return None
    if len(segments) < target.hierarchy_level:
        return None
    return target.hierarchy_separator.join(segments[: target.hierarchy_level])


def _observed_class_keys(
    run: ReviewBatchRun,
    *,
    target: TargetFieldSnapshot,
) -> tuple[str, ...] | None:
    configured = (run.configuration_snapshot or {}).get("classification_observed_class_keys")
    if isinstance(configured, dict):
        configured = configured.get(str(target.metadata_definition_id), configured.get(target.stable_key))
    if not isinstance(configured, list):
        return None
    return tuple(
        sorted(
            {
                projected
                for value in configured
                if (projected := _target_value(value, target)) is not None
            }
        )
    )


def _run_is_blinded(run: ReviewBatchRun) -> bool:
    configuration = run.configuration_snapshot or {}
    explicitly_eligible = configuration.get("classification_training_eligible") is True
    blinded = (
        run.purpose == "ASSESSMENT"
        or configuration.get("blinded") is True
        or configuration.get("sealed_validation") is True
        or configuration.get("random_audit") is True
    )
    return blinded and not explicitly_eligible


def _validate_run_source(
    db: Session,
    *,
    matter: Matter,
    source: LabelSourceSpecification,
    blocking_errors: list[ClassificationDatasetIssue],
) -> ReviewBatchRun | None:
    if source.source_reference_id is None:
        _append_issue(
            blocking_errors,
            code="LABEL_SOURCE_REFERENCE_REQUIRED",
            message=f"{source.kind.value} requires a pinned review batch run",
        )
        return None
    run = db.get(ReviewBatchRun, source.source_reference_id)
    batch = db.get(ReviewBatch, run.review_batch_id) if run is not None else None
    if run is None or batch is None:
        _append_issue(
            blocking_errors,
            code="LABEL_SOURCE_NOT_FOUND",
            message=f"Pinned label source {source.source_reference_id} was not found",
        )
        return None
    if batch.matter_id != matter.id:
        _append_issue(
            blocking_errors,
            code="LABEL_SOURCE_WRONG_MATTER",
            message="Pinned label sources must belong to the model's matter",
        )
        return None
    if run.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
        _append_issue(
            blocking_errors,
            code="LABEL_SOURCE_NOT_COMPLETE",
            message="Pinned label source runs must be complete",
        )
        return None
    if source.kind == LabelSourceKind.ACCEPTED_JEV and run.run_type not in {"AGENT", "WORKFLOW"}:
        _append_issue(
            blocking_errors,
            code="LABEL_SOURCE_KIND_MISMATCH",
            message="ACCEPTED_JEV sources must reference an agent or workflow run",
        )
        return None
    return run


def _validate_reviewer_source(
    db: Session,
    *,
    matter: Matter,
    source: LabelSourceSpecification,
    blocking_errors: list[ClassificationDatasetIssue],
) -> ReviewBatchReviewerCoding | None:
    if source.source_reference_id is None:
        _append_issue(
            blocking_errors,
            code="LABEL_SOURCE_REFERENCE_REQUIRED",
            message=f"{source.kind.value} requires a pinned reviewer coding source",
        )
        return None
    coding = db.get(ReviewBatchReviewerCoding, source.source_reference_id)
    batch = db.get(ReviewBatch, coding.review_batch_id) if coding is not None else None
    if coding is None or batch is None:
        _append_issue(
            blocking_errors,
            code="LABEL_SOURCE_NOT_FOUND",
            message=f"Pinned label source {source.source_reference_id} was not found",
        )
        return None
    if batch.matter_id != matter.id:
        _append_issue(
            blocking_errors,
            code="LABEL_SOURCE_WRONG_MATTER",
            message="Pinned label sources must belong to the model's matter",
        )
        return None
    if (
        source.source_revision is not None
        and db.get(
            ReviewBatchReviewerRevision,
            (coding.id, source.source_revision),
        )
        is None
    ):
        _append_issue(
            blocking_errors,
            code="LABEL_SOURCE_REVISION_NOT_FOUND",
            message=(
                f"Reviewer source {coding.id} does not contain revision "
                f"{source.source_revision}"
            ),
        )
        return None
    return coding


def _matter_claims(
    db: Session,
    *,
    document_ids: list[uuid.UUID],
    target: TargetFieldSnapshot,
    policy_index: int,
    source: LabelSourceSpecification,
) -> dict[uuid.UUID, LabelClaim]:
    grouped: dict[uuid.UUID, list[DocumentMetadataCurrent]] = defaultdict(list)
    for offset in range(0, len(document_ids), 10_000):
        rows = db.scalars(
            select(DocumentMetadataCurrent)
            .where(
                DocumentMetadataCurrent.matter_document_id.in_(document_ids[offset : offset + 10_000]),
                DocumentMetadataCurrent.metadata_definition_id == target.metadata_definition_id,
            )
            .order_by(
                DocumentMetadataCurrent.matter_document_id,
                DocumentMetadataCurrent.value_ordinal,
            )
        ).all()
        for row in rows:
            grouped[row.matter_document_id].append(row)
    claims: dict[uuid.UUID, LabelClaim] = {}
    for document_id, values in grouped.items():
        claims[document_id] = _matter_claim(
            document_id=document_id,
            values=values,
            target=target,
            policy_index=policy_index,
            source=source,
        )
    return claims


def _matter_claim(
    *,
    document_id: uuid.UUID,
    values: list[DocumentMetadataCurrent],
    target: TargetFieldSnapshot,
    policy_index: int,
    source: LabelSourceSpecification,
) -> LabelClaim:
    usable = [row for row in values if row.resolution_state == "VALUE"]
    raw_values = tuple(
        sorted(
            {
                value
                for row in usable
                if (value := _target_value(event_value(row), target)) is not None
            }
        )
    )
    provenance = sorted(
        {
            value
            for row in values
            for value in [
                *(row.supporting_event_ids or []),
                *(row.conflicting_event_ids or []),
                *(row.pending_event_ids or []),
                *([str(row.source_event_id)] if row.source_event_id else []),
            ]
        }
    )
    return LabelClaim(
        policy_index=policy_index,
        source=source,
        raw_value_keys=raw_values,
        source_record_ids=tuple(provenance),
        label_timestamp=max((row.updated_at for row in values), default=None),
        observed_class_keys=None,
        target_conflict=(
            any(row.resolution_state == "CONFLICTED" for row in values)
            or (target.cardinality == TargetCardinality.SINGLE and len(raw_values) > 1)
        ),
    )


def _run_claims(
    db: Session,
    *,
    run: ReviewBatchRun,
    document_ids: list[uuid.UUID],
    target: TargetFieldSnapshot,
    policy_index: int,
    source: LabelSourceSpecification,
) -> dict[uuid.UUID, LabelClaim]:
    rows = db.scalars(
        select(ReviewBatchRunValue)
        .where(
            ReviewBatchRunValue.review_batch_run_id == run.id,
            ReviewBatchRunValue.matter_document_id.in_(document_ids),
            ReviewBatchRunValue.metadata_definition_id == target.metadata_definition_id,
        )
        .order_by(
            ReviewBatchRunValue.matter_document_id,
            ReviewBatchRunValue.value_ordinal,
        )
    ).all()
    grouped: dict[uuid.UUID, list[ReviewBatchRunValue]] = defaultdict(list)
    for row in rows:
        grouped[row.matter_document_id].append(row)
    observed = _observed_class_keys(run, target=target)
    claims: dict[uuid.UUID, LabelClaim] = {}
    for document_id, values in grouped.items():
        raw_values = tuple(
            sorted(
                {
                    value
                    for row in values
                    if (value := _target_value(event_value(row), target)) is not None
                }
            )
        )
        record_ids = tuple(
            f"{run.id}:{document_id}:{target.metadata_definition_id}:{row.value_ordinal}"
            for row in values
        )
        claims[document_id] = LabelClaim(
            policy_index=policy_index,
            source=source,
            raw_value_keys=raw_values,
            source_record_ids=record_ids,
            label_timestamp=max((row.updated_at for row in values), default=None),
            observed_class_keys=observed,
            target_conflict=(target.cardinality.value == "SINGLE" and len(raw_values) > 1),
        )
    return claims


def _reviewer_claims(
    db: Session,
    *,
    coding: ReviewBatchReviewerCoding,
    document_ids: list[uuid.UUID],
    target: TargetFieldSnapshot,
    policy_index: int,
    source: LabelSourceSpecification,
) -> dict[uuid.UUID, LabelClaim]:
    from app.reviewer_coding import resolve_reviewer_coding_as_of

    source_revision = source.source_revision or coding.revision
    resolved = resolve_reviewer_coding_as_of(
        db,
        reviewer_coding_id=coding.id,
        revision=source_revision,
        document_ids=document_ids,
        metadata_definition_ids=[target.metadata_definition_id],
    )
    completed_document_ids = {
        row.matter_document_id for row in resolved.documents if row.status == "COMPLETED"
    }
    grouped: dict[uuid.UUID, list[Any]] = defaultdict(list)
    for row in resolved.values:
        if row.matter_document_id in completed_document_ids:
            grouped[row.matter_document_id].append(row)
    revision_timestamps = {
        revision: created_at
        for revision, created_at in db.execute(
            select(
                ReviewBatchReviewerRevision.revision,
                ReviewBatchReviewerRevision.created_at,
            ).where(
                ReviewBatchReviewerRevision.reviewer_coding_id == coding.id,
                ReviewBatchReviewerRevision.revision.in_({row.revision for row in resolved.values}),
            )
        )
    }
    claims: dict[uuid.UUID, LabelClaim] = {}
    for document_id, values in grouped.items():
        raw_values = tuple(
            sorted(
                {
                    value
                    for row in values
                    if (value := _target_value(event_value(row), target)) is not None
                }
            )
        )
        claims[document_id] = LabelClaim(
            policy_index=policy_index,
            source=source,
            raw_value_keys=raw_values,
            source_record_ids=tuple(
                f"{coding.id}:{document_id}:{target.metadata_definition_id}:{row.value_ordinal}:r{row.revision}"
                for row in values
            ),
            label_timestamp=max(
                (revision_timestamps[row.revision] for row in values),
                default=None,
            ),
            observed_class_keys=None,
            target_conflict=(target.cardinality.value == "SINGLE" and len(raw_values) > 1),
        )
    return claims


def _shape_claim(
    claim: LabelClaim,
    *,
    configuration: ClassificationModelConfiguration,
    class_keys: tuple[str, ...],
    binary_mapping: dict[str, Any] | None,
) -> tuple[tuple[Any, ...] | None, str | None]:
    if claim.target_conflict:
        return None, "TARGET_VALUE_CONFLICT"
    raw_values = set(claim.raw_value_keys)
    strategy = ClassificationStrategy(configuration.classification_strategy)
    if strategy == ClassificationStrategy.BINARY:
        mapping = binary_mapping or {}
        negative = raw_values.intersection(mapping.get("negative_value_keys", []))
        positive = raw_values.intersection(mapping.get("positive_value_keys", []))
        if negative and positive:
            return None, "TARGET_VALUE_CONFLICT"
        if positive:
            return ("BINARY", 1, mapping["positive_class_key"]), None
        if negative:
            return ("BINARY", 0, mapping["negative_class_key"]), None
        return None, "TARGET_VALUE_OUT_OF_SCOPE"
    if strategy == ClassificationStrategy.MULTICLASS:
        selected = raw_values.intersection(class_keys)
        if len(selected) > 1:
            return None, "TARGET_VALUE_CONFLICT"
        if len(selected) == 1:
            return ("MULTICLASS", next(iter(selected))), None
        return None, "TARGET_VALUE_OUT_OF_SCOPE"

    observed = set(claim.observed_class_keys or class_keys)
    observed.update(raw_values.intersection(class_keys))
    mask = tuple(class_key in observed for class_key in class_keys)
    if not any(mask):
        return None, "TARGET_VALUE_OUT_OF_SCOPE"
    labels = tuple(int(class_key in raw_values) for class_key in class_keys)
    return ("ONE_VS_REST", labels, mask), None


def _unresolved_label(
    document_id: uuid.UUID,
    *,
    status: str,
    reason: str,
    class_keys: tuple[str, ...],
    raw_values: tuple[str, ...] = (),
    claims: list[LabelClaim] | None = None,
) -> ResolvedLabel:
    return ResolvedLabel(
        matter_document_id=document_id,
        raw_value_keys=raw_values,
        binary_label=None,
        multiclass_key=None,
        one_vs_rest_labels=(),
        one_vs_rest_observed_mask=(),
        source_kind=None,
        source_reference_id=None,
        authority=None,
        source_record_ids=(),
        label_timestamp=None,
        source_weight=None,
        candidate_claims_json=_claims_json(claims or []),
        resolution_status=status,
        reason_code=reason,
        class_keys=class_keys,
    )


def _resolved_label(
    document_id: uuid.UUID,
    claim: LabelClaim,
    signature: tuple[Any, ...],
    *,
    class_keys: tuple[str, ...],
    claims: list[LabelClaim],
) -> ResolvedLabel:
    binary_label: int | None = None
    multiclass_key: str | None = None
    ovr_labels: tuple[int, ...] = ()
    ovr_mask: tuple[bool, ...] = ()
    if signature[0] == "BINARY":
        binary_label = signature[1]
    elif signature[0] == "MULTICLASS":
        multiclass_key = signature[1]
    else:
        ovr_labels = signature[1]
        ovr_mask = signature[2]
    return ResolvedLabel(
        matter_document_id=document_id,
        raw_value_keys=claim.raw_value_keys,
        binary_label=binary_label,
        multiclass_key=multiclass_key,
        one_vs_rest_labels=ovr_labels,
        one_vs_rest_observed_mask=ovr_mask,
        source_kind=claim.source.kind.value,
        source_reference_id=claim.source.source_reference_id,
        authority=claim.source.authority.value,
        source_record_ids=claim.source_record_ids,
        label_timestamp=claim.label_timestamp,
        source_weight=claim.source.source_weight,
        candidate_claims_json=_claims_json(claims),
        resolution_status="LABELED",
        reason_code="RESOLVED",
        class_keys=class_keys,
    )


def _claims_json(claims: list[LabelClaim]) -> str:
    return _canonical_json(
        [
            {
                "policy_index": claim.policy_index,
                "source_kind": claim.source.kind.value,
                "source_reference_id": (
                    str(claim.source.source_reference_id)
                    if claim.source.source_reference_id
                    else None
                ),
                "authority": claim.source.authority.value,
                "source_weight": claim.source.source_weight,
                "raw_value_keys": list(claim.raw_value_keys),
                "source_record_ids": list(claim.source_record_ids),
                "label_timestamp": _timestamp_text(claim.label_timestamp),
                "observed_class_keys": (
                    list(claim.observed_class_keys)
                    if claim.observed_class_keys is not None
                    else None
                ),
                "target_conflict": claim.target_conflict,
            }
            for claim in sorted(claims, key=lambda value: value.policy_index)
        ]
    ).decode("utf-8")


def _intersect_claims(claims: list[LabelClaim]) -> LabelClaim | None:
    """Resolve equal-authority multi-value claims to values every source selected."""
    if not claims:
        return None
    shared_values = set(claims[0].raw_value_keys)
    for claim in claims[1:]:
        shared_values.intersection_update(claim.raw_value_keys)
    if not shared_values:
        return None
    timestamps = [claim.label_timestamp for claim in claims if claim.label_timestamp is not None]
    observed_sets = [set(claim.observed_class_keys) for claim in claims if claim.observed_class_keys is not None]
    shared_observed = set.intersection(*observed_sets) if observed_sets else None
    first = claims[0]
    return LabelClaim(
        policy_index=first.policy_index,
        source=first.source,
        raw_value_keys=tuple(sorted(shared_values)),
        source_record_ids=tuple(sorted({value for claim in claims for value in claim.source_record_ids})),
        label_timestamp=max(timestamps) if timestamps else None,
        observed_class_keys=tuple(sorted(shared_observed)) if shared_observed is not None else None,
    )


def _resolve_labels(
    *,
    document_ids: list[uuid.UUID],
    claims_by_document: dict[uuid.UUID, list[LabelClaim]],
    configuration: ClassificationModelConfiguration,
    policy: LabelPolicy,
    blocking_errors: list[ClassificationDatasetIssue],
    warnings: list[ClassificationDatasetIssue],
) -> tuple[list[ResolvedLabel], list[str]]:
    resolved: list[ResolvedLabel] = []
    source_conflicts = 0
    intersected_source_conflicts = 0
    target_conflicts = 0
    missing = 0
    strategy = ClassificationStrategy(configuration.classification_strategy)
    binary_mapping = configuration.binary_mapping
    configured_class_keys = tuple(configuration.ordered_class_keys)
    selections: dict[
        uuid.UUID,
        tuple[LabelClaim | None, list[LabelClaim], str | None, str | None],
    ] = {}
    for document_id in document_ids:
        claims = claims_by_document.get(document_id, [])
        if not claims:
            missing += 1
            selections[document_id] = (None, claims, "UNLABELED", "MISSING_LABEL")
            continue
        highest = max(AUTHORITY_RANK[claim.source.authority] for claim in claims)
        authoritative = sorted(
            [claim for claim in claims if AUTHORITY_RANK[claim.source.authority] == highest],
            key=lambda claim: claim.policy_index,
        )
        invalid = next((claim for claim in authoritative if claim.target_conflict), None)
        if invalid is not None:
            target_conflicts += 1
            selections[document_id] = (invalid, claims, "CONFLICTING", "TARGET_VALUE_CONFLICT")
            continue
        if configured_class_keys:
            shaped = [
                (
                    claim,
                    *_shape_claim(
                        claim,
                        configuration=configuration,
                        class_keys=configured_class_keys,
                        binary_mapping=binary_mapping,
                    ),
                )
                for claim in authoritative
            ]
            invalid_shapes = [
                (claim, reason)
                for claim, signature, reason in shaped
                if signature is None
            ]
            if invalid_shapes:
                target_conflicts += 1
                invalid_claim, invalid_reason = invalid_shapes[0]
                selections[document_id] = (
                    invalid_claim,
                    claims,
                    "EXCLUDED" if invalid_reason == "TARGET_VALUE_OUT_OF_SCOPE" else "CONFLICTING",
                    invalid_reason or "TARGET_VALUE_CONFLICT",
                )
                continue
            shaped_signatures = {signature for _, signature, _ in shaped}
            if len(shaped_signatures) != 1:
                if (
                    policy.source_conflict_action == LabelConflictAction.INTERSECT
                    and strategy == ClassificationStrategy.ONE_VS_REST
                ):
                    intersected = _intersect_claims(authoritative)
                    if intersected is not None:
                        intersected_source_conflicts += 1
                        selections[document_id] = (intersected, claims, None, None)
                        continue
                source_conflicts += 1
                selections[document_id] = (None, claims, "CONFLICTING", "SOURCE_CONFLICT")
                continue
            selections[document_id] = (authoritative[0], claims, None, None)
            continue
        raw_signatures = {claim.raw_value_keys for claim in authoritative}
        if len(raw_signatures) != 1:
            if (
                policy.source_conflict_action == LabelConflictAction.INTERSECT
                and strategy == ClassificationStrategy.ONE_VS_REST
            ):
                intersected = _intersect_claims(authoritative)
                if intersected is not None:
                    intersected_source_conflicts += 1
                    selections[document_id] = (intersected, claims, None, None)
                    continue
            source_conflicts += 1
            selections[document_id] = (None, claims, "CONFLICTING", "SOURCE_CONFLICT")
            continue
        selected_claim = authoritative[0]
        if not selected_claim.raw_value_keys:
            target_conflicts += 1
            selections[document_id] = (selected_claim, claims, "EXCLUDED", "TARGET_VALUE_OUT_OF_SCOPE")
            continue
        selections[document_id] = (selected_claim, claims, None, None)

    discovered_values = sorted(
        {
            value
            for selected, _, status, _ in selections.values()
            if selected is not None and status is None
            for value in selected.raw_value_keys
        }
    )
    if configured_class_keys:
        class_keys = configured_class_keys
    elif strategy == ClassificationStrategy.BINARY and binary_mapping:
        class_keys = (
            binary_mapping["negative_class_key"],
            binary_mapping["positive_class_key"],
        )
    else:
        class_keys = tuple(discovered_values)
    if len(class_keys) < 2:
        _append_issue(
            blocking_errors,
            code="INSUFFICIENT_DISCOVERED_CLASSES",
            message="The frozen training labels must contain at least two distinct classes",
            count=len(class_keys),
        )
    if len(class_keys) > 1000:
        _append_issue(
            blocking_errors,
            code="TOO_MANY_DISCOVERED_CLASSES",
            message="The frozen training labels contain more than 1,000 distinct classes",
            count=len(class_keys),
        )
    if strategy == ClassificationStrategy.BINARY and not binary_mapping and len(class_keys) != 2:
        _append_issue(
            blocking_errors,
            code="BINARY_CLASS_COUNT_INVALID",
            message="Binary training requires exactly two discovered classes",
            count=len(class_keys),
        )

    for document_id in document_ids:
        selected_claim, claims, status, reason = selections[document_id]
        if status is not None or selected_claim is None:
            raw_values = selected_claim.raw_value_keys if selected_claim is not None else tuple(
                sorted({value for claim in claims for value in claim.raw_value_keys})
            )
            resolved.append(
                _unresolved_label(
                    document_id,
                    status=status or "UNLABELED",
                    reason=reason or "MISSING_LABEL",
                    class_keys=class_keys,
                    raw_values=raw_values,
                    claims=claims,
                )
            )
            continue
        signature, shape_reason = _shape_claim(
            selected_claim,
            configuration=configuration,
            class_keys=class_keys,
            binary_mapping=binary_mapping,
        )
        if signature is None:
            target_conflicts += 1
            resolved.append(
                _unresolved_label(
                    document_id,
                    status="EXCLUDED" if shape_reason == "TARGET_VALUE_OUT_OF_SCOPE" else "CONFLICTING",
                    reason=shape_reason or "TARGET_VALUE_CONFLICT",
                    class_keys=class_keys,
                    raw_values=selected_claim.raw_value_keys,
                    claims=claims,
                )
            )
            continue
        resolved.append(
            _resolved_label(
                document_id,
                selected_claim,
                signature,
                class_keys=class_keys,
                claims=claims,
            )
        )

    if missing:
        if policy.missing_label_action == MissingLabelAction.FAIL_SNAPSHOT:
            _append_issue(
                blocking_errors,
                code="MISSING_LABELS_FORBIDDEN",
                message="The label policy requires every scoped document to have a label",
                count=missing,
            )
        else:
            _append_issue(
                warnings,
                code="UNLABELED_DOCUMENTS_EXCLUDED",
                message="Documents without an explicit resolved label are excluded from training",
                count=missing,
            )
    if source_conflicts:
        if policy.source_conflict_action == LabelConflictAction.FAIL_SNAPSHOT:
            _append_issue(
                blocking_errors,
                code="SOURCE_CONFLICTS_FORBIDDEN",
                message="Equally authoritative label sources disagree",
                count=source_conflicts,
            )
        else:
            code = (
                "LABELS_REQUIRE_ADJUDICATION"
                if policy.source_conflict_action == LabelConflictAction.REQUIRE_ADJUDICATION
                else "SOURCE_CONFLICTS_EXCLUDED"
            )
            _append_issue(
                warnings,
                code=code,
                message="Conflicting authoritative labels are excluded from training",
                count=source_conflicts,
            )
    if target_conflicts:
        if policy.target_value_conflict_action == LabelConflictAction.FAIL_SNAPSHOT:
            _append_issue(
                blocking_errors,
                code="TARGET_CONFLICTS_FORBIDDEN",
                message="Target values do not satisfy the configured classification strategy",
                count=target_conflicts,
            )
        else:
            _append_issue(
                warnings,
                code="TARGET_CONFLICTS_EXCLUDED",
                message="Invalid or out-of-scope target values are excluded from training",
                count=target_conflicts,
            )
    if intersected_source_conflicts:
        _append_issue(
            warnings,
            code="SOURCE_CONFLICTS_INTERSECTED",
            message=(
                "Multi-value source disagreements were resolved to values selected by every authoritative source"
            ),
            count=intersected_source_conflicts,
        )
    return resolved, list(class_keys)


def _class_support(
    rows: list[ResolvedLabel],
    *,
    strategy: ClassificationStrategy,
    class_keys: list[str],
) -> dict[str, int]:
    support = {key: 0 for key in class_keys}
    for row in rows:
        if row.resolution_status != "LABELED":
            continue
        if strategy == ClassificationStrategy.BINARY:
            support[class_keys[row.binary_label or 0]] += 1
        elif strategy == ClassificationStrategy.MULTICLASS and row.multiclass_key is not None:
            support[row.multiclass_key] += 1
        else:
            for key, label, observed in zip(
                class_keys,
                row.one_vs_rest_labels,
                row.one_vs_rest_observed_mask,
                strict=True,
            ):
                if observed and label:
                    support[key] += 1
    return support


def target_distribution(
    db: Session,
    *,
    matter: Matter,
    definition_id: uuid.UUID,
    training_scope: TrainingScopeSpecification,
    hierarchy_level: int | None = None,
) -> ClassificationTargetDistributionRead:
    definition = eligible_target_definition(
        db,
        matter=matter,
        definition_id=definition_id,
    )
    if training_scope.scope_type == TrainingScopeType.MATTER:
        if training_scope.scope_id != matter.id:
            raise ClassificationDatasetConflictError(
                "Matter scope ID must equal the model's matter ID"
            )
        population_count = int(
            db.scalar(
                select(func.count())
                .select_from(MatterDocument)
                .where(MatterDocument.matter_id == matter.id)
            )
            or 0
        )
        current_values = (
            select(DocumentMetadataCurrent)
            .where(
                DocumentMetadataCurrent.matter_id == matter.id,
                DocumentMetadataCurrent.metadata_definition_id == definition.id,
            )
            .order_by(
                DocumentMetadataCurrent.matter_document_id,
                DocumentMetadataCurrent.value_ordinal,
            )
        )
    else:
        batch = db.get(ReviewBatch, training_scope.scope_id)
        if batch is None:
            raise ClassificationDatasetConflictError("Training review batch was not found")
        if batch.matter_id != matter.id:
            raise ClassificationDatasetConflictError(
                "Training review batch does not belong to the model's matter"
            )
        if batch.status != "READY":
            raise ClassificationDatasetConflictError(
                "Training review batch must be ready before it can be used"
            )
        population_count = int(
            db.scalar(
                select(func.count())
                .select_from(ReviewBatchDocument)
                .join(
                    MatterDocument,
                    MatterDocument.id == ReviewBatchDocument.matter_document_id,
                )
                .where(
                    ReviewBatchDocument.review_batch_id == batch.id,
                    MatterDocument.matter_id == matter.id,
                )
            )
            or 0
        )
        if population_count != batch.document_count:
            raise ClassificationDatasetConflictError(
                "Ready review-batch membership does not match its recorded document count"
            )
        current_values = (
            select(DocumentMetadataCurrent)
            .join(
                ReviewBatchDocument,
                ReviewBatchDocument.matter_document_id
                == DocumentMetadataCurrent.matter_document_id,
            )
            .where(
                ReviewBatchDocument.review_batch_id == batch.id,
                DocumentMetadataCurrent.matter_id == matter.id,
                DocumentMetadataCurrent.metadata_definition_id == definition.id,
            )
            .order_by(
                DocumentMetadataCurrent.matter_document_id,
                DocumentMetadataCurrent.value_ordinal,
            )
        )

    try:
        base_target = target_field_snapshot(matter=matter, definition=definition)
        target = TargetFieldSnapshot.model_validate(
            {**base_target.model_dump(mode="json"), "hierarchy_level": hierarchy_level}
        )
    except ValidationError as exc:
        raise ClassificationDatasetConflictError(str(exc)) from exc
    source = LabelSourceSpecification(
        kind=LabelSourceKind.MATTER_ASSERTED,
        authority=LabelAuthority.SILVER,
        source_weight=1.0,
    )
    counts: Counter[str] = Counter()
    labeled_count = 0
    conflicting_count = 0

    def count_values(document_id: uuid.UUID, values: list[DocumentMetadataCurrent]) -> None:
        nonlocal conflicting_count, labeled_count
        claim = _matter_claim(
            document_id=document_id,
            values=values,
            target=target,
            policy_index=0,
            source=source,
        )
        if not claim.raw_value_keys:
            return
        if claim.target_conflict:
            conflicting_count += 1
            return
        labeled_count += 1
        counts.update(set(claim.raw_value_keys))

    current_document_id: uuid.UUID | None = None
    document_values: list[DocumentMetadataCurrent] = []
    for current in db.scalars(current_values).yield_per(5_000):
        if current_document_id is not None and current.matter_document_id != current_document_id:
            count_values(current_document_id, document_values)
            document_values = []
        current_document_id = current.matter_document_id
        document_values.append(current)
    if current_document_id is not None:
        count_values(current_document_id, document_values)

    configured_keys = (
        ["false", "true"]
        if definition.type == "BOOLEAN"
        else active_value_keys(definition)
        if definition.type == "ENUM"
        else []
    )
    class_keys = [*configured_keys, *sorted(set(counts) - set(configured_keys))]
    labels = {
        str(value["key"]): str(value.get("label") or value["key"])
        for value in definition.allowed_values or []
    }
    if definition.type == "BOOLEAN":
        labels.update({"false": "False", "true": "True"})
    return ClassificationTargetDistributionRead(
        target_field=target,
        training_scope=training_scope,
        population_count=population_count,
        labeled_document_count=labeled_count,
        unlabeled_document_count=population_count - labeled_count - conflicting_count,
        conflicting_document_count=conflicting_count,
        class_counts=[
            {
                "class_key": class_key,
                "label": labels.get(class_key, class_key),
                "document_count": counts[class_key],
            }
            for class_key in class_keys
        ],
    )


def prepare_dataset_snapshot(
    db: Session,
    *,
    matter: Matter,
    classification_model_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    request: ClassificationDatasetSnapshotRequest,
) -> PreparedDatasetSnapshot:
    model = get_classification_model(
        db,
        matter_id=matter.id,
        classification_model_id=classification_model_id,
    )
    configuration = _configuration(db, model=model, configuration_id=request.configuration_id)
    blocking_errors: list[ClassificationDatasetIssue] = []
    warnings: list[ClassificationDatasetIssue] = []
    if model.status != "ACTIVE":
        _append_issue(
            blocking_errors,
            code="CLASSIFICATION_MODEL_NOT_ACTIVE",
            message="Classification model must be active",
        )
    try:
        target = TargetFieldSnapshot.model_validate(configuration.target_field_snapshot)
        policy = LabelPolicy.model_validate(configuration.label_policy)
    except ValidationError as exc:
        raise ClassificationDatasetConflictError(str(exc)) from exc
    definition = model.target_metadata_definition
    if (
        definition.status != "ACTIVE"
        or definition.value_source != "ASSERTED"
        or definition.type not in CLASSIFICATION_TARGET_TYPES
    ):
        _append_issue(
            blocking_errors,
            code="TARGET_FIELD_NOT_ELIGIBLE",
            message=(
                "The configured target field must remain an active Boolean, enum, text, "
                "or long-text coding field"
            ),
        )
    if target.schema_fingerprint != target_schema_fingerprint(definition):
        _append_issue(
            blocking_errors,
            code="TARGET_SCHEMA_INCOMPATIBLE",
            message="The target field schema no longer matches the selected configuration",
        )

    documents = _scope_documents(
        db,
        matter=matter,
        scope=request.training_scope,
        blocking_errors=blocking_errors,
    )
    if not documents:
        _append_issue(
            blocking_errors,
            code="EMPTY_SCOPE",
            message="The selected training scope contains no documents",
        )
    item_ids = [document.collection_item_id for document, _ in documents]
    try:
        grouping = get_collection_item_grouping_snapshots(
            item_ids=item_ids,
            actor_user_id=actor_user_id,
            tenant_id=model.tenant_id,
            client_id=matter.client_id,
        )
    except (httpx.HTTPError, PermissionError, ValueError) as exc:
        raise ClassificationDatasetConflictError(str(exc)) from exc
    population_rows = _population_rows(documents, grouping=grouping)
    missing_grouping_count = sum(
        row.collection_item_id not in grouping or row.exact_duplicate_group_id is None
        for row in population_rows
    )
    family_grouped_count = sum(row.family_id is not None for row in population_rows)
    duplicate_counts = Counter(
        row.exact_duplicate_group_id
        for row in population_rows
        if row.exact_duplicate_group_id is not None
    )
    exact_duplicate_grouped_count = sum(
        row.exact_duplicate_group_id is not None
        and duplicate_counts[row.exact_duplicate_group_id] > 1
        for row in population_rows
    )
    if missing_grouping_count:
        _append_issue(
            warnings,
            code="GROUPING_METADATA_MISSING",
            message="Some documents lack Artifact grouping or native-content identity",
            count=missing_grouping_count,
        )

    if request.purpose == "SCORING":
        population_manifest = _write_population_parquet(
            population_rows,
            configuration_hash=configuration.configuration_hash,
            scope=request.training_scope,
        )
        population_hash = population_manifest.content_hash
        aggregate_hash = _sha256(
            _canonical_json(
                {
                    "snapshot_schema_version": 1,
                    "classification_model_id": str(model.id),
                    "configuration_id": str(configuration.id),
                    "configuration_hash": configuration.configuration_hash,
                    "purpose": request.purpose,
                    "training_scope": request.training_scope.model_dump(mode="json"),
                    "embedding_job_id": str(request.embedding_job_id),
                    "target_field_snapshot": target.model_dump(mode="json"),
                    "population_content_hash": population_hash,
                }
            )
        )
        preview = ClassificationDatasetSnapshotPreview(
            classification_model_id=model.id,
            configuration_id=configuration.id,
            configuration_hash=configuration.configuration_hash,
            purpose=request.purpose,
            training_scope=request.training_scope,
            target_field_snapshot=target,
            population_count=len(population_rows),
            labeled_count=0,
            unlabeled_count=len(population_rows),
            excluded_count=0,
            conflicting_count=0,
            family_grouped_count=family_grouped_count,
            exact_duplicate_grouped_count=exact_duplicate_grouped_count,
            missing_grouping_count=missing_grouping_count,
            ordered_class_keys=[],
            class_support={},
            source_summary=[],
            population_content_hash=population_hash,
            label_content_hash=None,
            aggregate_hash=aggregate_hash,
            blocking_errors=blocking_errors,
            warnings=warnings,
            can_materialize=not blocking_errors,
        )
        return PreparedDatasetSnapshot(
            model=model,
            configuration=configuration,
            label_policy=policy,
            request=request,
            population_rows=population_rows,
            label_rows=[],
            population_manifest=population_manifest,
            label_bytes=None,
            preview=preview,
        )

    document_ids = [row.matter_document_id for row in population_rows]
    claims_by_document: dict[uuid.UUID, list[LabelClaim]] = defaultdict(list)
    summaries: list[ClassificationLabelSourceSummary] = []
    resolved_sources: list[LabelSourceSpecification] = []
    for policy_index, source in enumerate(policy.allowed_sources):
        resolved_source = source
        excluded_blinded = False
        source_claims: dict[uuid.UUID, LabelClaim] = {}
        if source.kind == LabelSourceKind.MATTER_ASSERTED:
            if source.source_reference_id is not None:
                _append_issue(
                    blocking_errors,
                    code="MATTER_LABEL_SOURCE_REFERENCE_INVALID",
                    message="MATTER_ASSERTED must not specify a source reference",
                )
            else:
                source_claims = _matter_claims(
                    db,
                    document_ids=document_ids,
                    target=target,
                    policy_index=policy_index,
                    source=source,
                )
        elif source.kind in {LabelSourceKind.HUMAN_REVIEW, LabelSourceKind.ADJUDICATED_REVIEW}:
            coding = _validate_reviewer_source(
                db,
                matter=matter,
                source=source,
                blocking_errors=blocking_errors,
            )
            if coding is not None:
                resolved_source = source.model_copy(
                    update={"source_revision": source.source_revision or coding.revision}
                )
                source_claims = _reviewer_claims(
                    db,
                    coding=coding,
                    document_ids=document_ids,
                    target=target,
                    policy_index=policy_index,
                    source=resolved_source,
                )
        else:
            run = _validate_run_source(
                db,
                matter=matter,
                source=source,
                blocking_errors=blocking_errors,
            )
            if run is not None and _run_is_blinded(run):
                excluded_blinded = True
                _append_issue(
                    warnings,
                    code="BLINDED_LABEL_SOURCE_EXCLUDED",
                    message="Blinded validation or audit labels remain excluded from training",
                    count=1,
                )
            elif run is not None:
                source_claims = _run_claims(
                    db,
                    run=run,
                    document_ids=document_ids,
                    target=target,
                    policy_index=policy_index,
                    source=source,
                )
        resolved_sources.append(resolved_source)
        for document_id, claim in source_claims.items():
            claims_by_document[document_id].append(claim)
        summaries.append(
            ClassificationLabelSourceSummary(
                kind=source.kind.value,
                source_reference_id=source.source_reference_id,
                authority=source.authority.value,
                source_weight=source.source_weight,
                claim_count=len(source_claims),
                selected_count=0,
                selected_weight_total=0,
                excluded_blinded=excluded_blinded,
            )
        )

    policy = policy.model_copy(update={"allowed_sources": resolved_sources})

    label_rows, ordered_class_keys = _resolve_labels(
        document_ids=document_ids,
        claims_by_document=claims_by_document,
        configuration=configuration,
        policy=policy,
        blocking_errors=blocking_errors,
        warnings=warnings,
    )
    summary_lookup = {
        (summary.kind, summary.source_reference_id): summary for summary in summaries
    }
    for row in label_rows:
        if row.resolution_status == "LABELED" and row.source_kind is not None:
            summary = summary_lookup[(row.source_kind, row.source_reference_id)]
            summary.selected_count += 1
            summary.selected_weight_total += row.source_weight or 0

    labeled_count = sum(row.resolution_status == "LABELED" for row in label_rows)
    unlabeled_count = sum(row.resolution_status == "UNLABELED" for row in label_rows)
    excluded_count = sum(row.resolution_status == "EXCLUDED" for row in label_rows)
    conflicting_count = sum(row.resolution_status == "CONFLICTING" for row in label_rows)
    if not labeled_count:
        _append_issue(
            blocking_errors,
            code="NO_LABELED_DOCUMENTS",
            message="The selected scope has no usable labels",
        )

    population_manifest = _write_population_parquet(
        population_rows,
        configuration_hash=configuration.configuration_hash,
        scope=request.training_scope,
    )
    label_bytes = _write_label_parquet(
        label_rows,
        configuration_hash=configuration.configuration_hash,
        strategy=configuration.classification_strategy,
        ordered_class_keys=ordered_class_keys,
    )
    frozen_target = target.model_copy(update={"discovered_class_keys": ordered_class_keys})
    population_hash = population_manifest.content_hash
    label_hash = _sha256(label_bytes)
    aggregate_hash = _sha256(
        _canonical_json(
            {
                "snapshot_schema_version": 1,
                "classification_model_id": str(model.id),
                "configuration_id": str(configuration.id),
                "configuration_hash": configuration.configuration_hash,
                "purpose": request.purpose,
                "training_scope": request.training_scope.model_dump(mode="json"),
                "target_field_snapshot": frozen_target.model_dump(mode="json"),
                "label_policy": policy.model_dump(mode="json"),
                "ordered_class_keys": ordered_class_keys,
                "population_content_hash": population_hash,
                "label_content_hash": label_hash,
            }
        )
    )
    preview = ClassificationDatasetSnapshotPreview(
        classification_model_id=model.id,
        configuration_id=configuration.id,
        configuration_hash=configuration.configuration_hash,
        purpose=request.purpose,
        training_scope=request.training_scope,
        target_field_snapshot=frozen_target,
        population_count=len(population_rows),
        labeled_count=labeled_count,
        unlabeled_count=unlabeled_count,
        excluded_count=excluded_count,
        conflicting_count=conflicting_count,
        family_grouped_count=family_grouped_count,
        exact_duplicate_grouped_count=exact_duplicate_grouped_count,
        missing_grouping_count=missing_grouping_count,
        ordered_class_keys=ordered_class_keys,
        class_support=_class_support(
            label_rows,
            strategy=ClassificationStrategy(configuration.classification_strategy),
            class_keys=ordered_class_keys,
        ),
        source_summary=summaries,
        population_content_hash=population_hash,
        label_content_hash=label_hash,
        aggregate_hash=aggregate_hash,
        blocking_errors=blocking_errors,
        warnings=warnings,
        can_materialize=not blocking_errors,
    )
    return PreparedDatasetSnapshot(
        model=model,
        configuration=configuration,
        label_policy=policy,
        request=request,
        population_rows=population_rows,
        label_rows=label_rows,
        population_manifest=population_manifest,
        label_bytes=label_bytes,
        preview=preview,
    )


def begin_dataset_snapshot(
    db: Session,
    *,
    prepared: PreparedDatasetSnapshot,
    actor_user_id: uuid.UUID,
) -> tuple[ClassificationDatasetSnapshot, bool, bool]:
    if prepared.preview.blocking_errors:
        codes = ", ".join(issue.code for issue in prepared.preview.blocking_errors)
        raise ClassificationDatasetConflictError(f"Dataset snapshot is blocked: {codes}")
    existing = db.scalar(
        select(ClassificationDatasetSnapshot).where(
            ClassificationDatasetSnapshot.classification_model_id == prepared.model.id,
            ClassificationDatasetSnapshot.aggregate_hash == prepared.preview.aggregate_hash,
        )
    )
    if existing is not None:
        if existing.status in {"READY_FOR_FEATURES", "READY_FOR_TRAINING", "READY_FOR_SCORING"}:
            return existing, False, False
        existing.status = "BUILDING"
        existing.failure_code = None
        existing.failure_summary = None
        existing.completed_at = None
        return existing, True, False

    preview = prepared.preview
    snapshot = ClassificationDatasetSnapshot(
        id=uuid.uuid5(prepared.model.id, preview.aggregate_hash),
        tenant_id=prepared.model.tenant_id,
        matter_id=prepared.model.matter_id,
        classification_model_id=prepared.model.id,
        configuration_id=prepared.configuration.id,
        purpose=prepared.request.purpose,
        scope_type=prepared.request.training_scope.scope_type.value,
        scope_id=prepared.request.training_scope.scope_id,
        target_field_snapshot=preview.target_field_snapshot.model_dump(mode="json"),
        label_policy_snapshot=prepared.label_policy.model_dump(mode="json"),
        aggregate_hash=preview.aggregate_hash,
        status="BUILDING",
        population_count=preview.population_count,
        labeled_count=preview.labeled_count,
        unlabeled_count=preview.unlabeled_count,
        excluded_count=preview.excluded_count,
        conflicting_count=preview.conflicting_count,
        family_grouped_count=preview.family_grouped_count,
        exact_duplicate_grouped_count=preview.exact_duplicate_grouped_count,
        missing_grouping_count=preview.missing_grouping_count,
        class_support=preview.class_support,
        source_summary=[value.model_dump(mode="json") for value in preview.source_summary],
        warnings=[value.model_dump(mode="json") for value in preview.warnings],
        created_by_user_id=actor_user_id,
    )
    db.add(snapshot)
    db.flush()
    return snapshot, True, True


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


def materialize_dataset_snapshot(
    db: Session,
    *,
    snapshot: ClassificationDatasetSnapshot,
    prepared: PreparedDatasetSnapshot,
    actor_user_id: uuid.UUID,
) -> ClassificationDatasetSnapshot:
    input_manifest, population_reference = publish_analysis_input_manifest(
        db,
        prepared=prepared.population_manifest,
        tenant_id=snapshot.tenant_id,
        client_id=prepared.model.matter.client_id,
        matter_id=snapshot.matter_id,
        owner_type="CLASSIFICATION_DATASET_SNAPSHOT",
        owner_id=snapshot.id,
        producer_kind="CORE",
        producer_run_id=snapshot.id,
        actor_user_id=actor_user_id,
    )
    label_reference: WorkflowArtifactReference | None = None
    if prepared.label_bytes is not None and prepared.preview.label_content_hash is not None:
        label_reference, _ = store_classification_workflow_artifact(
            content=io.BytesIO(prepared.label_bytes),
            artifact_type=ArtifactType.CLASSIFICATION_LABELS,
            core_scope_kind="CLASSIFICATION_DATASET_SNAPSHOT",
            core_scope_id=snapshot.id,
            schema_version=LABEL_SCHEMA_VERSION,
            derivation_key=_derivation_key(
                snapshot_id=snapshot.id,
                artifact_type=ArtifactType.CLASSIFICATION_LABELS,
                content_hash=prepared.preview.label_content_hash,
            ),
            content_hash=prepared.preview.label_content_hash,
            byte_length=len(prepared.label_bytes),
            media_type=PARQUET_MEDIA_TYPE,
            producer_kind="CORE",
            producer_run_id=snapshot.id,
            artifact_metadata={
                "snapshot_aggregate_hash": snapshot.aggregate_hash,
                "configuration_hash": prepared.configuration.configuration_hash,
                "classification_strategy": prepared.configuration.classification_strategy,
                "population_artifact_id": str(population_reference.artifact_id),
                "row_count": snapshot.population_count,
                "labeled_count": snapshot.labeled_count,
            },
            lineage=[
                WorkflowArtifactLineageInput(
                    source_kind="WORKFLOW_ARTIFACT",
                    source_artifact_id=population_reference.artifact_id,
                )
            ],
            actor_user_id=actor_user_id,
            tenant_id=snapshot.tenant_id,
            client_id=prepared.model.matter.client_id,
            filename="classification-labels.parquet",
        )
    _finalize_snapshot(
        snapshot,
        input_manifest_id=input_manifest.id,
        population_reference=population_reference,
        label_reference=label_reference,
    )
    record_audit(
        db,
        tenant_id=snapshot.tenant_id,
        actor_user_id=actor_user_id,
        action="classification_dataset_snapshot.created",
        target_type="classification_dataset_snapshot",
        target_id=snapshot.id,
        details={
            "matter_id": str(snapshot.matter_id),
            "classification_model_id": str(snapshot.classification_model_id),
            "configuration_id": str(snapshot.configuration_id),
            "scope_type": snapshot.scope_type,
            "scope_id": str(snapshot.scope_id),
            "aggregate_hash": snapshot.aggregate_hash,
            "population_count": snapshot.population_count,
            "labeled_count": snapshot.labeled_count,
        },
    )
    db.flush()
    return snapshot


def _finalize_snapshot(
    snapshot: ClassificationDatasetSnapshot,
    *,
    input_manifest_id: uuid.UUID,
    population_reference: WorkflowArtifactReference,
    label_reference: WorkflowArtifactReference | None,
) -> None:
    snapshot.analysis_input_manifest_id = input_manifest_id
    snapshot.population_artifact_id = population_reference.artifact_id
    snapshot.population_content_hash = population_reference.content_hash
    snapshot.label_artifact_id = label_reference.artifact_id if label_reference else None
    snapshot.label_content_hash = label_reference.content_hash if label_reference else None
    snapshot.status = "READY_FOR_FEATURES"
    snapshot.failure_code = None
    snapshot.failure_summary = None
    snapshot.completed_at = datetime.now(timezone.utc)


def fail_dataset_snapshot(
    snapshot: ClassificationDatasetSnapshot,
    *,
    failure_code: str,
    failure_summary: str,
) -> None:
    snapshot.status = "FAILED"
    snapshot.failure_code = failure_code[:100]
    snapshot.failure_summary = failure_summary[:4000]
    snapshot.completed_at = datetime.now(timezone.utc)


def get_dataset_snapshot(
    db: Session,
    *,
    matter_id: uuid.UUID,
    classification_model_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> ClassificationDatasetSnapshot:
    snapshot = db.scalar(
        select(ClassificationDatasetSnapshot).where(
            ClassificationDatasetSnapshot.id == snapshot_id,
            ClassificationDatasetSnapshot.matter_id == matter_id,
            ClassificationDatasetSnapshot.classification_model_id == classification_model_id,
        )
    )
    if snapshot is None:
        raise ClassificationDatasetNotFoundError("Classification dataset snapshot not found")
    return snapshot
