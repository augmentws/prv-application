import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

CollectionStatus = Literal["OPEN", "SEALED", "ARCHIVED"]
RecordType = Literal["EMAIL", "FILE", "CHAT", "TRANSCRIPT", "OTHER"]
ProcessingStatus = Literal["NOT_PROCESSED", "METADATA_INCOMPLETE", "READY", "FAILED"]
RecipientType = Literal["TO", "CC", "BCC"]
SourceItemId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TenantStorageRead(ORMModel):
    tenant_id: uuid.UUID
    tenant_slug_snapshot: str
    bucket_name: str
    status: str
    created_at: datetime


class TenantStorageEnsure(BaseModel):
    tenant_slug: Annotated[
        str,
        StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z][a-z0-9-]{1,78}[a-z0-9]$"),
    ]


class CollectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)


class CollectionRead(ORMModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    name: str
    description: str | None
    status: CollectionStatus
    created_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class CollectionCustodianSummary(BaseModel):
    custodian_id: uuid.UUID
    item_count: int = Field(ge=1)


class EmailRecipientInput(BaseModel):
    recipient_type: RecipientType
    display_name: str | None = Field(default=None, max_length=500)
    email_address: str | None = Field(default=None, max_length=500)


class EmailMetadataInput(BaseModel):
    sender: str | None = Field(default=None, max_length=4000)
    subject: str | None = Field(default=None, max_length=10000)
    sent_at: datetime | None = None
    received_at: datetime | None = None
    message_id: str | None = Field(default=None, max_length=1000)
    recipients: list[EmailRecipientInput] = Field(default_factory=list, max_length=10000)


class CollectionItemUploadMetadata(BaseModel):
    source_item_id: SourceItemId
    record_type: RecordType
    original_filename: str = Field(min_length=1, max_length=500)
    original_source_path: str | None = Field(default=None, max_length=10000)
    custodian_ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)
    primary_custodian_id: uuid.UUID | None = None
    parent_collection_item_id: uuid.UUID | None = None
    family_id: uuid.UUID | None = None
    source_created_at: datetime | None = None
    source_modified_at: datetime | None = None
    processing_status: ProcessingStatus = "NOT_PROCESSED"
    email: EmailMetadataInput | None = None
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
    unmapped_metadata: dict[str, Any] = Field(default_factory=dict)
    source_container_artifact_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_email_and_custodians(self) -> "CollectionItemUploadMetadata":
        unique_custodians = list(dict.fromkeys(self.custodian_ids))
        if len(unique_custodians) != len(self.custodian_ids):
            raise ValueError("custodian_ids must not contain duplicates")
        if self.primary_custodian_id is not None and self.primary_custodian_id not in self.custodian_ids:
            raise ValueError("primary_custodian_id must be included in custodian_ids")
        if self.record_type == "EMAIL" and self.email is None:
            raise ValueError("email metadata is required when record_type is EMAIL")
        if self.record_type != "EMAIL" and self.email is not None:
            raise ValueError("email metadata is only valid when record_type is EMAIL")
        return self


class ArtifactRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    artifact_class: str
    artifact_type: str
    role: str
    original_filename: str
    media_type: str
    byte_length: int
    sha256: str
    status: str
    created_at: datetime
    finalized_at: datetime
    collection_id: uuid.UUID | None = None
    collection_item_id: uuid.UUID | None = None
    derivation_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


DerivedArtifactType = Literal["CHUNK_SET", "CHUNK_VECTOR_SET"]
DerivedArtifactRelationship = Literal["CHUNKED_FROM", "EMBEDDED_FROM"]


class DerivedArtifactUploadMetadata(BaseModel):
    artifact_type: DerivedArtifactType
    source_artifact_id: uuid.UUID
    relationship: DerivedArtifactRelationship
    processing_run_id: uuid.UUID
    derivation_key: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_relationship(self) -> "DerivedArtifactUploadMetadata":
        expected = "CHUNKED_FROM" if self.artifact_type == "CHUNK_SET" else "EMBEDDED_FROM"
        if self.relationship != expected:
            raise ValueError(f"{self.artifact_type} requires the {expected} relationship")
        return self


class DerivedArtifactUploadResponse(BaseModel):
    artifact: ArtifactRead
    created: bool


class ArtifactLineageRead(ORMModel):
    artifact_id: uuid.UUID
    source_artifact_id: uuid.UUID
    relationship: str
    created_at: datetime


class SourceContainerUploadResponse(BaseModel):
    artifact: ArtifactRead


class CollectionItemRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    collection_id: uuid.UUID
    source_item_id: str
    record_type: RecordType
    original_filename: str
    original_extension: str | None
    original_source_path: str | None
    source_created_at: datetime | None
    source_modified_at: datetime | None
    family_id: uuid.UUID | None
    parent_collection_item_id: uuid.UUID | None
    processing_status: ProcessingStatus
    custodian_ids: list[uuid.UUID]
    email: EmailMetadataInput | None
    raw_metadata: dict[str, Any]
    unmapped_metadata: dict[str, Any]
    native_artifact: ArtifactRead
    created_at: datetime


class FacetValue(BaseModel):
    value: str
    count: int = Field(ge=1)


class CollectionSearchFacets(BaseModel):
    custodians: list[FacetValue]
    file_extensions: list[FacetValue]
    record_types: list[FacetValue]
    processing_statuses: list[FacetValue]


class CollectionItemSearchResponse(BaseModel):
    items: list[CollectionItemRead]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    facets: CollectionSearchFacets


class CollectionSelectionCreate(BaseModel):
    request_id: uuid.UUID
    mode: Literal["QUERY", "EXPLICIT"] = "QUERY"
    q: str | None = Field(default=None, max_length=500)
    custodian_ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)
    file_extensions: list[str] = Field(default_factory=list, max_length=1000)
    record_types: list[RecordType] = Field(default_factory=list, max_length=20)
    processing_statuses: list[ProcessingStatus] = Field(default_factory=list, max_length=20)
    item_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def validate_selection(self) -> "CollectionSelectionCreate":
        if self.mode == "EXPLICIT" and not self.item_ids:
            raise ValueError("EXPLICIT selections require at least one item ID")
        if self.mode == "QUERY" and self.item_ids:
            raise ValueError("item_ids are only valid for EXPLICIT selections")
        return self


class CollectionSelectionRead(ORMModel):
    id: uuid.UUID
    request_id: uuid.UUID
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    collection_id: uuid.UUID
    selection: dict[str, Any]
    status: Literal["READY", "DISPATCHED"]
    total_count: int
    created_at: datetime


class CollectionSelectionBatchCustodian(BaseModel):
    custodian_id: uuid.UUID
    relationship_type: Literal["PRIMARY", "COMMON"]


class CollectionSelectionBatchItem(BaseModel):
    item_id: uuid.UUID
    custodian_ids: list[uuid.UUID]
    custodians: list[CollectionSelectionBatchCustodian]


class CollectionSelectionBatch(BaseModel):
    selection_id: uuid.UUID
    offset: int
    total_count: int
    item_ids: list[uuid.UUID]
    items: list[CollectionSelectionBatchItem]


class CollectionItemUploadResponse(BaseModel):
    item: CollectionItemRead
    created: bool
