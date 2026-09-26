import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenant"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_tenant_status"),
        CheckConstraint(
            "(is_root AND parent_tenant_id IS NULL) OR (NOT is_root AND parent_tenant_id IS NOT NULL)",
            name="ck_tenant_root_parent",
        ),
        Index(
            "uq_single_root_tenant",
            "is_root",
            unique=True,
            postgresql_where=text("is_root = true"),
            sqlite_where=text("is_root = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    parent_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    is_root: Mapped[bool] = mapped_column(Boolean, default=False)

    parent: Mapped["Tenant | None"] = relationship(remote_side="Tenant.id", back_populates="children")
    children: Mapped[list["Tenant"]] = relationship(back_populates="parent")


class User(TimestampMixin, Base):
    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("normalized_email", name="uq_user_normalized_email"),
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_user_status"),
        CheckConstraint("tenant_role = 'ADMIN'", name="ck_user_tenant_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    normalized_email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    tenant_role: Mapped[str] = mapped_column(String(20), default="ADMIN")
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    last_authenticated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tenant: Mapped[Tenant] = relationship()
    password_credential: Mapped["PasswordCredential"] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )


class PasswordCredential(Base):
    __tablename__ = "password_credential"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text)
    hash_scheme: Mapped[str] = mapped_column(String(50), default="argon2id")
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    failed_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="password_credential")


class AuthSession(Base):
    __tablename__ = "auth_session"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    refresh_jti_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()


class Client(TimestampMixin, Base):
    __tablename__ = "client"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_client_tenant_name"),
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_client_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")

    tenant: Mapped[Tenant] = relationship()


