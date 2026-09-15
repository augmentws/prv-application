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


class MatterEmbeddingBatch(TimestampMixin, Base):
    __tablename__ = "matter_embedding_batch"
    __table_args__ = (
        UniqueConstraint("job_id", "batch_number", name="uq_matter_embedding_batch_number"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_matter_embedding_batch_status",
        ),
        CheckConstraint(
            "item_count >= 0 AND processed_count >= 0 AND embedded_count >= 0 AND "
            "skipped_count >= 0 AND failed_count >= 0 AND chunk_count >= 0",
            name="ck_matter_embedding_batch_counts",
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


class MatterTopicJob(TimestampMixin, Base):
    __tablename__ = "matter_topic_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'SAMPLING', 'CLUSTERING', 'PUBLISHING', 'COMPLETED', "
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
            postgresql_where=text("status IN ('QUEUED', 'SAMPLING', 'CLUSTERING', 'PUBLISHING')"),
            sqlite_where=text("status IN ('QUEUED', 'SAMPLING', 'CLUSTERING', 'PUBLISHING')"),
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
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    matter: Mapped[Matter] = relationship()
    embedding_job: Mapped[MatterEmbeddingJob] = relationship()
    metadata_definition: Mapped["MetadataDefinition | None"] = relationship()
    created_by: Mapped[User] = relationship()
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
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')",
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


class ReviewBatch(TimestampMixin, Base):
    __tablename__ = "review_batch"
    __table_args__ = (
        CheckConstraint(
            "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH')",
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


class ReviewBatchRun(TimestampMixin, Base):
    __tablename__ = "review_batch_run"
    __table_args__ = (
        CheckConstraint("run_type IN ('HUMAN', 'AGENT')", name="ck_review_batch_run_type"),
        CheckConstraint("purpose IN ('REVIEW', 'REFERENCE', 'CANDIDATE')", name="ck_review_batch_run_purpose"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_review_batch_run_status",
        ),
        CheckConstraint("result_policy IN ('ISOLATED', 'PUBLISH_TO_MATTER')", name="ck_review_batch_run_policy"),
        CheckConstraint(
            "(run_type = 'HUMAN' AND actor_user_id IS NOT NULL AND agent_definition_version_id IS NULL) OR "
            "(run_type = 'AGENT' AND actor_user_id IS NULL AND agent_definition_version_id IS NOT NULL)",
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
    status: Mapped[str] = mapped_column(String(20), default="RUNNING", index=True)
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
    configuration_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    initiated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    workflow_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    processed_document_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewBatchRunDocument(Base):
    __tablename__ = "review_batch_run_document"
    __table_args__ = (
        CheckConstraint(
            "status IN ('IN_PROGRESS', 'COMPLETED', 'SKIPPED')",
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
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    agent_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_definition.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    system_prompt: Mapped[str] = mapped_column(Text)
    model_key: Mapped[str] = mapped_column(String(200))
    model_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
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


class AgentConversation(TimestampMixin, Base):
    __tablename__ = "agent_conversation"
    __table_args__ = (
        CheckConstraint("workflow_type IN ('MATTER_DEFINITION_SETUP')", name="ck_agent_conversation_workflow_type"),
        CheckConstraint(
            "status IN ('ACTIVE', 'WAITING_APPROVAL', 'COMPLETED', 'FAILED', 'ARCHIVED')",
            name="ck_agent_conversation_status",
        ),
        Index("ix_agent_conversation_matter_created", "matter_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("client.id", ondelete="RESTRICT"), index=True)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    agent_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_definition.id", ondelete="RESTRICT"), index=True
    )
    agent_definition_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_definition_version.id", ondelete="RESTRICT"), index=True
    )
    workflow_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", index=True)
    initiated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )


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
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
            "source_kind IN ('PASTE', 'MARKDOWN', 'TEXT', 'DOCX', 'AGENT_EDIT', 'USER_EDIT')",
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
