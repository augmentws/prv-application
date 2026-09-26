import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

ResourceStatus = Literal["ACTIVE", "SUSPENDED", "ARCHIVED"]
AdminRole = Literal["ADMIN"]
MetadataType = Literal["TEXT", "LONG_TEXT", "INTEGER", "DECIMAL", "BOOLEAN", "DATE", "DATETIME", "ENUM", "JSON"]
MetadataValueSource = Literal["SYSTEM", "IMPORTED", "ASSERTED"]
MetadataReferenceTarget = Literal["CUSTODIAN"]
Cardinality = Literal["SINGLE", "MULTIPLE"]
AssertionPolicy = Literal["IMMEDIATE", "REQUIRES_CONFIRMATION"]
ResolutionPolicy = Literal["EXPLICIT_ONLY", "LATEST_VALID", "HUMAN_PRECEDENCE"]
MetadataOperation = Literal["SET", "ADD", "REMOVE", "CLEAR", "CONFIRM", "REJECT"]
MetadataEventSource = Literal["HUMAN", "AGENT", "EXTRACTOR", "IMPORT", "RULE", "SYSTEM"]
MetadataResolutionState = Literal["VALUE", "EMPTY", "PENDING", "CONFLICTED"]
MetadataEffectiveStatus = Literal["ACTIVE", "SUPERSEDED", "REJECTED", "INVALIDATED"]
MetadataConfirmationState = Literal["UNREVIEWED", "CONFIRMED", "REJECTED"]
AgentScope = Literal["SYSTEM", "TENANT"]
AgentVersionStatus = Literal["DRAFT", "PUBLISHED", "RETIRED"]
AgentInvocationMode = Literal["CHAT", "STRUCTURED"]
SkillScope = Literal["SYSTEM", "TENANT"]
SkillVersionStatus = Literal["DRAFT", "PUBLISHED", "RETIRED"]
WorkflowBindingStatus = Literal["ACTIVE", "INACTIVE"]
MatterDefinitionSourceKind = Literal[
    "PASTE", "MARKDOWN", "TEXT", "DOCX", "AGENT_EDIT", "USER_EDIT", "ASSESSMENT_REFINEMENT"
]
MatterDefinitionUserSourceKind = Literal["PASTE", "MARKDOWN", "TEXT", "USER_EDIT"]
Slug = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z][a-z0-9-]{1,78}[a-z0-9]$")
]
MetadataKey = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z][a-z0-9_]{0,99}$")]
EnumValueKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,99}$"),
]
AgentConversationTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(min_length=12, max_length=1024)


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    access_expires_in: int
    refresh_expires_in: int


class UserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=1024)
    role: AdminRole = "ADMIN"