class ClientMembership(Base):
    __tablename__ = "client_membership"
    __table_args__ = (
        UniqueConstraint("client_id", "user_id", name="uq_client_membership"),
        CheckConstraint("role = 'ADMIN'", name="ck_client_membership_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("client.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="ADMIN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Custodian(TimestampMixin, Base):
    __tablename__ = "custodian"
    __table_args__ = (
        UniqueConstraint("client_id", "normalized_name", name="uq_custodian_client_name"),
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_custodian_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("client.id", ondelete="RESTRICT"), index=True)
    display_name: Mapped[str] = mapped_column(String(300))
    normalized_name: Mapped[str] = mapped_column(String(300))
    email_addresses: Mapped[list[str]] = mapped_column(JSON, default=list)
    external_reference: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")

    client: Mapped[Client] = relationship()


class Matter(TimestampMixin, Base):
    __tablename__ = "matter"
    __table_args__ = (
        UniqueConstraint("client_id", "name", name="uq_matter_client_name"),
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_matter_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("client.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")

    client: Mapped[Client] = relationship()


class MatterMembership(Base):
    __tablename__ = "matter_membership"
    __table_args__ = (
        UniqueConstraint("matter_id", "user_id", name="uq_matter_membership"),
        CheckConstraint("role = 'ADMIN'", name="ck_matter_membership_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="ADMIN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExternalProviderUsage(Base):
    """Immutable token-usage ledger for billable external provider calls."""

    __tablename__ = "external_provider_usage"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_external_provider_usage_idempotency_key"),
        CheckConstraint(
            "request_count >= 0 AND input_tokens >= 0 AND cached_input_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND output_tokens >= 0",
            name="ck_external_provider_usage_counts",
        ),
        Index("ix_external_provider_usage_tenant_job_date", "tenant_id", "job_created_at"),
        Index("ix_external_provider_usage_job", "job_type", "job_id"),
        Index("ix_external_provider_usage_provider_date", "provider", "job_created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("client.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    matter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter.id", ondelete="SET NULL"), nullable=True, index=True
    )
    started_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    job_type: Mapped[str] = mapped_column(String(80), index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    job_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    provider: Mapped[str] = mapped_column(String(100), index=True)
    model: Mapped[str] = mapped_column(String(500))
    request_count: Mapped[int] = mapped_column(Integer, default=1)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    model_invocation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("model_invocation.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(500))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    started_by: Mapped[User] = relationship()

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def started_by_email(self) -> str:
        return self.started_by.email

    @property
    def started_by_display_name(self) -> str:
        return self.started_by.display_name


class MatterDocumentImportJob(TimestampMixin, Base):
    __tablename__ = "matter_document_import_job"
    __table_args__ = (
        CheckConstraint("selection_type IN ('QUERY', 'EXPLICIT')", name="ck_matter_import_selection_type"),
        CheckConstraint(
            "status IN ('QUEUED', 'SNAPSHOTTING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_matter_import_status",
        ),
        CheckConstraint(
            "matched_count >= 0 AND batch_count >= 0 AND processed_count >= 0 AND "
            "added_count >= 0 AND duplicate_count >= 0 AND failed_count >= 0",
            name="ck_matter_import_counts",
        ),
        Index("ix_matter_import_matter_created", "matter_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    source_collection_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    selection_type: Mapped[str] = mapped_column(String(20))
    selection: Mapped[dict[str, Any]] = mapped_column(JSON)
    selection_summary: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    workflow_id: Mapped[str] = mapped_column(String(255), unique=True)
    artifact_selection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    matched_count: Mapped[int] = mapped_column(Integer, default=0)
    batch_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    added_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    matter: Mapped[Matter] = relationship()
    created_by: Mapped[User] = relationship()


class MatterDocumentImportBatch(TimestampMixin, Base):
    __tablename__ = "matter_document_import_batch"
    __table_args__ = (
        UniqueConstraint("job_id", "batch_number", name="uq_matter_import_batch_number"),
        CheckConstraint("status IN ('QUEUED', 'COMPLETED', 'FAILED')", name="ck_matter_import_batch_status"),
        CheckConstraint(
            "item_count >= 0 AND added_count >= 0 AND duplicate_count >= 0 AND failed_count >= 0",
            name="ck_matter_import_batch_counts",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document_import_job.id", ondelete="CASCADE"), index=True
    )
    batch_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")
    item_count: Mapped[int] = mapped_column(Integer)
    added_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)


class MatterDocument(Base):
    __tablename__ = "matter_document"
    __table_args__ = (
        UniqueConstraint("matter_id", "collection_item_id", name="uq_matter_document_source_item"),
        Index("ix_matter_document_matter_created", "matter_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    source_collection_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    collection_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    added_by_import_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document_import_job.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MatterEmbeddingJob(TimestampMixin, Base):
    __tablename__ = "matter_embedding_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'PLANNING', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_matter_embedding_job_status",
        ),
        CheckConstraint(
            "total_count >= 0 AND batch_count >= 0 AND processed_count >= 0 AND "
            "embedded_count >= 0 AND skipped_count >= 0 AND failed_count >= 0 AND chunk_count >= 0",
            name="ck_matter_embedding_job_counts",
        ),
        Index("ix_matter_embedding_job_matter_created", "matter_id", "created_at"),
        Index(
            "uq_matter_embedding_job_active",
            "matter_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'PLANNING', 'RUNNING')"),
            sqlite_where=text("status IN ('QUEUED', 'PLANNING', 'RUNNING')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    workflow_id: Mapped[str] = mapped_column(String(255), unique=True)
    configuration_hash: Mapped[str] = mapped_column(String(64), index=True)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON)
    embedding_model: Mapped[str] = mapped_column(String(500))
    embedding_model_revision: Mapped[str | None] = mapped_column(String(255))
    embedding_dimensions: Mapped[int] = mapped_column(Integer)
    embedding_normalized: Mapped[bool] = mapped_column(Boolean)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    batch_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    embedded_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    matter: Mapped[Matter] = relationship()
    created_by: Mapped[User] = relationship()

    # API reporting fields are aggregated from the immutable provider-usage
    # ledger; these defaults let the ORM object validate before the router
    # overlays the current aggregate values.
    @property
    def provider_request_count(self) -> int:
        return 0

    @property
    def input_tokens(self) -> int:
        return 0

    @property
    def output_tokens(self) -> int:
        return 0

    @property
    def total_tokens(self) -> int:
        return 0


class MatterEmbeddingBatch(TimestampMixin, Base):
    __tablename__ = "matter_embedding_batch"
    __table_args__ = (
        UniqueConstraint("job_id", "batch_number", name="uq_matter_embedding_batch_number"),
        UniqueConstraint("provider_batch_id", name="uq_matter_embedding_batch_provider_batch_id"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_matter_embedding_batch_status",
        ),
        CheckConstraint(
            "item_count >= 0 AND processed_count >= 0 AND embedded_count >= 0 AND "
            "skipped_count >= 0 AND failed_count >= 0 AND chunk_count >= 0",
            name="ck_matter_embedding_batch_counts",
        ),
        CheckConstraint(
            "provider_request_count >= 0",
            name="ck_matter_embedding_batch_provider_request_count",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_embedding_job.id", ondelete="CASCADE"), index=True
    )
    batch_number: Mapped[int] = mapped_column(Integer)
    document_ids: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")
    item_count: Mapped[int] = mapped_column(Integer)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    embedded_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    provider_input_file_id: Mapped[str | None] = mapped_column(String(255))
    provider_batch_id: Mapped[str | None] = mapped_column(String(255))
    provider_output_file_id: Mapped[str | None] = mapped_column(String(255))
    provider_error_file_id: Mapped[str | None] = mapped_column(String(255))
    provider_status: Mapped[str | None] = mapped_column(String(40))
    provider_request_count: Mapped[int] = mapped_column(Integer, default=0)
    provider_manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    provider_last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MatterTopicJob(TimestampMixin, Base):
    __tablename__ = "matter_topic_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'SAMPLING', 'CLUSTERING', 'AWAITING_REVIEW', 'PUBLISHING', 'COMPLETED', "
            "'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_matter_topic_job_status",
        ),
        CheckConstraint("operating_mode IN ('AUTO', 'FIXED')", name="ck_matter_topic_job_operating_mode"),
        CheckConstraint("assignment_mode IN ('REPLACE', 'APPEND')", name="ck_matter_topic_job_assignment_mode"),
        CheckConstraint(
            "sample_size > 0 AND document_count >= 0 AND sampled_chunk_count >= 0 AND "
            "processed_document_count >= 0 AND assigned_document_count >= 0 AND topic_count >= 0 AND "
            "outlier_document_count >= 0 AND failed_count >= 0",
            name="ck_matter_topic_job_counts",
        ),
        CheckConstraint(
            "(operating_mode = 'FIXED' AND requested_topic_count IS NOT NULL AND requested_topic_count >= 2) OR "
            "(operating_mode = 'AUTO' AND requested_topic_count IS NULL)",
            name="ck_matter_topic_job_requested_count",
        ),
        Index("ix_matter_topic_job_matter_created", "matter_id", "created_at"),
        Index(
            "uq_matter_topic_job_active",
            "matter_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'SAMPLING', 'CLUSTERING', 'AWAITING_REVIEW', 'PUBLISHING')"),
            sqlite_where=text("status IN ('QUEUED', 'SAMPLING', 'CLUSTERING', 'AWAITING_REVIEW', 'PUBLISHING')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    embedding_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_embedding_job.id", ondelete="RESTRICT"), index=True
    )
    metadata_definition_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("metadata_definition.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    workflow_id: Mapped[str] = mapped_column(String(255), unique=True)
    operating_mode: Mapped[str] = mapped_column(String(20))
    sample_size: Mapped[int] = mapped_column(Integer)
    requested_topic_count: Mapped[int | None] = mapped_column(Integer)
    assignment_mode: Mapped[str] = mapped_column(String(20), default="REPLACE")
    configuration_hash: Mapped[str] = mapped_column(String(64), index=True)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON)
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    sampled_chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_document_count: Mapped[int] = mapped_column(Integer, default=0)
    assigned_document_count: Mapped[int] = mapped_column(Integer, default=0)
    topic_count: Mapped[int] = mapped_column(Integer, default=0)
    outlier_document_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def scope_mode(self) -> str:
        return str((self.configuration.get("document_scope") or {}).get("mode", "ENTIRE_MATTER"))

    @property
    def saved_search_id(self) -> uuid.UUID | None:
        value = (self.configuration.get("document_scope") or {}).get("saved_search_id")
        return uuid.UUID(str(value)) if value else None

    @property
    def saved_search_name(self) -> str | None:
        value = (self.configuration.get("document_scope") or {}).get("saved_search_name")
        return str(value) if value else None

    @property
    def destination_mode(self) -> str | None:
        value = (self.configuration.get("topic_destination") or {}).get("mode")
        return str(value) if value else None

    @property
    def destination_metadata_definition_id(self) -> uuid.UUID | None:
        value = (self.configuration.get("topic_destination") or {}).get("metadata_definition_id")
        return uuid.UUID(str(value)) if value else None

    @property
    def destination_field_key(self) -> str | None:
        value = (self.configuration.get("topic_destination") or {}).get("field_key")
        return str(value) if value else None

    @property
    def destination_field_name(self) -> str | None:
        value = (self.configuration.get("topic_destination") or {}).get("field_name")
        return str(value) if value else None

    matter: Mapped[Matter] = relationship()
    embedding_job: Mapped[MatterEmbeddingJob] = relationship()
    metadata_definition: Mapped["MetadataDefinition | None"] = relationship()
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_user_id])
    reviewed_by: Mapped[User | None] = relationship(foreign_keys=[reviewed_by_user_id])
    clusters: Mapped[list["MatterTopicCluster"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="MatterTopicCluster.ordinal"
    )


class MatterTopicCluster(TimestampMixin, Base):
    __tablename__ = "matter_topic_cluster"
    __table_args__ = (
        UniqueConstraint("job_id", "ordinal", name="uq_matter_topic_cluster_ordinal"),
        UniqueConstraint("job_id", "topic_key", name="uq_matter_topic_cluster_key"),
        CheckConstraint(
            "ordinal >= 0 AND sampled_chunk_count >= 0 AND assigned_chunk_count >= 0 AND assigned_document_count >= 0",
            name="ck_matter_topic_cluster_counts",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter_topic_job.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    topic_key: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    centroid: Mapped[list[float]] = mapped_column(JSON)
    representative_excerpts: Mapped[list[str]] = mapped_column(JSON, default=list)
    included: Mapped[bool] = mapped_column(Boolean, default=True)
    sampled_chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    assigned_chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    assigned_document_count: Mapped[int] = mapped_column(Integer, default=0)

    job: Mapped[MatterTopicJob] = relationship(back_populates="clusters")


class MatterTopicBatch(TimestampMixin, Base):
    __tablename__ = "matter_topic_batch"
    __table_args__ = (
        UniqueConstraint("job_id", "batch_number", name="uq_matter_topic_batch_number"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_matter_topic_batch_status",
        ),
        CheckConstraint(
            "item_count >= 0 AND processed_count >= 0 AND assigned_count >= 0 AND "
            "outlier_count >= 0 AND failed_count >= 0",
            name="ck_matter_topic_batch_counts",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter_topic_job.id", ondelete="CASCADE"), index=True)
    batch_number: Mapped[int] = mapped_column(Integer)
    document_ids: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")
    item_count: Mapped[int] = mapped_column(Integer)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    assigned_count: Mapped[int] = mapped_column(Integer, default=0)
    outlier_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str | None] = mapped_column(Text)


class MatterTopicAssignment(Base):
    __tablename__ = "matter_topic_assignment"
    __table_args__ = (
        UniqueConstraint("job_id", "matter_document_id", "topic_cluster_id", name="uq_matter_topic_assignment"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_matter_topic_assignment_confidence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter_topic_job.id", ondelete="CASCADE"), index=True)
    matter_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document.id", ondelete="CASCADE"), index=True
    )
    topic_cluster_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_topic_cluster.id", ondelete="CASCADE"), index=True
    )
    confidence: Mapped[float] = mapped_column(Float)
    assigned_chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    supporting_chunk_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MatterDocumentCustodian(Base):
    __tablename__ = "matter_document_custodian"
    __table_args__ = (
        CheckConstraint("relationship_type IN ('PRIMARY', 'COMMON')", name="ck_matter_document_custodian_type"),
    )

    matter_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document.id", ondelete="CASCADE"), primary_key=True
    )
    custodian_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("custodian.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    relationship_type: Mapped[str] = mapped_column(String(20), default="PRIMARY")


class SearchIndexGeneration(TimestampMixin, Base):
    __tablename__ = "search_index_generation"
    __table_args__ = (
        UniqueConstraint("matter_id", "generation", name="uq_search_index_matter_generation"),
        UniqueConstraint("index_name", name="uq_search_index_name"),
        CheckConstraint(
            "status IN ('CREATING', 'ACTIVE', 'RETIRED', 'FAILED')",
            name="ck_search_index_generation_status",
        ),
        Index("ix_search_index_matter_status", "matter_id", "status"),
        Index(
            "uq_search_index_active_matter",
            "matter_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
            sqlite_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    index_name: Mapped[str] = mapped_column(String(255))
    alias_name: Mapped[str] = mapped_column(String(255))
    schema_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="CREATING")
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    schema_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SearchProjectionOperation(TimestampMixin, Base):
    __tablename__ = "search_projection_operation"
    __table_args__ = (
        UniqueConstraint("workflow_id", name="uq_search_projection_workflow"),
        CheckConstraint(
            "kind IN ('SCHEMA_SYNC', 'REBUILD', 'DOCUMENT_UPSERT', 'DOCUMENT_DELETE')",
            name="ck_search_projection_kind",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'AWAITING_USER', 'COMPLETED', 'FAILED')",
            name="ck_search_projection_status",
        ),
        Index("ix_search_projection_matter_created", "matter_id", "created_at"),
        Index("ix_search_projection_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")
    workflow_id: Mapped[str] = mapped_column(String(255))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="SET NULL"), index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MatterSavedSearch(TimestampMixin, Base):
    __tablename__ = "matter_saved_search"
    __table_args__ = (
        UniqueConstraint(
            "matter_id",
            "owner_user_id",
            "normalized_name",
            name="uq_matter_saved_search_owner_name",
        ),
        CheckConstraint(
            "visibility IN ('PRIVATE', 'PUBLIC', 'SHARED')",
            name="ck_matter_saved_search_visibility",
        ),
        Index("ix_matter_saved_search_matter_updated", "matter_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(1000))
    visibility: Mapped[str] = mapped_column(String(20), default="PRIVATE", index=True)
    search_definition: Mapped[dict[str, Any]] = mapped_column(JSON)

    matter: Mapped[Matter] = relationship()
    owner: Mapped[User] = relationship()
    user_shares: Mapped[list["MatterSavedSearchUserShare"]] = relationship(
        back_populates="saved_search",
        cascade="all, delete-orphan",
    )


class MatterSavedSearchUserShare(Base):
    __tablename__ = "matter_saved_search_user_share"

    saved_search_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("matter_saved_search.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    saved_search: Mapped[MatterSavedSearch] = relationship(back_populates="user_shares")
    user: Mapped[User] = relationship()


class MatterBulkTagJob(TimestampMixin, Base):
    __tablename__ = "matter_bulk_tag_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'SNAPSHOTTING', 'RUNNING', 'COMPLETED', "
            "'COMPLETED_WITH_ERRORS', 'FAILED')",
            name="ck_matter_bulk_tag_job_status",
        ),
        CheckConstraint(
            "matched_count >= 0 AND batch_count >= 0 AND processed_count >= 0 AND "
            "tagged_count >= 0 AND failed_count >= 0",
            name="ck_matter_bulk_tag_job_counts",
        ),
        Index("ix_matter_bulk_tag_job_matter_created", "matter_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    metadata_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("metadata_definition.id", ondelete="RESTRICT"), index=True
    )
    search_index_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("search_index_generation.id", ondelete="SET NULL"), nullable=True, index=True
    )
    search_index_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    search_definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    value: Mapped[Any] = mapped_column(JSON)
    assignments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    workflow_id: Mapped[str] = mapped_column(String(255), unique=True)
    matched_count: Mapped[int] = mapped_column(Integer, default=0)
    batch_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    tagged_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    matter: Mapped[Matter] = relationship()
    metadata_definition: Mapped["MetadataDefinition"] = relationship()
    search_index_generation: Mapped[SearchIndexGeneration | None] = relationship()
    created_by: Mapped[User] = relationship()


class MatterBulkTagBatch(Base):
    __tablename__ = "matter_bulk_tag_batch"
    __table_args__ = (
        UniqueConstraint("job_id", "batch_number", name="uq_matter_bulk_tag_batch_number"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="ck_matter_bulk_tag_batch_status",
        ),
        CheckConstraint(
            "item_count >= 0 AND processed_count >= 0 AND tagged_count >= 0 AND failed_count >= 0",
            name="ck_matter_bulk_tag_batch_counts",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_bulk_tag_job.id", ondelete="CASCADE"), index=True
    )
    batch_number: Mapped[int] = mapped_column(Integer)
    document_ids: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")
    item_count: Mapped[int] = mapped_column(Integer)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    tagged_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str | None] = mapped_column(Text)


class ReviewBatch(TimestampMixin, Base):
    __tablename__ = "review_batch"
    __table_args__ = (
        CheckConstraint(
            "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH', "
            "'DEFINITION_ASSESSMENT')",
            name="ck_review_batch_selection_type",
        ),
        CheckConstraint(
            "reviewer_value_visibility IN ('OWN_VALUES', 'ALL_REVIEWER_VALUES')",
            name="ck_review_batch_value_visibility",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'BUILDING', 'READY', 'FAILED', 'ARCHIVED')",
            name="ck_review_batch_status",
        ),
        CheckConstraint(
            "search_status IN ('QUEUED', 'SYNCING', 'READY', 'FAILED', 'NOT_CONFIGURED')",
            name="ck_review_batch_search_status",
        ),
        CheckConstraint("document_count >= 0", name="ck_review_batch_document_count"),
        CheckConstraint("sample_size IS NULL OR sample_size > 0", name="ck_review_batch_sample_size"),
        Index("ix_review_batch_matter_created", "matter_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    selection_type: Mapped[str] = mapped_column(String(30))
    selection_definition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_batch.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    search_index_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("search_index_generation.id", ondelete="SET NULL"), nullable=True, index=True
    )
    sample_size: Mapped[int | None] = mapped_column(Integer)
    random_seed: Mapped[str | None] = mapped_column(String(100))
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assigned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewer_value_visibility: Mapped[str] = mapped_column(String(30), default="OWN_VALUES")
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    search_status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    search_error_message: Mapped[str | None] = mapped_column(Text)
    workflow_id: Mapped[str] = mapped_column(String(255), unique=True)
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewBatchDocument(Base):
    __tablename__ = "review_batch_document"
    __table_args__ = (
        UniqueConstraint("review_batch_id", "sequence_number", name="uq_review_batch_document_sequence"),
        CheckConstraint("sequence_number > 0", name="ck_review_batch_document_sequence"),
        CheckConstraint(
            "review_status IN ('NOT_STARTED', 'IN_PROGRESS', 'COMPLETED', 'SKIPPED')",
            name="ck_review_batch_document_status",
        ),
    )

    review_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch.id", ondelete="CASCADE"), primary_key=True
    )
    matter_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    sequence_number: Mapped[int] = mapped_column(Integer)
    review_status: Mapped[str] = mapped_column(String(20), default="NOT_STARTED")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewBatchCodingGroup(Base):
    __tablename__ = "review_batch_coding_group"
    __table_args__ = (UniqueConstraint("review_batch_id", "sort_order", name="uq_review_batch_group_order"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch.id", ondelete="CASCADE"), index=True
    )
    source_metadata_group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("metadata_group.id", ondelete="SET NULL"), nullable=True, index=True
    )
    display_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer)


class ReviewBatchCodingField(Base):
    __tablename__ = "review_batch_coding_field"
    __table_args__ = (
        UniqueConstraint("review_batch_coding_group_id", "metadata_definition_id", name="uq_review_batch_group_field"),
        UniqueConstraint("review_batch_coding_group_id", "sort_order", name="uq_review_batch_field_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_batch_coding_group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch_coding_group.id", ondelete="CASCADE"), index=True
    )
    metadata_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("metadata_definition.id", ondelete="RESTRICT"), index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer)
    definition_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)


class ReviewBatchNote(Base):
    __tablename__ = "review_batch_note"
    __table_args__ = (
        CheckConstraint("author_type IN ('USER', 'AGENT')", name="ck_review_batch_note_author_type"),
        CheckConstraint(
            "(author_type = 'USER' AND author_user_id IS NOT NULL AND author_run_id IS NULL) OR "
            "(author_type = 'AGENT' AND author_user_id IS NULL AND author_run_id IS NOT NULL)",
            name="ck_review_batch_note_author",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch.id", ondelete="CASCADE"), index=True
    )
    body: Mapped[str] = mapped_column(Text)
    author_type: Mapped[str] = mapped_column(String(20), default="USER")
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    author_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_batch_run.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkflowRun(TimestampMixin, Base):
    __tablename__ = "workflow_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_workflow_run_status",
        ),
        Index("ix_workflow_run_matter_created", "matter_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("client.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    matter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter.id", ondelete="CASCADE"), nullable=True, index=True
    )
    workflow_key: Mapped[str] = mapped_column(String(100), index=True)
    code_version: Mapped[str] = mapped_column(String(100))
    dbos_workflow_id: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    binding_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    configuration_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    initiated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowStepRun(TimestampMixin, Base):
    __tablename__ = "workflow_step_run"
    __table_args__ = (
        UniqueConstraint("workflow_run_id", "ordinal", name="uq_workflow_step_run_ordinal"),
        CheckConstraint("ordinal > 0", name="ck_workflow_step_run_ordinal_positive"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_workflow_step_run_status",
        ),
        CheckConstraint(
            "request_count >= 0 AND tool_call_count >= 0 AND input_tokens >= 0 "
            "AND cached_input_tokens >= 0 AND cache_write_tokens >= 0 AND output_tokens >= 0",
            name="ck_workflow_step_run_usage",
        ),
        Index("ix_workflow_step_run_workflow_status", "workflow_run_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="CASCADE"), index=True
    )
    role_key: Mapped[str] = mapped_column(String(100))
    ordinal: Mapped[int] = mapped_column(Integer)
    fan_out_group: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SkillRun(TimestampMixin, Base):
    __tablename__ = "skill_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'SKIPPED', 'FAILED', 'CANCELED')",
            name="ck_skill_run_status",
        ),
        CheckConstraint(
            "request_count >= 0 AND tool_call_count >= 0 AND input_tokens >= 0 "
            "AND cached_input_tokens >= 0 AND cache_write_tokens >= 0 AND output_tokens >= 0",
            name="ck_skill_run_usage",
        ),
        Index("ix_skill_run_step_status", "workflow_step_run_id", "status"),
        Index("ix_skill_run_scope", "scope_type", "scope_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="CASCADE"), index=True
    )
    workflow_step_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_step_run.id", ondelete="CASCADE"), index=True
    )
    skill_definition_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("skill_definition_version.id", ondelete="RESTRICT"), index=True
    )
    parent_skill_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("skill_run.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    root_skill_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("skill_run.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    scope_type: Mapped[str] = mapped_column(String(80))
    scope_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    configuration_hash: Mapped[str] = mapped_column(String(64))
    cache_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    output_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewBatchRun(TimestampMixin, Base):
    __tablename__ = "review_batch_run"
    __table_args__ = (
        CheckConstraint("run_type IN ('HUMAN', 'AGENT', 'WORKFLOW')", name="ck_review_batch_run_type"),
        CheckConstraint(
            "purpose IN ('REVIEW', 'REFERENCE', 'CANDIDATE', 'ASSESSMENT')",
            name="ck_review_batch_run_purpose",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_review_batch_run_status",
        ),
        CheckConstraint("result_policy IN ('ISOLATED', 'PUBLISH_TO_MATTER')", name="ck_review_batch_run_policy"),
        CheckConstraint(
            "(run_type = 'HUMAN' AND actor_user_id IS NOT NULL "
            "AND agent_definition_version_id IS NULL AND workflow_run_record_id IS NULL) OR "
            "(run_type = 'AGENT' AND actor_user_id IS NULL "
            "AND agent_definition_version_id IS NOT NULL AND workflow_run_record_id IS NULL) OR "
            "(run_type = 'WORKFLOW' AND actor_user_id IS NULL "
            "AND agent_definition_version_id IS NULL AND workflow_run_record_id IS NOT NULL)",
            name="ck_review_batch_run_actor",
        ),
        CheckConstraint("processed_document_count >= 0", name="ck_review_batch_run_processed_count"),
        Index("ix_review_batch_run_batch_created", "review_batch_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch.id", ondelete="CASCADE"), index=True
    )
    run_type: Mapped[str] = mapped_column(String(20))
    purpose: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default="RUNNING", index=True)
    result_policy: Mapped[str] = mapped_column(String(30), default="ISOLATED")
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_batch_run.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    agent_definition_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_definition_version.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    workflow_run_record_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    configuration_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    initiated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    dbos_workflow_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    processed_document_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewBatchRunDocument(Base):
    __tablename__ = "review_batch_run_document"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'IN_PROGRESS', 'COMPLETED', 'SKIPPED', 'FAILED')",
            name="ck_review_batch_run_document_status",
        ),
        Index("ix_review_batch_run_document_run_status", "review_batch_run_id", "status"),
    )

    review_batch_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch_run.id", ondelete="CASCADE"), primary_key=True
    )
    matter_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="IN_PROGRESS")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ReviewBatchRunValue(Base):
    __tablename__ = "review_batch_run_value"
    __table_args__ = (
        CheckConstraint("value_ordinal >= 0", name="ck_review_batch_run_value_ordinal"),
        CheckConstraint(
            "(CASE WHEN value_text IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_long IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_float IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_boolean IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_date IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_datetime IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_json IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_review_batch_run_value_shape",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_review_batch_run_value_confidence",
        ),
        Index("ix_review_batch_run_value_document", "matter_document_id", "metadata_definition_id"),
    )

    review_batch_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch_run.id", ondelete="CASCADE"), primary_key=True
    )
    matter_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document.id", ondelete="CASCADE"), primary_key=True
    )
    metadata_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("metadata_definition.id", ondelete="RESTRICT"), primary_key=True
    )
    value_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_long: Mapped[int | None] = mapped_column(BigInteger)
    value_float: Mapped[float | None] = mapped_column(Float)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean)
    value_date: Mapped[date | None] = mapped_column(Date)
    value_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    value_json: Mapped[Any | None] = mapped_column(JSON(none_as_null=True))
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MetadataDefinition(TimestampMixin, Base):
    __tablename__ = "metadata_definition"
    __table_args__ = (
        UniqueConstraint("matter_id", "key", name="uq_metadata_definition_matter_key"),
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_metadata_definition_status"),
        CheckConstraint(
            "type IN ('TEXT', 'LONG_TEXT', 'INTEGER', 'DECIMAL', 'BOOLEAN', 'DATE', 'DATETIME', 'ENUM', 'JSON')",
            name="ck_metadata_definition_type",
        ),
        CheckConstraint("cardinality IN ('SINGLE', 'MULTIPLE')", name="ck_metadata_definition_cardinality"),
        CheckConstraint(
            "value_source IN ('SYSTEM', 'IMPORTED', 'ASSERTED')",
            name="ck_metadata_definition_value_source",
        ),
        CheckConstraint(
            "reference_target IS NULL OR reference_target IN ('CUSTODIAN')",
            name="ck_metadata_definition_reference_target",
        ),
        CheckConstraint(
            "(template_key IS NULL AND template_version IS NULL) OR "
            "(template_key IS NOT NULL AND template_version IS NOT NULL AND template_version > 0)",
            name="ck_metadata_definition_template_identity",
        ),
        CheckConstraint(
            "assertion_policy IN ('IMMEDIATE', 'REQUIRES_CONFIRMATION')",
            name="ck_metadata_definition_assertion_policy",
        ),
        CheckConstraint(
            "resolution_policy IN ('EXPLICIT_ONLY', 'LATEST_VALID', 'HUMAN_PRECEDENCE')",
            name="ck_metadata_definition_resolution_policy",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="RESTRICT"), index=True)
    key: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String(30))
    cardinality: Mapped[str] = mapped_column(String(20), default="SINGLE")
    allowed_values: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    value_source: Mapped[str] = mapped_column(String(20), default="ASSERTED")
    reference_target: Mapped[str | None] = mapped_column(String(50))
    template_key: Mapped[str | None] = mapped_column(String(100))
    template_version: Mapped[int | None] = mapped_column(Integer)
    assertion_policy: Mapped[str] = mapped_column(String(30), default="IMMEDIATE")
    resolution_policy: Mapped[str] = mapped_column(String(30), default="EXPLICIT_ONLY")
    searchable: Mapped[bool] = mapped_column(Boolean, default=True)
    facetable: Mapped[bool] = mapped_column(Boolean, default=False)
    normalize_to_lowercase: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewable: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_assignable: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")

    matter: Mapped[Matter] = relationship()


