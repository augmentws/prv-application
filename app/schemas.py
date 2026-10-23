import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.decision_engine import DecisionAnswer
from app.decision_specifications import DecisionSpecification
from app.fact_queries import FacetName, FactFilters, FactSort
from app.fact_specifications import FactDevelopmentSpecification
from classification_service.schemas import (
    BinaryClassMapping,
    ClassificationStrategy,
    ClassValue,
    FeatureContract,
    LabelPolicy,
    PredictionResolutionPolicy,
    Sha256,
    TargetCardinality,
    TargetFieldSnapshot,
    TargetMetadataType,
    TrainerSpecification,
    TrainingScopeSpecification,
    ValidationPolicy,
)

ResourceStatus = Literal["ACTIVE", "SUSPENDED", "ARCHIVED"]
AdminRole = Literal["ADMIN"]
LegacyTenantRole = Literal["ADMIN", "USER"]
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
    "PASTE", "MARKDOWN", "TEXT", "DOCX", "AGENT_EDIT", "USER_EDIT", "ASSESSMENT_REFINEMENT", "CLONE"
]
MatterDefinitionUserSourceKind = Literal["PASTE", "MARKDOWN", "TEXT", "USER_EDIT"]
MatterAnalysisTaskType = Literal["QUESTION_ANSWERING", "CLASSIFICATION", "FACT_DEVELOPMENT"]
MatterAnalysisTaskStatus = Literal["ACTIVE", "SUSPENDED", "ARCHIVED"]
MatterAnalysisTaskVersionStatus = Literal["DRAFT", "PUBLISHED", "RETIRED"]
MatterAnalysisTaskCompilationStatus = Literal["NOT_GENERATED", "STALE", "GENERATING", "READY", "FAILED"]
JobExecutionStatus = Literal[
    "QUEUED",
    "RUNNING",
    "WAITING",
    "COMPLETED",
    "COMPLETED_WITH_ERRORS",
    "FAILED",
    "CANCELED",
]
DecisionConfidenceKind = Literal[
    "PROVIDER_CONFIDENCE",
    "SELECTED_PROBABILITY",
    "DERIVED_PROBABILITY",
    "NONE",
]
Slug = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z][a-z0-9-]{1,78}[a-z0-9]$")
]
MetadataKey = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z][a-z0-9_]{0,99}$")]
EnumValueKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,99}$"),
]
AgentConversationTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
HierarchySeparator = Annotated[str, StringConstraints(min_length=1, max_length=10)]
HierarchyNodeId = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


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
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=1024)


class UserRead(ORMModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: EmailStr
    display_name: str
    status: ResourceStatus
    tenant_role: LegacyTenantRole
    is_superuser: bool
    created_at: datetime


class TenantAdminCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=1024)
    role: AdminRole = "ADMIN"


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


class AuditRecordRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    actor_user_id: uuid.UUID | None
    actor_display_name: str | None
    actor_email: str | None
    action: str
    target_type: str
    target_id: uuid.UUID
    details: dict[str, Any] | None
    created_at: datetime


class AuditRecordPage(BaseModel):
    items: list[AuditRecordRead]
    next_cursor: str | None


class NotificationRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    scope_type: Literal["TENANT", "CLIENT", "COLLECTION", "MATTER"]
    client_id: uuid.UUID | None
    client_name: str | None
    matter_id: uuid.UUID | None
    matter_name: str | None
    type_key: str
    category: str
    severity: Literal["INFO", "SUCCESS", "WARNING", "ERROR"]
    kind: Literal["INFORMATIONAL", "ACTION_REQUIRED"]
    title: str
    summary: str
    source_type: str
    source_id: uuid.UUID
    primary_route_name: str
    primary_route_params: dict[str, Any]
    occurred_at: datetime
    created_at: datetime
    resolved_at: datetime | None
    resolution_code: str | None
    seen_at: datetime | None
    read_at: datetime | None
    dismissed_at: datetime | None


class NotificationPage(BaseModel):
    items: list[NotificationRead]
    next_cursor: str | None


class NotificationCounts(BaseModel):
    unread: int
    action_required: int


class NotificationBulkRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notification_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class NotificationMutationResult(BaseModel):
    updated: int


class PermissionRead(ORMModel):
    id: uuid.UUID
    key: str
    display_name: str
    description: str
    category: str
    resource_type: Literal["TENANT", "CLIENT", "COLLECTION", "MATTER"]
    dangerous: bool
    status: Literal["ACTIVE", "ARCHIVED"]


class RoleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Annotated[
        str,
        StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Z][A-Z0-9_]{1,99}$"),
    ]
    display_name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    permission_keys: list[str] = Field(default_factory=list, max_length=100)


class RoleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    row_version: int = Field(ge=1)


class RolePermissionsReplace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permission_keys: list[str] = Field(max_length=100)
    row_version: int = Field(ge=1)

    @field_validator("permission_keys")
    @classmethod
    def unique_permission_keys(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("permission_keys must not contain duplicates")
        return value


class RoleRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    key: str
    display_name: str
    description: str | None
    role_kind: Literal["SYSTEM", "CUSTOM"]
    status: Literal["ACTIVE", "ARCHIVED"]
    row_version: int
    permission_keys: list[str]
    grant_count: int
    created_at: datetime
    updated_at: datetime


class AccessGroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Annotated[
        str,
        StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z][a-z0-9_-]{1,99}$"),
    ]
    display_name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class AccessGroupUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    row_version: int = Field(ge=1)