class UserRead(ORMModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: EmailStr
    display_name: str
    status: ResourceStatus
    tenant_role: AdminRole
    is_superuser: bool
    created_at: datetime


class TenantAdminCreate(UserCreate):
    pass


class TenantCreate(BaseModel):
    parent_tenant_id: uuid.UUID
    slug: Slug
    name: str = Field(min_length=1, max_length=200)
    initial_admin: TenantAdminCreate


class TenantRead(ORMModel):
    id: uuid.UUID
    parent_tenant_id: uuid.UUID | None
    slug: str
    name: str
    status: ResourceStatus
    is_root: bool
    created_at: datetime


class TenantCreated(BaseModel):
    tenant: TenantRead
    initial_admin: UserRead


class ClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ClientRead(ORMModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    status: ResourceStatus
    created_at: datetime


class CustodianCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=300)
    email_addresses: list[EmailStr] = Field(default_factory=list, max_length=100)
    external_reference: str | None = Field(default=None, max_length=1000)


class CustodianRead(ORMModel):
    id: uuid.UUID
    client_id: uuid.UUID
    display_name: str
    email_addresses: list[str]
    external_reference: str | None
    status: ResourceStatus
    created_at: datetime


class MatterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    template_id: uuid.UUID | None = None
    clone_from_matter_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_configuration_source(self) -> "MatterCreate":
        if self.template_id and self.clone_from_matter_id:
            raise ValueError("Choose a matter template or a source matter to clone, not both")
        return self


class MatterRead(ORMModel):
    id: uuid.UUID
    client_id: uuid.UUID
    name: str
    status: ResourceStatus
    created_at: datetime


class ExternalProviderUsageRead(ORMModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    client_id: uuid.UUID | None
    matter_id: uuid.UUID | None
    started_by_user_id: uuid.UUID
    started_by_email: str
    started_by_display_name: str
    job_type: str
    job_id: uuid.UUID
    job_created_at: datetime
    provider: str
    model: str
    request_count: int
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    model_invocation_id: uuid.UUID | None
    total_tokens: int
    details: dict[str, Any]
    created_at: datetime


MatterDocumentSelectionType = Literal["QUERY", "EXPLICIT"]
MatterDocumentImportStatus = Literal["QUEUED", "SNAPSHOTTING", "RUNNING", "COMPLETED", "FAILED", "CANCELED"]
MatterEmbeddingJobStatus = Literal[
    "QUEUED",
    "PLANNING",
    "RUNNING",
    "COMPLETED",
    "COMPLETED_WITH_ERRORS",
    "FAILED",
    "CANCELED",
]
MatterTopicJobStatus = Literal[
    "QUEUED",
    "SAMPLING",
    "CLUSTERING",
    "AWAITING_REVIEW",
    "PUBLISHING",
    "COMPLETED",
    "COMPLETED_WITH_ERRORS",
    "FAILED",
    "CANCELED",
]
MatterTopicOperatingMode = Literal["AUTO", "FIXED"]
MatterTopicDestinationMode = Literal["TOPICS", "EXISTING_FIELD", "NEW_FIELD"]
MatterTopicAssignmentMode = Literal["REPLACE", "APPEND"]
CollectionRecordType = Literal["EMAIL", "FILE", "CHAT", "TRANSCRIPT", "OTHER"]
CollectionProcessingStatus = Literal["NOT_PROCESSED", "METADATA_INCOMPLETE", "READY", "FAILED"]


class MatterDocumentSelection(BaseModel):
    mode: MatterDocumentSelectionType = "QUERY"
    q: str | None = Field(default=None, max_length=500)
    custodian_ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)
    file_extensions: list[str] = Field(default_factory=list, max_length=1000)
    record_types: list[CollectionRecordType] = Field(default_factory=list, max_length=20)
    processing_statuses: list[CollectionProcessingStatus] = Field(default_factory=list, max_length=20)
    file_date_from: datetime | None = None
    file_date_to: datetime | None = None
    item_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def validate_selection(self) -> "MatterDocumentSelection":
        if self.mode == "EXPLICIT" and not self.item_ids:
            raise ValueError("EXPLICIT selections require at least one item ID")
        if self.mode == "QUERY" and self.item_ids:
            raise ValueError("item_ids are only valid for EXPLICIT selections")
        return self


class MatterDocumentImportCreate(BaseModel):
    source_collection_id: uuid.UUID
    selection: MatterDocumentSelection
    selection_summary: str = Field(min_length=1, max_length=1000)


class MatterDocumentImportRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    source_collection_id: uuid.UUID
    selection_type: MatterDocumentSelectionType
    selection: dict
    selection_summary: str
    status: MatterDocumentImportStatus
    workflow_id: str
    artifact_selection_id: uuid.UUID | None
    matched_count: int
    batch_count: int
    processed_count: int
    added_count: int
    duplicate_count: int
    failed_count: int
    error_message: str | None
    created_by_user_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MatterDocumentRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    source_collection_id: uuid.UUID
    collection_item_id: uuid.UUID
    added_by_import_job_id: uuid.UUID
    created_at: datetime


class MatterOverviewCounts(BaseModel):
    document_count: int = Field(ge=0)
    custodian_count: int = Field(ge=0)


class MatterEmbeddingJobRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    status: MatterEmbeddingJobStatus
    workflow_id: str
    configuration_hash: str
    embedding_model: str
    embedding_model_revision: str | None
    embedding_dimensions: int
    embedding_normalized: bool
    total_count: int
    batch_count: int
    processed_count: int
    embedded_count: int
    skipped_count: int
    failed_count: int
    chunk_count: int
    provider_request_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    error_message: str | None
    created_by_user_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MatterEmbeddingBatchRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    batch_number: int
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELED"]
    item_count: int
    processed_count: int
    embedded_count: int
    skipped_count: int
    failed_count: int
    chunk_count: int
    error_message: str | None
    provider_batch_id: str | None
    provider_status: str | None
    provider_request_count: int
    provider_last_polled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MatterTopicJobCreate(BaseModel):
    operating_mode: MatterTopicOperatingMode = "AUTO"
    sample_size: int = Field(default=10_000, ge=10, le=200_000)
    requested_topic_count: int | None = Field(default=None, ge=2, le=200)
    assignment_mode: MatterTopicAssignmentMode = "REPLACE"
    scope_mode: Literal["ENTIRE_MATTER", "INCLUDE_SAVED_SEARCH", "EXCLUDE_SAVED_SEARCH"] = "ENTIRE_MATTER"
    saved_search_id: uuid.UUID | None = None
    destination_mode: MatterTopicDestinationMode = "TOPICS"
    existing_metadata_definition_id: uuid.UUID | None = None
    new_field_key: MetadataKey | None = None
    new_field_name: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_operating_mode(self) -> "MatterTopicJobCreate":
        if self.operating_mode == "FIXED" and self.requested_topic_count is None:
            raise ValueError("FIXED mode requires requested_topic_count")
        if self.operating_mode == "AUTO" and self.requested_topic_count is not None:
            raise ValueError("requested_topic_count is only valid in FIXED mode")
        if self.requested_topic_count is not None and self.requested_topic_count > self.sample_size:
            raise ValueError("requested_topic_count cannot exceed sample_size")
        if self.scope_mode == "ENTIRE_MATTER" and self.saved_search_id is not None:
            raise ValueError("Entire matter scope does not accept a saved search")
        if self.scope_mode != "ENTIRE_MATTER" and self.saved_search_id is None:
            raise ValueError("Saved-search scope requires saved_search_id")
        if self.destination_mode == "TOPICS":
            if any(
                value is not None
                for value in (
                    self.existing_metadata_definition_id,
                    self.new_field_key,
                    self.new_field_name,
                )
            ):
                raise ValueError("The Topics destination does not accept field details")
        elif self.destination_mode == "EXISTING_FIELD":
            if self.existing_metadata_definition_id is None:
                raise ValueError("Existing-field destination requires a metadata definition")
            if self.new_field_key is not None or self.new_field_name is not None:
                raise ValueError("Existing-field destination does not accept new field details")
        elif self.new_field_key is None or self.new_field_name is None:
            raise ValueError("New-field destination requires a field name and key")
        elif self.existing_metadata_definition_id is not None:
            raise ValueError("New-field destination does not accept an existing metadata definition")
        elif not self.new_field_name.strip():
            raise ValueError("New field name cannot be blank")
        else:
            self.new_field_name = self.new_field_name.strip()
        return self


class MatterTopicClusterRead(ORMModel):
    id: uuid.UUID
    ordinal: int
    topic_key: str
    name: str
    description: str | None
    keywords: list[str]
    representative_excerpts: list[str]
    included: bool
    sampled_chunk_count: int
    assigned_chunk_count: int
    assigned_document_count: int


class MatterTopicJobRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    embedding_job_id: uuid.UUID
    metadata_definition_id: uuid.UUID | None
    status: MatterTopicJobStatus
    workflow_id: str
    operating_mode: MatterTopicOperatingMode
    sample_size: int
    requested_topic_count: int | None
    assignment_mode: MatterTopicAssignmentMode
    scope_mode: Literal["ENTIRE_MATTER", "INCLUDE_SAVED_SEARCH", "EXCLUDE_SAVED_SEARCH"]
    saved_search_id: uuid.UUID | None
    saved_search_name: str | None
    destination_mode: MatterTopicDestinationMode | None
    destination_metadata_definition_id: uuid.UUID | None
    destination_field_key: str | None
    destination_field_name: str | None
    configuration_hash: str
    document_count: int
    sampled_chunk_count: int
    processed_document_count: int
    assigned_document_count: int
    topic_count: int
    outlier_document_count: int
    failed_count: int
    error_message: str | None
    created_by_user_id: uuid.UUID
    reviewed_by_user_id: uuid.UUID | None
    started_at: datetime | None
    reviewed_at: datetime | None
    completed_at: datetime | None
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    clusters: list[MatterTopicClusterRead] = Field(default_factory=list)


class MatterTopicProposalReview(BaseModel):
    id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    included: bool = True


class MatterTopicApplyRequest(BaseModel):
    topics: list[MatterTopicProposalReview] = Field(min_length=1, max_length=200)
    destination_mode: MatterTopicDestinationMode = "TOPICS"
    existing_metadata_definition_id: uuid.UUID | None = None
    new_field_key: MetadataKey | None = None
    new_field_name: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_destination(self) -> "MatterTopicApplyRequest":
        if self.destination_mode == "TOPICS":
            if any(
                value is not None
                for value in (
                    self.existing_metadata_definition_id,
                    self.new_field_key,
                    self.new_field_name,
                )
            ):
                raise ValueError("The Topics destination does not accept field details")
        elif self.destination_mode == "EXISTING_FIELD":
            if self.existing_metadata_definition_id is None:
                raise ValueError("Existing-field destination requires a metadata definition")
            if self.new_field_key is not None or self.new_field_name is not None:
                raise ValueError("Existing-field destination does not accept new field details")
        elif self.new_field_key is None or self.new_field_name is None:
            raise ValueError("New-field destination requires a field name and key")
        elif self.existing_metadata_definition_id is not None:
            raise ValueError("New-field destination does not accept an existing metadata definition")
        elif not self.new_field_name.strip():
            raise ValueError("New field name cannot be blank")
        else:
            self.new_field_name = self.new_field_name.strip()
        return self


SearchFilterOperator = Literal["EQ", "IN", "RANGE", "EXISTS", "NOT_EXISTS"]
SearchSortDirection = Literal["ASC", "DESC"]
SearchMode = Literal["KEYWORD", "SEMANTIC", "HYBRID"]
SearchOperationKind = Literal["SCHEMA_SYNC", "REBUILD", "DOCUMENT_UPSERT", "DOCUMENT_DELETE"]
SearchOperationStatus = Literal["QUEUED", "RUNNING", "AWAITING_USER", "COMPLETED", "FAILED"]
SearchIndexStatus = Literal["CREATING", "ACTIVE", "RETIRED", "FAILED"]
DateHistogramInterval = Literal["week", "month", "year"]


class MatterSearchFilter(BaseModel):
    field: MetadataKey
    operator: SearchFilterOperator
    value: Any | None = None
    values: list[Any] | None = Field(default=None, max_length=1000)
    from_value: Any | None = Field(default=None, alias="from")
    to_value: Any | None = Field(default=None, alias="to")
    include_missing: bool = False

    @model_validator(mode="after")
    def validate_operator_values(self) -> "MatterSearchFilter":
        if self.operator == "EQ" and self.value is None:
            raise ValueError("EQ filters require value")
        if self.operator == "IN" and not self.values and not self.include_missing:
            raise ValueError("IN filters require one or more values or include_missing")
        if self.operator == "RANGE" and self.from_value is None and self.to_value is None:
            raise ValueError("RANGE filters require from or to")
        if self.operator in {"EXISTS", "NOT_EXISTS"} and any(
            value is not None for value in (self.value, self.values, self.from_value, self.to_value)
        ):
            raise ValueError(f"{self.operator} filters do not accept values")
        if self.include_missing and self.operator != "IN":
            raise ValueError("include_missing is supported only for IN filters")
        return self


class MatterSearchSort(BaseModel):
    field: MetadataKey | Literal["created_at", "_score"]
    direction: SearchSortDirection = "DESC"


class MatterSearchRequest(BaseModel):
    query: str | None = Field(default=None, max_length=2000)
    search_mode: SearchMode = "KEYWORD"
    minimum_similarity: float | None = Field(default=None, ge=0, le=1)
    candidate_limit: int | None = Field(default=None, ge=1, le=10_000)
    query_fields: list[MetadataKey] = Field(default_factory=list, max_length=100)
    filters: list[MatterSearchFilter] = Field(default_factory=list, max_length=100)
    facets: list[MetadataKey] = Field(default_factory=list, max_length=50)
    sort: list[MatterSearchSort] = Field(default_factory=list, max_length=10)
    offset: int = Field(default=0, ge=0, le=10000)
    size: int = Field(default=100, ge=1, le=500)

    @model_validator(mode="after")
    def validate_search_mode(self) -> "MatterSearchRequest":
        if self.search_mode != "KEYWORD" and not (self.query or "").strip():
            raise ValueError(f"{self.search_mode} search requires a query")
        if self.search_mode != "KEYWORD" and any(item.field != "_score" for item in self.sort):
            raise ValueError(f"{self.search_mode} search only supports relevance sorting")
        if self.minimum_similarity is not None and self.search_mode != "SEMANTIC":
            raise ValueError("minimum_similarity is supported only for SEMANTIC search")
        if self.candidate_limit is not None and self.search_mode == "KEYWORD":
            raise ValueError("candidate_limit is supported only for SEMANTIC and HYBRID search")
        if self.search_mode == "HYBRID" and self.candidate_limit is not None and self.candidate_limit > 100:
            raise ValueError("HYBRID candidate_limit must be less than or equal to 100")
        return self


class MatterFacetValuesRequest(BaseModel):
    search: MatterSearchRequest
    query: str | None = Field(default=None, max_length=200)
    size: int = Field(default=20, ge=1, le=50)


class MatterDateHistogramRequest(BaseModel):
    search: MatterSearchRequest
    interval: DateHistogramInterval = "month"


class MatterDateHistogramBucket(BaseModel):
    start: datetime
    count: int = Field(ge=0)


class MatterDateHistogramResponse(BaseModel):
    field: MetadataKey
    interval: DateHistogramInterval
    buckets: list[MatterDateHistogramBucket]


class MatterSearchPassage(BaseModel):
    chunk_id: str
    ordinal: int = Field(ge=0)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    text: str
    score: float | None = None


class MatterSearchHit(BaseModel):
    document_id: uuid.UUID
    score: float | None
    fields: dict[str, Any]
    highlights: dict[str, list[str]] = Field(default_factory=dict)
    best_passage: MatterSearchPassage | None = None


class MatterSearchFacetValue(BaseModel):
    value: Any
    count: int = Field(ge=0)


class MatterFacetValuesResponse(BaseModel):
    field: MetadataKey
    values: list[MatterSearchFacetValue]
    missing_count: int = Field(default=0, ge=0)


class MatterSearchResponse(BaseModel):
    total: int = Field(ge=0)
    took_ms: int = Field(ge=0)
    timed_out: bool
    hits: list[MatterSearchHit]
    facets: dict[str, list[MatterSearchFacetValue]]


BulkTagJobStatus = Literal[
    "QUEUED",
    "SNAPSHOTTING",
    "RUNNING",
    "COMPLETED",
    "COMPLETED_WITH_ERRORS",
    "FAILED",
]


class MatterBulkTagAssignment(BaseModel):
    metadata_definition_id: uuid.UUID
    value: Any


class MatterBulkTagPreviewRequest(BaseModel):
    search: MatterSearchRequest
    candidate_limit: int = Field(ge=1, le=10_000)

    @model_validator(mode="after")
    def validate_candidate_limit(self) -> "MatterBulkTagPreviewRequest":
        if self.search.search_mode == "HYBRID" and self.candidate_limit > 100:
            raise ValueError("HYBRID candidate_limit must be less than or equal to 100")
        return self


class MatterBulkTagPreviewResponse(BaseModel):
    candidate_count: int = Field(ge=0)
    matched_count: int = Field(ge=0)


class MatterBulkTagCreate(BaseModel):
    search: MatterSearchRequest
    candidate_limit: int | None = Field(default=None, ge=1, le=10_000)
    assignments: list[MatterBulkTagAssignment] | None = Field(default=None, min_length=1, max_length=50)
    metadata_definition_id: uuid.UUID | None = None
    value: Any = None

    @model_validator(mode="after")
    def validate_search_scope(self) -> "MatterBulkTagCreate":
        if not (self.search.query or "").strip() and not self.search.filters:
            raise ValueError("Bulk tagging requires an active search query or filter")
        if self.search.search_mode == "KEYWORD" and self.candidate_limit is not None:
            raise ValueError("candidate_limit is supported only for semantic and hybrid bulk tagging")
        if self.search.search_mode != "KEYWORD" and self.candidate_limit is None:
            raise ValueError("Semantic and hybrid bulk tagging requires a candidate_limit")
        if self.search.search_mode == "HYBRID" and self.candidate_limit is not None and self.candidate_limit > 100:
            raise ValueError("HYBRID candidate_limit must be less than or equal to 100")
        if self.assignments is None and self.metadata_definition_id is None:
            raise ValueError("Bulk tagging requires at least one field assignment")
        if self.assignments is not None and self.metadata_definition_id is not None:
            raise ValueError("Use assignments or the legacy single-field form, not both")
        return self

    def normalized_assignments(self) -> list[MatterBulkTagAssignment]:
        if self.assignments is not None:
            return self.assignments
        return [MatterBulkTagAssignment(metadata_definition_id=self.metadata_definition_id, value=self.value)]


class MatterBulkTagJobRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    metadata_definition_id: uuid.UUID
    search_index_generation_id: uuid.UUID | None
    search_index_snapshot: dict[str, Any]
    search_definition: dict[str, Any]
    value: Any
    assignments: list[MatterBulkTagAssignment]
    status: BulkTagJobStatus
    matched_count: int = Field(ge=0)
    batch_count: int = Field(ge=0)
    processed_count: int = Field(ge=0)
    tagged_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    error_message: str | None
    created_by_user_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


SavedSearchVisibility = Literal["PRIVATE", "PUBLIC", "SHARED"]


class MatterSavedSearchUserRead(BaseModel):
    id: uuid.UUID
    display_name: str
    email: EmailStr


class MatterSavedSearchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    visibility: SavedSearchVisibility = "PRIVATE"
    search: MatterSearchRequest
    shared_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def validate_sharing(self) -> "MatterSavedSearchCreate":
        if self.visibility == "SHARED" and not self.shared_user_ids:
            raise ValueError("SHARED saved searches require at least one user")
        if self.visibility != "SHARED" and self.shared_user_ids:
            raise ValueError("Only SHARED saved searches accept shared users")
        return self


class MatterSavedSearchUpdate(MatterSavedSearchCreate):
    pass


class MatterSavedSearchRead(BaseModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    name: str
    description: str | None
    visibility: SavedSearchVisibility
    search: MatterSearchRequest
    owner: MatterSavedSearchUserRead
    shared_users: list[MatterSavedSearchUserRead]
    is_owner: bool
    created_at: datetime
    updated_at: datetime


class MatterSavedSearchExecute(BaseModel):
    offset: int = Field(default=0, ge=0, le=10000)
    size: int | None = Field(default=None, ge=1, le=500)


ReviewBatchSelectionType = Literal[
    "ALL_MATTER", "SEARCH_QUERY", "RANDOM_MATTER", "RANDOM_BATCH", "DEFINITION_ASSESSMENT"
]
InteractiveReviewBatchSelectionType = Literal["ALL_MATTER", "SEARCH_QUERY", "RANDOM_MATTER", "RANDOM_BATCH"]
ReviewBatchValueVisibility = Literal["OWN_VALUES", "ALL_REVIEWER_VALUES"]
ReviewBatchStatus = Literal["QUEUED", "BUILDING", "READY", "FAILED", "ARCHIVED"]
ReviewBatchSearchStatus = Literal["QUEUED", "SYNCING", "READY", "FAILED", "NOT_CONFIGURED"]
ReviewBatchRunType = Literal["HUMAN", "AGENT", "WORKFLOW"]
InteractiveReviewBatchRunType = Literal["HUMAN", "AGENT"]
ReviewBatchRunPurpose = Literal["REVIEW", "REFERENCE", "CANDIDATE", "ASSESSMENT"]
InteractiveReviewBatchRunPurpose = Literal["REVIEW", "REFERENCE", "CANDIDATE"]
ReviewBatchRunStatus = Literal[
    "QUEUED",
    "RUNNING",
    "COMPLETED",
    "COMPLETED_WITH_ERRORS",
    "FAILED",
    "CANCELED",
]


class ReviewBatchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    selection_type: InteractiveReviewBatchSelectionType
    search: MatterSearchRequest | None = None
    source_batch_id: uuid.UUID | None = None
    sample_size: int | None = Field(default=None, ge=1, le=10_000_000)
    random_seed: str | None = Field(default=None, min_length=1, max_length=100)
    assigned_user_id: uuid.UUID | None = None
    reviewer_value_visibility: ReviewBatchValueVisibility = "OWN_VALUES"
    coding_group_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    note: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_selection(self) -> "ReviewBatchCreate":
        if self.selection_type == "SEARCH_QUERY":
            if self.search is None:
                raise ValueError("SEARCH_QUERY batches require a search")
            if self.search.search_mode != "KEYWORD":
                raise ValueError("Batch creation currently supports keyword searches only")
        elif self.search is not None:
            raise ValueError("search is only valid for SEARCH_QUERY batches")
        if self.selection_type == "RANDOM_BATCH":
            if self.source_batch_id is None:
                raise ValueError("RANDOM_BATCH batches require source_batch_id")
        elif self.source_batch_id is not None:
            raise ValueError("source_batch_id is only valid for RANDOM_BATCH batches")
        if self.selection_type in {"ALL_MATTER", "SEARCH_QUERY"} and self.sample_size is not None:
            raise ValueError("sample_size is only valid for random batches")
        return self


class ReviewBatchAssignmentUpdate(BaseModel):
    assigned_user_id: uuid.UUID | None


class ReviewBatchCodingGroupsAdd(BaseModel):
    coding_group_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class ReviewBatchCodingGroupsUpdate(BaseModel):
    coding_group_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class ReviewBatchCodingFieldRead(BaseModel):
    id: uuid.UUID
    metadata_definition_id: uuid.UUID
    sort_order: int
    definition_snapshot: dict[str, Any]


class ReviewBatchCodingGroupRead(BaseModel):
    id: uuid.UUID
    source_metadata_group_id: uuid.UUID | None
    display_name: str
    description: str | None
    sort_order: int
    fields: list[ReviewBatchCodingFieldRead]


class ReviewBatchRead(BaseModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    name: str
    description: str | None
    selection_type: ReviewBatchSelectionType
    selection_definition: dict[str, Any]
    source_batch_id: uuid.UUID | None
    search_index_generation_id: uuid.UUID | None
    sample_size: int | None
    random_seed: str | None
    assigned_user_id: uuid.UUID | None
    assigned_user: MatterSavedSearchUserRead | None
    reviewer_value_visibility: ReviewBatchValueVisibility
    status: ReviewBatchStatus
    search_status: ReviewBatchSearchStatus
    search_error_message: str | None
    workflow_id: str
    document_count: int
    error_message: str | None
    created_by_user_id: uuid.UUID
    coding_groups: list[ReviewBatchCodingGroupRead]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class ReviewBatchDocumentRead(BaseModel):
    matter_document_id: uuid.UUID
    source_collection_id: uuid.UUID
    collection_item_id: uuid.UUID
    sequence_number: int
    review_status: Literal["NOT_STARTED", "IN_PROGRESS", "COMPLETED", "SKIPPED"]


class ReviewBatchNoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class ReviewBatchNoteRead(ORMModel):
    id: uuid.UUID
    review_batch_id: uuid.UUID
    body: str
    author_type: Literal["USER", "AGENT"]
    author_user_id: uuid.UUID | None
    author_run_id: uuid.UUID | None
    created_at: datetime


class ReviewBatchRunCreate(BaseModel):
    run_type: InteractiveReviewBatchRunType
    purpose: InteractiveReviewBatchRunPurpose = "REVIEW"
    actor_user_id: uuid.UUID | None = None
    agent_definition_version_id: uuid.UUID | None = None
    parent_run_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_actor(self) -> "ReviewBatchRunCreate":
        if self.run_type == "HUMAN" and self.agent_definition_version_id is not None:
            raise ValueError("Human runs cannot specify an agent version")
        if self.run_type == "AGENT" and self.agent_definition_version_id is None:
            raise ValueError("Agent runs require an agent definition version")
        if self.run_type == "AGENT" and self.actor_user_id is not None:
            raise ValueError("Agent runs cannot specify a human actor")
        return self


class ReviewBatchRunRead(ORMModel):
    id: uuid.UUID
    review_batch_id: uuid.UUID
    run_type: ReviewBatchRunType
    purpose: ReviewBatchRunPurpose
    status: ReviewBatchRunStatus
    result_policy: Literal["ISOLATED", "PUBLISH_TO_MATTER"]
    parent_run_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    agent_definition_version_id: uuid.UUID | None
    workflow_run_record_id: uuid.UUID | None
    configuration_snapshot: dict[str, Any]
    initiated_by_user_id: uuid.UUID
    processed_document_count: int
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ReviewBatchDocumentAnalysisRead(BaseModel):
    review_batch_run_id: uuid.UUID
    matter_document_id: uuid.UUID
    status: Literal["QUEUED", "IN_PROGRESS", "COMPLETED", "SKIPPED", "FAILED"]
    output_artifact_id: uuid.UUID | None
    analysis: dict[str, Any] | None


class ReviewBatchRunFieldValue(BaseModel):
    metadata_definition_id: uuid.UUID
    values: list[Any] = Field(max_length=1000)
    confidence: float | None = Field(default=None, ge=0, le=1)


class ReviewBatchRunDocumentValues(BaseModel):
    matter_document_id: uuid.UUID
    fields: list[ReviewBatchRunFieldValue] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_unique_fields(self) -> "ReviewBatchRunDocumentValues":
        field_ids = [field.metadata_definition_id for field in self.fields]
        if len(field_ids) != len(set(field_ids)):
            raise ValueError("Each coding field may appear only once per document save")
        return self


class ReviewBatchRunValueRead(BaseModel):
    review_batch_run_id: uuid.UUID
    matter_document_id: uuid.UUID
    metadata_definition_id: uuid.UUID
    value_ordinal: int
    value: Any
    confidence: float | None


class ReviewBatchReviewerValueRead(BaseModel):
    review_batch_run_id: uuid.UUID
    actor_user_id: uuid.UUID
    actor_user: MatterSavedSearchUserRead
    metadata_definition_id: uuid.UUID
    values: list[Any]


class ReviewBatchDocumentCodingRead(BaseModel):
    matter_document_id: uuid.UUID
    review_status: Literal["NOT_STARTED", "IN_PROGRESS", "COMPLETED", "SKIPPED"]
    values: list[ReviewBatchRunValueRead]
    reviewer_values: list[ReviewBatchReviewerValueRead]


class ReviewBatchRunProgressRead(BaseModel):
    review_batch_run_id: uuid.UUID
    document_count: int
    not_started_count: int
    in_progress_count: int
    completed_count: int
    skipped_count: int


class ReviewBatchComparisonFieldRead(BaseModel):
    metadata_definition_id: uuid.UUID
    display_name: str
    compared_count: int
    match_count: int
    mismatch_count: int
    missing_left_count: int
    missing_right_count: int
    agreement: float | None


class ReviewBatchComparisonRead(BaseModel):
    left_run_id: uuid.UUID
    right_run_id: uuid.UUID
    document_count: int
    fields: list[ReviewBatchComparisonFieldRead]


class SearchIndexGenerationRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    generation: int
    index_name: str
    alias_name: str
    schema_hash: str
    status: SearchIndexStatus
    document_count: int
    physical_index_exists: bool | None = None
    alias_points_to_index: bool | None = None
    live_document_count: int | None = None
    verification_error: str | None = None
    error_message: str | None
    activated_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SearchProjectionOperationRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    kind: SearchOperationKind
    payload: dict[str, Any]
    status: SearchOperationStatus
    workflow_id: str
    created_by_user_id: uuid.UUID | None
    attempt_count: int
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SearchProjectionRetryResponse(BaseModel):
    requeued_operation_count: int = Field(ge=0)
    requeued_document_count: int = Field(ge=0)


class EnumValue(BaseModel):
    key: EnumValueKey
    label: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    active: bool = True


class MetadataDefinitionCreate(BaseModel):
    key: MetadataKey
    display_name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    type: MetadataType
    cardinality: Cardinality = "SINGLE"
    allowed_values: list[EnumValue] | None = None
    assertion_policy: AssertionPolicy = "IMMEDIATE"
    resolution_policy: ResolutionPolicy = "EXPLICIT_ONLY"
    searchable: bool = True
    facetable: bool = False
    normalize_to_lowercase: bool = False
    reviewable: bool = True
    ai_assignable: bool = False

    @model_validator(mode="after")
    def validate_allowed_values(self) -> "MetadataDefinitionCreate":
        if self.type == "ENUM":
            if not self.allowed_values:
                raise ValueError("ENUM definitions require at least one allowed value")
            keys = [value.key for value in self.allowed_values]
            if len(keys) != len(set(keys)):
                raise ValueError("ENUM allowed-value keys must be unique")
        elif self.allowed_values is not None:
            raise ValueError("allowed_values is only valid for ENUM definitions")
        if self.facetable and self.type in {"LONG_TEXT", "JSON"}:
            raise ValueError("LONG_TEXT and JSON fields cannot be facetable in phase one")
        if self.normalize_to_lowercase and self.type not in {"TEXT", "LONG_TEXT"}:
            raise ValueError("Lowercase normalization is only valid for text fields")
        return self


class MetadataDefinitionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    assertion_policy: AssertionPolicy | None = None
    resolution_policy: ResolutionPolicy | None = None
    searchable: bool | None = None
    facetable: bool | None = None
    normalize_to_lowercase: bool | None = None
    reviewable: bool | None = None
    ai_assignable: bool | None = None
    status: ResourceStatus | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> "MetadataDefinitionUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one metadata definition change is required")
        return self


class MetadataEnumValueCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: EnumValueKey
    label: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class MetadataEnumValueUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_changes(self) -> "MetadataEnumValueUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one enum value change is required")
        return self


class MetadataDefinitionRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    key: str
    display_name: str
    description: str | None
    type: MetadataType
    cardinality: Cardinality
    allowed_values: list[EnumValue] | None
    value_source: MetadataValueSource
    reference_target: MetadataReferenceTarget | None
    template_key: str | None
    template_version: int | None
    assertion_policy: AssertionPolicy
    resolution_policy: ResolutionPolicy
    searchable: bool
    facetable: bool
    normalize_to_lowercase: bool = False
    reviewable: bool
    ai_assignable: bool
    status: ResourceStatus
    created_at: datetime


class MetadataEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: MetadataOperation
    value: Any | None = None
    target_event_id: uuid.UUID | None = None
    supersedes_id: uuid.UUID | None = None
    source_id: str | None = Field(default=None, max_length=500)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_event_shape(self) -> "MetadataEventCreate":
        if self.operation in {"SET", "ADD"}:
            if self.value is None:
                raise ValueError(f"{self.operation} requires a value")
        elif self.value is not None:
            raise ValueError(f"{self.operation} does not accept a value")
        if self.operation in {"REMOVE", "CONFIRM", "REJECT"}:
            if self.target_event_id is None:
                raise ValueError(f"{self.operation} requires target_event_id")
        elif self.target_event_id is not None:
            raise ValueError(f"{self.operation} does not accept target_event_id")
        if self.supersedes_id is not None and self.operation != "SET":
            raise ValueError("supersedes_id is only valid for SET")
        return self


class MetadataEventRead(BaseModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    matter_document_id: uuid.UUID
    metadata_definition_id: uuid.UUID
    operation: MetadataOperation
    value: Any | None
    source_type: MetadataEventSource
    source_id: str | None
    actor_id: uuid.UUID | None
    agent_run_id: uuid.UUID | None
    confidence: float | None
    target_event_id: uuid.UUID | None
    supersedes_id: uuid.UUID | None
    effective_status: MetadataEffectiveStatus
    confirmation_state: MetadataConfirmationState
    created_at: datetime


class DocumentMetadataValueRead(BaseModel):
    value: Any | None
    source_event_id: uuid.UUID
    supporting_event_ids: list[uuid.UUID]


class DocumentMetadataFieldRead(BaseModel):
    matter_document_id: uuid.UUID
    metadata_definition_id: uuid.UUID
    key: str
    display_name: str
    type: MetadataType
    cardinality: Cardinality
    resolution_state: MetadataResolutionState
    values: list[DocumentMetadataValueRead]
    pending_event_ids: list[uuid.UUID]
    conflicting_event_ids: list[uuid.UUID]
    updated_at: datetime | None


class MetadataMutationRead(BaseModel):
    event: MetadataEventRead
    current: DocumentMetadataFieldRead
    search_operation_id: uuid.UUID | None = None


MetadataGroupScope = Literal["SYSTEM", "MATTER", "PERSONAL"]
MetadataSurface = Literal["TABLE", "DOCUMENT"]
MatterTemplateScope = Literal["TENANT", "CLIENT"]


class MetadataGroupCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    scope: Literal["MATTER", "PERSONAL"] = "PERSONAL"
    definition_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)
    default_table_visible: bool = True
    default_document_visible: bool = True

    @model_validator(mode="after")
    def validate_definition_ids(self) -> "MetadataGroupCreate":
        if len(self.definition_ids) != len(set(self.definition_ids)):
            raise ValueError("Metadata group field definitions must be unique")
        return self


class MetadataGroupRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    scope: MetadataGroupScope
    owner_user_id: uuid.UUID | None
    created_by_user_id: uuid.UUID
    key: str
    display_name: str
    description: str | None
    sort_order: int
    default_table_visible: bool
    default_document_visible: bool
    table_visible: bool
    document_visible: bool
    definition_ids: list[uuid.UUID]
    status: ResourceStatus
    template_key: str | None
    template_version: int | None
    created_at: datetime


class MetadataGroupVisibilityUpdate(BaseModel):
    surface: MetadataSurface
    visible: bool


class MatterTemplateCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    scope: MatterTemplateScope = "TENANT"


class MatterTemplateRead(ORMModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    client_id: uuid.UUID | None
    scope: MatterTemplateScope
    name: str
    description: str | None
    current_version: int
    source_matter_id: uuid.UUID | None
    definition_count: int
    group_count: int
    status: ResourceStatus
    created_by_user_id: uuid.UUID
    created_at: datetime


class AgentToolRead(BaseModel):
    key: str
    name: str
    description: str
    requires_approval: bool
    runtime_available: bool


class AgentModelRead(BaseModel):
    key: str
    name: str
    description: str
    configured_model: str | None
    available: bool


class AgentToolAssignment(BaseModel):
    key: str = Field(min_length=1, max_length=150)
    configuration: dict[str, Any] = Field(default_factory=dict)


class AgentVersionCreate(BaseModel):
    system_prompt: str = Field(min_length=1, max_length=100_000)
    model_key: str = Field(min_length=1, max_length=200)
    model_policy: dict[str, Any] = Field(default_factory=dict)
    invocation_mode: AgentInvocationMode = "CHAT"
    usage_instructions: str | None = Field(default=None, max_length=10_000)
    scope_types: list[str] = Field(default_factory=list, max_length=20)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    tools: list[AgentToolAssignment] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_unique_tools(self) -> "AgentVersionCreate":
        keys = [tool.key for tool in self.tools]
        if len(keys) != len(set(keys)):
            raise ValueError("Agent tool assignments must be unique")
        return self


class AgentDefinitionCreate(BaseModel):
    key: MetadataKey
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    initial_version: AgentVersionCreate


class AgentDefinitionRead(ORMModel):
    id: uuid.UUID
    owner_tenant_id: uuid.UUID
    scope: AgentScope
    key: str
    name: str
    description: str | None
    current_version: int
    published_version: int | None
    status: ResourceStatus
    created_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class AgentDefinitionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    status: ResourceStatus | None = None

    @model_validator(mode="after")
    def validate_nonempty_update(self) -> "AgentDefinitionUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one agent property must be supplied")
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("Agent name cannot be blank")
        return self


class AgentDefinitionVersionRead(ORMModel):
    id: uuid.UUID
    agent_definition_id: uuid.UUID
    version: int
    system_prompt: str
    model_key: str
    model_policy: dict[str, Any]
    invocation_mode: AgentInvocationMode
    usage_instructions: str | None
    scope_types: list[str]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    limits: dict[str, Any]
    status: AgentVersionStatus
    created_by_user_id: uuid.UUID
    created_at: datetime
    published_at: datetime | None
    tools: list[AgentToolAssignment]


class AgentDefinitionCreated(BaseModel):
    agent: AgentDefinitionRead
    version: AgentDefinitionVersionRead


class AgentPackageVersionRead(BaseModel):
    id: uuid.UUID
    version: int
    invocation_mode: AgentInvocationMode
    usage_instructions: str | None
    scope_types: list[str]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class AgentPackageRead(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    description: str | None
    scope: AgentScope
    version: AgentPackageVersionRead


class AgentInvocationScope(BaseModel):
    type: str = Field(min_length=1, max_length=50)
    id: uuid.UUID


class AgentInvokeRequest(BaseModel):
    scope: AgentInvocationScope
    input: dict[str, Any]


class AgentInvokeResponse(BaseModel):
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    version: int
    output: dict[str, Any]


class DocumentCleanerChange(BaseModel):
    rule_id: str = Field(min_length=1, max_length=150)
    rule_name: str = Field(min_length=1, max_length=200)
    match_count: int = Field(ge=1)


class DocumentCleanerDocument(BaseModel):
    item_id: uuid.UUID
    filename: str = Field(min_length=1, max_length=500)
    original_text: str = Field(max_length=20_000)
    normalized_text: str = Field(max_length=20_000)
    changes: list[DocumentCleanerChange] = Field(default_factory=list, max_length=100)


class DocumentCleanerClarification(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=4000)


class DocumentCleanerCurrentRule(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    action: Literal["REMOVE_LINE", "REMOVE_BLOCK", "REPLACE"]
    pattern: str = Field(min_length=1, max_length=1000)
    end_pattern: str | None = Field(default=None, max_length=1000)
    replacement: str = Field(default="", max_length=2000)
    case_sensitive: bool = False
    enabled: bool = True


class DocumentCleanerProposalRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)
    documents: list[DocumentCleanerDocument] = Field(min_length=1, max_length=25)
    current_rules: list[DocumentCleanerCurrentRule] = Field(default_factory=list, max_length=50)
    clarification_history: list[DocumentCleanerClarification] = Field(default_factory=list, max_length=5)


class DocumentCleanerProposedRule(DocumentCleanerCurrentRule):
    enabled: bool = True


class DocumentCleanerProposalResponse(BaseModel):
    status: Literal["CLARIFICATION", "PROPOSAL"]
    clarifying_question: str | None = None
    explanation: str = Field(min_length=1, max_length=4000)
    rule: DocumentCleanerProposedRule | None = None
    replace_rule_id: str | None = None


class SkillVersionCreate(BaseModel):
    instructions: str = Field(min_length=1, max_length=200_000)
    input_schema_key: MetadataKey
    input_schema: dict[str, Any]
    output_schema_key: MetadataKey
    output_schema: dict[str, Any]
    model_key: str = Field(min_length=1, max_length=200)
    model_policy: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: list[str] = Field(default_factory=list, max_length=50)
    required_tools: list[str] = Field(default_factory=list, max_length=100)
    cache_policy: dict[str, Any] = Field(default_factory=dict)
    evaluation_fixtures: list[dict[str, Any]] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_contract(self) -> "SkillVersionCreate":
        if not self.input_schema:
            raise ValueError("input_schema cannot be empty")
        if not self.output_schema:
            raise ValueError("output_schema cannot be empty")
        if len(self.required_capabilities) != len(set(self.required_capabilities)):
            raise ValueError("required_capabilities must be unique")
        if len(self.required_tools) != len(set(self.required_tools)):
            raise ValueError("required_tools must be unique")
        return self


class SkillDefinitionCreate(BaseModel):
    key: MetadataKey
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    initial_version: SkillVersionCreate


class SkillDefinitionRead(ORMModel):
    id: uuid.UUID
    owner_tenant_id: uuid.UUID
    scope: SkillScope
    key: str
    name: str
    description: str | None
    current_version: int
    published_version: int | None
    status: ResourceStatus
    created_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class SkillDefinitionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    status: ResourceStatus | None = None

    @model_validator(mode="after")
    def validate_nonempty_update(self) -> "SkillDefinitionUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one skill property must be supplied")
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("Skill name cannot be blank")
        return self


class SkillDefinitionVersionRead(ORMModel):
    id: uuid.UUID
    skill_definition_id: uuid.UUID
    version: int
    instructions: str
    input_schema_key: str
    input_schema: dict[str, Any]
    output_schema_key: str
    output_schema: dict[str, Any]
    model_key: str
    model_policy: dict[str, Any]
    limits: dict[str, Any]
    required_capabilities: list[str]
    required_tools: list[str]
    cache_policy: dict[str, Any]
    evaluation_fixtures: list[dict[str, Any]]
    status: SkillVersionStatus
    created_by_user_id: uuid.UUID
    created_at: datetime
    published_at: datetime | None


class SkillDefinitionCreated(BaseModel):
    skill: SkillDefinitionRead
    version: SkillDefinitionVersionRead


class WorkflowRoleSpecRead(BaseModel):
    key: str
    input_schema_key: str
    output_schema_key: str
    allowed_capabilities: list[str]
    allowed_tool_keys: list[str]


class WorkflowSpecRead(BaseModel):
    key: str
    code_version: str
    name: str
    description: str
    roles: list[WorkflowRoleSpecRead]


class WorkflowSkillBindingUpsert(BaseModel):
    skill_definition_version_id: uuid.UUID
    configuration: dict[str, Any] = Field(default_factory=dict)
    status: WorkflowBindingStatus = "ACTIVE"


class WorkflowSkillBindingRead(ORMModel):
    id: uuid.UUID
    workflow_key: str
    role_key: str
    scope: SkillScope
    owner_tenant_id: uuid.UUID
    skill_definition_id: uuid.UUID
    skill_definition_version_id: uuid.UUID
    configuration: dict[str, Any]
    status: WorkflowBindingStatus
    created_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class MatterDefinitionRevisionCreate(BaseModel):
    content_markdown: str = Field(min_length=1, max_length=2_000_000)
    source_kind: MatterDefinitionUserSourceKind = "USER_EDIT"
    source_filename: str | None = Field(default=None, max_length=500)
    based_on_revision: int | None = Field(default=None, ge=1)


class MatterDefinitionRevisionRead(ORMModel):
    id: uuid.UUID
    matter_definition_id: uuid.UUID
    revision: int
    content_markdown: str
    source_kind: MatterDefinitionSourceKind
    source_artifact_id: uuid.UUID | None
    source_filename: str | None
    based_on_revision: int | None
    created_by_user_id: uuid.UUID
    agent_run_id: uuid.UUID | None
    source_skill_run_id: uuid.UUID | None
    created_at: datetime


class MatterDefinitionRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    current_revision: int
    published_revision: int | None
    created_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    revision: MatterDefinitionRevisionRead


AgentConversationWorkflow = Literal["MATTER_DEFINITION_SETUP", "BATCH_CHAT"]
AgentConversationStatus = Literal["ACTIVE", "WAITING_APPROVAL", "FAILED", "ARCHIVED"]
AgentExecutionStatus = Literal["QUEUED", "RUNNING", "WAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELED"]
AgentActionStatus = Literal["PENDING", "APPROVED", "REJECTED", "EXECUTED", "FAILED"]
AgentActionDecisionValue = Literal["APPROVE", "REJECT"]


class AgentConversationCreate(BaseModel):
    agent_definition_id: uuid.UUID
    title: AgentConversationTitle | None = None
    workflow_type: AgentConversationWorkflow = "MATTER_DEFINITION_SETUP"
    review_batch_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_workflow_scope(self) -> "AgentConversationCreate":
        if self.workflow_type == "BATCH_CHAT" and self.review_batch_id is None:
            raise ValueError("BATCH_CHAT conversations require review_batch_id")
        if self.workflow_type != "BATCH_CHAT" and self.review_batch_id is not None:
            raise ValueError("review_batch_id is supported only for BATCH_CHAT conversations")
        return self


class AgentConversationUpdate(BaseModel):
    title: AgentConversationTitle


class AgentConversationRead(ORMModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    matter_id: uuid.UUID
    review_batch_id: uuid.UUID | None
    agent_definition_id: uuid.UUID
    agent_definition_version_id: uuid.UUID
    title: str | None
    workflow_type: AgentConversationWorkflow
    status: AgentConversationStatus
    initiated_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    @field_validator("status", mode="before")
    @classmethod
    def normalize_legacy_completed_status(cls, value: str) -> str:
        # Rolling deployments may read a row before migration 0031 has normalized it.
        return "ACTIVE" if value == "COMPLETED" else value


class AgentTurnCreate(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)


class AgentMessageRead(ORMModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    turn_id: uuid.UUID | None
    sequence: int
    role: Literal["USER", "ASSISTANT", "SYSTEM", "TOOL"]
    content: str
    message_data: dict[str, Any]
    created_by_user_id: uuid.UUID | None
    agent_run_id: uuid.UUID | None
    created_at: datetime


class AgentRunRead(ORMModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    turn_id: uuid.UUID
    sequence: int
    workflow_id: str
    parent_run_id: uuid.UUID | None
    deferred_from_run_id: uuid.UUID | None
    agent_definition_version_id: uuid.UUID
    actor_user_id: uuid.UUID
    model_key: str
    status: AgentExecutionStatus
    output_text: str | None
    request_count: int
    tool_call_count: int
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class AgentTurnRead(ORMModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    sequence: int
    status: AgentExecutionStatus
    created_by_user_id: uuid.UUID
    completed_at: datetime | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class AgentTurnCreated(BaseModel):
    turn: AgentTurnRead
    message: AgentMessageRead
    run: AgentRunRead


class AgentActionRequestRead(ORMModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    turn_id: uuid.UUID
    agent_run_id: uuid.UUID
    tool_call_id: str
    tool_key: str
    arguments: dict[str, Any]
    summary: str
    status: AgentActionStatus
    requested_at: datetime
    executed_at: datetime | None


class AgentActionDecisionCreate(BaseModel):
    decision: AgentActionDecisionValue
    reason: str | None = Field(default=None, max_length=2000)


class AgentActionDecisionRead(ORMModel):
    id: uuid.UUID
    action_request_id: uuid.UUID
    decision: AgentActionDecisionValue
    reason: str | None
    decided_by_user_id: uuid.UUID
    created_at: datetime


class AgentActionDecisionResult(BaseModel):
    action_request: AgentActionRequestRead
    decision: AgentActionDecisionRead
    resumed_run: AgentRunRead | None


MatterDefinitionAssessmentStatus = Literal[
    "QUEUED",
    "PLANNING",
    "RETRIEVING",
    "BUILDING_BATCH",
    "SUMMARIZING",
    "SYNTHESIZING",
    "COMPLETED",
    "COMPLETED_WITH_ERRORS",
    "FAILED",
    "CANCELED",
]


class MatterDefinitionAssessmentCreate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    revision: int | None = Field(default=None, ge=1)
    maximum_document_count: int = Field(default=500, ge=1, le=10_000_000)
    control_sample_size: int = Field(default=0, ge=0, le=1_000_000)
    use_batching: bool = True
    acknowledge_large_run_warning: bool = False


class MatterDefinitionAssessmentUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        name = " ".join(value.split())
        if not name:
            raise ValueError("Assessment name must not be blank")
        return name


class MatterDefinitionAssessmentRead(ORMModel):
    id: uuid.UUID
    name: str
    matter_id: uuid.UUID
    matter_definition_revision_id: uuid.UUID
    definition_content_hash: str
    workflow_run_id: uuid.UUID
    search_index_generation_id: uuid.UUID | None
    review_batch_id: uuid.UUID | None
    review_batch_run_id: uuid.UUID | None
    configuration_snapshot: dict[str, Any]
    requested_document_count: int
    control_sample_size: int
    use_batching: bool
    large_run_warning_acknowledged: bool
    warning_acknowledged_by_user_id: uuid.UUID | None
    warning_acknowledged_at: datetime | None
    estimated_input_tokens: int | None
    estimated_output_tokens: int | None
    token_estimator: str | None
    token_estimator_version: str | None
    estimation_model: str | None
    candidate_count: int
    selected_count: int
    summarized_count: int
    skipped_count: int
    failed_count: int
    partial_coverage_count: int
    invalid_result_count: int
    coverage_snapshot: dict[str, Any] | None
    synthesis_result: dict[str, Any] | None
    guidance_refinement_status: Literal[
        "NOT_READY", "QUEUED", "RUNNING", "COMPLETED", "NOT_REQUIRED", "FAILED"
    ]
    guidance_refinement_workflow_run_id: uuid.UUID | None
    refined_matter_definition_revision_id: uuid.UUID | None
    guidance_refinement_error_message: str | None
    status: MatterDefinitionAssessmentStatus
    error_message: str | None
    initiated_by_user_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MatterDefinitionAssessmentQueryRead(ORMModel):
    id: uuid.UUID
    assessment_run_id: uuid.UUID
    ordinal: int
    criterion_key: str
    criterion_label: str
    rationale: str
    search_request: dict[str, Any]
    quota: int
    result_count: int
    created_at: datetime


class MatterDefinitionAssessmentQuestionRead(ORMModel):
    id: uuid.UUID
    assessment_run_id: uuid.UUID
    question: str
    rationale: str
    priority: Literal["HIGH", "MEDIUM", "LOW"]
    blocking: bool
    evidence: list[dict[str, Any]]
    suggested_answers: list[str]
    status: Literal["OPEN", "ANSWERED", "DISMISSED"]
    answer: str | None
    answered_by_user_id: uuid.UUID | None
    answered_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MatterDefinitionAssessmentQuestionUpdate(BaseModel):
    status: Literal["ANSWERED", "DISMISSED"]
    answer: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def validate_answer(self) -> "MatterDefinitionAssessmentQuestionUpdate":
        if self.status == "ANSWERED" and not (self.answer or "").strip():
            raise ValueError("ANSWERED questions require an answer")
        if self.status == "DISMISSED" and self.answer is not None:
            raise ValueError("DISMISSED questions do not accept an answer")
        return self


class BatchTopicRead(BaseModel):
    id: uuid.UUID
    topic_key: str
    label: str
    description: str | None
    ordinal: int
    assignment_count: int


class BatchTopicTaxonomyRead(BaseModel):
    id: uuid.UUID
    review_batch_id: uuid.UUID
    source_assessment_run_id: uuid.UUID
    review_batch_run_id: uuid.UUID | None
    version: int
    status: Literal["ACTIVE", "RETIRED"]
    topics: list[BatchTopicRead]
    created_at: datetime


class WorkflowStepExecutionRead(ORMModel):
    id: uuid.UUID
    role_key: str
    ordinal: int
    fan_out_group: str | None
    status: str
    total_count: int
    completed_count: int
    failed_count: int
    request_count: int
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None


class SkillRunExecutionRead(ORMModel):
    id: uuid.UUID
    workflow_step_run_id: uuid.UUID
    skill_definition_version_id: uuid.UUID
    scope_type: str
    scope_id: uuid.UUID | None
    output_artifact_id: uuid.UUID | None
    status: str
    request_count: int
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None


class WorkflowExecutionRead(BaseModel):
    id: uuid.UUID
    workflow_key: str
    code_version: str
    status: str
    request_count: int
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    steps: list[WorkflowStepExecutionRead]
    skill_runs: list[SkillRunExecutionRead]


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


def normalize_email(email: str) -> str:
    return email.strip().casefold()
