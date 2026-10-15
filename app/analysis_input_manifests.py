from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifact_gateway import (
    WorkflowArtifactReference,
    copy_workflow_artifact_content,
    get_workflow_artifact_reference,
    store_workflow_artifact,
)
from app.audit import record_audit
from app.models import AnalysisInputManifest
from artifact_service.schemas import WorkflowArtifactLineageInput
from artifact_service.workflow_artifact_types import ArtifactType

ANALYSIS_INPUT_MANIFEST_SCHEMA = "analysis-input-manifest"
ANALYSIS_INPUT_MANIFEST_SCHEMA_VERSION = 1
ANALYSIS_INPUT_MANIFEST_MEDIA_TYPE = "application/vnd.apache.parquet"
ANALYSIS_INPUT_MANIFEST_ROW_GROUP_SIZE = 5_000
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

ExtensionType = Literal["STRING", "INT64", "FLOAT64", "BOOLEAN"]


class AnalysisInputManifestError(ValueError):
    pass


class AnalysisInputManifestConflict(AnalysisInputManifestError):
    pass


class AnalysisInputExtensionField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    type: ExtensionType
    nullable: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _KEY_PATTERN.fullmatch(value):
            raise ValueError("extension field names must be stable lowercase keys")
        if value in BASE_COLUMN_NAMES:
            raise ValueError("extension field name collides with a base manifest column")
        return value