class AccessGroupMembersReplace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_ids: list[uuid.UUID] = Field(max_length=1000)
    row_version: int = Field(ge=1)

    @field_validator("user_ids")
    @classmethod
    def unique_user_ids(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(value) != len(set(value)):
            raise ValueError("user_ids must not contain duplicates")
        return value


class AccessGroupRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    key: str
    display_name: str
    description: str | None
    group_kind: Literal["LOCAL"]
    status: Literal["ACTIVE", "ARCHIVED"]
    row_version: int
    member_ids: list[uuid.UUID]
    grant_count: int
    created_at: datetime
    updated_at: datetime


class AuthorizationSubjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["USER", "ACCESS_GROUP"]
    id: uuid.UUID


class AuthorizationScopeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["TENANT", "CLIENT", "COLLECTION", "MATTER"]
    id: uuid.UUID


class AuthorizationGrantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: AuthorizationSubjectInput
    role_id: uuid.UUID
    scope: AuthorizationScopeInput


class AuthorizationGrantUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_id: uuid.UUID
    row_version: int = Field(ge=1)


class AuthorizationGrantRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    subject_type: Literal["USER", "ACCESS_GROUP"]
    subject_id: uuid.UUID
    subject_display_name: str
    role_id: uuid.UUID
    role_key: str
    role_display_name: str
    scope_type: Literal["TENANT", "CLIENT", "COLLECTION", "MATTER"]
    scope_id: uuid.UUID
    scope_display_name: str
    row_version: int
    created_by_user_id: uuid.UUID
    updated_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class EffectivePermissionsRead(BaseModel):
    resource_type: Literal["TENANT", "CLIENT", "COLLECTION", "MATTER"]
    resource_id: uuid.UUID
    authorization_revision: int
    permission_keys: list[str]


class AuthorizationExplanationRead(BaseModel):
    allowed: bool
    permission_key: str
    resource_type: Literal["TENANT", "CLIENT", "COLLECTION", "MATTER"]
    resource_id: uuid.UUID
    grant_id: uuid.UUID | None = None
    role_id: uuid.UUID | None = None
    role_key: str | None = None
    subject_type: str | None = None
    subject_id: uuid.UUID | None = None
    scope_type: str | None = None
    scope_id: uuid.UUID | None = None


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


class ProviderUsageTotalsRead(BaseModel):
    record_count: int
    request_count: int
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    total_tokens: int


class ProviderUsageModelBreakdownRead(ProviderUsageTotalsRead):
    provider: str
    model: str


class ProviderUsageJobBreakdownRead(ProviderUsageTotalsRead):
    job_type: str


class MatterProviderUsageReportRead(BaseModel):
    matter_id: uuid.UUID
    totals: ProviderUsageTotalsRead
    by_model: list[ProviderUsageModelBreakdownRead]
    by_job_type: list[ProviderUsageJobBreakdownRead]
    entries: list[ExternalProviderUsageRead]
    entries_offset: int
    entries_limit: int


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


class JobExecutionRead(ORMModel):
    id: uuid.UUID
    job_type: str | None
    job_id: uuid.UUID | None
    attempt_number: int
    retry_of_workflow_run_id: uuid.UUID | None
    workflow_key: str
    dbos_workflow_id: str
    notification_policy: Literal["DEFAULT", "PARENT_MANAGED"]
    notification_parent_type: str | None
    notification_parent_id: uuid.UUID | None
    status: JobExecutionStatus
    current_stage: str | None
    stage_label: str
    progress_completed: int
    progress_total: int | None
    progress_unit: str | None
    progress: dict[str, Any]
    cancel_requested_at: datetime | None
    cancel_requested_by_user_id: uuid.UUID | None
    last_heartbeat_at: datetime | None
    failure_code: str | None
    failure_summary: str | None
    failure_retriable: bool
    initiated_by_user_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    available_actions: list[Literal["CANCEL", "RETRY"]]


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
    imported_label_document_count: int
    imported_label_event_count: int
    imported_label_warning_count: int
    error_message: str | None
    created_by_user_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    execution: JobExecutionRead | None = None


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
    reused_count: int
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
    execution: JobExecutionRead | None = None


class MatterEmbeddingBatchRead(ORMModel):
    id: uuid.UUID
    job_id: uuid.UUID
    batch_number: int
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELED"]
    item_count: int
    processed_count: int
    embedded_count: int
    reused_count: int
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


JobOutcomeStatus = Literal["SUCCEEDED", "REUSED", "SKIPPED", "FAILED", "EXCLUDED", "CANCELED"]


class JobOutcomeManifestRead(ORMModel):
    id: uuid.UUID | None
    job_type: str
    job_id: uuid.UUID
    manifest_status: Literal["BUILDING", "FINALIZED", "FAILED", "UNAVAILABLE"]
    outcome_schema: str
    outcome_schema_version: int
    item_count: int
    status_counts: dict[str, int]
    reason_counts: dict[str, int]
    artifact_id: uuid.UUID | None
    artifact_content_hash: str | None
    artifact_byte_length: int | None
    finalized_at: datetime | None
    failure_summary: str | None
    item_details_available: bool
    legacy: bool = False
    created_at: datetime
    updated_at: datetime


class JobOutcomeItemRead(BaseModel):
    ordinal: int
    item_kind: str
    item_id: str
    outcome_status: JobOutcomeStatus
    reason_code: str
    message: str | None
    batch_id: str | None
    attempt_number: int
    input_fingerprint: str | None
    output_references: list[dict[str, Any]]
    metrics: dict[str, Any]
    payload: dict[str, Any]
    started_at: datetime | None
    completed_at: datetime | None


class JobOutcomeItemPage(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[JobOutcomeItemRead]


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


SearchFilterOperator = Literal["EQ", "IN", "NOT_IN", "RANGE", "EXISTS", "NOT_EXISTS"]
SearchSortDirection = Literal["ASC", "DESC"]
SearchMode = Literal["KEYWORD", "SEMANTIC", "HYBRID"]
SearchOperationKind = Literal[
    "SCHEMA_SYNC",
    "REBUILD",
    "DOCUMENT_UPSERT",
    "DOCUMENT_DELETE",
    "BATCH_CODING_SYNC",
]
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
        if (
            self.operator in {"IN", "NOT_IN"}
            and not self.values
            and not (self.operator == "IN" and self.include_missing)
        ):
            raise ValueError(f"{self.operator} filters require one or more values")
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
    size: int = Field(default=20, ge=1, le=1000)
    parent: HierarchyNodeId | None = None


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
    label: str | None = None
    parent: str | None = None
    has_children: bool = False


class MatterFacetValuesResponse(BaseModel):
    field: MetadataKey
    values: list[MatterSearchFacetValue]
    missing_count: int = Field(default=0, ge=0)
    total_count: int = Field(default=0, ge=0)
    has_more: bool = False


class MatterSearchResponse(BaseModel):
    total: int = Field(ge=0)
    took_ms: int = Field(ge=0)
    timed_out: bool
    hits: list[MatterSearchHit]
    facets: dict[str, list[MatterSearchFacetValue]]


class SearchExportFieldRead(BaseModel):
    id: str
    stable_key: str
    display_name: str
    group_id: uuid.UUID | None = None
    group_display_name: str | None = None
    source_path: str
    value_type: str
    cardinality: Literal["SINGLE", "MULTIPLE"]
    selectable: bool = True
    unavailable_reason: str | None = None
    large_value: bool = False
    enum_values: list[dict[str, Any]] = Field(default_factory=list)


class MatterSearchExportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: MatterSearchRequest
    field_ids: list[str] = Field(min_length=1, max_length=250)
    spreadsheet_safe: bool = True
    requested_filename: str = Field(default="search-results.csv", min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_export(self) -> "MatterSearchExportCreate":
        if len(self.field_ids) != len(set(self.field_ids)):
            raise ValueError("field_ids must be unique")
        filename = self.requested_filename.strip()
        if not filename.lower().endswith(".csv"):
            filename += ".csv"
        self.requested_filename = filename
        self.idempotency_key = self.idempotency_key.strip()
        return self


class MatterSearchExportRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    search_index_generation_id: uuid.UUID | None
    requested_filename: str
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELED"]
    current_stage: str | None
    matched_count: int
    exported_count: int
    page_count: int
    selected_field_count: int
    output_artifact_id: uuid.UUID | None
    output_content_hash: str | None
    output_byte_length: int | None
    output_filename: str | None
    snapshot_started_at: datetime | None
    expires_at: datetime | None
    failure_code: str | None
    failure_summary: str | None
    failure_retriable: bool
    created_by_user_id: uuid.UUID
    created_by_display_name: str
    created_by_email: str
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    execution: JobExecutionRead | None = None


class MatterSearchExportArtifactRead(BaseModel):
    artifact_id: uuid.UUID
    export_job_id: uuid.UUID
    filename: str
    byte_length: int = Field(ge=0)
    content_hash: str
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    created_at: datetime


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
    "ALL_MATTER",
    "SEARCH_QUERY",
    "RANDOM_MATTER",
    "RANDOM_BATCH",
    "RANDOM_SAVED_SEARCH",
    "RANDOM_ASSESSMENT",
    "DEFINITION_ASSESSMENT",
    "CLASSIFICATION_PREDICTIONS",
    "COMPARISON_RESULT",
    "TASK_BATCH_PARTITION",
]
InteractiveReviewBatchSelectionType = Literal[
    "ALL_MATTER",
    "SEARCH_QUERY",
    "RANDOM_MATTER",
    "RANDOM_BATCH",
    "RANDOM_SAVED_SEARCH",
    "RANDOM_ASSESSMENT",
]
ReviewBatchValueVisibility = Literal["OWN_VALUES", "ALL_REVIEWER_VALUES"]
ReviewBatchStatus = Literal["QUEUED", "BUILDING", "READY", "FAILED", "CANCELED", "ARCHIVED"]
ReviewBatchSearchStatus = Literal["QUEUED", "SYNCING", "READY", "FAILED", "NOT_CONFIGURED"]
ReviewBatchRunType = Literal["AGENT", "WORKFLOW", "CLASSIFIER"]
InteractiveReviewBatchRunType = Literal["AGENT"]
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
ReviewBatchRunPromotionStatus = Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED"]


class ReviewBatchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    selection_type: InteractiveReviewBatchSelectionType
    search: MatterSearchRequest | None = None
    source_batch_id: uuid.UUID | None = None
    saved_search_id: uuid.UUID | None = None
    assessment_run_id: uuid.UUID | None = None
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
        if self.selection_type == "RANDOM_SAVED_SEARCH":
            if self.saved_search_id is None:
                raise ValueError("RANDOM_SAVED_SEARCH batches require saved_search_id")
            if self.sample_size is None:
                raise ValueError("RANDOM_SAVED_SEARCH batches require sample_size")
        elif self.saved_search_id is not None:
            raise ValueError("saved_search_id is only valid for RANDOM_SAVED_SEARCH batches")
        if self.selection_type == "RANDOM_ASSESSMENT":
            if self.assessment_run_id is None:
                raise ValueError("RANDOM_ASSESSMENT batches require assessment_run_id")
            if self.sample_size is None:
                raise ValueError("RANDOM_ASSESSMENT batches require sample_size")
        elif self.assessment_run_id is not None:
            raise ValueError("assessment_run_id is only valid for RANDOM_ASSESSMENT batches")
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


class ReviewBatchSearchCodingFieldRead(BaseModel):
    metadata_definition_id: uuid.UUID
    key: str
    display_name: str
    type: MetadataType
    value_count: int


class ReviewBatchRead(BaseModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    name: str
    description: str | None
    selection_type: ReviewBatchSelectionType
    selection_definition: dict[str, Any]
    source_batch_id: uuid.UUID | None
    analysis_task_batch_set_id: uuid.UUID | None = None
    batch_set_ordinal: int | None = None
    search_index_generation_id: uuid.UUID | None
    sample_size: int | None
    random_seed: str | None
    assigned_user_id: uuid.UUID | None
    assigned_user: MatterSavedSearchUserRead | None
    reviewer_value_visibility: ReviewBatchValueVisibility
    status: ReviewBatchStatus
    search_status: ReviewBatchSearchStatus
    search_error_message: str | None
    searchable_coding_run_id: uuid.UUID | None
    searchable_coding_status: ReviewBatchSearchStatus | None
    searchable_coding_error_message: str | None
    searchable_coding_fields: list[ReviewBatchSearchCodingFieldRead]
    workflow_id: str
    document_count: int
    error_message: str | None
    created_by_user_id: uuid.UUID
    coding_groups: list[ReviewBatchCodingGroupRead]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    execution: JobExecutionRead | None = None


class ReviewBatchDocumentRead(BaseModel):
    matter_document_id: uuid.UUID
    source_collection_id: uuid.UUID
    collection_item_id: uuid.UUID
    sequence_number: int
    review_status: Literal["NOT_STARTED", "IN_PROGRESS", "COMPLETED", "SKIPPED", "FAILED"]


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
        if self.run_type == "AGENT" and self.agent_definition_version_id is None:
            raise ValueError("Agent runs require an agent definition version")
        if self.run_type == "AGENT" and self.actor_user_id is not None:
            raise ValueError("Agent runs cannot specify a human actor")
        return self


class ReviewBatchRunRead(ORMModel):
    id: uuid.UUID
    review_batch_id: uuid.UUID | None
    scope_type: Literal["MATTER", "REVIEW_BATCH"] = "REVIEW_BATCH"
    scope_id: uuid.UUID | None = None
    analysis_input_manifest_id: uuid.UUID | None = None
    analysis_task_batch_set_id: uuid.UUID | None = None
    batch_set_ordinal: int | None = None
    run_type: ReviewBatchRunType
    purpose: ReviewBatchRunPurpose
    status: ReviewBatchRunStatus
    result_policy: Literal["ISOLATED", "PUBLISH_TO_MATTER"]
    parent_run_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    agent_definition_version_id: uuid.UUID | None
    workflow_run_record_id: uuid.UUID | None
    classification_scoring_run_id: uuid.UUID | None = None
    derivation_key: Sha256 | None = None
    idempotency_key: str | None = None
    configuration_snapshot: dict[str, Any]
    result_summary: dict[str, Any] | None = None
    initiated_by_user_id: uuid.UUID
    processed_document_count: int
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    archived: bool = False
    archived_at: datetime | None = None
    execution: JobExecutionRead | None = None


class ReviewBatchAnalysisRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matter_analysis_task_id: uuid.UUID
    matter_analysis_task_version_id: uuid.UUID | None = None


class QuestionAnsweringWholeMatterRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=200)
    matter_analysis_task_version_id: uuid.UUID | None = None

    @field_validator("idempotency_key")
    @classmethod
    def normalize_question_answering_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("idempotency_key cannot be blank")
        return normalized


class ReviewBatchClassifierRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification_scoring_run_id: uuid.UUID
    minimum_confidence: float = Field(ge=0, le=1)
    maximum_confidence: float | None = Field(default=None, ge=0, le=1)
    selected_class_keys: list[ClassValue] = Field(default_factory=list, max_length=1000)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_confidence_window(self) -> "ReviewBatchClassifierRunCreate":
        if self.maximum_confidence is not None and self.maximum_confidence <= self.minimum_confidence:
            raise ValueError("maximum_confidence must be greater than minimum_confidence")
        if len(self.selected_class_keys) != len(set(self.selected_class_keys)):
            raise ValueError("selected_class_keys must be unique")
        self.idempotency_key = self.idempotency_key.strip()
        if not self.idempotency_key:
            raise ValueError("idempotency_key is required")
        return self


class ClassificationReviewBatchCreate(BaseModel):
    """Create a review batch from one completed classification analysis output."""

    model_config = ConfigDict(extra="forbid")

    classification_analysis_run_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    minimum_confidence: float = Field(ge=0, le=1)
    maximum_confidence: float | None = Field(default=None, ge=0, le=1)
    selected_class_keys: list[ClassValue] = Field(default_factory=list, max_length=1000)
    assigned_user_id: uuid.UUID | None = None
    reviewer_value_visibility: ReviewBatchValueVisibility = "OWN_VALUES"
    idempotency_key: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_selection(self) -> "ClassificationReviewBatchCreate":
        if self.maximum_confidence is not None and self.maximum_confidence <= self.minimum_confidence:
            raise ValueError("maximum_confidence must be greater than minimum_confidence")
        if len(self.selected_class_keys) != len(set(self.selected_class_keys)):
            raise ValueError("selected_class_keys must be unique")
        self.name = " ".join(self.name.split())
        self.idempotency_key = self.idempotency_key.strip()
        if not self.name:
            raise ValueError("name is required")
        if not self.idempotency_key:
            raise ValueError("idempotency_key is required")
        return self


class ReviewBatchRunPromotionDestinationRead(BaseModel):
    id: uuid.UUID
    key: MetadataKey
    display_name: str
    type: MetadataType
    cardinality: Cardinality
    assertion_policy: AssertionPolicy
    resolution_policy: ResolutionPolicy


class ReviewBatchRunPromotionRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    review_batch_id: uuid.UUID
    review_batch_run_id: uuid.UUID | None
    reviewer_coding_id: uuid.UUID | None
    reviewer_coding_revision: int | None
    matter_analysis_task_version_id: uuid.UUID | None
    status: ReviewBatchRunPromotionStatus
    mode: Literal["REPLACE"]
    destination_snapshot: list[ReviewBatchRunPromotionDestinationRead]
    total_document_count: int
    eligible_document_count: int
    processed_document_count: int
    skipped_document_count: int
    event_count: int
    created_by_user_id: uuid.UUID
    dbos_workflow_id: str
    search_operation_id: uuid.UUID | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    execution: JobExecutionRead | None = None


class ReviewBatchFieldCodingCountRead(BaseModel):
    coding_group_id: uuid.UUID
    coding_group_name: str
    metadata_definition_id: uuid.UUID
    field_key: MetadataKey
    field_display_name: str
    document_count: int = Field(ge=0)


class ReviewBatchReviewerStatisticsRead(BaseModel):
    reviewer_coding_id: uuid.UUID
    reviewer: MatterSavedSearchUserRead
    completed_document_count: int = Field(ge=0)
    skipped_document_count: int = Field(ge=0)
    incomplete_document_count: int = Field(ge=0)
    field_counts: list[ReviewBatchFieldCodingCountRead]
    promotion: ReviewBatchRunPromotionRead | None
    created_at: datetime
    updated_at: datetime


class ReviewBatchStatisticsRead(BaseModel):
    review_batch_id: uuid.UUID
    document_count: int = Field(ge=0)
    reviewers: list[ReviewBatchReviewerStatisticsRead]
    can_promote: bool


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
    origin: Literal["REVIEW_UI", "IMPORT"] = "REVIEW_UI"
    request_key: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_unique_fields(self) -> "ReviewBatchRunDocumentValues":
        field_ids = [field.metadata_definition_id for field in self.fields]
        if len(field_ids) != len(set(field_ids)):
            raise ValueError("Each coding field may appear only once per document save")
        return self


class ReviewBatchReviewerCodingRead(ORMModel):
    id: uuid.UUID
    review_batch_id: uuid.UUID
    reviewer_user_id: uuid.UUID
    revision: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class ReviewBatchReviewerCodingValueRead(BaseModel):
    reviewer_coding_id: uuid.UUID
    matter_document_id: uuid.UUID
    metadata_definition_id: uuid.UUID
    value_ordinal: int
    value: Any


class ReviewBatchReviewerProgressRead(BaseModel):
    reviewer_coding_id: uuid.UUID
    document_count: int
    not_started_count: int
    in_progress_count: int
    completed_count: int
    skipped_count: int


class ReviewBatchRunValueRead(BaseModel):
    review_batch_run_id: uuid.UUID
    matter_document_id: uuid.UUID
    metadata_definition_id: uuid.UUID
    value_ordinal: int
    value: Any
    confidence: float | None
    confidence_kind: DecisionConfidenceKind | None
    review_decision_result_id: uuid.UUID | None
    question_key: str | None


class ReviewBatchSearchCodingSelectionRead(BaseModel):
    review_batch_id: uuid.UUID
    review_batch_run_id: uuid.UUID
    status: ReviewBatchSearchStatus
    selected_by_user_id: uuid.UUID
    selected_at: datetime
    projected_at: datetime | None
    error_message: str | None


class ReviewBatchCodingSearchFilter(BaseModel):
    field: MetadataKey
    values: list[Any] = Field(min_length=1, max_length=1000)
    minimum_confidence: float | None = Field(default=None, ge=0, le=1)
    maximum_confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_confidence_window(self) -> "ReviewBatchCodingSearchFilter":
        if (
            self.minimum_confidence is not None
            and self.maximum_confidence is not None
            and self.maximum_confidence <= self.minimum_confidence
        ):
            raise ValueError("maximum_confidence must be greater than minimum_confidence")
        return self


class ReviewBatchSearchRequest(MatterSearchRequest):
    coding_filters: list[ReviewBatchCodingSearchFilter] = Field(default_factory=list, max_length=100)


class ReviewBatchSearchExportCreate(MatterSearchExportCreate):
    search: ReviewBatchSearchRequest


class ReviewBatchFacetValuesRequest(MatterFacetValuesRequest):
    search: ReviewBatchSearchRequest


class ReviewBatchReviewerValueRead(BaseModel):
    reviewer_coding_id: uuid.UUID
    actor_user_id: uuid.UUID
    actor_user: MatterSavedSearchUserRead
    metadata_definition_id: uuid.UUID
    values: list[Any]


class ReviewBatchDocumentCodingRead(BaseModel):
    matter_document_id: uuid.UUID
    review_status: Literal["NOT_STARTED", "IN_PROGRESS", "COMPLETED", "SKIPPED"]
    values: list[ReviewBatchReviewerCodingValueRead]
    reviewer_values: list[ReviewBatchReviewerValueRead]


class ReviewBatchCodingHistoryEntryRead(BaseModel):
    review_batch_run_id: uuid.UUID | None = None
    reviewer_coding_id: uuid.UUID | None = None
    reviewer_revision: int | None = Field(default=None, ge=1)
    reviewer_origin: Literal["REVIEW_UI", "IMPORT", "SYSTEM_MIGRATION"] | None = None
    event_kind: Literal["VALUE", "NO_VALUE", "STATUS"] | None = None
    document_status: Literal["IN_PROGRESS", "COMPLETED", "SKIPPED"] | None = None
    run_type: ReviewBatchRunType | None = None
    purpose: ReviewBatchRunPurpose | None = None
    metadata_definition_id: uuid.UUID | None = None
    field_key: str
    field_display_name: str
    value: Any | None
    value_label: str
    recorded_at: datetime
    source_kind: Literal["HUMAN", "JEV", "AGENT", "WORKFLOW", "CLASSIFIER"]
    source_label: str
    source_detail: str | None
    score: float | None
    score_kind: DecisionConfidenceKind | None
    question_key: str | None
    review_decision_result_id: uuid.UUID | None


class DirectCodingHistoryEntryRead(BaseModel):
    metadata_event_id: uuid.UUID
    metadata_definition_id: uuid.UUID
    field_key: str
    field_display_name: str
    operation: MetadataOperation
    value: Any | None
    value_label: str
    recorded_at: datetime
    source_kind: MetadataEventSource
    source_label: str
    source_detail: str | None
    score: float | None
    effective_status: MetadataEffectiveStatus
    confirmation_state: MetadataConfirmationState


class DocumentCodingHistoryBatchRead(BaseModel):
    review_batch_id: uuid.UUID
    batch_name: str
    batch_status: str
    entries: list[ReviewBatchCodingHistoryEntryRead]


class DocumentCodingHistoryRead(BaseModel):
    direct: list[DirectCodingHistoryEntryRead]
    batches: list[DocumentCodingHistoryBatchRead]


class ReviewBatchRunProgressRead(BaseModel):
    review_batch_run_id: uuid.UUID
    document_count: int
    not_started_count: int
    in_progress_count: int
    completed_count: int
    skipped_count: int
    failed_count: int


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


ReviewBatchComparisonRowFilter = Literal["ALL", "AGREEMENT", "DISAGREEMENT", "INCOMPLETE"]
ReviewBatchComparisonSourceState = Literal["EVALUATED", "SKIPPED", "FAILED", "INCOMPLETE", "NOT_EVALUATED"]


class ReviewBatchComparisonRequest(BaseModel):
    metadata_definition_id: uuid.UUID
    source_ids: list[uuid.UUID] = Field(min_length=2, max_length=12)
    row_filter: ReviewBatchComparisonRowFilter = "ALL"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)

    @model_validator(mode="after")
    def validate_unique_runs(self) -> "ReviewBatchComparisonRequest":
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("Each comparison source may be selected only once")
        return self


class ReviewBatchDisagreementBatchPreviewRequest(BaseModel):
    metadata_definition_id: uuid.UUID
    source_ids: list[uuid.UUID] = Field(min_length=2, max_length=12)

    @model_validator(mode="after")
    def validate_unique_sources(self) -> "ReviewBatchDisagreementBatchPreviewRequest":
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("Each comparison source may be selected only once")
        return self


class ReviewBatchDisagreementBatchCreate(ReviewBatchDisagreementBatchPreviewRequest):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)


