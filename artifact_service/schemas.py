import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

CollectionStatus = Literal["OPEN", "SEALED", "ARCHIVED", "DELETING"]
RecordType = Literal["EMAIL", "FILE", "CHAT", "TRANSCRIPT", "OTHER"]
ProcessingStatus = Literal["NOT_PROCESSED", "METADATA_INCOMPLETE", "READY", "FAILED"]
DateHistogramInterval = Literal["week", "month", "year"]
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
    active_text_processing_run_id: uuid.UUID | None


class CollectionDeletionJobRead(ORMModel):
    id: uuid.UUID
    collection_id: uuid.UUID
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    collection_name: str
    status: Literal[
        "QUEUED",
        "VALIDATING",
        "DELETING_DATABASE_ROWS",
        "DELETING_BLOBS",
        "COMPLETED",
        "FAILED",
    ]
    workflow_id: str
    attempt_count: int
    item_count: int
    artifact_count: int
    blob_count: int
    deleted_item_count: int
    deleted_artifact_count: int
    deleted_blob_count: int
    error_message: str | None
    requested_by_user_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CollectionDeletionFailure(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


TextProcessingRuleAction = Literal["REMOVE_LINE", "REMOVE_BLOCK", "REPLACE"]
TextProcessingRuleId = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
MAX_TEXT_PROCESSING_TEST_ITEMS = 25


class TextProcessingRule(BaseModel):
    id: TextProcessingRuleId
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    action: TextProcessingRuleAction
    pattern: str = Field(min_length=1, max_length=1000)
    end_pattern: str | None = Field(default=None, max_length=1000)
    replacement: str = Field(default="", max_length=2000)
    case_sensitive: bool = False
    enabled: bool = True

    @model_validator(mode="after")
    def validate_action_fields(self) -> "TextProcessingRule":
        if self.action == "REMOVE_BLOCK" and not self.end_pattern:
            raise ValueError("REMOVE_BLOCK requires end_pattern")
        if self.action != "REMOVE_BLOCK" and self.end_pattern is not None:
            raise ValueError("end_pattern is only valid for REMOVE_BLOCK")
        if self.action != "REPLACE" and self.replacement:
            raise ValueError("replacement is only valid for REPLACE")
        return self


class CollectionTextProcessingProfileUpdate(BaseModel):
    rules: list[TextProcessingRule] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_unique_rule_ids(self) -> "CollectionTextProcessingProfileUpdate":
        ids = [rule.id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("Rule IDs must be unique")
        return self


class SystemTextProcessingRule(BaseModel):
    id: str
    name: str
    description: str
    action: str
    match_description: str
    match_pattern: str
    stop_pattern: str


class CollectionTextProcessingProfileRead(BaseModel):
    collection_id: uuid.UUID
    processor_version: str
    revision: int
    default_rules: list[SystemTextProcessingRule]
    rules: list[TextProcessingRule]
    active_run_id: uuid.UUID | None
    updated_at: datetime | None


class CollectionTextProcessingTestRequest(BaseModel):
    item_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_TEXT_PROCESSING_TEST_ITEMS)
    rules: list[TextProcessingRule] = Field(default_factory=list, max_length=50)
    disabled_rule_ids: list[TextProcessingRuleId] = Field(default_factory=list, max_length=54)

    @model_validator(mode="after")
    def validate_unique_rule_ids(self) -> "CollectionTextProcessingTestRequest":
        rule_ids = [rule.id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("Rule IDs must be unique")
        if len(self.disabled_rule_ids) != len(set(self.disabled_rule_ids)):
            raise ValueError("Disabled rule IDs must be unique")
        return self


class TextProcessingChange(BaseModel):
    rule_id: str
    rule_name: str
    match_count: int = Field(ge=1)


class CollectionTextProcessingTestItem(BaseModel):
    item_id: uuid.UUID
    filename: str
    source_role: str | None
    original_text: str | None
    normalized_text: str | None
    original_char_count: int = Field(ge=0)
    normalized_char_count: int = Field(ge=0)
    changes: list[TextProcessingChange]
    warnings: list[str]


class CollectionTextProcessingTestResponse(BaseModel):
    processor_version: str
    items: list[CollectionTextProcessingTestItem]


class CollectionTextProcessingRunCreate(BaseModel):
    enabled_rule_ids: list[TextProcessingRuleId] | None = Field(default=None, max_length=54)

    @model_validator(mode="after")
    def validate_unique_rule_ids(self) -> "CollectionTextProcessingRunCreate":
        if self.enabled_rule_ids is not None and len(self.enabled_rule_ids) != len(set(self.enabled_rule_ids)):
            raise ValueError("Enabled rule IDs must be unique")
        return self


class CollectionTextProcessingRunRead(ORMModel):
    id: uuid.UUID
    collection_id: uuid.UUID
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"]
    processor_version: str
    profile_revision: int
    rules_snapshot: list[TextProcessingRule]
    disabled_rule_ids: list[TextProcessingRuleId]
    configuration_hash: str
    total_count: int
    processed_count: int
    created_count: int
    reused_count: int
    skipped_count: int
    failed_count: int
    error_message: str | None
    requested_by_user_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CollectionCustodianSummary(BaseModel):
    custodian_id: uuid.UUID
    item_count: int = Field(ge=1)


class EmailRecipientInput(BaseModel):
    recipient_type: RecipientType
    display_name: str | None = Field(default=None, max_length=500)
    email_address: str | None = Field(default=None, max_length=500)

    @field_validator("display_name", "email_address", mode="before")
    @classmethod
    def reject_nul_characters(cls, value: Any) -> Any:
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("must not contain NUL characters")
        return value


class EmailMetadataInput(BaseModel):
    sender: str | None = Field(default=None, max_length=4000)
    subject: str | None = Field(default=None, max_length=10000)
    sent_at: datetime | None = None
    received_at: datetime | None = None
    message_id: str | None = Field(default=None, max_length=1000)
    recipients: list[EmailRecipientInput] = Field(default_factory=list, max_length=10000)

    @field_validator("sender", "subject", "message_id", mode="before")
    @classmethod
    def reject_nul_characters(cls, value: Any) -> Any:
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("must not contain NUL characters")
        return value


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
    processing_run_id: uuid.UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


DerivedArtifactType = Literal["CHUNK_SET", "CHUNK_VECTOR_SET", "SUMMARY"]
DerivedArtifactRelationship = Literal["CHUNKED_FROM", "EMBEDDED_FROM", "DERIVED_FROM"]
DERIVED_ARTIFACT_RELATIONSHIPS: dict[str, str] = {
    "CHUNK_SET": "CHUNKED_FROM",
    "CHUNK_VECTOR_SET": "EMBEDDED_FROM",
    "SUMMARY": "DERIVED_FROM",
}


class DerivedArtifactUploadMetadata(BaseModel):
    artifact_type: DerivedArtifactType
    source_artifact_id: uuid.UUID
    relationship: DerivedArtifactRelationship
    processing_run_id: uuid.UUID
    derivation_key: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_relationship(self) -> "DerivedArtifactUploadMetadata":
        expected = DERIVED_ARTIFACT_RELATIONSHIPS[self.artifact_type]
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
    file_date: datetime | None
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


CollectionFacetKey = Literal["custodians", "file_extensions", "record_types", "processing_statuses"]


class CollectionItemSearchResponse(BaseModel):
    items: list[CollectionItemRead]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class DateHistogramBucket(BaseModel):
    start: datetime
    count: int = Field(ge=0)


class CollectionDateHistogramResponse(BaseModel):
    interval: DateHistogramInterval
    buckets: list[DateHistogramBucket]
    missing_count: int = Field(ge=0)


class CollectionSelectionCreate(BaseModel):
    request_id: uuid.UUID
    mode: Literal["QUERY", "EXPLICIT"] = "QUERY"
    q: str | None = Field(default=None, max_length=500)
    custodian_ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)
    file_extensions: list[str] = Field(default_factory=list, max_length=1000)
    record_types: list[RecordType] = Field(default_factory=list, max_length=20)
    processing_statuses: list[ProcessingStatus] = Field(default_factory=list, max_length=20)
    file_date_from: datetime | None = None
    file_date_to: datetime | None = None
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