class AnalysisInputContract(BaseModel):
    """Frozen declaration of the inputs a task expects the manifest to carry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    version: int = Field(gt=0)
    resolver_version: str = Field(min_length=1, max_length=100)
    required_input_paths: list[str] = Field(default_factory=list)
    source_artifact_required: bool = False
    extension_fields: list[AnalysisInputExtensionField] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not _KEY_PATTERN.fullmatch(value):
            raise ValueError("contract key must be a stable lowercase key")
        return value

    @field_validator("required_input_paths")
    @classmethod
    def validate_paths(cls, value: list[str]) -> list[str]:
        normalized = [path.strip() for path in value]
        if any(not path or len(path) > 200 for path in normalized):
            raise ValueError("required input paths must be non-empty and at most 200 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("required input paths must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_extensions(self) -> AnalysisInputContract:
        names = [item.name for item in self.extension_fields]
        if len(names) != len(set(names)):
            raise ValueError("extension field names must be unique")
        return self


@dataclass(frozen=True)
class AnalysisInputDocument:
    matter_document_id: uuid.UUID
    collection_item_id: uuid.UUID
    source_collection_id: uuid.UUID
    scope_ordinal: int
    input_status: str = "READY"
    failure_code: str | None = None
    source_artifact_id: uuid.UUID | None = None
    source_content_hash: str | None = None
    resolved_inputs: dict[str, Any] = field(default_factory=dict)
    artifact_references: list[dict[str, Any]] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)


BASE_COLUMNS: tuple[tuple[str, pa.DataType], ...] = (
    ("matter_document_id", pa.string()),
    ("collection_item_id", pa.string()),
    ("source_collection_id", pa.string()),
    ("scope_ordinal", pa.int64()),
    ("input_status", pa.string()),
    ("failure_code", pa.string()),
    ("source_artifact_id", pa.string()),
    ("source_content_hash", pa.string()),
    ("resolved_inputs_json", pa.large_string()),
    ("artifact_references_json", pa.large_string()),
    ("input_content_hash", pa.string()),
)
BASE_COLUMN_NAMES = frozenset(name for name, _ in BASE_COLUMNS)

_EXTENSION_TYPES: dict[ExtensionType, pa.DataType] = {
    "STRING": pa.string(),
    "INT64": pa.int64(),
    "FLOAT64": pa.float64(),
    "BOOLEAN": pa.bool_(),
}


@dataclass(frozen=True)
class PreparedAnalysisInputManifest:
    content: bytes
    content_hash: str
    byte_length: int
    item_count: int
    contract: AnalysisInputContract
    contract_hash: str
    scope_type: str
    scope_id: uuid.UUID


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def canonical_value_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def analysis_input_contract_hash(contract: AnalysisInputContract) -> str:
    return canonical_value_hash(contract.model_dump(mode="json"))


def _metadata(values: dict[str, Any]) -> dict[bytes, bytes]:
    return {
        f"pvr.{key}".encode(): (
            canonical_json_bytes(value) if isinstance(value, (dict, list)) else str(value).encode()
        )
        for key, value in sorted(values.items())
        if value is not None
    }


def _validate_document(document: AnalysisInputDocument, contract: AnalysisInputContract) -> None:
    if document.scope_ordinal <= 0:
        raise AnalysisInputManifestError("scope ordinals must be positive")
    if document.input_status not in {"READY", "MISSING", "FAILED", "EXCLUDED"}:
        raise AnalysisInputManifestError("input status must be READY, MISSING, FAILED, or EXCLUDED")
    if document.input_status == "READY" and document.failure_code is not None:
        raise AnalysisInputManifestError("ready manifest rows cannot have a failure code")
    if document.input_status != "READY" and not document.failure_code:
        raise AnalysisInputManifestError("non-ready manifest rows require a stable failure code")
    if document.input_status == "READY" and contract.source_artifact_required and (
        document.source_artifact_id is None or document.source_content_hash is None
    ):
        raise AnalysisInputManifestError("the input contract requires a source artifact and content hash")
    if document.source_content_hash is not None and not _SHA256_PATTERN.fullmatch(document.source_content_hash):
        raise AnalysisInputManifestError("source content hashes must be lowercase SHA-256 values")
    declared = {item.name: item for item in contract.extension_fields}
    if set(document.extensions) != set(declared):
        raise AnalysisInputManifestError("manifest row extension fields do not match the frozen input contract")
    for name, specification in declared.items():
        if document.extensions[name] is None and not specification.nullable:
            raise AnalysisInputManifestError(f"manifest extension field {name} is required")
    if document.input_status == "READY":
        for path in contract.required_input_paths:
            value: Any = document.resolved_inputs
            for component in path.split("."):
                if not isinstance(value, dict) or component not in value:
                    raise AnalysisInputManifestError(
                        f"ready manifest row is missing required input path {path}"
                    )
                value = value[component]
            if value is None:
                raise AnalysisInputManifestError(
                    f"ready manifest row has null required input path {path}"
                )


def _row(document: AnalysisInputDocument, contract: AnalysisInputContract) -> dict[str, Any]:
    _validate_document(document, contract)
    resolved_inputs_json = canonical_json_bytes(document.resolved_inputs).decode()
    artifact_references_json = canonical_json_bytes(document.artifact_references).decode()
    identity = {
        "matter_document_id": str(document.matter_document_id),
        "collection_item_id": str(document.collection_item_id),
        "source_collection_id": str(document.source_collection_id),
        "scope_ordinal": document.scope_ordinal,
        "input_status": document.input_status,
        "failure_code": document.failure_code,
        "source_artifact_id": str(document.source_artifact_id) if document.source_artifact_id else None,
        "source_content_hash": document.source_content_hash,
        "resolved_inputs": document.resolved_inputs,
        "artifact_references": document.artifact_references,
        "extensions": document.extensions,
    }
    return {
        **identity,
        "resolved_inputs_json": resolved_inputs_json,
        "artifact_references_json": artifact_references_json,
        "input_content_hash": canonical_value_hash(identity),
        **document.extensions,
    }


def prepare_analysis_input_manifest(
    documents: list[AnalysisInputDocument],
    *,
    contract: AnalysisInputContract,
    scope_type: str,
    scope_id: uuid.UUID,
) -> PreparedAnalysisInputManifest:
    """Build deterministic Parquet bytes for a frozen document population.

    This function has no domain-specific database behavior. Producers resolve and
    authorize their population before calling it, then publish the prepared result
    with ``publish_analysis_input_manifest``.
    """

    ordered = sorted(documents, key=lambda value: (value.scope_ordinal, value.matter_document_id))
    document_ids = [value.matter_document_id for value in ordered]
    ordinals = [value.scope_ordinal for value in ordered]
    if len(document_ids) != len(set(document_ids)):
        raise AnalysisInputManifestError("a document may appear only once in an input manifest")
    if len(ordinals) != len(set(ordinals)):
        raise AnalysisInputManifestError("scope ordinals must be unique within an input manifest")
    contract_hash = analysis_input_contract_hash(contract)
    schema = pa.schema(
        [*BASE_COLUMNS, *((item.name, _EXTENSION_TYPES[item.type]) for item in contract.extension_fields)],
        metadata=_metadata(
            {
                "schema": ANALYSIS_INPUT_MANIFEST_SCHEMA,
                "schema_version": ANALYSIS_INPUT_MANIFEST_SCHEMA_VERSION,
                "contract_key": contract.key,
                "contract_version": contract.version,
                "contract_hash": contract_hash,
                "contract": contract.model_dump(mode="json"),
                "resolver_version": contract.resolver_version,
                "scope_type": scope_type,
                "scope_id": scope_id,
            }
        ),
    )
    table = pa.Table.from_pylist([_row(document, contract) for document in ordered], schema=schema)
    output = io.BytesIO()
    pq.write_table(
        table,
        output,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
        data_page_version="2.0",
        row_group_size=ANALYSIS_INPUT_MANIFEST_ROW_GROUP_SIZE,
    )
    content = output.getvalue()
    return PreparedAnalysisInputManifest(
        content=content,
        content_hash=hashlib.sha256(content).hexdigest(),
        byte_length=len(content),
        item_count=len(ordered),
        contract=contract,
        contract_hash=contract_hash,
        scope_type=scope_type,
        scope_id=scope_id,
    )


def _derivation_key(
    *,
    owner_type: str,
    owner_id: uuid.UUID,
    prepared: PreparedAnalysisInputManifest,
) -> str:
    return canonical_value_hash(
        {
            "contract_version": 1,
            "owner_type": owner_type,
            "owner_id": str(owner_id),
            "scope_type": prepared.scope_type,
            "scope_id": str(prepared.scope_id),
            "manifest_schema": ANALYSIS_INPUT_MANIFEST_SCHEMA,
            "manifest_schema_version": ANALYSIS_INPUT_MANIFEST_SCHEMA_VERSION,
            "input_contract_hash": prepared.contract_hash,
            "artifact_content_hash": prepared.content_hash,
        }
    )


def publish_analysis_input_manifest(
    db: Session,
    *,
    prepared: PreparedAnalysisInputManifest,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    matter_id: uuid.UUID,
    owner_type: str,
    owner_id: uuid.UUID,
    producer_kind: str,
    producer_run_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    lineage: list[WorkflowArtifactLineageInput] | None = None,
) -> tuple[AnalysisInputManifest, WorkflowArtifactReference]:
    derivation_key = _derivation_key(owner_type=owner_type, owner_id=owner_id, prepared=prepared)
    manifest = db.scalar(
        select(AnalysisInputManifest).where(
            AnalysisInputManifest.owner_type == owner_type,
            AnalysisInputManifest.owner_id == owner_id,
        )
    )
    if manifest is None:
        manifest = AnalysisInputManifest(
            id=uuid.uuid5(owner_id, f"analysis-input-manifest:{owner_type}"),
            tenant_id=tenant_id,
            client_id=client_id,
            matter_id=matter_id,
            owner_type=owner_type,
            owner_id=owner_id,
            scope_type=prepared.scope_type,
            scope_id=prepared.scope_id,
            manifest_schema=ANALYSIS_INPUT_MANIFEST_SCHEMA,
            manifest_schema_version=ANALYSIS_INPUT_MANIFEST_SCHEMA_VERSION,
            resolver_version=prepared.contract.resolver_version,
            input_contract=prepared.contract.model_dump(mode="json"),
            input_contract_hash=prepared.contract_hash,
            derivation_key=derivation_key,
            item_count=prepared.item_count,
            manifest_status="BUILDING",
            producer_kind=producer_kind,
            producer_run_id=producer_run_id,
            created_by_user_id=actor_user_id,
        )
        db.add(manifest)
        db.flush()
    expected = (
        tenant_id,
        client_id,
        matter_id,
        prepared.scope_type,
        prepared.scope_id,
        ANALYSIS_INPUT_MANIFEST_SCHEMA,
        ANALYSIS_INPUT_MANIFEST_SCHEMA_VERSION,
        prepared.contract.resolver_version,
        prepared.contract_hash,
        derivation_key,
        prepared.item_count,
        producer_kind,
        producer_run_id,
    )
    actual = (
        manifest.tenant_id,
        manifest.client_id,
        manifest.matter_id,
        manifest.scope_type,
        manifest.scope_id,
        manifest.manifest_schema,
        manifest.manifest_schema_version,
        manifest.resolver_version,
        manifest.input_contract_hash,
        manifest.derivation_key,
        manifest.item_count,
        manifest.producer_kind,
        manifest.producer_run_id,
    )
    if actual != expected or manifest.input_contract != prepared.contract.model_dump(mode="json"):
        raise AnalysisInputManifestConflict("input manifest owner is already bound to different frozen inputs")
    reference, _ = store_workflow_artifact(
        content=io.BytesIO(prepared.content),
        artifact_type=ArtifactType.ANALYSIS_INPUT_MANIFEST,
        core_scope_kind="ANALYSIS_INPUT_MANIFEST",
        core_scope_id=manifest.id,
        schema_version=ANALYSIS_INPUT_MANIFEST_SCHEMA_VERSION,
        derivation_key=derivation_key,
        content_hash=prepared.content_hash,
        byte_length=prepared.byte_length,
        media_type=ANALYSIS_INPUT_MANIFEST_MEDIA_TYPE,
        producer_kind=producer_kind,
        producer_run_id=producer_run_id,
        artifact_metadata={
            "owner_type": owner_type,
            "owner_id": str(owner_id),
            "matter_id": str(matter_id),
            "scope_type": prepared.scope_type,
            "scope_id": str(prepared.scope_id),
            "manifest_schema": ANALYSIS_INPUT_MANIFEST_SCHEMA,
            "manifest_schema_version": ANALYSIS_INPUT_MANIFEST_SCHEMA_VERSION,
            "input_contract_hash": prepared.contract_hash,
            "resolver_version": prepared.contract.resolver_version,
            "row_count": prepared.item_count,
        },
        lineage=lineage or [],
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        client_id=client_id,
        filename=f"analysis-input-manifest-{manifest.id}.parquet",
    )
    if (
        reference.artifact_type != ArtifactType.ANALYSIS_INPUT_MANIFEST
        or reference.core_scope_kind != "ANALYSIS_INPUT_MANIFEST"
        or reference.core_scope_id != manifest.id
        or reference.derivation_key != derivation_key
        or reference.content_hash != prepared.content_hash
        or reference.byte_length != prepared.byte_length
    ):
        raise AnalysisInputManifestConflict("stored input manifest artifact identity does not match Core")
    manifest.artifact_id = reference.artifact_id
    manifest.artifact_content_hash = reference.content_hash
    manifest.artifact_byte_length = reference.byte_length
    manifest.manifest_status = "FINALIZED"
    manifest.finalized_at = datetime.now(timezone.utc)
    manifest.failure_summary = None
    record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="analysis_input_manifest.finalized",
        target_type="analysis_input_manifest",
        target_id=manifest.id,
        details={
            "matter_id": str(matter_id),
            "owner_type": owner_type,
            "owner_id": str(owner_id),
            "scope_type": prepared.scope_type,
            "scope_id": str(prepared.scope_id),
            "input_contract_hash": prepared.contract_hash,
            "artifact_id": str(reference.artifact_id),
            "item_count": prepared.item_count,
        },
    )
    db.flush()
    return manifest, reference


def read_analysis_input_manifest_table(
    manifest: AnalysisInputManifest,
    *,
    actor_user_id: uuid.UUID,
) -> pa.Table:
    if manifest.manifest_status != "FINALIZED" or manifest.artifact_id is None:
        raise AnalysisInputManifestError("analysis input manifest is not finalized")
    reference = get_workflow_artifact_reference(
        artifact_id=manifest.artifact_id,
        actor_user_id=actor_user_id,
        tenant_id=manifest.tenant_id,
        client_id=manifest.client_id,
    )
    if (
        reference.artifact_type != ArtifactType.ANALYSIS_INPUT_MANIFEST
        or reference.core_scope_kind != "ANALYSIS_INPUT_MANIFEST"
        or reference.core_scope_id != manifest.id
        or reference.derivation_key != manifest.derivation_key
        or reference.content_hash != manifest.artifact_content_hash
        or reference.byte_length != manifest.artifact_byte_length
    ):
        raise AnalysisInputManifestError("analysis input manifest artifact identity does not match Core")
    destination = io.BytesIO()
    copy_workflow_artifact_content(
        artifact_id=reference.artifact_id,
        destination=destination,
        actor_user_id=actor_user_id,
        tenant_id=manifest.tenant_id,
        client_id=manifest.client_id,
    )
    content = destination.getvalue()
    if hashlib.sha256(content).hexdigest() != manifest.artifact_content_hash:
        raise AnalysisInputManifestError("analysis input manifest content hash does not match Core")
    try:
        table = pq.read_table(io.BytesIO(content))
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise AnalysisInputManifestError("analysis input manifest is not readable Parquet") from exc
    metadata = table.schema.metadata or {}
    expected_metadata = {
        b"pvr.schema": manifest.manifest_schema.encode(),
        b"pvr.schema_version": str(manifest.manifest_schema_version).encode(),
        b"pvr.contract_hash": manifest.input_contract_hash.encode(),
        b"pvr.resolver_version": manifest.resolver_version.encode(),
        b"pvr.scope_type": manifest.scope_type.encode(),
        b"pvr.scope_id": str(manifest.scope_id).encode(),
    }
    if any(metadata.get(key) != value for key, value in expected_metadata.items()):
        raise AnalysisInputManifestError("analysis input manifest Parquet metadata does not match Core")
    if not BASE_COLUMN_NAMES.issubset(table.schema.names):
        raise AnalysisInputManifestError("analysis input manifest is missing required base columns")
    contract = AnalysisInputContract.model_validate(manifest.input_contract)
    extension_names = {item.name for item in contract.extension_fields}
    if not extension_names.issubset(table.schema.names):
        raise AnalysisInputManifestError("analysis input manifest is missing contract extension columns")
    if table.num_rows != manifest.item_count:
        raise AnalysisInputManifestError("analysis input manifest row count does not match Core")
    return table