class ReviewBatchDisagreementSourceSnapshotRead(BaseModel):
    id: uuid.UUID
    source_type: Literal["REVIEWER", "RUN"]
    label: str
    reviewer_revision: int | None = Field(default=None, ge=0)


class ReviewBatchDisagreementBatchPreviewRead(BaseModel):
    source_batch_id: uuid.UUID
    metadata_definition_id: uuid.UUID
    field_display_name: str
    candidate_count: int = Field(ge=0)
    sources: list[ReviewBatchDisagreementSourceSnapshotRead]


class ReviewBatchDisagreementDocumentSourceRead(BaseModel):
    id: uuid.UUID
    source_type: Literal["REVIEWER", "RUN"]
    run_type: ReviewBatchRunType | None = None
    label: str
    detail: str | None = None
    reviewer_revision: int | None = Field(default=None, ge=0)
    state: ReviewBatchComparisonSourceState
    values: list[Any]
    display_values: list[str]
    top_confidence: float | None = Field(default=None, ge=0, le=1)


class ReviewBatchDisagreementDocumentRead(BaseModel):
    review_batch_id: uuid.UUID
    source_batch_id: uuid.UUID
    matter_document_id: uuid.UUID
    metadata_definition_id: uuid.UUID
    field_display_name: str
    field_type: MetadataType
    cardinality: Cardinality
    allowed_values: list[dict[str, Any]]
    agreement_status: Literal["PARTIAL", "DISAGREEMENT"]
    sources: list[ReviewBatchDisagreementDocumentSourceRead]


class ReviewBatchComparisonSourceOptionRead(BaseModel):
    id: uuid.UUID
    source_type: Literal["REVIEWER", "RUN"]
    run_type: ReviewBatchRunType | None = None
    purpose: ReviewBatchRunPurpose | None = None
    status: ReviewBatchRunStatus | None = None
    label: str
    detail: str | None
    metadata_definition_ids: list[uuid.UUID]
    created_at: datetime


class ReviewBatchComparisonFieldOptionRead(BaseModel):
    id: uuid.UUID
    key: str
    display_name: str
    type: MetadataType
    cardinality: Cardinality
    allowed_values: list[dict[str, Any]]


class ReviewBatchComparisonOptionsRead(BaseModel):
    review_batch_id: uuid.UUID
    sources: list[ReviewBatchComparisonSourceOptionRead]
    fields: list[ReviewBatchComparisonFieldOptionRead]


class ReviewBatchComparisonValueCountRead(BaseModel):
    value_key: str
    display_label: str
    document_count: int = Field(ge=0)


class ReviewBatchComparisonSourceSummaryRead(BaseModel):
    source_id: uuid.UUID
    evaluated_document_count: int = Field(ge=0)
    no_value_document_count: int = Field(ge=0)
    skipped_document_count: int = Field(ge=0)
    failed_document_count: int = Field(ge=0)
    incomplete_document_count: int = Field(ge=0)
    not_evaluated_document_count: int = Field(ge=0)
    value_counts: list[ReviewBatchComparisonValueCountRead]
    value_counts_truncated: bool = False


class ReviewBatchComparisonMatrixCellRead(BaseModel):
    left_value: str
    right_value: str
    document_count: int = Field(ge=0)


class ReviewBatchComparisonClassAgreementRead(BaseModel):
    value_key: str
    display_label: str
    both_selected_count: int = Field(ge=0)
    left_only_count: int = Field(ge=0)
    right_only_count: int = Field(ge=0)
    both_not_selected_count: int = Field(ge=0)
    jaccard: float | None = Field(default=None, ge=0, le=1)


class ReviewBatchComparisonSourceValueCountRead(BaseModel):
    source_id: uuid.UUID
    document_count: int = Field(ge=0)


class ReviewBatchMultiSourceClassAgreementRead(BaseModel):
    value_key: str
    display_label: str
    source_counts: list[ReviewBatchComparisonSourceValueCountRead]
    all_selected_count: int = Field(ge=0)
    any_selected_count: int = Field(ge=0)
    jaccard: float | None = Field(default=None, ge=0, le=1)


class ReviewBatchMultiSourceAgreementRead(BaseModel):
    compared_document_count: int = Field(ge=0)
    incomplete_document_count: int = Field(ge=0)
    class_agreement: list[ReviewBatchMultiSourceClassAgreementRead]


class ReviewBatchPairwiseAgreementRead(BaseModel):
    left_source_id: uuid.UUID
    right_source_id: uuid.UUID
    compared_document_count: int = Field(ge=0)
    exact_match_count: int = Field(ge=0)
    partial_match_count: int = Field(ge=0)
    disagreement_count: int = Field(ge=0)
    incomplete_document_count: int = Field(ge=0)
    exact_agreement: float | None = Field(default=None, ge=0, le=1)
    mean_jaccard: float | None = Field(default=None, ge=0, le=1)
    confusion_matrix: list[ReviewBatchComparisonMatrixCellRead]
    class_agreement: list[ReviewBatchComparisonClassAgreementRead]


class ReviewBatchComparisonDocumentSourceRead(BaseModel):
    source_id: uuid.UUID
    state: ReviewBatchComparisonSourceState
    value_keys: list[str]
    display_values: list[str]
    top_confidence: float | None = Field(default=None, ge=0, le=1)


class ReviewBatchComparisonDocumentRead(BaseModel):
    matter_document_id: uuid.UUID
    collection_item_id: uuid.UUID
    sequence_number: int = Field(ge=1)
    agreement_status: Literal["EXACT", "PARTIAL", "DISAGREEMENT", "INCOMPLETE"]
    sources: list[ReviewBatchComparisonDocumentSourceRead]


class ReviewBatchCodingComparisonRead(BaseModel):
    review_batch_id: uuid.UUID
    metadata_definition_id: uuid.UUID
    field_display_name: str
    cardinality: Cardinality
    document_count: int = Field(ge=0)
    filtered_document_count: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    sources: list[ReviewBatchComparisonSourceOptionRead]
    source_summaries: list[ReviewBatchComparisonSourceSummaryRead]
    multi_source_agreement: ReviewBatchMultiSourceAgreementRead
    pairwise_agreements: list[ReviewBatchPairwiseAgreementRead]
    documents: list[ReviewBatchComparisonDocumentRead]


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
    parent_key: EnumValueKey | None = None


def _validate_enum_hierarchy(values: list[EnumValue]) -> None:
    by_key = {value.key: value for value in values}
    for value in values:
        if value.parent_key == value.key:
            raise ValueError(f"ENUM value '{value.key}' cannot be its own parent")
        if value.parent_key is not None and value.parent_key not in by_key:
            raise ValueError(f"ENUM value '{value.key}' references unknown parent '{value.parent_key}'")
        if value.parent_key is not None and value.active and not by_key[value.parent_key].active:
            raise ValueError(f"Active ENUM value '{value.key}' cannot have an inactive parent")

    for value in values:
        visited: set[str] = set()
        current: EnumValue | None = value
        while current is not None and current.parent_key is not None:
            if current.key in visited:
                raise ValueError("ENUM allowed-value hierarchy cannot contain a cycle")
            visited.add(current.key)
            current = by_key[current.parent_key]


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
    hierarchy_separator: HierarchySeparator | None = None
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
            _validate_enum_hierarchy(self.allowed_values)
        elif self.allowed_values is not None:
            raise ValueError("allowed_values is only valid for ENUM definitions")
        if self.facetable and self.type in {"LONG_TEXT", "JSON"}:
            raise ValueError("LONG_TEXT and JSON fields cannot be facetable in phase one")
        if self.normalize_to_lowercase and self.type not in {"TEXT", "LONG_TEXT"}:
            raise ValueError("Lowercase normalization is only valid for text fields")
        if self.hierarchy_separator is not None:
            if self.type != "TEXT":
                raise ValueError("Path hierarchy is supported only for TEXT definitions")
            if not self.searchable or not self.facetable:
                raise ValueError("Path hierarchy fields must be searchable and facetable")
            if not self.hierarchy_separator.strip():
                raise ValueError("Path hierarchy separator cannot be whitespace")
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
    hierarchy_separator: HierarchySeparator | None = None
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
    parent_key: EnumValueKey | None = None


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
    hierarchy_separator: str | None = None
    reviewable: bool
    ai_assignable: bool
    status: ResourceStatus
    created_at: datetime


class EmailDomainUpdateRead(BaseModel):
    metadata_definition_id: uuid.UUID
    definition_created: bool
    search_operation_id: uuid.UUID
    status: SearchOperationStatus


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
    source_guidance_id: uuid.UUID | None
    source_revision_id: uuid.UUID | None
    source_content_hash: str | None
    created_at: datetime


class MatterDefinitionRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    key: str
    name: str
    description: str | None
    status: Literal["ACTIVE", "ARCHIVED"]
    current_revision: int
    published_revision: int | None
    created_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    revision: MatterDefinitionRevisionRead


class MatterDefinitionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: MetadataKey
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    content_markdown: str = Field(min_length=1, max_length=2_000_000)
    source_kind: MatterDefinitionUserSourceKind = "PASTE"
    source_filename: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        name = " ".join(value.split())
        if not name:
            raise ValueError("Guidance name must not be blank")
        return name


class MatterDefinitionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)

    @model_validator(mode="after")
    def validate_update(self) -> "MatterDefinitionUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one guidance property must be supplied")
        if self.name is not None:
            self.name = " ".join(self.name.split())
            if not self.name:
                raise ValueError("Guidance name must not be blank")
        return self


class MatterDefinitionCloneCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: MetadataKey
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    source_revision: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        name = " ".join(value.split())
        if not name:
            raise ValueError("Guidance name must not be blank")
        return name


AnalysisTaskScopeMode = Literal["AD_HOC_BATCH", "WHOLE_MATTER", "PARTITIONED_SET"]


class AnalysisTaskScopePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_modes: list[AnalysisTaskScopeMode] = Field(min_length=1, max_length=3)
    default_mode: AnalysisTaskScopeMode

    @model_validator(mode="after")
    def validate_modes(self) -> "AnalysisTaskScopePolicy":
        if len(self.allowed_modes) != len(set(self.allowed_modes)):
            raise ValueError("allowed_modes must be unique")
        if self.default_mode not in self.allowed_modes:
            raise ValueError("default_mode must be included in allowed_modes")
        return self


class MatterAnalysisTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: MetadataKey
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    task_type: MatterAnalysisTaskType
    decision_provider: Literal["JEV", "OPENAI_DECISIONS"] = "JEV"
    definition_markdown: str = Field(min_length=1, max_length=2_000_000)
    destination_metadata_definition_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    classification_model_version_id: uuid.UUID | None = None
    review_guidance_id: uuid.UUID | None = None
    review_guidance_revision: int | None = Field(default=None, ge=1)
    scope_policy: AnalysisTaskScopePolicy | None = None

    @field_validator("destination_metadata_definition_ids")
    @classmethod
    def validate_unique_destinations(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(value) != len(set(value)):
            raise ValueError("Destination metadata fields must be unique")
        return value


class MatterAnalysisTaskVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition_markdown: str = Field(min_length=1, max_length=2_000_000)
    based_on_version: int = Field(ge=1)
    decision_provider: Literal["JEV", "OPENAI_DECISIONS"] | None = None
    destination_metadata_definition_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    classification_model_version_id: uuid.UUID | None = None
    review_guidance_revision: int | None = Field(default=None, ge=1)
    scope_policy: AnalysisTaskScopePolicy | None = None

    @field_validator("destination_metadata_definition_ids")
    @classmethod
    def validate_unique_destinations(cls, value: list[uuid.UUID] | None) -> list[uuid.UUID] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("Destination metadata fields must be unique")
        return value


class MatterAnalysisTaskSpecificationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_specification: DecisionSpecification
    input_contract: dict[str, Any] = Field(default_factory=dict)
    output_contract: dict[str, Any] = Field(default_factory=dict)
    evidence_policy: dict[str, Any] = Field(default_factory=dict)
    routing_policy: dict[str, Any] = Field(default_factory=dict)
    compiler_skill_definition_version_id: uuid.UUID | None = None
    compiler_skill_run_id: uuid.UUID | None = None
    compiler_model_configuration: dict[str, Any] = Field(default_factory=dict)
    validation_report: dict[str, Any] = Field(default_factory=dict)
    source_provenance: dict[str, Any] = Field(default_factory=dict)


class FactDevelopmentSpecificationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_development_specification: FactDevelopmentSpecification


class FactIssueMembershipPreviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_batch_id: uuid.UUID
    coding_source: Literal["MATTER_METADATA", "REVIEWER_CODING"] = "MATTER_METADATA"
    reviewer_coding_id: uuid.UUID | None = None


class FactIssueMembershipCount(BaseModel):
    issue_key: str
    name: str
    document_count: int


class FactIssueMembershipPreviewRead(BaseModel):
    review_batch_id: uuid.UUID
    coding_source: Literal["MATTER_METADATA", "REVIEWER_CODING"]
    reviewer_coding_id: uuid.UUID | None
    reviewer_coding_revision: int | None
    batch_document_count: int
    matched_document_count: int
    issues: list[FactIssueMembershipCount]


class MatterAnalysisTaskPlaygroundCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_batch_id: uuid.UUID
    matter_document_id: uuid.UUID


class MatterAnalysisTaskPlaygroundRunRead(BaseModel):
    workflow_run_id: uuid.UUID
    review_batch_id: uuid.UUID
    review_batch_run_id: uuid.UUID
    matter_document_id: uuid.UUID
    task_version_id: uuid.UUID
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"]
    result_id: uuid.UUID | None = None
    error_message: str | None = None


class MatterAnalysisTaskVersionRead(ORMModel):
    id: uuid.UUID
    matter_analysis_task_id: uuid.UUID
    version: int
    status: MatterAnalysisTaskVersionStatus
    compilation_status: MatterAnalysisTaskCompilationStatus
    definition_markdown: str
    definition_content_hash: str
    destination_metadata_definition_ids: list[uuid.UUID] | None
    decision_provider: Literal["JEV", "OPENAI_DECISIONS"]
    decision_model: str | None
    classification_model_id: uuid.UUID | None
    classification_model_version_id: uuid.UUID | None
    classification_embedding_job_id: uuid.UUID | None = None
    review_guidance_id: uuid.UUID | None = None
    review_guidance_revision_id: uuid.UUID | None = None
    review_guidance_revision: int | None = None
    review_guidance_content_hash: str | None = None
    review_guidance_pin_stale: bool = False
    decision_specification: DecisionSpecification | None
    fact_development_specification: FactDevelopmentSpecification | None = None
    specification_content_hash: str | None
    input_contract: dict[str, Any]
    output_contract: dict[str, Any]
    evidence_policy: dict[str, Any]
    routing_policy: dict[str, Any]
    scope_policy: dict[str, Any]
    compiler_skill_definition_version_id: uuid.UUID | None
    compiler_skill_run_id: uuid.UUID | None
    compiler_workflow_run_id: uuid.UUID | None
    compilation_progress: dict[str, Any] = Field(default_factory=dict)
    compiler_model_configuration: dict[str, Any]
    validation_report: dict[str, Any]
    source_provenance: dict[str, Any]
    created_by_user_id: uuid.UUID
    published_by_user_id: uuid.UUID | None
    created_at: datetime
    published_at: datetime | None


class AnalysisTaskExecutionProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: MetadataKey
    name: str = Field(min_length=1, max_length=200)
    decision_provider: Literal["JEV", "OPENAI_DECISIONS"]

    @field_validator("name")
    @classmethod
    def normalize_execution_profile_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Execution profile name is required")
        return normalized


class AnalysisTaskExecutionProfileRead(ORMModel):
    id: uuid.UUID
    matter_analysis_task_version_id: uuid.UUID
    key: str
    name: str
    decision_provider: Literal["JEV", "OPENAI_DECISIONS"]
    model_key: str
    resolved_model: str
    model_policy: dict[str, Any]
    required_capabilities: list[str]
    skill_definition_version_id: uuid.UUID
    provider_adapter_version: str
    content_hash: str
    created_by_user_id: uuid.UUID
    created_at: datetime


class MatterAnalysisTaskRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    key: str
    name: str
    description: str | None
    task_type: MatterAnalysisTaskType
    workflow_key: str
    current_version: int
    published_version: int | None
    status: MatterAnalysisTaskStatus
    created_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    version: MatterAnalysisTaskVersionRead
    published_version_details: MatterAnalysisTaskVersionRead | None = None


AnalysisTaskBatchSetSourceType = Literal[
    "ALL_MATTER", "SOURCE_BATCH", "SEARCH_QUERY", "SAVED_SEARCH", "INPUT_MANIFEST"
]
AnalysisTaskBatchSetStatus = Literal["QUEUED", "BUILDING", "READY", "FAILED", "CANCELED", "ARCHIVED"]


class AnalysisTaskBatchSetPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    population_source_type: AnalysisTaskBatchSetSourceType
    source_review_batch_id: uuid.UUID | None = None
    input_manifest_id: uuid.UUID | None = None
    search: MatterSearchRequest | None = None
    saved_search_id: uuid.UUID | None = None
    target_batch_size: int = Field(ge=1, le=10_000_000)
    keep_together: list[Literal["FAMILY"]] = Field(default_factory=list, max_length=1)

    @model_validator(mode="after")
    def validate_source(self) -> "AnalysisTaskBatchSetPlanRequest":
        if self.population_source_type == "SOURCE_BATCH" and self.source_review_batch_id is None:
            raise ValueError("SOURCE_BATCH requires source_review_batch_id")
        if self.population_source_type != "SOURCE_BATCH" and self.source_review_batch_id is not None:
            raise ValueError("source_review_batch_id is only valid for SOURCE_BATCH")
        if self.population_source_type == "INPUT_MANIFEST" and self.input_manifest_id is None:
            raise ValueError("INPUT_MANIFEST requires input_manifest_id")
        if self.population_source_type != "INPUT_MANIFEST" and self.input_manifest_id is not None:
            raise ValueError("input_manifest_id is only valid for INPUT_MANIFEST")
        if self.population_source_type == "SEARCH_QUERY":
            if self.search is None:
                raise ValueError("SEARCH_QUERY requires search")
            if self.search.search_mode != "KEYWORD":
                raise ValueError("Batch Set search populations currently require a Keyword search")
        elif self.search is not None:
            raise ValueError("search is only valid for SEARCH_QUERY")
        if self.population_source_type == "SAVED_SEARCH" and self.saved_search_id is None:
            raise ValueError("SAVED_SEARCH requires saved_search_id")
        if self.population_source_type != "SAVED_SEARCH" and self.saved_search_id is not None:
            raise ValueError("saved_search_id is only valid for SAVED_SEARCH")
        if len(self.keep_together) != len(set(self.keep_together)):
            raise ValueError("keep_together values must be unique")
        return self


class AnalysisTaskBatchSetPreviewCreate(AnalysisTaskBatchSetPlanRequest):
    pass


class AnalysisTaskBatchSetCreate(AnalysisTaskBatchSetPlanRequest):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    random_seed: str | None = Field(default=None, min_length=1, max_length=100)


class AnalysisTaskBatchSetPreviewRead(BaseModel):
    population_count: int
    estimated_batch_count: int
    estimated_batch_sizes: list[int]
    population_hash: str
    oversized_groups: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)


class AnalysisTaskBatchSetChildRead(BaseModel):
    id: uuid.UUID
    ordinal: int
    name: str
    status: ReviewBatchStatus
    search_status: ReviewBatchSearchStatus
    search_error_message: str | None
    document_count: int


class AnalysisTaskBatchSetRead(ORMModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    matter_analysis_task_id: uuid.UUID
    matter_analysis_task_version_id: uuid.UUID
    name: str
    description: str | None
    population_source_type: AnalysisTaskBatchSetSourceType
    selection_definition: dict[str, Any]
    source_review_batch_id: uuid.UUID | None
    input_manifest_id: uuid.UUID | None
    search_index_generation_id: uuid.UUID | None
    target_batch_size: int
    allocation_policy: dict[str, Any]
    random_seed: str
    allocation_algorithm_version: str
    status: AnalysisTaskBatchSetStatus
    workflow_id: str
    population_count: int
    batch_count: int
    population_hash: str | None
    warning_details: list[dict[str, Any]]
    error_message: str | None
    created_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    children: list[AnalysisTaskBatchSetChildRead] = Field(default_factory=list)


AnalysisTaskBatchSetRunStatus = Literal[
    "QUEUED", "RUNNING", "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"
]
AnalysisTaskBatchSetRunChildStatus = Literal[
    "PENDING", "QUEUED", "RUNNING", "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"
]


class AnalysisTaskBatchSetRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=200)
    max_concurrency: int = Field(default=2, ge=1, le=32)
    embedding_job_id: uuid.UUID | None = None
    coding_source: Literal["MATTER_METADATA"] = "MATTER_METADATA"
    use_batch_api: bool = False
    execution_profile_id: uuid.UUID | None = None

    @field_validator("idempotency_key")
    @classmethod
    def normalize_batch_set_run_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Idempotency key is required")
        return normalized

class AnalysisTaskBatchSetRunChildRead(ORMModel):
    id: uuid.UUID
    review_batch_id: uuid.UUID
    ordinal: int
    run_type: MatterAnalysisTaskType | None
    child_run_id: uuid.UUID | None
    status: AnalysisTaskBatchSetRunChildStatus
    attempt_count: int
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None