class MetadataEvent(Base):
    __tablename__ = "metadata_event"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('SET', 'ADD', 'REMOVE', 'CLEAR', 'CONFIRM', 'REJECT')",
            name="ck_metadata_event_operation",
        ),
        CheckConstraint(
            "source_type IN ('HUMAN', 'AGENT', 'EXTRACTOR', 'IMPORT', 'RULE', 'SYSTEM')",
            name="ck_metadata_event_source_type",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_metadata_event_confidence",
        ),
        CheckConstraint(
            "(operation IN ('SET', 'ADD') AND "
            "(CASE WHEN value_text IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_long IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_float IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_boolean IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_date IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_datetime IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_json IS NOT NULL THEN 1 ELSE 0 END) = 1) OR "
            "(operation IN ('REMOVE', 'CLEAR', 'CONFIRM', 'REJECT') AND "
            "value_text IS NULL AND value_long IS NULL AND value_float IS NULL AND "
            "value_boolean IS NULL AND value_date IS NULL AND value_datetime IS NULL AND value_json IS NULL)",
            name="ck_metadata_event_value_shape",
        ),
        CheckConstraint(
            "(operation IN ('REMOVE', 'CONFIRM', 'REJECT') AND target_event_id IS NOT NULL) OR "
            "(operation IN ('SET', 'ADD', 'CLEAR') AND target_event_id IS NULL)",
            name="ck_metadata_event_target_shape",
        ),
        Index(
            "ix_metadata_event_document_definition_created",
            "matter_document_id",
            "metadata_definition_id",
            "created_at",
            "id",
        ),
        Index("ix_metadata_event_matter_created", "matter_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    matter_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document.id", ondelete="CASCADE"), index=True
    )
    metadata_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("metadata_definition.id", ondelete="RESTRICT"), index=True
    )
    operation: Mapped[str] = mapped_column(String(20))
    value_text: Mapped[str | None] = mapped_column(Text)
    value_long: Mapped[int | None] = mapped_column(BigInteger)
    value_float: Mapped[float | None] = mapped_column(Float)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean)
    value_date: Mapped[date | None] = mapped_column(Date)
    value_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    value_json: Mapped[Any | None] = mapped_column(JSON(none_as_null=True))
    source_type: Mapped[str] = mapped_column(String(20))
    source_id: Mapped[str | None] = mapped_column(String(500))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    target_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("metadata_event.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("metadata_event.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentMetadataCurrent(Base):
    __tablename__ = "document_metadata_current"
    __table_args__ = (
        CheckConstraint(
            "resolution_state IN ('VALUE', 'EMPTY', 'PENDING', 'CONFLICTED')",
            name="ck_document_metadata_current_state",
        ),
        CheckConstraint("value_ordinal >= 0", name="ck_document_metadata_current_ordinal"),
        CheckConstraint(
            "(resolution_state = 'EMPTY' AND source_event_id IS NULL AND "
            "value_text IS NULL AND value_long IS NULL AND value_float IS NULL AND "
            "value_boolean IS NULL AND value_date IS NULL AND value_datetime IS NULL AND value_json IS NULL) OR "
            "(resolution_state = 'PENDING' AND source_event_id IS NOT NULL) OR "
            "(resolution_state IN ('VALUE', 'CONFLICTED') AND source_event_id IS NOT NULL AND "
            "(CASE WHEN value_text IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_long IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_float IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_boolean IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_date IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_datetime IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_json IS NOT NULL THEN 1 ELSE 0 END) = 1)",
            name="ck_document_metadata_current_value_shape",
        ),
        Index("ix_document_metadata_current_matter_definition", "matter_id", "metadata_definition_id"),
    )

    matter_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document.id", ondelete="CASCADE"), primary_key=True
    )
    metadata_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("metadata_definition.id", ondelete="RESTRICT"), primary_key=True
    )
    value_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    resolution_state: Mapped[str] = mapped_column(String(20))
    value_text: Mapped[str | None] = mapped_column(Text)
    value_long: Mapped[int | None] = mapped_column(BigInteger)
    value_float: Mapped[float | None] = mapped_column(Float)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean)
    value_date: Mapped[date | None] = mapped_column(Date)
    value_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    value_json: Mapped[Any | None] = mapped_column(JSON(none_as_null=True))
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("metadata_event.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    supporting_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    pending_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    conflicting_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MetadataGroup(TimestampMixin, Base):
    __tablename__ = "metadata_group"
    __table_args__ = (
        CheckConstraint("scope IN ('SYSTEM', 'MATTER', 'PERSONAL')", name="ck_metadata_group_scope"),
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_metadata_group_status"),
        CheckConstraint(
            "(scope = 'PERSONAL' AND owner_user_id IS NOT NULL) OR "
            "(scope IN ('SYSTEM', 'MATTER') AND owner_user_id IS NULL)",
            name="ck_metadata_group_owner",
        ),
        CheckConstraint(
            "(template_key IS NULL AND template_version IS NULL) OR "
            "(template_key IS NOT NULL AND template_version IS NOT NULL AND template_version > 0)",
            name="ck_metadata_group_template_identity",
        ),
        Index(
            "uq_metadata_group_shared_key",
            "matter_id",
            "key",
            unique=True,
            postgresql_where=text("scope IN ('SYSTEM', 'MATTER')"),
            sqlite_where=text("scope IN ('SYSTEM', 'MATTER')"),
        ),
        Index(
            "uq_metadata_group_personal_key",
            "matter_id",
            "owner_user_id",
            "key",
            unique=True,
            postgresql_where=text("scope = 'PERSONAL'"),
            sqlite_where=text("scope = 'PERSONAL'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(20))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    key: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    default_table_visible: Mapped[bool] = mapped_column(Boolean, default=True)
    default_document_visible: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    template_key: Mapped[str | None] = mapped_column(String(100))
    template_version: Mapped[int | None] = mapped_column(Integer)


class MetadataGroupField(Base):
    __tablename__ = "metadata_group_field"
    __table_args__ = (UniqueConstraint("metadata_group_id", "sort_order", name="uq_metadata_group_field_order"),)

    metadata_group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("metadata_group.id", ondelete="CASCADE"), primary_key=True
    )
    metadata_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("metadata_definition.id", ondelete="RESTRICT"), primary_key=True
    )
    sort_order: Mapped[int] = mapped_column(Integer)


class MetadataGroupPreference(TimestampMixin, Base):
    __tablename__ = "metadata_group_preference"
    __table_args__ = (
        UniqueConstraint("user_id", "metadata_group_id", "surface", name="uq_metadata_group_preference"),
        CheckConstraint("surface IN ('TABLE', 'DOCUMENT')", name="ck_metadata_group_preference_surface"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    metadata_group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("metadata_group.id", ondelete="CASCADE"), index=True
    )
    surface: Mapped[str] = mapped_column(String(20))
    visible: Mapped[bool] = mapped_column(Boolean)


class MatterTemplate(TimestampMixin, Base):
    __tablename__ = "matter_template"
    __table_args__ = (
        CheckConstraint("scope IN ('TENANT', 'CLIENT')", name="ck_matter_template_scope"),
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_matter_template_status"),
        CheckConstraint(
            "(scope = 'TENANT' AND client_id IS NULL) OR (scope = 'CLIENT' AND client_id IS NOT NULL)",
            name="ck_matter_template_client_scope",
        ),
        Index(
            "uq_matter_template_tenant_name",
            "tenant_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("scope = 'TENANT'"),
            sqlite_where=text("scope = 'TENANT'"),
        ),
        Index(
            "uq_matter_template_client_name",
            "client_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("scope = 'CLIENT'"),
            sqlite_where=text("scope = 'CLIENT'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("client.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    scope: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )


class MatterTemplateVersion(Base):
    __tablename__ = "matter_template_version"
    __table_args__ = (
        UniqueConstraint("matter_template_id", "version", name="uq_matter_template_version"),
        CheckConstraint("version > 0", name="ck_matter_template_version_positive"),
        CheckConstraint("base_profile_version > 0", name="ck_matter_template_base_version_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_template_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_template.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    base_profile_key: Mapped[str] = mapped_column(String(100))
    base_profile_version: Mapped[int] = mapped_column(Integer)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_matter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentDefinition(TimestampMixin, Base):
    __tablename__ = "agent_definition"
    __table_args__ = (
        UniqueConstraint("owner_tenant_id", "key", name="uq_agent_definition_tenant_key"),
        CheckConstraint("scope IN ('SYSTEM', 'TENANT')", name="ck_agent_definition_scope"),
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_agent_definition_status"),
        CheckConstraint("current_version > 0", name="ck_agent_definition_current_version"),
        CheckConstraint(
            "published_version IS NULL OR (published_version > 0 AND published_version <= current_version)",
            name="ck_agent_definition_published_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    scope: Mapped[str] = mapped_column(String(20))
    key: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    published_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )

    owner_tenant: Mapped[Tenant] = relationship()


class AgentDefinitionVersion(Base):
    __tablename__ = "agent_definition_version"
    __table_args__ = (
        UniqueConstraint("agent_definition_id", "version", name="uq_agent_definition_version"),
        CheckConstraint("version > 0", name="ck_agent_definition_version_positive"),
        CheckConstraint("status IN ('DRAFT', 'PUBLISHED', 'RETIRED')", name="ck_agent_version_status"),
        CheckConstraint("invocation_mode IN ('CHAT', 'STRUCTURED')", name="ck_agent_version_invocation_mode"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    agent_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_definition.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    system_prompt: Mapped[str] = mapped_column(Text)
    model_key: Mapped[str] = mapped_column(String(200))
    model_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    invocation_mode: Mapped[str] = mapped_column(String(20), default="CHAT")
    usage_instructions: Mapped[str | None] = mapped_column(Text)
    scope_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    limits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentVersionTool(Base):
    __tablename__ = "agent_version_tool"
    __table_args__ = (UniqueConstraint("agent_definition_version_id", "tool_key", name="uq_agent_version_tool"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    agent_definition_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_definition_version.id", ondelete="CASCADE"), index=True
    )
    tool_key: Mapped[str] = mapped_column(String(150))
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SkillDefinition(TimestampMixin, Base):
    __tablename__ = "skill_definition"
    __table_args__ = (
        UniqueConstraint("owner_tenant_id", "key", name="uq_skill_definition_tenant_key"),
        CheckConstraint("scope IN ('SYSTEM', 'TENANT')", name="ck_skill_definition_scope"),
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_skill_definition_status"),
        CheckConstraint("current_version > 0", name="ck_skill_definition_current_version"),
        CheckConstraint(
            "published_version IS NULL OR (published_version > 0 AND published_version <= current_version)",
            name="ck_skill_definition_published_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True
    )
    scope: Mapped[str] = mapped_column(String(20))
    key: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    published_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )


class SkillDefinitionVersion(Base):
    __tablename__ = "skill_definition_version"
    __table_args__ = (
        UniqueConstraint("skill_definition_id", "version", name="uq_skill_definition_version"),
        CheckConstraint("version > 0", name="ck_skill_definition_version_positive"),
        CheckConstraint("status IN ('DRAFT', 'PUBLISHED', 'RETIRED')", name="ck_skill_version_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    skill_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("skill_definition.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    instructions: Mapped[str] = mapped_column(Text)
    input_schema_key: Mapped[str] = mapped_column(String(150))
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_schema_key: Mapped[str] = mapped_column(String(150))
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON)
    model_key: Mapped[str] = mapped_column(String(200))
    model_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    limits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    required_capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    required_tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    cache_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evaluation_fixtures: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowSkillBinding(TimestampMixin, Base):
    __tablename__ = "workflow_skill_binding"
    __table_args__ = (
        UniqueConstraint(
            "workflow_key",
            "role_key",
            "scope",
            "owner_tenant_id",
            name="uq_workflow_skill_binding_role_scope",
        ),
        CheckConstraint("scope IN ('SYSTEM', 'TENANT')", name="ck_workflow_skill_binding_scope"),
        CheckConstraint("status IN ('ACTIVE', 'INACTIVE')", name="ck_workflow_skill_binding_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workflow_key: Mapped[str] = mapped_column(String(100), index=True)
    role_key: Mapped[str] = mapped_column(String(100))
    scope: Mapped[str] = mapped_column(String(20))
    owner_tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenant.id", ondelete="CASCADE"), index=True
    )
    skill_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("skill_definition.id", ondelete="RESTRICT"), index=True
    )
    skill_definition_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("skill_definition_version.id", ondelete="RESTRICT"), index=True
    )
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )


class AgentConversation(TimestampMixin, Base):
    __tablename__ = "agent_conversation"
    __table_args__ = (
        CheckConstraint(
            "workflow_type IN ('MATTER_DEFINITION_SETUP', 'BATCH_CHAT')",
            name="ck_agent_conversation_workflow_type",
        ),
        CheckConstraint(
            "(workflow_type = 'MATTER_DEFINITION_SETUP' AND review_batch_id IS NULL) OR "
            "(workflow_type = 'BATCH_CHAT' AND review_batch_id IS NOT NULL)",
            name="ck_agent_conversation_workflow_scope",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'WAITING_APPROVAL', 'FAILED', 'ARCHIVED')",
            name="ck_agent_conversation_status",
        ),
        Index("ix_agent_conversation_matter_created", "matter_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("client.id", ondelete="RESTRICT"), index=True)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    review_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_batch.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    agent_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_definition.id", ondelete="RESTRICT"), index=True
    )
    agent_definition_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_definition_version.id", ondelete="RESTRICT"), index=True
    )
    title: Mapped[str | None] = mapped_column(String(200))
    workflow_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    initiated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )


class AgentConversationEventCursor(TimestampMixin, Base):
    __tablename__ = "agent_conversation_event_cursor"
    __table_args__ = (
        CheckConstraint("newest_sequence >= 0", name="ck_agent_conversation_event_cursor_newest"),
        CheckConstraint("oldest_sequence >= 1", name="ck_agent_conversation_event_cursor_oldest"),
    )

    matter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter.id", ondelete="CASCADE"), primary_key=True
    )
    newest_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    oldest_sequence: Mapped[int] = mapped_column(BigInteger, default=1)


class AgentConversationEvent(Base):
    __tablename__ = "agent_conversation_event"
    __table_args__ = (
        UniqueConstraint("matter_id", "matter_sequence", name="uq_agent_conversation_event_matter_sequence"),
        CheckConstraint("matter_sequence > 0", name="ck_agent_conversation_event_sequence_positive"),
        CheckConstraint("schema_version > 0", name="ck_agent_conversation_event_schema_version_positive"),
        Index(
            "ix_agent_conversation_event_scope_sequence",
            "matter_id",
            "workflow_type",
            "review_batch_id",
            "matter_sequence",
        ),
        Index("ix_agent_conversation_event_conversation_sequence", "conversation_id", "matter_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_conversation.id", ondelete="CASCADE"), index=True
    )
    workflow_type: Mapped[str] = mapped_column(String(50))
    review_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_batch.id", ondelete="SET NULL"), nullable=True, index=True
    )
    matter_sequence: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_turn.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_run.id", ondelete="SET NULL"), nullable=True, index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_message.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_action_request.id", ondelete="SET NULL"), nullable=True, index=True
    )
    attempt_id: Mapped[str | None] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentTurn(TimestampMixin, Base):
    __tablename__ = "agent_turn"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_agent_turn_sequence"),
        CheckConstraint("sequence > 0", name="ck_agent_turn_sequence_positive"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_agent_turn_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_conversation.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class AgentMessage(Base):
    __tablename__ = "agent_message"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_agent_message_sequence"),
        CheckConstraint("sequence > 0", name="ck_agent_message_sequence_positive"),
        CheckConstraint("role IN ('USER', 'ASSISTANT', 'SYSTEM', 'TOOL')", name="ck_agent_message_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_conversation.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_turn.id", ondelete="CASCADE"), nullable=True, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    message_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_run.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_run"
    __table_args__ = (
        UniqueConstraint("turn_id", "sequence", name="uq_agent_run_turn_sequence"),
        UniqueConstraint("workflow_id", name="uq_agent_run_workflow_id"),
        CheckConstraint("sequence > 0", name="ck_agent_run_sequence_positive"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_agent_run_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_conversation.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_turn.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    workflow_id: Mapped[str] = mapped_column(String(255))
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_run.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    deferred_from_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_run.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    agent_definition_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_definition_version.id", ondelete="RESTRICT"), index=True
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True)
    model_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    message_history: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    output_text: Mapped[str | None] = mapped_column(Text)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelInvocation(Base):
    __tablename__ = "model_invocation"
    __table_args__ = (
        UniqueConstraint(
            "agent_run_id",
            "attempt",
            "request_sequence",
            name="uq_model_invocation_agent_request",
        ),
        UniqueConstraint(
            "skill_run_id",
            "attempt",
            "request_sequence",
            name="uq_model_invocation_skill_request",
        ),
        CheckConstraint(
            "(agent_run_id IS NOT NULL AND skill_run_id IS NULL) OR "
            "(agent_run_id IS NULL AND skill_run_id IS NOT NULL)",
            name="ck_model_invocation_owner",
        ),
        CheckConstraint("attempt > 0 AND request_sequence > 0", name="ck_model_invocation_sequence"),
        CheckConstraint(
            "status IN ('RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_model_invocation_status",
        ),
        CheckConstraint(
            "request_count >= 0 AND input_tokens >= 0 AND cached_input_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND output_tokens >= 0 AND latency_ms >= 0",
            name="ck_model_invocation_usage",
        ),
        Index("ix_model_invocation_agent_created", "agent_run_id", "created_at"),
        Index("ix_model_invocation_skill_created", "skill_run_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=True, index=True
    )
    skill_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("skill_run.id", ondelete="CASCADE"), nullable=True, index=True
    )
    provider_request_id: Mapped[str | None] = mapped_column(String(500))
    provider: Mapped[str] = mapped_column(String(100), index=True)
    model: Mapped[str] = mapped_column(String(500))
    model_configuration_hash: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    request_sequence: Mapped[int] = mapped_column(Integer)
    request_count: Mapped[int] = mapped_column(Integer, default=1)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="COMPLETED", index=True)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentActionRequest(Base):
    __tablename__ = "agent_action_request"
    __table_args__ = (
        UniqueConstraint("agent_run_id", "tool_call_id", name="uq_agent_action_request_tool_call"),
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXECUTED', 'FAILED')",
            name="ck_agent_action_request_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_conversation.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_turn.id", ondelete="CASCADE"), index=True)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_run.id", ondelete="CASCADE"), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(500))
    tool_key: Mapped[str] = mapped_column(String(150))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON)
    summary: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentActionDecision(Base):
    __tablename__ = "agent_action_decision"
    __table_args__ = (
        UniqueConstraint("action_request_id", name="uq_agent_action_decision_request"),
        CheckConstraint("decision IN ('APPROVE', 'REJECT')", name="ck_agent_action_decision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    action_request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_action_request.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(String(2000))
    decided_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentToolExecution(Base):
    __tablename__ = "agent_tool_execution"
    __table_args__ = (
        CheckConstraint("status IN ('RUNNING', 'COMPLETED', 'FAILED')", name="ck_agent_tool_execution_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("agent_run.id", ondelete="CASCADE"), index=True)
    action_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_action_request.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tool_call_id: Mapped[str] = mapped_column(String(500))
    tool_key: Mapped[str] = mapped_column(String(150), index=True)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MatterDefinition(TimestampMixin, Base):
    __tablename__ = "matter_definition"
    __table_args__ = (
        UniqueConstraint("matter_id", name="uq_matter_definition_matter"),
        CheckConstraint("current_revision > 0", name="ck_matter_definition_current_revision"),
        CheckConstraint(
            "published_revision IS NULL OR (published_revision > 0 AND published_revision <= current_revision)",
            name="ck_matter_definition_published_revision",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    current_revision: Mapped[int] = mapped_column(Integer, default=1)
    published_revision: Mapped[int | None] = mapped_column(Integer)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )


class MatterDefinitionRevision(Base):
    __tablename__ = "matter_definition_revision"
    __table_args__ = (
        UniqueConstraint("matter_definition_id", "revision", name="uq_matter_definition_revision"),
        CheckConstraint("revision > 0", name="ck_matter_definition_revision_positive"),
        CheckConstraint(
            "source_kind IN ('PASTE', 'MARKDOWN', 'TEXT', 'DOCX', 'AGENT_EDIT', 'USER_EDIT', "
            "'ASSESSMENT_REFINEMENT')",
            name="ck_matter_definition_revision_source_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_definition.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    content_markdown: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(20))
    source_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    source_filename: Mapped[str | None] = mapped_column(String(500))
    based_on_revision: Mapped[int | None] = mapped_column(Integer)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    source_skill_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("skill_run.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MatterDefinitionAssessmentRun(TimestampMixin, Base):
    __tablename__ = "matter_definition_assessment_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'PLANNING', 'RETRIEVING', 'BUILDING_BATCH', "
            "'SUMMARIZING', 'SYNTHESIZING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', "
            "'FAILED', 'CANCELED')",
            name="ck_matter_definition_assessment_status",
        ),
        CheckConstraint("requested_document_count > 0", name="ck_matter_definition_assessment_requested_count"),
        CheckConstraint(
            "candidate_count >= 0 AND selected_count >= 0 AND summarized_count >= 0 "
            "AND skipped_count >= 0 AND failed_count >= 0 AND partial_coverage_count >= 0 "
            "AND invalid_result_count >= 0",
            name="ck_matter_definition_assessment_counts",
        ),
        CheckConstraint(
            "guidance_refinement_status IN "
            "('NOT_READY', 'QUEUED', 'RUNNING', 'COMPLETED', 'NOT_REQUIRED', 'FAILED')",
            name="ck_definition_assessment_guidance_refinement_status",
        ),
        Index("ix_definition_assessment_matter_created", "matter_id", "created_at"),
        Index("ix_definition_assessment_matter", "matter_id"),
        Index("ix_definition_assessment_revision", "matter_definition_revision_id"),
        Index("ix_definition_assessment_generation", "search_index_generation_id"),
        Index("ix_definition_assessment_status", "status"),
        Index("ix_definition_assessment_initiator", "initiated_by_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"))
    matter_definition_revision_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_definition_revision.id", ondelete="RESTRICT")
    )
    definition_content_hash: Mapped[str] = mapped_column(String(64))
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="RESTRICT"), unique=True
    )
    search_index_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("search_index_generation.id", ondelete="SET NULL"), nullable=True
    )
    review_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_batch.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    review_batch_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_batch_run.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    configuration_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    binding_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    requested_document_count: Mapped[int] = mapped_column(Integer, default=500)
    control_sample_size: Mapped[int] = mapped_column(Integer, default=0)
    large_run_warning_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    warning_acknowledged_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    warning_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    estimated_output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    token_estimator: Mapped[str | None] = mapped_column(String(100))
    token_estimator_version: Mapped[str | None] = mapped_column(String(100))
    estimation_model: Mapped[str | None] = mapped_column(String(500))
    estimate_source_hashes: Mapped[list[str]] = mapped_column(JSON, default=list)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    selected_count: Mapped[int] = mapped_column(Integer, default=0)
    summarized_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    partial_coverage_count: Mapped[int] = mapped_column(Integer, default=0)
    invalid_result_count: Mapped[int] = mapped_column(Integer, default=0)
    coverage_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    synthesis_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    guidance_refinement_status: Mapped[str] = mapped_column(String(30), default="NOT_READY")
    guidance_refinement_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    refined_matter_definition_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter_definition_revision.id", ondelete="SET NULL"), nullable=True
    )
    guidance_refinement_error_message: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED")
    error_message: Mapped[str | None] = mapped_column(Text)
    initiated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def use_batching(self) -> bool:
        return self.configuration_snapshot.get("use_batching", True) is not False


class MatterDefinitionAssessmentProviderBatch(TimestampMixin, Base):
    __tablename__ = "matter_definition_assessment_provider_batch"
    __table_args__ = (
        UniqueConstraint(
            "review_batch_run_id",
            "ordinal",
            name="uq_definition_assessment_provider_batch_ordinal",
        ),
        UniqueConstraint("provider_batch_id", name="uq_definition_assessment_provider_batch_id"),
        CheckConstraint(
            "status IN ('QUEUED', 'SUBMITTED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', "
            "'FAILED', 'CANCELED')",
            name="ck_definition_assessment_provider_batch_status",
        ),
        CheckConstraint("ordinal > 0 AND request_count >= 0", name="ck_definition_assessment_provider_batch_counts"),
        Index("ix_def_assess_provider_batch_assessment", "assessment_run_id", "status"),
        Index("ix_def_assess_provider_batch_review_run", "review_batch_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    assessment_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("matter_definition_assessment_run.id", ondelete="CASCADE"),
    )
    review_batch_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("review_batch_run.id", ondelete="CASCADE"),
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    document_ids: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED")
    provider: Mapped[str] = mapped_column(String(100), default="google")
    model: Mapped[str] = mapped_column(String(500))
    provider_batch_id: Mapped[str | None] = mapped_column(String(500))
    provider_status: Mapped[str | None] = mapped_column(String(80))
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MatterDefinitionAssessmentQuery(Base):
    __tablename__ = "matter_definition_assessment_query"
    __table_args__ = (
        UniqueConstraint("assessment_run_id", "ordinal", name="uq_definition_assessment_query_ordinal"),
        CheckConstraint("ordinal > 0 AND quota > 0 AND result_count >= 0", name="ck_definition_assessment_query_counts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    assessment_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_definition_assessment_run.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    criterion_key: Mapped[str] = mapped_column(String(200))
    criterion_label: Mapped[str] = mapped_column(String(500))
    rationale: Mapped[str] = mapped_column(Text)
    search_request: Mapped[dict[str, Any]] = mapped_column(JSON)
    quota: Mapped[int] = mapped_column(Integer)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MatterDefinitionAssessmentCandidate(Base):
    __tablename__ = "matter_definition_assessment_candidate"
    __table_args__ = (
        UniqueConstraint(
            "assessment_run_id", "matter_document_id", name="uq_definition_assessment_candidate_document"
        ),
        CheckConstraint("selection_order IS NULL OR selection_order > 0", name="ck_definition_assessment_selection_order"),
        Index("ix_definition_assessment_candidate_selected", "assessment_run_id", "selected"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    assessment_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_definition_assessment_run.id", ondelete="CASCADE"), index=True
    )
    matter_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document.id", ondelete="CASCADE"), index=True
    )
    retrieval_provenance: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    fused_score: Mapped[float] = mapped_column(Float, default=0)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    selection_order: Mapped[int | None] = mapped_column(Integer)
    selection_reason: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MatterDefinitionAssessmentQuestion(TimestampMixin, Base):
    __tablename__ = "matter_definition_assessment_question"
    __table_args__ = (
        CheckConstraint("priority IN ('HIGH', 'MEDIUM', 'LOW')", name="ck_definition_assessment_question_priority"),
        CheckConstraint("status IN ('OPEN', 'ANSWERED', 'DISMISSED')", name="ck_definition_assessment_question_status"),
        Index("ix_definition_assessment_question_status", "assessment_run_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    assessment_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_definition_assessment_run.id", ondelete="CASCADE"), index=True
    )
    question: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(20), default="MEDIUM")
    blocking: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    suggested_answers: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    answer: Mapped[str | None] = mapped_column(Text)
    answered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BatchTopicTaxonomy(TimestampMixin, Base):
    __tablename__ = "batch_topic_taxonomy"
    __table_args__ = (
        UniqueConstraint("review_batch_id", "version", name="uq_batch_topic_taxonomy_version"),
        CheckConstraint("version > 0", name="ck_batch_topic_taxonomy_version_positive"),
        CheckConstraint("status IN ('ACTIVE', 'RETIRED')", name="ck_batch_topic_taxonomy_status"),
        Index(
            "uq_batch_topic_taxonomy_active",
            "review_batch_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
            sqlite_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch.id", ondelete="CASCADE"), index=True
    )
    source_assessment_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_definition_assessment_run.id", ondelete="CASCADE"), unique=True
    )
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")


class BatchTopic(TimestampMixin, Base):
    __tablename__ = "batch_topic"
    __table_args__ = (
        UniqueConstraint("taxonomy_id", "topic_key", name="uq_batch_topic_key"),
        UniqueConstraint("taxonomy_id", "ordinal", name="uq_batch_topic_ordinal"),
        CheckConstraint("ordinal > 0", name="ck_batch_topic_ordinal_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    taxonomy_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("batch_topic_taxonomy.id", ondelete="CASCADE"), index=True
    )
    topic_key: Mapped[str] = mapped_column(String(100))
    label: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    ordinal: Mapped[int] = mapped_column(Integer)


class BatchTopicAssignment(Base):
    __tablename__ = "batch_topic_assignment"
    __table_args__ = (
        UniqueConstraint("taxonomy_id", "matter_document_id", "topic_id", name="uq_batch_topic_assignment"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_batch_topic_assignment_confidence"),
        Index("ix_batch_topic_assignment_document", "matter_document_id", "taxonomy_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch.id", ondelete="CASCADE"), index=True
    )
    taxonomy_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("batch_topic_taxonomy.id", ondelete="CASCADE"), index=True
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("batch_topic.id", ondelete="CASCADE"), index=True)
    matter_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document.id", ondelete="CASCADE"), index=True
    )
    confidence: Mapped[float] = mapped_column(Float)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditRecord(Base):
    __tablename__ = "audit_record"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str] = mapped_column(String(100))
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