class AnalysisTaskBatchSetRunRead(ORMModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    analysis_task_batch_set_id: uuid.UUID
    matter_analysis_task_id: uuid.UUID
    matter_analysis_task_version_id: uuid.UUID
    execution_profile_id: uuid.UUID | None
    task_type: MatterAnalysisTaskType
    status: AnalysisTaskBatchSetRunStatus
    configuration_snapshot: dict[str, Any]
    idempotency_key: str
    workflow_id: str
    child_count: int
    completed_child_count: int
    failed_child_count: int
    created_by_user_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    execution: JobExecutionRead | None
    children: list[AnalysisTaskBatchSetRunChildRead] = Field(default_factory=list)


class MatterAnalysisTaskRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_type: Literal["MATTER", "REVIEW_BATCH"]
    scope_id: uuid.UUID
    embedding_job_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    matter_analysis_task_version_id: uuid.UUID | None = None

    @field_validator("idempotency_key")
    @classmethod
    def normalize_analysis_run_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Idempotency key is required")
        return normalized


class JobExecutionHistoryRead(BaseModel):
    job_type: str
    job_id: uuid.UUID
    current_execution: JobExecutionRead | None
    attempts: list[JobExecutionRead]


class FactDevelopmentRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_type: Literal["MATTER", "REVIEW_BATCH"] = "REVIEW_BATCH"
    review_batch_id: uuid.UUID | None = None
    coding_source: Literal["MATTER_METADATA", "REVIEWER_CODING"] = "MATTER_METADATA"
    reviewer_coding_id: uuid.UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    # Submit extraction calls through the provider's Batch API: lower cost, results in minutes to hours.
    use_batch_api: bool = False
    matter_analysis_task_version_id: uuid.UUID | None = None

    @field_validator("idempotency_key")
    @classmethod
    def normalize_fact_run_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("idempotency_key cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_fact_scope(self) -> "FactDevelopmentRunCreate":
        if self.scope_type == "REVIEW_BATCH" and self.review_batch_id is None:
            raise ValueError("REVIEW_BATCH scope requires review_batch_id")
        if self.scope_type == "MATTER" and self.review_batch_id is not None:
            raise ValueError("review_batch_id applies only to REVIEW_BATCH scope")
        if self.scope_type == "MATTER" and self.coding_source == "REVIEWER_CODING":
            raise ValueError("Reviewer coding applies only to REVIEW_BATCH scope")
        return self


class FactDevelopmentRunEstimateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_type: Literal["MATTER", "REVIEW_BATCH"] = "REVIEW_BATCH"
    review_batch_id: uuid.UUID | None = None
    coding_source: Literal["MATTER_METADATA", "REVIEWER_CODING"] = "MATTER_METADATA"
    reviewer_coding_id: uuid.UUID | None = None
    matter_analysis_task_version_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_fact_estimate_scope(self) -> "FactDevelopmentRunEstimateCreate":
        if self.scope_type == "REVIEW_BATCH" and self.review_batch_id is None:
            raise ValueError("REVIEW_BATCH scope requires review_batch_id")
        if self.scope_type == "MATTER" and self.review_batch_id is not None:
            raise ValueError("review_batch_id applies only to REVIEW_BATCH scope")
        if self.scope_type == "MATTER" and self.coding_source == "REVIEWER_CODING":
            raise ValueError("Reviewer coding applies only to REVIEW_BATCH scope")
        return self


class FactDevelopmentRunEstimateRead(BaseModel):
    """What a run launched now would process. Token figures are planning approximations, not billing figures."""

    scope_type: Literal["MATTER", "REVIEW_BATCH"]
    scope_id: uuid.UUID
    review_batch_id: uuid.UUID | None
    task_version: int
    coding_source: Literal["MATTER_METADATA", "REVIEWER_CODING"]
    reviewer_coding_revision: int | None
    batch_document_count: int
    matched_document_count: int
    excluded_document_count: int
    missing_text_document_count: int
    previously_extracted_count: int
    documents_to_extract: int
    text_bytes_to_extract: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    extraction_model: str
    # Whether the extraction model can run through a provider Batch API.
    batch_api_available: bool
    chunk_count: int
    issue_document_counts: list[FactIssueMembershipCount]


class FactDevelopmentRunRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    matter_analysis_task_id: uuid.UUID
    matter_analysis_task_version_id: uuid.UUID
    analysis_task_batch_set_id: uuid.UUID | None = None
    batch_set_ordinal: int | None = None
    run_kind: Literal["FACT_DEVELOPMENT"]
    scope_type: Literal["MATTER", "REVIEW_BATCH"]
    scope_id: uuid.UUID
    status: Literal[
        "QUEUED",
        "PREPARING",
        "EXTRACTING",
        "COMPLETED",
        "COMPLETED_WITH_ERRORS",
        "FAILED",
        "CANCELED",
    ]
    current_stage: str
    progress_completed: int
    progress_total: int
    population_document_count: int
    configuration_snapshot: dict[str, Any]
    report: dict[str, Any]
    failure_code: str | None
    failure_summary: str | None
    failure_retriable: bool
    created_by_user_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    execution: JobExecutionRead | None = None


class MatterAnalysisTaskRunRead(ORMModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    matter_analysis_task_id: uuid.UUID
    matter_analysis_task_version_id: uuid.UUID
    analysis_task_batch_set_id: uuid.UUID | None = None
    batch_set_ordinal: int | None = None
    scope_type: Literal["MATTER", "REVIEW_BATCH"]
    scope_id: uuid.UUID
    classification_model_id: uuid.UUID
    classification_model_version_id: uuid.UUID
    embedding_job_id: uuid.UUID
    dataset_snapshot_id: uuid.UUID | None
    classification_scoring_run_id: uuid.UUID | None
    status: Literal[
        "QUEUED",
        "SNAPSHOTTING",
        "PREPARING_FEATURES",
        "SCORING",
        "COMPLETED",
        "COMPLETED_WITH_ERRORS",
        "FAILED",
        "CANCELED",
    ]
    current_stage: str
    progress_completed: int
    progress_total: int
    population_document_count: int
    feature_processed_count: int
    feature_ready_count: int
    feature_missing_count: int
    feature_failed_count: int
    failure_code: str | None
    failure_summary: str | None
    failure_retriable: bool
    created_by_user_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    execution: JobExecutionRead | None = None


class ReviewBatchAnalysisJobRead(BaseModel):
    """One Analysis Task job whose frozen scope is a review batch."""

    id: uuid.UUID
    job_type: Literal["ANALYSIS_TASK_BATCH_RUN", "CLASSIFICATION_ANALYSIS_TASK"]
    task_type: Literal["QUESTION_ANSWERING", "CLASSIFICATION"]
    matter_analysis_task_id: uuid.UUID | None
    task_name: str
    task_version: int | None
    status: str
    created_at: datetime
    updated_at: datetime
    execution: JobExecutionRead | None = None
    review_batch_run: ReviewBatchRunRead | None = None
    classification_analysis_run: MatterAnalysisTaskRunRead | None = None

    @model_validator(mode="after")
    def validate_domain_run(self) -> "ReviewBatchAnalysisJobRead":
        populated = int(self.review_batch_run is not None) + int(self.classification_analysis_run is not None)
        if populated != 1:
            raise ValueError("Exactly one Analysis Task domain run is required")
        if self.task_type == "QUESTION_ANSWERING" and self.review_batch_run is None:
            raise ValueError("Question-answering jobs require a review batch run")
        if self.task_type == "CLASSIFICATION" and self.classification_analysis_run is None:
            raise ValueError("Classification jobs require a classification analysis run")
        return self


class MatterQuestionAnsweringJobRead(BaseModel):
    id: uuid.UUID
    scope_type: Literal["MATTER", "REVIEW_BATCH"] = "REVIEW_BATCH"
    scope_id: uuid.UUID
    review_batch_id: uuid.UUID | None
    review_batch_name: str
    matter_analysis_task_id: uuid.UUID | None
    task_name: str
    task_version: int | None
    decision_provider: Literal["JEV", "OPENAI_DECISIONS"]
    model_key: str
    status: ReviewBatchRunStatus
    processed_document_count: int
    successful_document_count: int
    document_count: int
    failed_document_count: int = 0
    retryable_failed_document_count: int = 0
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    execution: JobExecutionRead | None = None


class JobFailedDocumentRead(BaseModel):
    matter_document_id: uuid.UUID
    source_collection_id: uuid.UUID
    collection_item_id: uuid.UUID
    failure_code: str
    failure_message: str
    failure_retriable: bool
    attempt_number: int | None
    failed_at: datetime | None


class JobFailedDocumentPage(BaseModel):
    job_type: str
    job_id: uuid.UUID
    total: int
    retryable_count: int
    offset: int
    limit: int
    items: list[JobFailedDocumentRead]


class JobFailedDocumentRetryRequest(BaseModel):
    document_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=500)


class ReviewDecisionResultRead(ORMModel):
    id: uuid.UUID
    review_batch_run_id: uuid.UUID
    workflow_run_id: uuid.UUID
    matter_document_id: uuid.UUID
    matter_analysis_task_version_id: uuid.UUID
    definition_content_hash: str
    specification_content_hash: str
    source_artifact_id: uuid.UUID
    source_content_hash: str
    state_content_hash: str
    question_set_hash: str
    decision_policy_hash: str
    paragraph_map_version: str
    status: Literal["COMPLETED", "PARTIAL", "FAILED", "SKIPPED"]
    coverage: dict[str, Any]
    answers: dict[str, DecisionAnswer]
    recommendations: dict[str, Any]
    routes: dict[str, Any]
    evidence: dict[str, Any]
    raw_answer_hash: str | None
    policy_evaluation_hash: str | None
    engine_key: str | None
    provider: str | None
    model: str | None
    provider_request_id: str | None
    provider_metadata: dict[str, Any]
    evaluation_skill_run_id: uuid.UUID | None
    evidence_skill_run_id: uuid.UUID | None
    model_invocation_id: uuid.UUID | None
    reused_from_result_id: uuid.UUID | None
    attempts: int
    request_count: int
    input_tokens: int
    output_tokens: int
    latency_ms: int
    error_code: str | None
    error_message: str | None
    started_at: datetime
    completed_at: datetime
    created_at: datetime


AgentConversationWorkflow = Literal["MATTER_DEFINITION_SETUP", "BATCH_CHAT", "SEARCH_CHAT"]
AgentConversationStatus = Literal["ACTIVE", "WAITING_APPROVAL", "FAILED", "ARCHIVED"]
AgentExecutionStatus = Literal["QUEUED", "RUNNING", "WAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELED"]
AgentActionStatus = Literal["PENDING", "APPROVED", "REJECTED", "EXECUTED", "FAILED"]
AgentActionDecisionValue = Literal["APPROVE", "REJECT"]


class AgentConversationCreate(BaseModel):
    agent_definition_id: uuid.UUID
    title: AgentConversationTitle | None = None
    workflow_type: AgentConversationWorkflow = "MATTER_DEFINITION_SETUP"
    review_batch_id: uuid.UUID | None = None
    matter_definition_id: uuid.UUID | None = None
    search_definition: MatterSearchRequest | None = None

    @model_validator(mode="after")
    def validate_workflow_scope(self) -> "AgentConversationCreate":
        if self.workflow_type == "BATCH_CHAT" and self.review_batch_id is None:
            raise ValueError("BATCH_CHAT conversations require review_batch_id")
        if self.workflow_type != "BATCH_CHAT" and self.review_batch_id is not None:
            raise ValueError("review_batch_id is supported only for BATCH_CHAT conversations")
        if self.workflow_type == "BATCH_CHAT" and self.matter_definition_id is not None:
            raise ValueError("matter_definition_id is supported only for Matter Definition conversations")
        if self.workflow_type == "SEARCH_CHAT" and self.search_definition is None:
            raise ValueError("SEARCH_CHAT conversations require search_definition")
        if self.workflow_type != "SEARCH_CHAT" and self.search_definition is not None:
            raise ValueError("search_definition is supported only for SEARCH_CHAT conversations")
        if self.workflow_type == "SEARCH_CHAT" and self.matter_definition_id is not None:
            raise ValueError("matter_definition_id is supported only for Matter Definition conversations")
        return self


class AgentConversationUpdate(BaseModel):
    title: AgentConversationTitle


class AgentConversationRead(ORMModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    client_id: uuid.UUID
    matter_id: uuid.UUID
    matter_definition_id: uuid.UUID | None
    review_batch_id: uuid.UUID | None
    search_definition: MatterSearchRequest | None
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
    target_document_count: int = Field(
        default=500,
        ge=1,
        le=10_000_000,
        validation_alias=AliasChoices("target_document_count", "maximum_document_count"),
    )
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
    guidance_id: uuid.UUID | None = None
    guidance_key: str | None = None
    guidance_name: str | None = None
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
    guidance_refinement_status: Literal["NOT_READY", "QUEUED", "RUNNING", "COMPLETED", "NOT_REQUIRED", "FAILED"]
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


class ClassificationModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_metadata_definition_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Classification model name is required")
        return normalized


class ClassificationModelConfigurationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification_strategy: ClassificationStrategy
    hierarchy_level: int | None = Field(default=None, ge=1, le=32)
    ordered_class_keys: list[ClassValue] = Field(default_factory=list, max_length=1000)
    binary_mapping: BinaryClassMapping | None = None
    label_policy: LabelPolicy
    feature_contract: FeatureContract
    prediction_resolution_policy: PredictionResolutionPolicy
    trainer_specification: TrainerSpecification


class ClassificationTargetFieldRead(BaseModel):
    metadata_definition_id: uuid.UUID
    stable_key: str
    display_name: str
    metadata_type: TargetMetadataType
    cardinality: TargetCardinality
    hierarchy_separator: str | None
    status: ResourceStatus
    reviewable: bool
    active_value_keys: list[str]
    schema_fingerprint: str


class ClassificationModelConfigurationRead(BaseModel):
    id: uuid.UUID
    classification_model_id: uuid.UUID
    revision: int
    target_field_snapshot: TargetFieldSnapshot
    classification_strategy: ClassificationStrategy
    hierarchy_level: int | None
    ordered_class_keys: list[ClassValue]
    binary_mapping: BinaryClassMapping | None
    label_policy: LabelPolicy
    feature_contract: FeatureContract
    prediction_resolution_policy: PredictionResolutionPolicy
    trainer_specification: TrainerSpecification
    configuration_hash: str
    created_at: datetime
    created_by_user_id: uuid.UUID


class ClassificationModelRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    target_metadata_definition_id: uuid.UUID
    name: str
    description: str | None
    status: ResourceStatus
    current_configuration_id: uuid.UUID | None
    active_model_version_id: uuid.UUID | None
    target_field: ClassificationTargetFieldRead
    current_configuration: ClassificationModelConfigurationRead | None
    created_at: datetime
    created_by_user_id: uuid.UUID
    updated_at: datetime
    updated_by_user_id: uuid.UUID
    row_version: int


class ClassificationModelCompatibilityRead(BaseModel):
    classification_model_id: uuid.UUID
    configuration_id: uuid.UUID | None
    compatible: bool
    schema_fingerprint_matches: bool | None
    current_target_field: ClassificationTargetFieldRead
    configured_target_field_snapshot: TargetFieldSnapshot | None
    reasons: list[str]


class ClassificationDatasetSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration_id: uuid.UUID
    purpose: Literal["TRAINING", "SCORING"] = "TRAINING"
    training_scope: TrainingScopeSpecification
    embedding_job_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_snapshot_purpose(self) -> "ClassificationDatasetSnapshotRequest":
        if self.purpose == "SCORING" and self.embedding_job_id is None:
            raise ValueError("Scoring snapshots require an embedding job")
        if self.purpose == "TRAINING" and self.embedding_job_id is not None:
            raise ValueError("Training snapshots receive their embedding job during feature preparation")
        return self


class ClassificationTargetDistributionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    training_scope: TrainingScopeSpecification
    hierarchy_level: int | None = Field(default=None, ge=1, le=32)


class ClassificationClassCount(BaseModel):
    class_key: ClassValue
    label: str
    document_count: int = Field(ge=0)


class ClassificationTargetDistributionRead(BaseModel):
    target_field: TargetFieldSnapshot
    training_scope: TrainingScopeSpecification
    population_count: int = Field(ge=0)
    labeled_document_count: int = Field(ge=0)
    unlabeled_document_count: int = Field(ge=0)
    conflicting_document_count: int = Field(ge=0)
    class_counts: list[ClassificationClassCount]


class ClassificationFeatureSplitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    embedding_job_id: uuid.UUID
    validation_policy: ValidationPolicy = Field(default_factory=ValidationPolicy)
    random_seed: int = Field(default=42, ge=0, le=2**32 - 1)


class ClassificationTrainingJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration_id: uuid.UUID
    training_scope: TrainingScopeSpecification
    embedding_job_id: uuid.UUID
    validation_policy: ValidationPolicy = Field(default_factory=ValidationPolicy)
    random_seed: int = Field(default=42, ge=0, le=2**32 - 1)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_training_job_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Idempotency key is required")
        return normalized


class ClassificationDatasetIssue(BaseModel):
    code: str
    message: str
    count: int | None = None


class ClassificationClassExclusion(BaseModel):
    class_key: ClassValue
    reason_code: Literal[
        "INSUFFICIENT_POSITIVE_DOCUMENTS",
        "INSUFFICIENT_POSITIVE_GROUPS",
        "INSUFFICIENT_NEGATIVE_DOCUMENTS",
        "INSUFFICIENT_NEGATIVE_GROUPS",
        "FOLD_SUPPORT_INFEASIBLE",
    ]
    message: str
    feature_ready_positive_count: int
    positive_group_count: int
    feature_ready_negative_count: int
    negative_group_count: int
    required_positive_count: int
    required_group_count: int
    fold_support: list[int] | None = None
    fold_negative_support: list[int] | None = None


class ClassificationLabelSourceSummary(BaseModel):
    kind: str
    source_reference_id: uuid.UUID | None
    authority: str
    source_weight: float
    claim_count: int
    selected_count: int
    selected_weight_total: float
    excluded_blinded: bool = False


class ClassificationDatasetSnapshotPreview(BaseModel):
    classification_model_id: uuid.UUID
    configuration_id: uuid.UUID
    configuration_hash: Sha256
    purpose: Literal["TRAINING", "SCORING"]
    training_scope: TrainingScopeSpecification
    target_field_snapshot: TargetFieldSnapshot
    population_count: int
    labeled_count: int
    unlabeled_count: int
    excluded_count: int
    conflicting_count: int
    family_grouped_count: int
    exact_duplicate_grouped_count: int
    missing_grouping_count: int
    ordered_class_keys: list[ClassValue]
    class_support: dict[str, int]
    source_summary: list[ClassificationLabelSourceSummary]
    population_content_hash: Sha256
    label_content_hash: Sha256 | None
    aggregate_hash: Sha256
    blocking_errors: list[ClassificationDatasetIssue]
    warnings: list[ClassificationDatasetIssue]
    can_materialize: bool


class ClassificationDatasetSnapshotRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    classification_model_id: uuid.UUID
    configuration_id: uuid.UUID
    purpose: Literal["TRAINING", "SCORING"]
    training_scope: TrainingScopeSpecification
    target_field_snapshot: TargetFieldSnapshot
    label_policy_snapshot: LabelPolicy
    analysis_input_manifest_id: uuid.UUID | None
    population_artifact_id: uuid.UUID | None
    population_content_hash: Sha256 | None
    label_artifact_id: uuid.UUID | None
    label_content_hash: Sha256 | None
    feature_artifact_id: uuid.UUID | None
    feature_content_hash: Sha256 | None
    feature_specification: dict[str, Any] | None
    feature_specification_hash: Sha256 | None
    split_artifact_id: uuid.UUID | None
    split_content_hash: Sha256 | None
    split_specification: dict[str, Any] | None
    split_specification_hash: Sha256 | None
    training_aggregate_hash: Sha256 | None
    aggregate_hash: Sha256
    status: Literal["BUILDING", "READY_FOR_FEATURES", "READY_FOR_TRAINING", "READY_FOR_SCORING", "FAILED"]
    population_count: int
    labeled_count: int
    unlabeled_count: int
    excluded_count: int
    conflicting_count: int
    family_grouped_count: int
    exact_duplicate_grouped_count: int
    missing_grouping_count: int
    feature_ready_count: int
    feature_missing_count: int
    feature_failed_count: int
    training_eligible_count: int
    ordered_class_keys: list[ClassValue]
    trained_class_keys: list[ClassValue]
    excluded_classes: list[ClassificationClassExclusion]
    class_support: dict[str, int]
    source_summary: list[ClassificationLabelSourceSummary]
    warnings: list[ClassificationDatasetIssue]
    failure_code: str | None
    failure_summary: str | None
    created_by_user_id: uuid.UUID
    created_at: datetime
    completed_at: datetime | None


class ClassificationDatasetSnapshotCreateResponse(BaseModel):
    snapshot: ClassificationDatasetSnapshotRead
    created: bool


class ClassificationFeatureSplitCreateResponse(BaseModel):
    snapshot: ClassificationDatasetSnapshotRead
    materialized: bool


ClassificationTrainingRunStatus = Literal[
    "QUEUED",
    "SNAPSHOTTING",
    "WAITING_FOR_RUNTIME",
    "TRAINING",
    "PACKAGING",
    "EVALUATING",
    "PUBLISHING",
    "FINALIZING",
    "RECONCILING",
    "COMPLETED",
    "COMPLETED_WITH_ERRORS",
    "FAILED",
    "CANCELED",
]


class ClassificationModelBuildPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_snapshot_id: uuid.UUID


class ClassificationModelBuildCreate(ClassificationModelBuildPreviewRequest):
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Idempotency key is required")
        return normalized


class ClassificationModelBuildPreview(BaseModel):
    classification_model_id: uuid.UUID
    configuration_id: uuid.UUID
    dataset_snapshot_id: uuid.UUID
    configuration_hash: Sha256
    training_aggregate_hash: Sha256 | None
    feature_specification_hash: Sha256 | None
    validation_specification: ValidationPolicy
    random_seed: int
    runtime_image_digest: str
    source_revision: str
    package_schema_version: int
    derivation_key: Sha256
    training_eligible_count: int
    can_launch: bool
    blocking_errors: list[ClassificationDatasetIssue]
    warnings: list[ClassificationDatasetIssue]
    existing_training_run_id: uuid.UUID | None = None


class ClassificationModelVersionRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    classification_model_id: uuid.UUID
    configuration_id: uuid.UUID
    training_run_id: uuid.UUID
    dataset_snapshot_id: uuid.UUID
    version_number: int
    status: Literal["CANDIDATE", "APPROVED", "REJECTED", "RETIRED"]
    target_field_snapshot: TargetFieldSnapshot
    ordered_class_keys: list[ClassValue]
    observed_class_keys: list[ClassValue]
    trained_class_keys: list[ClassValue]
    excluded_classes: list[ClassificationClassExclusion]
    training_aggregate_hash: Sha256
    feature_specification_hash: Sha256
    model_artifact_id: uuid.UUID
    model_content_hash: Sha256
    prediction_artifact_id: uuid.UUID
    prediction_content_hash: Sha256
    evaluation_artifact_id: uuid.UUID
    evaluation_content_hash: Sha256
    result_manifest_artifact_id: uuid.UUID
    result_manifest_content_hash: Sha256
    package_schema_version: int
    runtime_image_digest: str
    source_revision: str
    metric_summary: dict[str, float | int | None]
    warnings: list[str]
    created_at: datetime
    created_by_user_id: uuid.UUID


ClassificationModelDecisionType = Literal[
    "APPROVAL",
    "REJECTION",
    "ACTIVATION",
    "ROLLBACK",
    "RETIREMENT",
]
ClassificationPromotionAction = Literal["ACTIVATION", "ROLLBACK"]


class ClassificationModelDecisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=4000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Decision reason is required")
        return normalized


class ClassificationModelDecisionRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    classification_model_id: uuid.UUID
    model_version_id: uuid.UUID
    decision_type: ClassificationModelDecisionType
    workflow_id: str | None
    reason: str
    policy_snapshot: dict[str, Any]
    metric_snapshot: dict[str, Any]
    before_active_model_version_id: uuid.UUID | None
    after_active_model_version_id: uuid.UUID | None
    authorization_context: dict[str, Any]
    actor_user_id: uuid.UUID
    created_at: datetime


class ClassificationPromotionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_version_id: uuid.UUID


class ClassificationPromotionPreview(BaseModel):
    action: ClassificationPromotionAction
    classification_model_id: uuid.UUID
    model_version: ClassificationModelVersionRead
    current_active_model_version_id: uuid.UUID | None
    expected_model_row_version: int
    preview_token: str
    can_promote: bool
    blocking_reasons: list[str]
    policy_snapshot: dict[str, Any]
    metric_snapshot: dict[str, Any]


class ClassificationPromotionCreate(ClassificationPromotionPreviewRequest):
    expected_model_row_version: int = Field(ge=1)
    preview_token: str = Field(min_length=64, max_length=64)
    reason: str = Field(min_length=1, max_length=4000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Promotion reason is required")
        return normalized


class ClassificationPromotionResponse(BaseModel):
    action: ClassificationPromotionAction
    workflow_id: str
    status: Literal["QUEUED", "COMPLETED"]
    classification_model_id: uuid.UUID
    model_version_id: uuid.UUID
    decision: ClassificationModelDecisionRead | None = None
    model: ClassificationModelRead | None = None


class ClassificationTrainingRunRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    classification_model_id: uuid.UUID
    configuration_id: uuid.UUID
    dataset_snapshot_id: uuid.UUID
    idempotency_key: str
    workflow_id: str
    attempt_number: int
    derivation_key: Sha256
    trainer_specification: TrainerSpecification
    validation_specification: ValidationPolicy
    random_seed: int
    package_schema_version: int
    runtime_image_digest: str
    source_revision: str
    status: ClassificationTrainingRunStatus
    current_stage: str
    progress_completed: int
    progress_total: int
    model_artifact_id: uuid.UUID | None
    model_content_hash: Sha256 | None
    prediction_artifact_id: uuid.UUID | None
    prediction_content_hash: Sha256 | None
    evaluation_artifact_id: uuid.UUID | None
    evaluation_content_hash: Sha256 | None
    result_manifest_artifact_id: uuid.UUID | None
    result_manifest_content_hash: Sha256 | None
    failure_code: str | None
    failure_summary: str | None
    failure_retriable: bool
    model_version: ClassificationModelVersionRead | None = None
    created_by_user_id: uuid.UUID
    execution_actor_user_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    row_version: int


class ClassificationModelBuildCreateResponse(BaseModel):
    training_run: ClassificationTrainingRunRead
    created: bool


ClassificationTrainingJobStatus = Literal[
    "QUEUED",
    "SNAPSHOTTING",
    "PREPARING_FEATURES",
    "LAUNCHING",
    "TRAINING",
    "FINALIZING",
    "COMPLETED",
    "FAILED",
    "CANCELED",
]


class ClassificationTrainingJobRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    classification_model_id: uuid.UUID
    configuration_id: uuid.UUID
    embedding_job_id: uuid.UUID
    dataset_snapshot_id: uuid.UUID | None
    training_run_id: uuid.UUID | None
    idempotency_key: str
    workflow_id: str
    training_scope: TrainingScopeSpecification
    validation_specification: ValidationPolicy
    random_seed: int
    status: ClassificationTrainingJobStatus
    current_stage: str
    progress_completed: int
    progress_total: int
    failure_code: str | None
    failure_summary: str | None
    failure_retriable: bool
    dataset_snapshot: ClassificationDatasetSnapshotRead | None = None
    training_run: ClassificationTrainingRunRead | None = None
    created_by_user_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    execution: JobExecutionRead | None = None


class ClassificationTrainingJobCreateResponse(BaseModel):
    training_job: ClassificationTrainingJobRead
    created: bool


ClassificationScoringRunStatus = Literal[
    "QUEUED",
    "VALIDATING",
    "WAITING_FOR_RUNTIME",
    "SCORING",
    "PUBLISHING",
    "FINALIZING",
    "RECONCILING",
    "COMPLETED",
    "COMPLETED_WITH_ERRORS",
    "FAILED",
    "CANCELED",
]


class ClassificationScoringPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_snapshot_id: uuid.UUID
    model_version_id: uuid.UUID | None = None


class ClassificationScoringCreate(ClassificationScoringPreviewRequest):
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Idempotency key is required")
        return normalized


class ClassificationScoringPreview(BaseModel):
    classification_model_id: uuid.UUID
    model_version_id: uuid.UUID | None
    dataset_snapshot_id: uuid.UUID
    configuration_hash: Sha256 | None
    model_content_hash: Sha256 | None
    feature_content_hash: Sha256 | None
    feature_specification_hash: Sha256 | None
    scoring_policy: PredictionResolutionPolicy | None
    scoring_policy_hash: Sha256 | None
    derivation_key: Sha256 | None
    population_document_count: int
    can_launch: bool
    blocking_errors: list[ClassificationDatasetIssue]
    warnings: list[ClassificationDatasetIssue]
    existing_scoring_run_id: uuid.UUID | None = None


class ClassificationScoringRunRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    classification_model_id: uuid.UUID
    model_version_id: uuid.UUID
    dataset_snapshot_id: uuid.UUID
    dataset_scope_type: Literal["MATTER", "REVIEW_BATCH"]
    dataset_scope_id: uuid.UUID
    idempotency_key: str
    workflow_id: str
    derivation_key: Sha256
    configuration_hash: Sha256
    model_content_hash: Sha256
    feature_content_hash: Sha256
    feature_specification_hash: Sha256
    scoring_policy: PredictionResolutionPolicy
    scoring_policy_hash: Sha256
    status: ClassificationScoringRunStatus
    current_stage: str
    progress_completed: int
    progress_total: int
    population_document_count: int
    scored_document_count: int
    abstained_document_count: int
    excluded_document_count: int
    failed_document_count: int
    prediction_artifact_id: uuid.UUID | None
    prediction_content_hash: Sha256 | None
    result_manifest_artifact_id: uuid.UUID | None
    result_manifest_content_hash: Sha256 | None
    failure_code: str | None
    failure_summary: str | None
    failure_retriable: bool
    created_by_user_id: uuid.UUID
    execution_actor_user_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    row_version: int
    execution: JobExecutionRead | None = None


class ClassificationScoringCreateResponse(BaseModel):
    scoring_run: ClassificationScoringRunRead
    created: bool


class ClassificationPredictionRowRead(BaseModel):
    matter_document_id: uuid.UUID
    collection_item_id: uuid.UUID | None
    scope_ordinal: int | None
    source_vector_content_hash: Sha256 | None
    result_status: Literal["SUCCESS", "EXCLUDED", "FAILED"]
    failure_reason: str | None
    probabilities: list[float]
    predicted_class_keys: list[str]
    abstained: bool
    probability_margin: float | None
    uncertainty: float | None


class ClassificationPredictionPreviewRead(BaseModel):
    scoring_run_id: uuid.UUID
    model_version_id: uuid.UUID
    ordered_class_keys: list[str]
    total: int
    offset: int
    limit: int
    items: list[ClassificationPredictionRowRead]


class ClassificationPredictionClassCount(BaseModel):
    class_key: ClassValue
    label: str
    document_count: int = Field(ge=0)


class ClassificationPredictionSummaryRead(BaseModel):
    scoring_run_id: uuid.UUID
    model_version_id: uuid.UUID
    ordered_class_keys: list[ClassValue]
    minimum_probability: float | None = Field(ge=0, le=1)
    total_document_count: int = Field(ge=0)
    evaluated_document_count: int = Field(ge=0)
    predicted_document_count: int = Field(ge=0)
    no_prediction_document_count: int = Field(ge=0)
    predicted_value_count: int = Field(ge=0)
    abstained_document_count: int = Field(ge=0)
    class_counts: list[ClassificationPredictionClassCount]


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


def normalize_email(email: str) -> str:
    return email.strip().casefold()


EntityTypeLiteral = Literal["PERSON", "ORGANIZATION"]
FactDatePrecision = Literal["DAY", "MONTH", "QUARTER", "YEAR", "RANGE", "UNKNOWN"]
FactDateBasis = Literal["STATED_IN_TEXT", "DOCUMENT_DATE", "INFERRED"]
FactParticipantRole = Literal["PARTICIPANT_ACTOR", "PARTICIPANT_RECIPIENT"]
FactStatus = Literal["WORKING", "CONFIRMED", "REJECTED", "MERGED"]


class MatterEntityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: EntityTypeLiteral
    display_name: str | None = Field(default=None, min_length=1, max_length=300)
    email: str | None = Field(default=None, max_length=320)
    title: str | None = Field(default=None, max_length=200)
    organization_id: uuid.UUID | None = None
    custodian_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=10_000)
    status: Literal["PROPOSED", "CONFIRMED"] = "CONFIRMED"

    @model_validator(mode="after")
    def validate_identity(self) -> "MatterEntityCreate":
        if not self.display_name and not self.email:
            raise ValueError("An entity requires a display name or an email address")
        return self


class MatterEntityUpdate(BaseModel):
    """Only fields present in the request are changed."""

    model_config = ConfigDict(extra="forbid")

    lock_version: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=300)
    title: str | None = Field(default=None, max_length=200)
    organization_id: uuid.UUID | None = None
    custodian_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=10_000)


class MatterEntityVariantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lock_version: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=300)
    email: str | None = Field(default=None, max_length=320)


class MatterEntityAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lock_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=4_000)


class MatterEntityMerge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lock_version: int = Field(ge=1)
    target_entity_id: uuid.UUID


class MatterEntityVariantRead(ORMModel):
    id: uuid.UUID
    display_name: str | None
    email: str | None
    lock_version: int
    created_at: datetime


class MatterEntityRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    entity_type: EntityTypeLiteral
    display_name: str | None
    email: str | None
    title: str | None
    organization_id: uuid.UUID | None
    custodian_id: uuid.UUID | None
    description: str | None
    status: Literal["PROPOSED", "CONFIRMED", "REJECTED"]
    lock_version: int
    created_by_user_id: uuid.UUID | None
    created_by_workflow_run_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    variants: list[MatterEntityVariantRead] = Field(default_factory=list)
    fact_count: int = 0


class MatterEntityPage(BaseModel):
    items: list[MatterEntityRead]
    total: int


class FactIssueRefModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matter_analysis_task_id: uuid.UUID
    issue_key: str = Field(min_length=1, max_length=100)


class FactParticipantModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: uuid.UUID
    role: FactParticipantRole


class FactEvidenceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matter_document_id: uuid.UUID
    paragraph_id: str = Field(min_length=1, max_length=50)
    quote: str = Field(min_length=1, max_length=5_000)
    occurrence: int = Field(default=1, ge=1, le=1_000)
    stance: Literal["SUPPORTS", "CONTRADICTS"] = "SUPPORTS"


class MatterFactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=4_000)
    date_precision: FactDatePrecision = "UNKNOWN"
    event_date_start: date | None = None
    event_date_end: date | None = None
    date_basis: FactDateBasis | None = None
    owner_entity_id: uuid.UUID | None = None
    issues: list[FactIssueRefModel] = Field(min_length=1, max_length=100)
    participants: list[FactParticipantModel] = Field(default_factory=list, max_length=200)
    evidence: list[FactEvidenceCreate] = Field(min_length=1, max_length=50)
    key_fact: bool = False


class MatterFactUpdate(BaseModel):
    """Only fields present in the request are changed; each content change appends a revision."""

    model_config = ConfigDict(extra="forbid")

    lock_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=4_000)
    summary: str | None = Field(default=None, min_length=1, max_length=4_000)
    date_precision: FactDatePrecision | None = None
    event_date_start: date | None = None
    event_date_end: date | None = None
    date_basis: FactDateBasis | None = None
    owner_entity_id: uuid.UUID | None = None
    issues: list[FactIssueRefModel] | None = Field(default=None, min_length=1, max_length=100)
    participants: list[FactParticipantModel] | None = Field(default=None, max_length=200)
    key_fact: bool | None = None


class MatterFactAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lock_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=4_000)


class MatterFactEvidenceAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lock_version: int = Field(ge=1)
    evidence: FactEvidenceCreate


class MatterFactMerge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lock_version: int = Field(ge=1)
    target_fact_id: uuid.UUID
    target_lock_version: int = Field(ge=1)


class MatterFactEntityRef(BaseModel):
    entity_id: uuid.UUID
    canonical_entity_id: uuid.UUID
    display_name: str
    canonical_display_name: str
    entity_type: EntityTypeLiteral
    role: FactParticipantRole | None = None


class MatterFactEvidenceRead(ORMModel):
    id: uuid.UUID
    matter_document_id: uuid.UUID
    control_number: str | None = None
    source_artifact_id: uuid.UUID
    source_content_hash: str
    paragraph_map_version: str
    paragraph_id: str
    char_start: int
    char_end: int
    quote: str
    stance: Literal["SUPPORTS", "CONTRADICTS"]
    status: Literal["ACTIVE", "REMOVED"]
    added_by_user_id: uuid.UUID | None
    workflow_run_id: uuid.UUID | None
    created_at: datetime
    removed_at: datetime | None


class MatterFactRevisionRead(ORMModel):
    revision: int
    summary: str
    event_date_start: date | None
    event_date_end: date | None
    date_precision: FactDatePrecision
    date_basis: FactDateBasis | None
    owner_entity_id: uuid.UUID | None
    issues: list[dict[str, Any]]
    participants: list[dict[str, Any]]
    author_type: Literal["HUMAN", "AGENT"]
    author_user_id: uuid.UUID | None
    workflow_run_id: uuid.UUID | None
    reason: str | None
    created_at: datetime


class MatterFactEventRead(ORMModel):
    id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    actor_type: Literal["HUMAN", "AGENT", "SYSTEM"]
    actor_user_id: uuid.UUID | None
    workflow_run_id: uuid.UUID | None
    created_at: datetime


class MatterFactRead(BaseModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    status: FactStatus
    summary: str
    event_date_start: date | None
    event_date_end: date | None
    date_precision: FactDatePrecision
    date_basis: FactDateBasis | None
    owner: MatterFactEntityRef | None
    participants: list[MatterFactEntityRef]
    issues: list[FactIssueRefModel]
    key_fact: bool
    has_contradiction: bool
    evidence_count: int
    document_count: int
    pending_suggestion_count: int = 0
    merged_into_fact_id: uuid.UUID | None
    confirmed_by_user_id: uuid.UUID | None
    confirmed_at: datetime | None
    created_by_user_id: uuid.UUID | None
    origin_workflow_run_id: uuid.UUID | None
    current_revision: int
    lock_version: int
    created_at: datetime
    updated_at: datetime


class MatterFactDetailRead(MatterFactRead):
    evidence: list[MatterFactEvidenceRead]
    revisions: list[MatterFactRevisionRead]
    events: list[MatterFactEventRead]


class MatterFactSuggestionRead(ORMModel):
    id: uuid.UUID
    fact_id: uuid.UUID
    kind: Literal["REVISION", "MERGE"]
    status: Literal["PENDING", "ACCEPTED", "DISMISSED"]
    base_revision: int
    changes: dict[str, Any]
    evidence: list[dict[str, Any]]
    target_fact_id: uuid.UUID | None
    rationale: str | None
    created_by_workflow_run_id: uuid.UUID | None
    resolved_by_user_id: uuid.UUID | None
    resolved_at: datetime | None
    resolution_note: str | None
    created_at: datetime


class MatterFactSuggestionAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lock_version: int = Field(ge=1)
    target_lock_version: int | None = Field(default=None, ge=1)


class MatterFactSuggestionDismiss(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=4_000)


class MatterDocumentFactRead(BaseModel):
    """A live fact that cites one document, with only the quotes taken from that document."""

    fact: MatterFactRead
    quotes: list[MatterFactEvidenceRead]


class FactIssueCatalogEntry(BaseModel):
    matter_analysis_task_id: uuid.UUID
    task_name: str
    issue_key: str
    name: str
    description: str | None


class MatterFactPage(BaseModel):
    items: list[MatterFactRead]
    total: int


class MatterFactExportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: FactFilters = Field(default_factory=FactFilters)
    requested_filename: str = Field(default="facts", min_length=1, max_length=150)
    spreadsheet_safe: bool = True
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_fact_export_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("idempotency_key must not be blank")
        return normalized


class MatterFactExportRead(ORMModel):
    id: uuid.UUID
    matter_id: uuid.UUID
    filter_snapshot: dict[str, Any]
    csv_policy_snapshot: dict[str, Any]
    requested_filename: str
    fact_count: int
    evidence_count: int
    facts_filename: str
    facts_byte_length: int
    evidence_filename: str
    evidence_byte_length: int
    snapshot_started_at: datetime
    expires_at: datetime
    created_by_user_id: uuid.UUID
    created_at: datetime


class MatterFactSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: FactFilters = Field(default_factory=FactFilters)
    sort: FactSort = "DATE_ASC"
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class MatterFactFacetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: FactFilters = Field(default_factory=FactFilters)
    facet: FacetName
    size: int = Field(default=50, ge=1, le=500)


class MatterFactFacetBucket(BaseModel):
    value: str | bool | uuid.UUID
    label: str
    count: int


class MatterFactFacetRead(BaseModel):
    facet: str
    buckets: list[MatterFactFacetBucket]


class MatterFactHistogramRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: FactFilters = Field(default_factory=FactFilters)
    interval: Literal["YEAR", "QUARTER", "MONTH"] = "MONTH"


class MatterFactHistogramBucket(BaseModel):
    start: date
    count: int


class MatterFactHistogramRead(BaseModel):
    interval: Literal["YEAR", "QUARTER", "MONTH"]
    buckets: list[MatterFactHistogramBucket]
    undated_count: int


ReviewProgramKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[a-z][a-z0-9_-]{1,98}[a-z0-9]$"),
]
ReviewProgramStatus = Literal["DRAFT", "ACTIVE", "SUSPENDED", "ARCHIVED"]
ReviewProgramBatchingPolicy = Literal["NONE", "AD_HOC_BATCH", "PARTITIONED_SET", "WHOLE_MATTER"]


class ReviewWorkflowPhaseRead(BaseModel):
    key: str
    name: str
    ordinal: int


class ReviewWorkflowStageRead(BaseModel):
    key: str
    name: str
    phase_key: str
    ordinal: int
    batching_policy: ReviewProgramBatchingPolicy
    requires_human_gate: bool


class ReviewWorkflowTemplateRead(BaseModel):
    key: str
    version: int
    schema_version: int
    content_hash: str
    name: str
    description: str
    workflow_type: str
    phases: list[ReviewWorkflowPhaseRead]
    stages: list[ReviewWorkflowStageRead]


class ReviewProgramCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_key: str = Field(default="FIRST_PASS_REVIEW", min_length=1, max_length=100)
    key: ReviewProgramKey
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4_000)
    owner_user_id: uuid.UUID | None = None
    matter_definition_id: uuid.UUID | None = None


class ReviewProgramVersionRead(ORMModel):
    id: uuid.UUID
    program_id: uuid.UUID
    version: int
    predecessor_version_id: uuid.UUID | None
    template_key: str
    template_version: int
    template_content_hash: str
    configuration_stage: str
    configuration_snapshot: dict[str, Any]
    source_hashes: dict[str, Any]
    content_hash: str
    created_by_user_id: uuid.UUID
    created_at: datetime
    published_at: datetime


class ReviewProgramRead(ORMModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    template_key: str
    template_version: int
    template_content_hash: str
    key: str
    name: str
    description: str | None
    status: ReviewProgramStatus
    matter_definition_id: uuid.UUID | None
    target_metadata_definition_id: uuid.UUID | None
    current_version_id: uuid.UUID | None
    current_run_id: uuid.UUID | None
    owner_user_id: uuid.UUID
    created_by_user_id: uuid.UUID
    row_version: int
    created_at: datetime
    updated_at: datetime
    current_version: ReviewProgramVersionRead | None = None


ReviewProgramRunStatus = Literal[
    "QUEUED",
    "RUNNING",
    "WAITING_FOR_USER",
    "COMPLETED",
    "COMPLETED_WITH_ERRORS",
    "FAILED",
    "CANCELED",
]


class ReviewProgramRunStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=500)


class ReviewProgramRunCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=500)
    reason: str | None = Field(default=None, max_length=4_000)


class ReviewProgramActionResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_hash: str = Field(min_length=64, max_length=64)
    choice_key: str = Field(min_length=1, max_length=100)
    reason: str | None = Field(default=None, max_length=4_000)
    input: dict[str, Any] = Field(default_factory=dict)


class ReviewProgramDefinitionSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_hash: str = Field(min_length=64, max_length=64)
    mode: Literal["CREATE", "CLONE", "ADOPT"]
    existing_definition_id: uuid.UUID | None = None
    source_revision: int | None = Field(default=None, ge=1)
    definition_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_-]*$",
    )
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    content_markdown: str | None = Field(default=None, min_length=1, max_length=2_000_000)
    assessment_target_document_count: int = Field(default=500, ge=1, le=10_000_000)
    assessment_control_sample_size: int = Field(default=0, ge=0, le=1_000_000)
    assessment_use_batching: bool = True
    acknowledge_large_run_warning: bool = False

    @model_validator(mode="after")
    def validate_setup(self) -> "ReviewProgramDefinitionSetup":
        if self.assessment_control_sample_size > self.assessment_target_document_count:
            raise ValueError("Control sample size cannot exceed the assessment target")
        if self.mode == "CREATE" and not (
            self.definition_key and self.name and (self.content_markdown or "").strip()
        ):
            raise ValueError("CREATE requires definition_key, name, and content_markdown")
        if self.mode in {"CLONE", "ADOPT"} and self.existing_definition_id is None:
            raise ValueError(f"{self.mode} requires existing_definition_id")
        if self.mode == "CLONE" and not (self.definition_key and self.name):
            raise ValueError("CLONE requires definition_key and name")
        return self


class ReviewProgramQASetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_hash: str = Field(min_length=64, max_length=64)
    task_key: MetadataKey
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    destination_metadata_definition_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)

    @field_validator("destination_metadata_definition_ids")
    @classmethod
    def validate_unique_qa_destinations(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(value) != len(set(value)):
            raise ValueError("Destination metadata fields must be unique")
        return value

    @field_validator("name")
    @classmethod
    def normalize_qa_task_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Q&A task name is required")
        return normalized


class ReviewProgramDiscoverySetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_hash: str = Field(min_length=64, max_length=64)
    target_document_count: int = Field(default=500, ge=1, le=100_000)
    control_sample_size: int = Field(default=50, ge=0, le=25_000)
    target_batch_size: int = Field(default=100, ge=1, le=10_000)
    keep_families_together: bool = True

    @model_validator(mode="after")
    def validate_discovery_setup(self) -> "ReviewProgramDiscoverySetup":
        if self.control_sample_size > self.target_document_count:
            raise ValueError("Control sample size cannot exceed the discovery target")
        return self


class ReviewProgramClassifierImprovementQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata_definition_id: uuid.UUID
    class_key: str = Field(min_length=1, max_length=500)
    query: str = Field(min_length=1, max_length=2000)
    quota: int = Field(default=50, ge=1, le=25_000)

    @field_validator("class_key", "query")
    @classmethod
    def normalize_improvement_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Classifier improvement text is required")
        return normalized


class ReviewProgramClassifierImprovementSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_hash: str = Field(min_length=64, max_length=64)
    queries: list[ReviewProgramClassifierImprovementQuery] = Field(min_length=1, max_length=100)
    control_sample_size: int = Field(default=25, ge=0, le=25_000)
    target_batch_size: int = Field(default=100, ge=1, le=10_000)
    keep_families_together: bool = True

    @model_validator(mode="after")
    def validate_improvement_setup(self) -> "ReviewProgramClassifierImprovementSetup":
        targets = [
            (query.metadata_definition_id, query.class_key)
            for query in self.queries
        ]
        if len(targets) != len(set(targets)):
            raise ValueError("Classifier improvement targets must be unique")
        if sum(query.quota for query in self.queries) + self.control_sample_size > 100_000:
            raise ValueError("Classifier improvement population cannot exceed 100,000 documents")
        return self


class ReviewProgramNegativePopulationQCSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_hash: str = Field(min_length=64, max_length=64)
    random_sample_size: int = Field(default=200, ge=0, le=25_000)
    near_threshold_sample_size: int = Field(default=100, ge=0, le=25_000)
    cohort_control_sample_size: int = Field(default=50, ge=0, le=25_000)
    mandatory_review_limit: int = Field(default=100, ge=0, le=25_000)
    target_batch_size: int = Field(default=100, ge=1, le=10_000)
    keep_families_together: bool = True

    @model_validator(mode="after")
    def validate_negative_population_qc_setup(self) -> "ReviewProgramNegativePopulationQCSetup":
        total = (
            self.random_sample_size
            + self.near_threshold_sample_size
            + self.cohort_control_sample_size
            + self.mandatory_review_limit
        )
        if total < 1:
            raise ValueError("Negative-population QC requires at least one selected document")
        if total > 100_000:
            raise ValueError("Negative-population QC cannot select more than 100,000 documents")
        return self


class ReviewProgramStageAttemptRead(ORMModel):
    id: uuid.UUID
    run_id: uuid.UUID
    stage_key: str
    stage_ordinal: int
    attempt: int
    status: Literal["QUEUED", "RUNNING", "WAITING", "COMPLETED", "FAILED", "CANCELED"]
    idempotency_key: str
    input_snapshot: dict[str, Any]
    policy_hashes: dict[str, Any]
    result_summary: dict[str, Any]
    error_code: str | None
    error_summary: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ReviewProgramActionChoiceRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    consequence: str


class ReviewProgramActionRead(ORMModel):
    id: uuid.UUID
    run_id: uuid.UUID
    stage_attempt_id: uuid.UUID | None
    stage_key: str
    action_type: str
    title: str
    explanation: str
    decision_context: dict[str, Any]
    preview_hash: str
    allowed_choices: list[ReviewProgramActionChoiceRead]
    status: Literal["OPEN", "RESOLVED", "SUPERSEDED", "CANCELED"]
    resolution_payload: dict[str, Any] | None
    resolution_reason: str | None
    resolved_by_user_id: uuid.UUID | None
    authorization_context: dict[str, Any]
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ReviewProgramEventRead(ORMModel):
    id: uuid.UUID
    event_type: str
    schema_version: int
    source_type: str
    source_id: uuid.UUID
    stage_attempt_id: uuid.UUID | None
    payload: dict[str, Any]
    idempotency_key: str
    status: Literal["PENDING", "PROCESSING", "PROCESSED", "FAILED"]
    processing_attempt: int
    failure_code: str | None
    failure_summary: str | None
    occurred_at: datetime
    created_at: datetime
    processed_at: datetime | None


class ReviewProgramRunRead(ORMModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    matter_id: uuid.UUID
    program_id: uuid.UUID
    starting_program_version_id: uuid.UUID
    current_program_version_id: uuid.UUID
    status: ReviewProgramRunStatus
    current_stage: str
    current_stage_ordinal: int
    current_discovery_round: int
    current_training_round: int
    pending_action_id: uuid.UUID | None
    progress_summary: dict[str, Any]
    frozen_configuration_hash: str
    failure_code: str | None
    failure_summary: str | None
    failure_retriable: bool
    initiated_by_user_id: uuid.UUID
    started_at: datetime | None
    completed_at: datetime | None
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    stage_attempts: list[ReviewProgramStageAttemptRead] = Field(default_factory=list)
    actions: list[ReviewProgramActionRead] = Field(default_factory=list)
    events: list[ReviewProgramEventRead] = Field(default_factory=list)
