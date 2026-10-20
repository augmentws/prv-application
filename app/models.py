import uuid
from datetime import date, datetime, timezone
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


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
    authorization_revision: Mapped[int] = mapped_column(BigInteger, default=1)

    parent: Mapped["Tenant | None"] = relationship(remote_side="Tenant.id", back_populates="children")
    children: Mapped[list["Tenant"]] = relationship(back_populates="parent")


class User(TimestampMixin, Base):
    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("normalized_email", name="uq_user_normalized_email"),
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_user_status"),
        CheckConstraint("tenant_role IN ('ADMIN', 'USER')", name="ck_user_tenant_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    normalized_email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    tenant_role: Mapped[str] = mapped_column(String(20), default="USER")
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


class Permission(TimestampMixin, Base):
    __tablename__ = "permission"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="ck_permission_status"),
        CheckConstraint(
            "resource_type IN ('TENANT', 'CLIENT', 'COLLECTION', 'MATTER')",
            name="ck_permission_resource_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(50), index=True)
    resource_type: Mapped[str] = mapped_column(String(30), index=True)
    dangerous: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")


class Role(TimestampMixin, Base):
    __tablename__ = "role"
    __table_args__ = (
        CheckConstraint("role_kind IN ('SYSTEM', 'CUSTOM')", name="ck_role_kind"),
        CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="ck_role_status"),
        CheckConstraint(
            "(role_kind = 'SYSTEM' AND tenant_id IS NULL) OR (role_kind = 'CUSTOM' AND tenant_id IS NOT NULL)",
            name="ck_role_kind_tenant",
        ),
        Index(
            "uq_system_role_key",
            "key",
            unique=True,
            postgresql_where=text("tenant_id IS NULL"),
            sqlite_where=text("tenant_id IS NULL"),
        ),
        Index(
            "uq_tenant_role_key",
            "tenant_id",
            "key",
            unique=True,
            postgresql_where=text("tenant_id IS NOT NULL"),
            sqlite_where=text("tenant_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tenant.id", ondelete="CASCADE"), nullable=True, index=True
    )
    key: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    role_kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )


class RolePermission(Base):
    __tablename__ = "role_permission"

    role_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("role.id", ondelete="CASCADE"), primary_key=True)
    permission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("permission.id", ondelete="RESTRICT"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AccessGroup(TimestampMixin, Base):
    __tablename__ = "access_group"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_access_group_tenant_key"),
        CheckConstraint("group_kind IN ('LOCAL')", name="ck_access_group_kind"),
        CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="ck_access_group_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    group_kind: Mapped[str] = mapped_column(String(20), default="LOCAL")
    external_reference: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"))


class AccessGroupMember(Base):
    __tablename__ = "access_group_member"

    access_group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("access_group.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True)
    added_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthorizationGrant(TimestampMixin, Base):
    __tablename__ = "authorization_grant"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL AND access_group_id IS NULL) OR (user_id IS NULL AND access_group_id IS NOT NULL)",
            name="ck_authorization_grant_subject",
        ),
        CheckConstraint(
            "(scope_type = 'TENANT' AND tenant_scope_id IS NOT NULL AND client_id IS NULL "
            "AND matter_id IS NULL AND collection_id IS NULL) OR "
            "(scope_type = 'CLIENT' AND tenant_scope_id IS NULL AND client_id IS NOT NULL "
            "AND matter_id IS NULL AND collection_id IS NULL) OR "
            "(scope_type = 'MATTER' AND tenant_scope_id IS NULL AND client_id IS NULL "
            "AND matter_id IS NOT NULL AND collection_id IS NULL) OR "
            "(scope_type = 'COLLECTION' AND tenant_scope_id IS NULL AND client_id IS NULL "
            "AND matter_id IS NULL AND collection_id IS NOT NULL)",
            name="ck_authorization_grant_scope",
        ),
        Index("ix_authorization_grant_tenant_scope", "tenant_id", "scope_type"),
        Index("ix_authorization_grant_client", "client_id"),
        Index("ix_authorization_grant_matter", "matter_id"),
        Index("ix_authorization_grant_collection", "collection_id"),
        Index(
            "uq_authorization_grant_user_tenant",
            "user_id",
            "role_id",
            "tenant_scope_id",
            unique=True,
            postgresql_where=text("scope_type = 'TENANT' AND user_id IS NOT NULL"),
            sqlite_where=text("scope_type = 'TENANT' AND user_id IS NOT NULL"),
        ),
        Index(
            "uq_authorization_grant_group_tenant",
            "access_group_id",
            "role_id",
            "tenant_scope_id",
            unique=True,
            postgresql_where=text("scope_type = 'TENANT' AND access_group_id IS NOT NULL"),
            sqlite_where=text("scope_type = 'TENANT' AND access_group_id IS NOT NULL"),
        ),
        Index(
            "uq_authorization_grant_user_client",
            "user_id",
            "role_id",
            "client_id",
            unique=True,
            postgresql_where=text("scope_type = 'CLIENT' AND user_id IS NOT NULL"),
            sqlite_where=text("scope_type = 'CLIENT' AND user_id IS NOT NULL"),
        ),
        Index(
            "uq_authorization_grant_group_client",
            "access_group_id",
            "role_id",
            "client_id",
            unique=True,
            postgresql_where=text("scope_type = 'CLIENT' AND access_group_id IS NOT NULL"),
            sqlite_where=text("scope_type = 'CLIENT' AND access_group_id IS NOT NULL"),
        ),
        Index(
            "uq_authorization_grant_user_matter",
            "user_id",
            "role_id",
            "matter_id",
            unique=True,
            postgresql_where=text("scope_type = 'MATTER' AND user_id IS NOT NULL"),
            sqlite_where=text("scope_type = 'MATTER' AND user_id IS NOT NULL"),
        ),
        Index(
            "uq_authorization_grant_group_matter",
            "access_group_id",
            "role_id",
            "matter_id",
            unique=True,
            postgresql_where=text("scope_type = 'MATTER' AND access_group_id IS NOT NULL"),
            sqlite_where=text("scope_type = 'MATTER' AND access_group_id IS NOT NULL"),
        ),
        Index(
            "uq_authorization_grant_user_collection",
            "user_id",
            "role_id",
            "collection_id",
            unique=True,
            postgresql_where=text("scope_type = 'COLLECTION' AND user_id IS NOT NULL"),
            sqlite_where=text("scope_type = 'COLLECTION' AND user_id IS NOT NULL"),
        ),
        Index(
            "uq_authorization_grant_group_collection",
            "access_group_id",
            "role_id",
            "collection_id",
            unique=True,
            postgresql_where=text("scope_type = 'COLLECTION' AND access_group_id IS NOT NULL"),
            sqlite_where=text("scope_type = 'COLLECTION' AND access_group_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=True, index=True
    )
    access_group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("access_group.id", ondelete="CASCADE"), nullable=True, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("role.id", ondelete="RESTRICT"), index=True)
    scope_type: Mapped[str] = mapped_column(String(20))
    tenant_scope_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tenant.id", ondelete="CASCADE"), nullable=True
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("client.id", ondelete="CASCADE"), nullable=True
    )
    matter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter.id", ondelete="CASCADE"), nullable=True
    )
    collection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"))
    updated_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"))
    row_version: Mapped[int] = mapped_column(Integer, default=1)


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
            "added_count >= 0 AND duplicate_count >= 0 AND failed_count >= 0 AND "
            "imported_label_document_count >= 0 AND imported_label_event_count >= 0 AND "
            "imported_label_warning_count >= 0",
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
    current_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("workflow_run.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    artifact_selection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    matched_count: Mapped[int] = mapped_column(Integer, default=0)
    batch_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    added_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_label_document_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_label_event_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_label_warning_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    matter: Mapped[Matter] = relationship()
    created_by: Mapped[User] = relationship()
    current_workflow_run: Mapped["WorkflowRun | None"] = relationship(foreign_keys=[current_workflow_run_id])

    @property
    def execution(self) -> "WorkflowRun | None":
        return self.current_workflow_run

class MatterDocumentImportBatch(TimestampMixin, Base):
    __tablename__ = "matter_document_import_batch"
    __table_args__ = (
        UniqueConstraint("job_id", "batch_number", name="uq_matter_import_batch_number"),
        CheckConstraint("status IN ('QUEUED', 'COMPLETED', 'FAILED')", name="ck_matter_import_batch_status"),
        CheckConstraint(
            "item_count >= 0 AND added_count >= 0 AND duplicate_count >= 0 AND failed_count >= 0 AND "
            "imported_label_document_count >= 0 AND imported_label_event_count >= 0 AND "
            "imported_label_warning_count >= 0",
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
    imported_label_document_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_label_event_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_label_warning_count: Mapped[int] = mapped_column(Integer, default=0)
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
            "embedded_count >= 0 AND reused_count >= 0 AND skipped_count >= 0 AND "
            "failed_count >= 0 AND chunk_count >= 0",
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
    current_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("workflow_run.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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
    reused_count: Mapped[int] = mapped_column(Integer, default=0)
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
    current_workflow_run: Mapped["WorkflowRun | None"] = relationship(foreign_keys=[current_workflow_run_id])

    @property
    def execution(self) -> "WorkflowRun | None":
        return self.current_workflow_run

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
            "reused_count >= 0 AND skipped_count >= 0 AND failed_count >= 0 AND chunk_count >= 0",
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
    reused_count: Mapped[int] = mapped_column(Integer, default=0)
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


class JobOutcomeManifest(TimestampMixin, Base):
    __tablename__ = "job_outcome_manifest"
    __table_args__ = (
        UniqueConstraint("job_type", "job_id", name="uq_job_outcome_manifest_job"),
        CheckConstraint(
            "manifest_status IN ('BUILDING', 'FINALIZED', 'FAILED')",
            name="ck_job_outcome_manifest_status",
        ),
        CheckConstraint("outcome_schema_version > 0", name="ck_job_outcome_manifest_schema_version"),
        CheckConstraint("item_count >= 0", name="ck_job_outcome_manifest_item_count"),
        CheckConstraint(
            "(artifact_id IS NULL AND artifact_content_hash IS NULL AND artifact_byte_length IS NULL) OR "
            "(artifact_id IS NOT NULL AND artifact_content_hash IS NOT NULL AND artifact_byte_length IS NOT NULL)",
            name="ck_job_outcome_manifest_artifact_tuple",
        ),
        CheckConstraint(
            "manifest_status != 'FINALIZED' OR (artifact_id IS NOT NULL AND finalized_at IS NOT NULL)",
            name="ck_job_outcome_manifest_finalized",
        ),
        Index("ix_job_outcome_manifest_scope", "tenant_id", "client_id", "matter_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("client.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    matter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter.id", ondelete="CASCADE"), nullable=True, index=True
    )
    job_type: Mapped[str] = mapped_column(String(80))
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    manifest_status: Mapped[str] = mapped_column(String(20), default="BUILDING")
    outcome_schema: Mapped[str] = mapped_column(String(100))
    outcome_schema_version: Mapped[int] = mapped_column(Integer)
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    artifact_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_byte_length: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    derivation_key: Mapped[str] = mapped_column(String(64))
    item_count: Mapped[int] = mapped_column(BigInteger, default=0)
    status_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reason_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    producer_kind: Mapped[str] = mapped_column(String(50))
    producer_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="RESTRICT"), index=True
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class AnalysisInputManifest(TimestampMixin, Base):
    """One Core control record for an immutable, Artifact-owned document input manifest."""

    __tablename__ = "analysis_input_manifest"
    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", name="uq_analysis_input_manifest_owner"),
        CheckConstraint(
            "manifest_status IN ('BUILDING', 'FINALIZED', 'FAILED')",
            name="ck_analysis_input_manifest_status",
        ),
        CheckConstraint("manifest_schema_version > 0", name="ck_analysis_input_manifest_schema_version"),
        CheckConstraint("item_count >= 0", name="ck_analysis_input_manifest_item_count"),
        CheckConstraint(
            "(artifact_id IS NULL AND artifact_content_hash IS NULL AND artifact_byte_length IS NULL) OR "
            "(artifact_id IS NOT NULL AND artifact_content_hash IS NOT NULL AND artifact_byte_length IS NOT NULL)",
            name="ck_analysis_input_manifest_artifact_tuple",
        ),
        CheckConstraint(
            "manifest_status != 'FINALIZED' OR (artifact_id IS NOT NULL AND finalized_at IS NOT NULL)",
            name="ck_analysis_input_manifest_finalized",
        ),
        Index("ix_analysis_input_manifest_scope", "tenant_id", "client_id", "matter_id"),
        Index("ix_analysis_input_manifest_derivation", "derivation_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("client.id", ondelete="RESTRICT"), index=True
    )
    matter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True
    )
    owner_type: Mapped[str] = mapped_column(String(80))
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    scope_type: Mapped[str] = mapped_column(String(40))
    scope_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    manifest_schema: Mapped[str] = mapped_column(String(100))
    manifest_schema_version: Mapped[int] = mapped_column(Integer)
    resolver_version: Mapped[str] = mapped_column(String(100))
    input_contract: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    input_contract_hash: Mapped[str] = mapped_column(String(64))
    derivation_key: Mapped[str] = mapped_column(String(64))
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    artifact_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_byte_length: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    item_count: Mapped[int] = mapped_column(BigInteger, default=0)
    manifest_status: Mapped[str] = mapped_column(String(20), default="BUILDING")
    producer_kind: Mapped[str] = mapped_column(String(50))
    producer_run_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


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


class MatterSearchExportJob(TimestampMixin, Base):
    __tablename__ = "matter_search_export_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_matter_search_export_status",
        ),
        CheckConstraint(
            "matched_count >= 0 AND exported_count >= 0 AND page_count >= 0 "
            "AND (output_byte_length IS NULL OR output_byte_length >= 0)",
            name="ck_matter_search_export_counts",
        ),
        UniqueConstraint(
            "matter_id",
            "created_by_user_id",
            "idempotency_key",
            name="uq_matter_search_export_idempotency",
        ),
        Index("ix_matter_search_export_matter_created", "matter_id", "created_at"),
        Index("ix_matter_search_export_matter_status_created", "matter_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    search_index_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("search_index_generation.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    search_definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    search_definition_hash: Mapped[str] = mapped_column(String(64), index=True)
    search_index_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    field_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    field_snapshot_hash: Mapped[str] = mapped_column(String(64), index=True)
    csv_policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    requested_filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="QUEUED")
    current_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("workflow_run.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    workflow_id: Mapped[str] = mapped_column(String(255), unique=True)
    current_stage: Mapped[str | None] = mapped_column(String(100))
    matched_count: Mapped[int] = mapped_column(BigInteger, default=0)
    exported_count: Mapped[int] = mapped_column(BigInteger, default=0)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    output_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    output_content_hash: Mapped[str | None] = mapped_column(String(64))
    output_byte_length: Mapped[int | None] = mapped_column(BigInteger)
    output_filename: Mapped[str | None] = mapped_column(String(255))
    snapshot_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    retry_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_summary: Mapped[str | None] = mapped_column(Text)
    failure_retriable: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    matter: Mapped["Matter"] = relationship()
    created_by: Mapped["User"] = relationship(foreign_keys=[created_by_user_id])
    current_workflow_run: Mapped["WorkflowRun | None"] = relationship(foreign_keys=[current_workflow_run_id])

    @property
    def execution(self) -> "WorkflowRun | None":
        return self.current_workflow_run

    @property
    def selected_field_count(self) -> int:
        return len(self.field_snapshot or [])

    @property
    def created_by_display_name(self) -> str:
        return self.created_by.display_name

    @property
    def created_by_email(self) -> str:
        return self.created_by.email


class SearchProjectionOperation(TimestampMixin, Base):
    __tablename__ = "search_projection_operation"
    __table_args__ = (
        UniqueConstraint("workflow_id", name="uq_search_projection_workflow"),
        CheckConstraint(
            "kind IN ('SCHEMA_SYNC', 'REBUILD', 'DOCUMENT_UPSERT', 'DOCUMENT_DELETE', 'BATCH_CODING_SYNC')",
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
            "status IN ('QUEUED', 'SNAPSHOTTING', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED')",
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


class AnalysisTaskBatchSet(TimestampMixin, Base):
    __tablename__ = "analysis_task_batch_set"
    __table_args__ = (
        CheckConstraint(
            "population_source_type IN ('ALL_MATTER', 'SOURCE_BATCH', 'SEARCH_QUERY', 'SAVED_SEARCH')",
            name="ck_analysis_task_batch_set_source_type",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'BUILDING', 'READY', 'FAILED', 'CANCELED', 'ARCHIVED')",
            name="ck_analysis_task_batch_set_status",
        ),
        CheckConstraint("target_batch_size > 0", name="ck_analysis_task_batch_set_target_size"),
        CheckConstraint(
            "population_count >= 0 AND batch_count >= 0",
            name="ck_analysis_task_batch_set_counts",
        ),
        Index("ix_analysis_task_batch_set_task_created", "matter_analysis_task_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    matter_analysis_task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_analysis_task.id", ondelete="CASCADE"), index=True
    )
    matter_analysis_task_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_analysis_task_version.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    population_source_type: Mapped[str] = mapped_column(String(30))
    selection_definition: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
    source_review_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_batch.id", ondelete="RESTRICT", use_alter=True), nullable=True, index=True
    )
    search_index_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("search_index_generation.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    target_batch_size: Mapped[int] = mapped_column(Integer)
    allocation_policy: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
    random_seed: Mapped[str] = mapped_column(String(100))
    allocation_algorithm_version: Mapped[str] = mapped_column(String(50), default="stable-sequence-v1")
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    workflow_id: Mapped[str] = mapped_column(String(255), unique=True)
    current_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="SET NULL"), nullable=True, index=True
    )
    population_count: Mapped[int] = mapped_column(Integer, default=0)
    batch_count: Mapped[int] = mapped_column(Integer, default=0)
    population_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    warning_details: Mapped[list[dict[str, Any]]] = mapped_column(JSON_DOCUMENT, default=list)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    current_workflow_run: Mapped["WorkflowRun | None"] = relationship(foreign_keys=[current_workflow_run_id])


class ReviewBatch(TimestampMixin, Base):
    __tablename__ = "review_batch"
    __table_args__ = (
        CheckConstraint(
            "selection_type IN ('ALL_MATTER', 'SEARCH_QUERY', 'RANDOM_MATTER', 'RANDOM_BATCH', "
            "'RANDOM_SAVED_SEARCH', 'RANDOM_ASSESSMENT', 'DEFINITION_ASSESSMENT', "
            "'CLASSIFICATION_PREDICTIONS', 'COMPARISON_RESULT', 'TASK_BATCH_PARTITION')",
            name="ck_review_batch_selection_type",
        ),
        CheckConstraint(
            "reviewer_value_visibility IN ('OWN_VALUES', 'ALL_REVIEWER_VALUES')",
            name="ck_review_batch_value_visibility",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'BUILDING', 'READY', 'FAILED', 'CANCELED', 'ARCHIVED')",
            name="ck_review_batch_status",
        ),
        CheckConstraint(
            "search_status IN ('QUEUED', 'SYNCING', 'READY', 'FAILED', 'NOT_CONFIGURED')",
            name="ck_review_batch_search_status",
        ),
        CheckConstraint("document_count >= 0", name="ck_review_batch_document_count"),
        CheckConstraint("sample_size IS NULL OR sample_size > 0", name="ck_review_batch_sample_size"),
        UniqueConstraint("id", "analysis_task_batch_set_id", name="uq_review_batch_id_batch_set"),
        UniqueConstraint(
            "analysis_task_batch_set_id", "batch_set_ordinal", name="uq_review_batch_batch_set_ordinal"
        ),
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
    analysis_task_batch_set_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("analysis_task_batch_set.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    batch_set_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    current_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("workflow_run.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    current_workflow_run: Mapped["WorkflowRun | None"] = relationship(foreign_keys=[current_workflow_run_id])

    @property
    def execution(self) -> "WorkflowRun | None":
        return self.current_workflow_run


class ReviewBatchDocument(Base):
    __tablename__ = "review_batch_document"
    __table_args__ = (
        UniqueConstraint("review_batch_id", "sequence_number", name="uq_review_batch_document_sequence"),
        UniqueConstraint(
            "analysis_task_batch_set_id",
            "matter_document_id",
            name="uq_review_batch_document_batch_set_document",
        ),
        ForeignKeyConstraint(
            ["review_batch_id", "analysis_task_batch_set_id"],
            ["review_batch.id", "review_batch.analysis_task_batch_set_id"],
            name="fk_review_batch_document_batch_set",
        ),
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
    analysis_task_batch_set_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("analysis_task_batch_set.id", ondelete="RESTRICT"), nullable=True, index=True
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
            "status IN ('QUEUED', 'RUNNING', 'WAITING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_workflow_run_status",
        ),
        CheckConstraint("attempt_number > 0", name="ck_workflow_run_attempt_positive"),
        CheckConstraint(
            "progress_completed >= 0 AND (progress_total IS NULL OR progress_total >= 0)",
            name="ck_workflow_run_progress_nonnegative",
        ),
        UniqueConstraint(
            "job_type",
            "job_id",
            "attempt_number",
            name="uq_workflow_run_job_attempt",
        ),
        Index("ix_workflow_run_matter_created", "matter_id", "created_at"),
        Index("ix_workflow_run_job_attempt", "job_type", "job_id", "attempt_number"),
        Index("ix_workflow_run_matter_status_created", "matter_id", "status", "created_at"),
        Index("ix_workflow_run_status_heartbeat", "status", "last_heartbeat_at"),
        Index("ix_workflow_run_retry_of", "retry_of_workflow_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("client.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    matter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter.id", ondelete="CASCADE"), nullable=True, index=True
    )
    job_type: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    retry_of_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("workflow_run.id", ondelete="SET NULL"),
        nullable=True,
    )
    workflow_key: Mapped[str] = mapped_column(String(100), index=True)
    code_version: Mapped[str] = mapped_column(String(100))
    dbos_workflow_id: Mapped[str] = mapped_column(String(255), unique=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    current_stage: Mapped[str | None] = mapped_column(String(100), nullable=True)
    progress_completed: Mapped[int] = mapped_column(BigInteger, default=0)
    progress_total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    progress_unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
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
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_retriable: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def stage_label(self) -> str:
        return (self.current_stage or self.status).replace("_", " ").title()

    @property
    def available_actions(self) -> list[str]:
        actions: list[str] = []
        if (
            self.job_type
            in {
                "CLASSIFICATION_ANALYSIS_TASK",
                "MATTER_DOCUMENT_IMPORT",
                "MATTER_EMBEDDING_JOB",
                "REVIEW_BATCH_MATERIALIZATION",
                "CLASSIFICATION_TRAINING_JOB",
                "CLASSIFICATION_SCORING_RUN",
                "ANALYSIS_TASK_BATCH_RUN",
                "MATTER_SEARCH_EXPORT",
                "FACT_DEVELOPMENT_ANALYSIS_TASK",
            }
            and self.status not in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"}
            and self.cancel_requested_at is None
        ):
            actions.append("CANCEL")
        elif self.job_type in {
            "CLASSIFICATION_ANALYSIS_TASK",
            "REVIEW_BATCH_MATERIALIZATION",
            "CLASSIFICATION_TRAINING_JOB",
            "CLASSIFICATION_SCORING_RUN",
            "CLASSIFICATION_PROJECTION",
            "MATTER_SEARCH_EXPORT",
        } and (self.status == "CANCELED" or (self.status == "FAILED" and self.failure_retriable)):
            actions.append("RETRY")
        elif self.job_type == "FACT_DEVELOPMENT_ANALYSIS_TASK" and self.status == "FAILED" and self.failure_retriable:
            # Cancellation finalizes the run's outcomes, so only a failed run can resume.
            actions.append("RETRY")
        return actions


class JobArchive(Base):
    __tablename__ = "job_archive"
    __table_args__ = (
        Index("ix_job_archive_matter_archived", "matter_id", "archived_at"),
    )

    job_type: Mapped[str] = mapped_column(String(80), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="CASCADE"), index=True)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    archived_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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


class ReviewBatchReviewerCoding(TimestampMixin, Base):
    __tablename__ = "review_batch_reviewer_coding"
    __table_args__ = (
        UniqueConstraint("review_batch_id", "reviewer_user_id", name="uq_review_batch_reviewer_coding_user"),
        CheckConstraint("revision >= 0", name="ck_review_batch_reviewer_coding_revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch.id", ondelete="CASCADE"), index=True
    )
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int] = mapped_column(BigInteger, default=0)


class ReviewBatchReviewerRevision(Base):
    __tablename__ = "review_batch_reviewer_revision"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_reviewer_revision_number"),
        CheckConstraint(
            "origin IN ('REVIEW_UI', 'IMPORT', 'SYSTEM_MIGRATION')",
            name="ck_reviewer_revision_origin",
        ),
        UniqueConstraint(
            "reviewer_coding_id",
            "request_key",
            name="uq_reviewer_revision_request",
        ),
        Index(
            "ix_reviewer_revision_coding_desc",
            "reviewer_coding_id",
            "revision",
        ),
        Index("ix_reviewer_revision_actor_user_id", "actor_user_id"),
    )

    reviewer_coding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("review_batch_reviewer_coding.id", ondelete="CASCADE"),
        primary_key=True,
    )
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        nullable=True,
    )
    origin: Mapped[str] = mapped_column(String(30))
    request_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewBatchReviewerDocumentRevision(Base):
    __tablename__ = "review_batch_reviewer_document_revision"
    __table_args__ = (
        ForeignKeyConstraint(
            ["reviewer_coding_id", "revision"],
            [
                "review_batch_reviewer_revision.reviewer_coding_id",
                "review_batch_reviewer_revision.revision",
            ],
            ondelete="CASCADE",
            name="fk_reviewer_doc_revision_header",
        ),
        CheckConstraint("revision >= 1", name="ck_reviewer_doc_revision_number"),
        CheckConstraint(
            "status IN ('IN_PROGRESS', 'COMPLETED', 'SKIPPED')",
            name="ck_reviewer_doc_revision_status",
        ),
        Index(
            "ix_reviewer_doc_revision_lookup",
            "reviewer_coding_id",
            "matter_document_id",
            "revision",
        ),
    )

    reviewer_coding_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    matter_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("matter_document.id", ondelete="CASCADE"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewBatchReviewerFieldRevision(Base):
    __tablename__ = "review_batch_reviewer_field_revision"
    __table_args__ = (
        ForeignKeyConstraint(
            ["reviewer_coding_id", "revision", "matter_document_id"],
            [
                "review_batch_reviewer_document_revision.reviewer_coding_id",
                "review_batch_reviewer_document_revision.revision",
                "review_batch_reviewer_document_revision.matter_document_id",
            ],
            ondelete="CASCADE",
            name="fk_reviewer_field_revision_document",
        ),
        CheckConstraint("revision >= 1", name="ck_reviewer_field_revision_number"),
        Index(
            "ix_reviewer_field_revision_lookup",
            "reviewer_coding_id",
            "matter_document_id",
            "metadata_definition_id",
            "revision",
        ),
    )

    reviewer_coding_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    matter_document_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    metadata_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("metadata_definition.id", ondelete="RESTRICT"),
        primary_key=True,
    )


class ReviewBatchReviewerValueRevision(Base):
    __tablename__ = "review_batch_reviewer_value_revision"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "reviewer_coding_id",
                "revision",
                "matter_document_id",
                "metadata_definition_id",
            ],
            [
                "review_batch_reviewer_field_revision.reviewer_coding_id",
                "review_batch_reviewer_field_revision.revision",
                "review_batch_reviewer_field_revision.matter_document_id",
                "review_batch_reviewer_field_revision.metadata_definition_id",
            ],
            ondelete="CASCADE",
            name="fk_reviewer_value_revision_field",
        ),
        CheckConstraint("revision >= 1", name="ck_reviewer_value_revision_number"),
        CheckConstraint("value_ordinal >= 0", name="ck_reviewer_value_revision_ordinal"),
        CheckConstraint(
            "(CASE WHEN value_text IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_long IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_float IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_boolean IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_date IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_datetime IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_json IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_reviewer_value_revision_shape",
        ),
        Index(
            "ix_reviewer_value_revision_document",
            "reviewer_coding_id",
            "matter_document_id",
            "metadata_definition_id",
        ),
    )

    reviewer_coding_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    matter_document_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    metadata_definition_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    value_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_long: Mapped[int | None] = mapped_column(BigInteger)
    value_float: Mapped[float | None] = mapped_column(Float)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean)
    value_date: Mapped[date | None] = mapped_column(Date)
    value_datetime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    value_json: Mapped[Any | None] = mapped_column(JSON(none_as_null=True))


class ReviewBatchReviewerDocument(Base):
    __tablename__ = "review_batch_reviewer_document"
    __table_args__ = (
        CheckConstraint(
            "status IN ('IN_PROGRESS', 'COMPLETED', 'SKIPPED')",
            name="ck_review_batch_reviewer_document_status",
        ),
        CheckConstraint("revision >= 1", name="ck_review_batch_reviewer_document_revision"),
        Index("ix_review_batch_reviewer_document_coding_status", "reviewer_coding_id", "status"),
    )

    reviewer_coding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch_reviewer_coding.id", ondelete="CASCADE"), primary_key=True
    )
    matter_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ReviewBatchReviewerValue(Base):
    __tablename__ = "review_batch_reviewer_value"
    __table_args__ = (
        CheckConstraint("value_ordinal >= 0", name="ck_review_batch_reviewer_value_ordinal"),
        CheckConstraint("revision >= 1", name="ck_review_batch_reviewer_value_revision"),
        CheckConstraint(
            "(CASE WHEN value_text IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_long IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_float IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_boolean IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_date IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_datetime IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_json IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_review_batch_reviewer_value_shape",
        ),
        Index("ix_review_batch_reviewer_value_document", "matter_document_id", "metadata_definition_id"),
    )

    reviewer_coding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch_reviewer_coding.id", ondelete="CASCADE"), primary_key=True
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
    revision: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ReviewBatchRun(TimestampMixin, Base):
    __tablename__ = "review_batch_run"
    __table_args__ = (
        CheckConstraint(
            "run_type IN ('AGENT', 'WORKFLOW', 'CLASSIFIER')",
            name="ck_review_batch_run_type",
        ),
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
            "(run_type = 'AGENT' AND actor_user_id IS NULL "
            "AND agent_definition_version_id IS NOT NULL AND workflow_run_record_id IS NULL "
            "AND classification_scoring_run_id IS NULL) OR "
            "(run_type = 'WORKFLOW' AND actor_user_id IS NULL "
            "AND agent_definition_version_id IS NULL AND workflow_run_record_id IS NOT NULL "
            "AND classification_scoring_run_id IS NULL) OR "
            "(run_type = 'CLASSIFIER' AND actor_user_id IS NULL "
            "AND agent_definition_version_id IS NULL AND workflow_run_record_id IS NULL "
            "AND classification_scoring_run_id IS NOT NULL)",
            name="ck_review_batch_run_actor",
        ),
        CheckConstraint("processed_document_count >= 0", name="ck_review_batch_run_processed_count"),
        Index("ix_review_batch_run_batch_created", "review_batch_id", "created_at"),
        Index(
            "uq_review_batch_classifier_idempotency",
            "review_batch_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("run_type = 'CLASSIFIER'"),
            sqlite_where=text("run_type = 'CLASSIFIER'"),
        ),
        Index(
            "uq_review_batch_classifier_derivation",
            "review_batch_id",
            "classification_scoring_run_id",
            "derivation_key",
            unique=True,
            postgresql_where=text("run_type = 'CLASSIFIER'"),
            sqlite_where=text("run_type = 'CLASSIFIER'"),
        ),
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
    classification_scoring_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("classification_scoring_run.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    derivation_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    configuration_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    initiated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    dbos_workflow_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    current_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("workflow_run.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    processed_document_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    current_workflow_run: Mapped["WorkflowRun | None"] = relationship(foreign_keys=[current_workflow_run_id])

    @property
    def execution(self) -> "WorkflowRun | None":
        return self.current_workflow_run


class ReviewBatchRunDocument(Base):
    __tablename__ = "review_batch_run_document"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'IN_PROGRESS', 'COMPLETED', 'SKIPPED', 'FAILED')",
            name="ck_review_batch_run_document_status",
        ),
        Index("ix_review_batch_run_document_run_status", "review_batch_run_id", "status"),
        Index("ix_review_batch_run_document_last_workflow", "last_workflow_run_id"),
    )

    review_batch_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch_run.id", ondelete="CASCADE"), primary_key=True
    )
    matter_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="IN_PROGRESS")
    last_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("workflow_run.id", ondelete="SET NULL"),
        nullable=True,
    )
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_retriable: Mapped[bool] = mapped_column(Boolean, default=False)
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
        CheckConstraint(
            "confidence_kind IS NULL OR (confidence IS NULL AND confidence_kind = 'NONE') OR "
            "(confidence IS NOT NULL AND confidence_kind IN "
            "('PROVIDER_CONFIDENCE', 'SELECTED_PROBABILITY', 'DERIVED_PROBABILITY'))",
            name="ck_review_batch_run_value_confidence_kind",
        ),
        Index("ix_review_batch_run_value_document", "matter_document_id", "metadata_definition_id"),
        Index("ix_review_batch_run_value_decision_result", "review_decision_result_id"),
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
    confidence_kind: Mapped[str | None] = mapped_column(String(30))
    review_decision_result_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_decision_result.id", ondelete="RESTRICT"), nullable=True
    )
    question_key: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ReviewBatchRunPromotion(TimestampMixin, Base):
    __tablename__ = "review_batch_run_promotion"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="ck_review_batch_run_promotion_status",
        ),
        CheckConstraint("mode = 'REPLACE'", name="ck_review_batch_run_promotion_mode"),
        CheckConstraint(
            "total_document_count >= 0 AND eligible_document_count >= 0 "
            "AND processed_document_count >= 0 AND skipped_document_count >= 0 "
            "AND event_count >= 0",
            name="ck_review_batch_run_promotion_counts",
        ),
        CheckConstraint(
            "reviewer_coding_revision IS NULL OR reviewer_coding_revision >= 0",
            name="ck_review_batch_run_promotion_reviewer_revision",
        ),
        CheckConstraint(
            "(review_batch_run_id IS NOT NULL AND reviewer_coding_id IS NULL "
            "AND reviewer_coding_revision IS NULL) OR "
            "(review_batch_run_id IS NULL AND reviewer_coding_id IS NOT NULL "
            "AND reviewer_coding_revision IS NOT NULL)",
            name="ck_review_batch_run_promotion_source",
        ),
        UniqueConstraint("review_batch_run_id", name="uq_review_batch_run_promotion_run"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    review_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch.id", ondelete="CASCADE"), index=True
    )
    review_batch_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_batch_run.id", ondelete="CASCADE"), nullable=True, index=True
    )
    reviewer_coding_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_batch_reviewer_coding.id", ondelete="CASCADE"), nullable=True, index=True
    )
    reviewer_coding_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    matter_analysis_task_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("matter_analysis_task_version.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    mode: Mapped[str] = mapped_column(String(20), default="REPLACE")
    destination_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON_DOCUMENT, default=list)
    total_document_count: Mapped[int] = mapped_column(Integer, default=0)
    eligible_document_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_document_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_document_count: Mapped[int] = mapped_column(Integer, default=0)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )
    dbos_workflow_id: Mapped[str] = mapped_column(String(255), unique=True)
    current_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("workflow_run.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    search_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("search_projection_operation.id", ondelete="SET NULL"), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    current_workflow_run: Mapped["WorkflowRun | None"] = relationship(foreign_keys=[current_workflow_run_id])

    @property
    def execution(self) -> "WorkflowRun | None":
        return self.current_workflow_run


class ReviewBatchPromotionDocument(Base):
    __tablename__ = "review_batch_promotion_document"

    promotion_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch_run_promotion.id", ondelete="CASCADE"), primary_key=True
    )
    matter_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class ReviewBatchPromotionValue(Base):
    __tablename__ = "review_batch_promotion_value"
    __table_args__ = (
        CheckConstraint("value_ordinal >= 0", name="ck_review_batch_promotion_value_ordinal"),
        CheckConstraint(
            "(CASE WHEN value_text IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_long IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_float IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_boolean IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_date IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_datetime IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN value_json IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_review_batch_promotion_value_shape",
        ),
    )

    promotion_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch_run_promotion.id", ondelete="CASCADE"), primary_key=True
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


class ReviewBatchSearchCodingRun(TimestampMixin, Base):
    """The batch run currently selected for Batch Coding search and facets."""

    __tablename__ = "review_batch_search_coding_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'SYNCING', 'READY', 'FAILED', 'NOT_CONFIGURED')",
            name="ck_review_batch_search_coding_status",
        ),
        UniqueConstraint("review_batch_run_id", name="uq_review_batch_search_coding_run"),
        Index("ix_review_batch_search_coding_run_id", "review_batch_run_id"),
        Index("ix_review_batch_search_coding_status", "status"),
        Index("ix_review_batch_search_coding_user", "selected_by_user_id"),
    )

    review_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch.id", ondelete="CASCADE"), primary_key=True
    )
    review_batch_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("review_batch_run.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")
    selected_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"))
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    projected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class ReviewBatchSearchCodingProjection(TimestampMixin, Base):
    """A retained search projection for one immutable batch run."""

    __tablename__ = "review_batch_search_coding_projection"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'SYNCING', 'READY', 'FAILED', 'NOT_CONFIGURED')",
            name="ck_review_batch_search_projection_status",
        ),
        Index("ix_review_batch_search_projection_run", "review_batch_run_id"),
        Index("ix_review_batch_search_projection_status", "status"),
        Index("ix_review_batch_search_projection_generation", "search_index_generation_id"),
        Index("ix_review_batch_search_projection_user", "requested_by_user_id"),
    )

    review_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("review_batch.id", ondelete="CASCADE"),
        primary_key=True,
    )
    review_batch_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("review_batch_run.id", ondelete="CASCADE"),
        primary_key=True,
    )
    search_index_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("search_index_generation.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="RESTRICT"),
    )
    projected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


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
    hierarchy_separator: Mapped[str | None] = mapped_column(String(10))
    reviewable: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_assignable: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")

    matter: Mapped[Matter] = relationship()


class ClassificationModel(TimestampMixin, Base):
    __tablename__ = "classification_model"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')",
            name="ck_classification_model_status",
        ),
        CheckConstraint("row_version > 0", name="ck_classification_model_row_version"),
        ForeignKeyConstraint(
            ["current_configuration_id", "id"],
            [
                "classification_model_configuration.id",
                "classification_model_configuration.classification_model_id",
            ],
            name="fk_classification_model_current_configuration",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["active_model_version_id", "id"],
            [
                "classification_model_version.id",
                "classification_model_version.classification_model_id",
            ],
            name="fk_classification_model_active_version",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        Index("ix_classification_model_matter_created", "matter_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="RESTRICT"), index=True)
    target_metadata_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("metadata_definition.id", ondelete="RESTRICT"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    current_configuration_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        nullable=True,
        index=True,
    )
    active_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        nullable=True,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        index=True,
    )
    updated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        index=True,
    )
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    tenant: Mapped[Tenant] = relationship()
    matter: Mapped[Matter] = relationship()
    target_metadata_definition: Mapped[MetadataDefinition] = relationship()
    current_configuration: Mapped["ClassificationModelConfiguration | None"] = relationship(
        foreign_keys=[current_configuration_id],
        post_update=True,
    )
    active_model_version: Mapped["ClassificationModelVersion | None"] = relationship(
        foreign_keys=[active_model_version_id],
        post_update=True,
    )
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_user_id])
    updated_by: Mapped[User] = relationship(foreign_keys=[updated_by_user_id])

    __mapper_args__: ClassVar[dict[str, Any]] = {"version_id_col": row_version}


class ClassificationModelConfiguration(Base):
    __tablename__ = "classification_model_configuration"
    __table_args__ = (
        UniqueConstraint(
            "classification_model_id",
            "revision",
            name="uq_classification_model_configuration_revision",
        ),
        UniqueConstraint(
            "classification_model_id",
            "configuration_hash",
            name="uq_classification_model_configuration_hash",
        ),
        UniqueConstraint(
            "id",
            "classification_model_id",
            name="uq_classification_model_configuration_identity",
        ),
        CheckConstraint("revision > 0", name="ck_classification_model_configuration_revision"),
        CheckConstraint(
            "classification_strategy IN ('BINARY', 'MULTICLASS', 'ONE_VS_REST')",
            name="ck_classification_model_configuration_strategy",
        ),
        Index(
            "ix_classification_model_configuration_model_created",
            "classification_model_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    classification_model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("classification_model.id", ondelete="CASCADE"),
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer)
    target_field_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    classification_strategy: Mapped[str] = mapped_column(String(30))
    ordered_class_keys: Mapped[list[str]] = mapped_column(JSON_DOCUMENT)
    binary_mapping: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    label_policy: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    feature_contract: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    prediction_resolution_policy: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    trainer_specification: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    configuration_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        index=True,
    )

    classification_model: Mapped[ClassificationModel] = relationship(
        foreign_keys=[classification_model_id],
    )
    created_by: Mapped[User] = relationship()


class ClassificationDatasetSnapshot(Base):
    __tablename__ = "classification_dataset_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "classification_model_id",
            "aggregate_hash",
            name="uq_classification_dataset_snapshot_aggregate",
        ),
        CheckConstraint(
            "purpose IN ('TRAINING', 'SCORING')",
            name="ck_classification_dataset_snapshot_purpose",
        ),
        CheckConstraint(
            "scope_type IN ('MATTER', 'REVIEW_BATCH')",
            name="ck_classification_dataset_snapshot_scope_type",
        ),
        CheckConstraint(
            "status IN ('BUILDING', 'READY_FOR_FEATURES', 'READY_FOR_TRAINING', 'READY_FOR_SCORING', 'FAILED')",
            name="ck_classification_dataset_snapshot_status",
        ),
        CheckConstraint(
            "population_count >= 0 AND labeled_count >= 0 AND unlabeled_count >= 0 "
            "AND excluded_count >= 0 AND conflicting_count >= 0 "
            "AND family_grouped_count >= 0 AND exact_duplicate_grouped_count >= 0 "
            "AND missing_grouping_count >= 0 AND feature_ready_count >= 0 "
            "AND feature_missing_count >= 0 AND feature_failed_count >= 0 "
            "AND training_eligible_count >= 0",
            name="ck_classification_dataset_snapshot_counts",
        ),
        CheckConstraint(
            "(feature_artifact_id IS NULL AND feature_content_hash IS NULL "
            "AND feature_specification IS NULL AND feature_specification_hash IS NULL) OR "
            "(feature_artifact_id IS NOT NULL AND feature_content_hash IS NOT NULL "
            "AND feature_specification IS NOT NULL AND feature_specification_hash IS NOT NULL)",
            name="ck_classification_dataset_snapshot_feature_artifact",
        ),
        CheckConstraint(
            "(split_artifact_id IS NULL AND split_content_hash IS NULL "
            "AND split_specification IS NULL AND split_specification_hash IS NULL "
            "AND training_aggregate_hash IS NULL) OR "
            "(split_artifact_id IS NOT NULL AND split_content_hash IS NOT NULL "
            "AND split_specification IS NOT NULL AND split_specification_hash IS NOT NULL "
            "AND training_aggregate_hash IS NOT NULL)",
            name="ck_classification_dataset_snapshot_split_artifact",
        ),
        CheckConstraint(
            "status NOT IN ('READY_FOR_FEATURES', 'READY_FOR_TRAINING', 'READY_FOR_SCORING') OR "
            "(population_artifact_id IS NOT NULL AND population_content_hash IS NOT NULL "
            "AND (purpose = 'SCORING' OR "
            "(label_artifact_id IS NOT NULL AND label_content_hash IS NOT NULL)))",
            name="ck_classification_dataset_snapshot_base_artifacts",
        ),
        CheckConstraint(
            "status != 'READY_FOR_TRAINING' OR (feature_artifact_id IS NOT NULL AND split_artifact_id IS NOT NULL)",
            name="ck_classification_dataset_snapshot_training_artifacts",
        ),
        ForeignKeyConstraint(
            ["configuration_id", "classification_model_id"],
            [
                "classification_model_configuration.id",
                "classification_model_configuration.classification_model_id",
            ],
            name="fk_classification_dataset_snapshot_configuration",
        ),
        Index(
            "ix_classification_dataset_snapshot_model_created",
            "classification_model_id",
            "created_at",
        ),
        Index(
            "ix_classification_dataset_snapshot_scope",
            "matter_id",
            "scope_type",
            "scope_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        index=True,
    )
    matter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("matter.id", ondelete="RESTRICT"),
        index=True,
    )
    classification_model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("classification_model.id", ondelete="CASCADE"),
        index=True,
    )
    configuration_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    purpose: Mapped[str] = mapped_column(String(30), default="TRAINING")
    scope_type: Mapped[str] = mapped_column(String(30))
    scope_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    target_field_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    label_policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    analysis_input_manifest_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("analysis_input_manifest.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    population_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    population_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    label_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    label_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feature_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    feature_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feature_specification: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT, nullable=True)
    feature_specification_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    split_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    split_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    split_specification: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT, nullable=True)
    split_specification_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    training_aggregate_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    aggregate_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(30), default="BUILDING", index=True)
    population_count: Mapped[int] = mapped_column(Integer, default=0)
    labeled_count: Mapped[int] = mapped_column(Integer, default=0)
    unlabeled_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0)
    conflicting_count: Mapped[int] = mapped_column(Integer, default=0)
    family_grouped_count: Mapped[int] = mapped_column(Integer, default=0)
    exact_duplicate_grouped_count: Mapped[int] = mapped_column(Integer, default=0)
    missing_grouping_count: Mapped[int] = mapped_column(Integer, default=0)
    feature_ready_count: Mapped[int] = mapped_column(Integer, default=0)
    feature_missing_count: Mapped[int] = mapped_column(Integer, default=0)
    feature_failed_count: Mapped[int] = mapped_column(Integer, default=0)
    training_eligible_count: Mapped[int] = mapped_column(Integer, default=0)
    trained_class_keys: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list)
    excluded_classes: Mapped[list[dict[str, Any]]] = mapped_column(JSON_DOCUMENT, default=list)
    class_support: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
    source_summary: Mapped[list[dict[str, Any]]] = mapped_column(JSON_DOCUMENT, default=list)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSON_DOCUMENT, default=list)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[User] = relationship()
    analysis_input_manifest: Mapped[AnalysisInputManifest | None] = relationship()


class ClassificationTrainingJob(TimestampMixin, Base):
    __tablename__ = "classification_training_job"
    __table_args__ = (
        UniqueConstraint(
            "classification_model_id",
            "idempotency_key",
            name="uq_classification_training_job_idempotency",
        ),
        UniqueConstraint("workflow_id", name="uq_classification_training_job_workflow"),
        UniqueConstraint("training_run_id", name="uq_classification_training_job_training_run"),
        CheckConstraint(
            "scope_type IN ('MATTER', 'REVIEW_BATCH')",
            name="ck_classification_training_job_scope_type",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'SNAPSHOTTING', 'PREPARING_FEATURES', 'LAUNCHING', "
            "'TRAINING', 'FINALIZING', 'COMPLETED', 'FAILED', 'CANCELED')",
            name="ck_classification_training_job_status",
        ),
        CheckConstraint(
            "progress_completed >= 0 AND progress_total > 0 AND progress_completed <= progress_total",
            name="ck_classification_training_job_progress",
        ),
        ForeignKeyConstraint(
            ["configuration_id", "classification_model_id"],
            [
                "classification_model_configuration.id",
                "classification_model_configuration.classification_model_id",
            ],
            name="fk_classification_training_job_configuration",
        ),
        Index(
            "ix_classification_training_job_model_created",
            "classification_model_id",
            "created_at",
        ),
        Index(
            "uq_classification_training_job_active",
            "classification_model_id",
            unique=True,
            postgresql_where=text(
                "status IN ('QUEUED', 'SNAPSHOTTING', 'PREPARING_FEATURES', 'LAUNCHING', 'TRAINING', 'FINALIZING')"
            ),
            sqlite_where=text(
                "status IN ('QUEUED', 'SNAPSHOTTING', 'PREPARING_FEATURES', 'LAUNCHING', 'TRAINING', 'FINALIZING')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        index=True,
    )
    matter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("matter.id", ondelete="RESTRICT"),
        index=True,
    )
    classification_model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("classification_model.id", ondelete="CASCADE"),
        index=True,
    )
    configuration_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    embedding_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("matter_embedding_job.id", ondelete="RESTRICT"),
        index=True,
    )
    dataset_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("classification_dataset_snapshot.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    training_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("classification_training_run.id", ondelete="RESTRICT"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(200))
    workflow_id: Mapped[str] = mapped_column(String(500))
    current_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("workflow_run.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    scope_type: Mapped[str] = mapped_column(String(30))
    scope_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    random_seed: Mapped[int] = mapped_column(BigInteger)
    validation_specification: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    status: Mapped[str] = mapped_column(String(40), default="QUEUED", index=True)
    current_stage: Mapped[str] = mapped_column(String(100), default="QUEUED")
    progress_completed: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=4)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_retriable: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[User] = relationship(foreign_keys=[created_by_user_id])
    dataset_snapshot: Mapped["ClassificationDatasetSnapshot | None"] = relationship(foreign_keys=[dataset_snapshot_id])
    training_run: Mapped["ClassificationTrainingRun | None"] = relationship(foreign_keys=[training_run_id])
    current_workflow_run: Mapped["WorkflowRun | None"] = relationship(foreign_keys=[current_workflow_run_id])

    @property
    def execution(self) -> "WorkflowRun | None":
        return self.current_workflow_run


class ClassificationTrainingRun(TimestampMixin, Base):
    __tablename__ = "classification_training_run"
    __table_args__ = (
        UniqueConstraint(
            "classification_model_id",
            "idempotency_key",
            name="uq_classification_training_run_idempotency",
        ),
        UniqueConstraint(
            "classification_model_id",
            "derivation_key",
            name="uq_classification_training_run_derivation",
        ),
        UniqueConstraint("workflow_id", name="uq_classification_training_run_workflow"),
        CheckConstraint(
            "status IN ('QUEUED', 'SNAPSHOTTING', 'WAITING_FOR_RUNTIME', 'TRAINING', "
            "'PACKAGING', 'EVALUATING', 'PUBLISHING', 'FINALIZING', 'RECONCILING', "
            "'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_classification_training_run_status",
        ),
        CheckConstraint(
            "progress_completed >= 0 AND progress_total >= 0 AND progress_completed <= progress_total",
            name="ck_classification_training_run_progress",
        ),
        CheckConstraint(
            "((model_artifact_id IS NULL AND model_content_hash IS NULL) OR "
            "(model_artifact_id IS NOT NULL AND model_content_hash IS NOT NULL)) AND "
            "((prediction_artifact_id IS NULL AND prediction_content_hash IS NULL) OR "
            "(prediction_artifact_id IS NOT NULL AND prediction_content_hash IS NOT NULL)) AND "
            "((evaluation_artifact_id IS NULL AND evaluation_content_hash IS NULL) OR "
            "(evaluation_artifact_id IS NOT NULL AND evaluation_content_hash IS NOT NULL)) AND "
            "((result_manifest_artifact_id IS NULL AND result_manifest_content_hash IS NULL) OR "
            "(result_manifest_artifact_id IS NOT NULL AND result_manifest_content_hash IS NOT NULL))",
            name="ck_classification_training_run_artifact_pairs",
        ),
        CheckConstraint(
            "status != 'COMPLETED' OR (model_artifact_id IS NOT NULL AND "
            "prediction_artifact_id IS NOT NULL AND evaluation_artifact_id IS NOT NULL AND "
            "result_manifest_artifact_id IS NOT NULL)",
            name="ck_classification_training_run_completed_outputs",
        ),
        CheckConstraint("row_version > 0", name="ck_classification_training_run_row_version"),
        CheckConstraint("attempt_number > 0", name="ck_classification_training_run_attempt_number"),
        ForeignKeyConstraint(
            ["configuration_id", "classification_model_id"],
            [
                "classification_model_configuration.id",
                "classification_model_configuration.classification_model_id",
            ],
            name="fk_classification_training_run_configuration",
        ),
        Index(
            "ix_classification_training_run_model_created",
            "classification_model_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="RESTRICT"), index=True)
    classification_model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("classification_model.id", ondelete="CASCADE"),
        index=True,
    )
    configuration_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    dataset_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("classification_dataset_snapshot.id", ondelete="RESTRICT"),
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(200))
    workflow_id: Mapped[str] = mapped_column(String(500))
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    derivation_key: Mapped[str] = mapped_column(String(64), index=True)
    trainer_specification: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    validation_specification: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    random_seed: Mapped[int] = mapped_column(BigInteger)
    package_schema_version: Mapped[int] = mapped_column(Integer, default=1)
    runtime_image_digest: Mapped[str] = mapped_column(String(500))
    source_revision: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(40), default="QUEUED", index=True)
    current_stage: Mapped[str] = mapped_column(String(100), default="QUEUED")
    progress_completed: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=5)
    model_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    model_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prediction_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    prediction_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evaluation_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    evaluation_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_manifest_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    result_manifest_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_retriable: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        index=True,
    )
    execution_actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    classification_model: Mapped[ClassificationModel] = relationship(foreign_keys=[classification_model_id])
    configuration: Mapped[ClassificationModelConfiguration] = relationship(
        overlaps="classification_model",
    )
    dataset_snapshot: Mapped[ClassificationDatasetSnapshot] = relationship()
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_user_id])
    execution_actor: Mapped[User] = relationship(foreign_keys=[execution_actor_user_id])

    __mapper_args__: ClassVar[dict[str, Any]] = {"version_id_col": row_version}


class ClassificationModelVersion(Base):
    __tablename__ = "classification_model_version"
    __table_args__ = (
        UniqueConstraint("training_run_id", name="uq_classification_model_version_run"),
        UniqueConstraint(
            "classification_model_id",
            "version_number",
            name="uq_classification_model_version_number",
        ),
        UniqueConstraint(
            "id",
            "classification_model_id",
            name="uq_classification_model_version_identity",
        ),
        CheckConstraint(
            "status IN ('CANDIDATE', 'APPROVED', 'REJECTED', 'RETIRED')",
            name="ck_classification_model_version_status",
        ),
        CheckConstraint("version_number > 0", name="ck_classification_model_version_number"),
        ForeignKeyConstraint(
            ["configuration_id", "classification_model_id"],
            [
                "classification_model_configuration.id",
                "classification_model_configuration.classification_model_id",
            ],
            name="fk_classification_model_version_configuration",
        ),
        Index(
            "ix_classification_model_version_model_created",
            "classification_model_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="RESTRICT"), index=True)
    classification_model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("classification_model.id", ondelete="CASCADE"),
        index=True,
    )
    configuration_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    training_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("classification_training_run.id", ondelete="RESTRICT"),
        index=True,
    )
    dataset_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("classification_dataset_snapshot.id", ondelete="RESTRICT"),
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="CANDIDATE", index=True)
    target_field_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    trained_class_keys: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list)
    excluded_classes: Mapped[list[dict[str, Any]]] = mapped_column(JSON_DOCUMENT, default=list)
    training_aggregate_hash: Mapped[str] = mapped_column(String(64))
    feature_specification_hash: Mapped[str] = mapped_column(String(64))
    model_artifact_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    model_content_hash: Mapped[str] = mapped_column(String(64))
    prediction_artifact_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    prediction_content_hash: Mapped[str] = mapped_column(String(64))
    evaluation_artifact_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    evaluation_content_hash: Mapped[str] = mapped_column(String(64))
    result_manifest_artifact_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    result_manifest_content_hash: Mapped[str] = mapped_column(String(64))
    package_schema_version: Mapped[int] = mapped_column(Integer)
    runtime_image_digest: Mapped[str] = mapped_column(String(500))
    source_revision: Mapped[str] = mapped_column(String(255))
    metric_summary: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    warnings: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        index=True,
    )

    classification_model: Mapped[ClassificationModel] = relationship(
        foreign_keys=[classification_model_id],
        overlaps="active_model_version",
    )
    training_run: Mapped[ClassificationTrainingRun] = relationship()
    dataset_snapshot: Mapped[ClassificationDatasetSnapshot] = relationship()
    created_by: Mapped[User] = relationship()


class ClassificationScoringRun(TimestampMixin, Base):
    __tablename__ = "classification_scoring_run"
    __table_args__ = (
        UniqueConstraint(
            "classification_model_id",
            "idempotency_key",
            name="uq_classification_scoring_run_idempotency",
        ),
        UniqueConstraint(
            "classification_model_id",
            "derivation_key",
            name="uq_classification_scoring_run_derivation",
        ),
        UniqueConstraint("workflow_id", name="uq_classification_scoring_run_workflow"),
        CheckConstraint(
            "status IN ('QUEUED', 'VALIDATING', 'WAITING_FOR_RUNTIME', 'SCORING', "
            "'PUBLISHING', 'FINALIZING', 'RECONCILING', 'COMPLETED', "
            "'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_classification_scoring_run_status",
        ),
        CheckConstraint(
            "progress_completed >= 0 AND progress_total >= 0 AND progress_completed <= progress_total",
            name="ck_classification_scoring_run_progress",
        ),
        CheckConstraint(
            "population_document_count >= 0 AND scored_document_count >= 0 "
            "AND abstained_document_count >= 0 AND excluded_document_count >= 0 "
            "AND failed_document_count >= 0 AND abstained_document_count <= scored_document_count",
            name="ck_classification_scoring_run_counts",
        ),
        CheckConstraint(
            "(prediction_artifact_id IS NULL AND prediction_content_hash IS NULL) OR "
            "(prediction_artifact_id IS NOT NULL AND prediction_content_hash IS NOT NULL)",
            name="ck_classification_scoring_run_prediction_artifact",
        ),
        CheckConstraint(
            "(result_manifest_artifact_id IS NULL AND result_manifest_content_hash IS NULL) OR "
            "(result_manifest_artifact_id IS NOT NULL AND result_manifest_content_hash IS NOT NULL)",
            name="ck_classification_scoring_run_manifest_artifact",
        ),
        CheckConstraint(
            "status NOT IN ('COMPLETED', 'COMPLETED_WITH_ERRORS') OR "
            "(prediction_artifact_id IS NOT NULL AND result_manifest_artifact_id IS NOT NULL "
            "AND population_document_count = scored_document_count + excluded_document_count "
            "+ failed_document_count)",
            name="ck_classification_scoring_run_completed",
        ),
        CheckConstraint("row_version > 0", name="ck_classification_scoring_run_row_version"),
        ForeignKeyConstraint(
            ["model_version_id", "classification_model_id"],
            ["classification_model_version.id", "classification_model_version.classification_model_id"],
            name="fk_classification_scoring_run_version",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_classification_scoring_run_model_created",
            "classification_model_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="RESTRICT"), index=True)
    classification_model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("classification_model.id", ondelete="RESTRICT"),
        index=True,
    )
    model_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    dataset_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("classification_dataset_snapshot.id", ondelete="RESTRICT"),
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(200))
    workflow_id: Mapped[str] = mapped_column(String(500))
    current_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("workflow_run.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    derivation_key: Mapped[str] = mapped_column(String(64), index=True)
    configuration_hash: Mapped[str] = mapped_column(String(64))
    model_content_hash: Mapped[str] = mapped_column(String(64))
    feature_content_hash: Mapped[str] = mapped_column(String(64))
    feature_specification_hash: Mapped[str] = mapped_column(String(64))
    scoring_policy: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    scoring_policy_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), default="QUEUED", index=True)
    current_stage: Mapped[str] = mapped_column(String(100), default="QUEUED")
    progress_completed: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=4)
    population_document_count: Mapped[int] = mapped_column(Integer, default=0)
    scored_document_count: Mapped[int] = mapped_column(Integer, default=0)
    abstained_document_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_document_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_document_count: Mapped[int] = mapped_column(Integer, default=0)
    prediction_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    prediction_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_manifest_artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    result_manifest_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_retriable: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        index=True,
    )
    execution_actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    classification_model: Mapped[ClassificationModel] = relationship(foreign_keys=[classification_model_id])
    model_version: Mapped[ClassificationModelVersion] = relationship(overlaps="classification_model")
    dataset_snapshot: Mapped[ClassificationDatasetSnapshot] = relationship()
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_user_id])
    execution_actor: Mapped[User] = relationship(foreign_keys=[execution_actor_user_id])
    current_workflow_run: Mapped["WorkflowRun | None"] = relationship(foreign_keys=[current_workflow_run_id])

    @property
    def execution(self) -> "WorkflowRun | None":
        return self.current_workflow_run

    __mapper_args__: ClassVar[dict[str, Any]] = {"version_id_col": row_version}


class ClassificationEvaluation(Base):
    __tablename__ = "classification_evaluation"
    __table_args__ = (
        UniqueConstraint(
            "model_version_id",
            "evaluation_kind",
            "dataset_snapshot_id",
            "metric_policy_version",
            name="uq_classification_evaluation_identity",
        ),
        CheckConstraint(
            "evaluation_kind IN ('CROSS_FIT_DEVELOPMENT', 'FORMAL_VALIDATION', 'RANDOM_AUDIT', 'TARGETED_QA')",
            name="ck_classification_evaluation_kind",
        ),
        Index(
            "ix_classification_evaluation_model_created",
            "classification_model_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="RESTRICT"), index=True)
    classification_model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("classification_model.id", ondelete="CASCADE"),
        index=True,
    )
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("classification_model_version.id", ondelete="CASCADE"),
        index=True,
    )
    dataset_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("classification_dataset_snapshot.id", ondelete="RESTRICT"),
        index=True,
    )
    evaluation_kind: Mapped[str] = mapped_column(String(40))
    metric_policy_version: Mapped[int] = mapped_column(Integer, default=1)
    evaluation_artifact_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    evaluation_content_hash: Mapped[str] = mapped_column(String(64))
    metric_summary: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    model_version: Mapped[ClassificationModelVersion] = relationship()
    dataset_snapshot: Mapped[ClassificationDatasetSnapshot] = relationship()


class ClassificationModelDecision(Base):
    __tablename__ = "classification_model_decision"
    __table_args__ = (
        UniqueConstraint("workflow_id", name="uq_classification_model_decision_workflow"),
        CheckConstraint(
            "decision_type IN ('APPROVAL', 'REJECTION', 'ACTIVATION', 'ROLLBACK', 'RETIREMENT')",
            name="ck_classification_model_decision_type",
        ),
        CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_classification_model_decision_reason",
        ),
        ForeignKeyConstraint(
            ["model_version_id", "classification_model_id"],
            ["classification_model_version.id", "classification_model_version.classification_model_id"],
            name="fk_classification_model_decision_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["before_active_model_version_id", "classification_model_id"],
            ["classification_model_version.id", "classification_model_version.classification_model_id"],
            name="fk_classification_model_decision_before_active",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["after_active_model_version_id", "classification_model_id"],
            ["classification_model_version.id", "classification_model_version.classification_model_id"],
            name="fk_classification_model_decision_after_active",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_classification_model_decision_model_created",
            "classification_model_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        index=True,
    )
    matter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("matter.id", ondelete="RESTRICT"),
        index=True,
    )
    classification_model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("classification_model.id", ondelete="RESTRICT"),
        index=True,
    )
    model_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    decision_type: Mapped[str] = mapped_column(String(30), index=True)
    workflow_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    metric_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    before_active_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        nullable=True,
        index=True,
    )
    after_active_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        nullable=True,
        index=True,
    )
    authorization_context: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    actor: Mapped[User] = relationship()


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
        Index(
            "uq_metadata_event_import_source",
            "matter_document_id",
            "metadata_definition_id",
            "source_id",
            unique=True,
            postgresql_where=text("source_type = 'IMPORT' AND source_id IS NOT NULL"),
            sqlite_where=text("source_type = 'IMPORT' AND source_id IS NOT NULL"),
        ),
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
    owner_tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="CASCADE"), index=True)
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
            "workflow_type IN ('MATTER_DEFINITION_SETUP', 'BATCH_CHAT', 'SEARCH_CHAT')",
            name="ck_agent_conversation_workflow_type",
        ),
        CheckConstraint(
            "(workflow_type = 'MATTER_DEFINITION_SETUP' AND review_batch_id IS NULL "
            "AND search_definition IS NULL) OR "
            "(workflow_type = 'BATCH_CHAT' AND review_batch_id IS NOT NULL "
            "AND matter_definition_id IS NULL AND search_definition IS NULL) OR "
            "(workflow_type = 'SEARCH_CHAT' AND review_batch_id IS NULL "
            "AND matter_definition_id IS NULL AND search_definition IS NOT NULL)",
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
    matter_definition_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter_definition.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    review_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_batch.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    search_definition: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
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

    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), primary_key=True)
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


class MatterAnalysisTask(TimestampMixin, Base):
    __tablename__ = "matter_analysis_task"
    __table_args__ = (
        UniqueConstraint("matter_id", "key", name="uq_matter_analysis_task_matter_key"),
        CheckConstraint(
            "task_type IN ('QUESTION_ANSWERING', 'CLASSIFICATION', 'FACT_DEVELOPMENT')",
            name="ck_matter_analysis_task_type",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')",
            name="ck_matter_analysis_task_status",
        ),
        CheckConstraint("current_version > 0", name="ck_matter_analysis_task_current_version"),
        CheckConstraint(
            "published_version IS NULL OR (published_version > 0 AND published_version <= current_version)",
            name="ck_matter_analysis_task_published_version",
        ),
        Index("ix_matter_analysis_task_matter_type", "matter_id", "task_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(String(30), index=True)
    workflow_key: Mapped[str] = mapped_column(String(100))
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    published_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )


class MatterAnalysisTaskVersion(Base):
    __tablename__ = "matter_analysis_task_version"
    __table_args__ = (
        UniqueConstraint("matter_analysis_task_id", "version", name="uq_matter_analysis_task_version"),
        CheckConstraint("version > 0", name="ck_matter_analysis_task_version_positive"),
        CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'RETIRED')",
            name="ck_matter_analysis_task_version_status",
        ),
        CheckConstraint(
            "compilation_status IN ('NOT_GENERATED', 'STALE', 'GENERATING', 'READY', 'FAILED')",
            name="ck_matter_analysis_task_compilation_status",
        ),
        CheckConstraint(
            "decision_provider IN ('JEV', 'OPENAI_DECISIONS')",
            name="ck_matter_analysis_task_version_decision_provider",
        ),
        Index("ix_matter_analysis_task_version_task_status", "matter_analysis_task_id", "status"),
        Index("ix_analysis_task_version_skill_definition", "compiler_skill_definition_version_id"),
        Index("ix_analysis_task_version_skill_run", "compiler_skill_run_id"),
        Index("ix_analysis_task_version_workflow", "compiler_workflow_run_id"),
        Index("ix_analysis_task_version_creator", "created_by_user_id"),
        Index("ix_analysis_task_version_publisher", "published_by_user_id"),
        Index("ix_analysis_task_version_classification_model", "classification_model_id"),
        Index("ix_analysis_task_version_classification_version", "classification_model_version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_analysis_task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_analysis_task.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    compilation_status: Mapped[str] = mapped_column(String(30), default="NOT_GENERATED")
    definition_markdown: Mapped[str] = mapped_column(Text)
    definition_content_hash: Mapped[str] = mapped_column(String(64), index=True)
    destination_metadata_definition_ids: Mapped[list[str] | None] = mapped_column(
        JSON_DOCUMENT,
        nullable=True,
    )
    decision_provider: Mapped[str] = mapped_column(String(30), default="JEV")
    decision_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    classification_model_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("classification_model.id", ondelete="RESTRICT"),
        nullable=True,
    )
    classification_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("classification_model_version.id", ondelete="RESTRICT"),
        nullable=True,
    )
    review_guidance_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("matter_definition.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    review_guidance_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("matter_definition_revision.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    review_guidance_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_specification: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    fact_development_specification: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    specification_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    input_contract: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_contract: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    routing_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    scope_policy: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
    compiler_skill_definition_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("skill_definition_version.id", ondelete="RESTRICT"), nullable=True
    )
    compiler_skill_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("skill_run.id", ondelete="SET NULL"), nullable=True
    )
    compiler_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="SET NULL"), nullable=True
    )
    compiler_model_configuration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"))
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MatterAnalysisTaskRun(TimestampMixin, Base):
    __tablename__ = "matter_analysis_task_run"
    __table_args__ = (
        UniqueConstraint(
            "matter_analysis_task_id",
            "idempotency_key",
            name="uq_analysis_task_run_idempotency",
        ),
        UniqueConstraint("workflow_id", name="uq_analysis_task_run_workflow"),
        CheckConstraint(
            "scope_type IN ('MATTER', 'REVIEW_BATCH')",
            name="ck_analysis_task_run_scope_type",
        ),
        CheckConstraint(
            "status IN ('QUEUED', 'SNAPSHOTTING', 'PREPARING_FEATURES', 'SCORING', 'PREPARING', 'EXTRACTING', "
            "'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
            name="ck_analysis_task_run_status",
        ),
        CheckConstraint("run_kind IN ('CLASSIFICATION', 'FACT_DEVELOPMENT')", name="ck_analysis_task_run_kind"),
        CheckConstraint(
            "run_kind <> 'CLASSIFICATION' OR (classification_model_id IS NOT NULL "
            "AND classification_model_version_id IS NOT NULL AND embedding_job_id IS NOT NULL)",
            name="ck_analysis_task_run_classification_inputs",
        ),
        CheckConstraint(
            "progress_completed >= 0 AND progress_total > 0 AND progress_completed <= progress_total",
            name="ck_analysis_task_run_progress",
        ),
        CheckConstraint(
            "population_document_count >= 0",
            name="ck_analysis_task_run_population_count",
        ),
        Index("ix_analysis_task_run_task_created", "matter_analysis_task_id", "created_at"),
        Index("ix_analysis_task_run_matter_status", "matter_id", "status"),
        Index("ix_analysis_task_run_scoring", "classification_scoring_run_id"),
        Index("ix_analysis_task_run_current_workflow", "current_workflow_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"))
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"))
    matter_analysis_task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_analysis_task.id", ondelete="CASCADE")
    )
    matter_analysis_task_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_analysis_task_version.id", ondelete="RESTRICT")
    )
    run_kind: Mapped[str] = mapped_column(String(30), default="CLASSIFICATION", server_default="CLASSIFICATION")
    scope_type: Mapped[str] = mapped_column(String(30))
    scope_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    classification_model_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("classification_model.id", ondelete="RESTRICT"), nullable=True
    )
    classification_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("classification_model_version.id", ondelete="RESTRICT"), nullable=True
    )
    embedding_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter_embedding_job.id", ondelete="RESTRICT"), nullable=True
    )
    # Frozen configuration and aggregate report for non-classification runs (fact development).
    configuration_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # The frozen population of a fact-development run (classification runs freeze theirs on the dataset snapshot).
    analysis_input_manifest_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("analysis_input_manifest.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dataset_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("classification_dataset_snapshot.id", ondelete="RESTRICT"),
        nullable=True,
    )
    classification_scoring_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("classification_scoring_run.id", ondelete="RESTRICT"),
        nullable=True,
    )
    current_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("workflow_run.id", ondelete="SET NULL"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(200))
    workflow_id: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(40), default="QUEUED")
    current_stage: Mapped[str] = mapped_column(String(100), default="QUEUED")
    progress_completed: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=4)
    population_document_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_retriable: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    dataset_snapshot: Mapped[ClassificationDatasetSnapshot | None] = relationship()
    classification_scoring_run: Mapped[ClassificationScoringRun | None] = relationship()
    current_workflow_run: Mapped[WorkflowRun | None] = relationship(foreign_keys=[current_workflow_run_id])

    @property
    def execution(self) -> WorkflowRun | None:
        return self.current_workflow_run

    @property
    def feature_ready_count(self) -> int:
        return self.dataset_snapshot.feature_ready_count if self.dataset_snapshot is not None else 0

    @property
    def feature_missing_count(self) -> int:
        return self.dataset_snapshot.feature_missing_count if self.dataset_snapshot is not None else 0

    @property
    def feature_failed_count(self) -> int:
        return self.dataset_snapshot.feature_failed_count if self.dataset_snapshot is not None else 0

    @property
    def feature_processed_count(self) -> int:
        return self.feature_ready_count + self.feature_missing_count + self.feature_failed_count


class MatterAnalysisTaskRunChunk(TimestampMixin, Base):
    """Execution state for one fixed-size range of a fact-development run's input manifest.

    One row per chunk (never per document). The documents, their issues and pinned text live in the run's
    Artifact-owned analysis input manifest; the chunk covers manifest rows ``ordinal_start`` (zero-based) through
    ``ordinal_start + item_count - 1`` and records the extraction fingerprints of the documents it finished.
    """

    __tablename__ = "matter_analysis_task_run_chunk"
    __table_args__ = (
        UniqueConstraint("run_id", "chunk_number", name="uq_analysis_task_run_chunk_number"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'CANCELED')",
            name="ck_analysis_task_run_chunk_status",
        ),
        CheckConstraint(
            "chunk_number >= 0 AND ordinal_start >= 0 AND item_count >= 0 AND processed_count >= 0 "
            "AND failed_count >= 0",
            name="ck_analysis_task_run_chunk_counts",
        ),
        CheckConstraint(
            "provider_batch_status IS NULL OR provider_batch_status IN "
            "('SUBMITTED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELED')",
            name="ck_analysis_task_run_chunk_provider_batch_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_analysis_task_run.id", ondelete="CASCADE"), index=True
    )
    chunk_number: Mapped[int] = mapped_column(Integer)
    ordinal_start: Mapped[int] = mapped_column(Integer)
    # Fingerprints of the documents this chunk finished, written with each document's facts. Later attempts and
    # later runs of the same task reuse any document whose fingerprint appears here.
    fingerprints: Mapped[list[str]] = mapped_column(JSON, default=list)
    item_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED")
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Set only when the run uses a provider Batch API: the chunk's extraction calls were submitted as one provider
    # batch. The manifest maps each request to its document, skill run, and the facts recalled when it was built.
    provider_batch_status: Mapped[str | None] = mapped_column(String(30))
    provider: Mapped[str | None] = mapped_column(String(50))
    provider_model: Mapped[str | None] = mapped_column(String(200))
    provider_batch_id: Mapped[str | None] = mapped_column(String(255))
    provider_state: Mapped[str | None] = mapped_column(String(100))
    provider_manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    provider_error: Mapped[str | None] = mapped_column(Text)
    provider_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MatterAnalysisTaskVersionDependency(Base):
    __tablename__ = "matter_analysis_task_version_dependency"
    __table_args__ = (
        CheckConstraint(
            "matter_analysis_task_version_id <> dependency_task_version_id",
            name="ck_matter_analysis_task_dependency_not_self",
        ),
        Index("ix_analysis_task_dependency_version", "dependency_task_version_id"),
    )

    matter_analysis_task_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("matter_analysis_task_version.id", ondelete="CASCADE"),
        primary_key=True,
    )
    dependency_task_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("matter_analysis_task_version.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(100), primary_key=True)
    dependency_content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewDecisionResult(Base):
    """Immutable typed decision output for one document in one isolated review run."""

    __tablename__ = "review_decision_result"
    __table_args__ = (
        UniqueConstraint(
            "review_batch_run_id",
            "matter_document_id",
            name="uq_review_decision_result_run_document",
        ),
        CheckConstraint(
            "status IN ('COMPLETED', 'PARTIAL', 'FAILED', 'SKIPPED')",
            name="ck_review_decision_result_status",
        ),
        CheckConstraint(
            "attempts > 0 AND request_count >= 0 AND input_tokens >= 0 AND output_tokens >= 0 AND latency_ms >= 0",
            name="ck_review_decision_result_usage",
        ),
        CheckConstraint(
            "status IN ('FAILED', 'SKIPPED') OR "
            "(engine_key IS NOT NULL AND provider IS NOT NULL AND model IS NOT NULL "
            "AND raw_answer_hash IS NOT NULL AND policy_evaluation_hash IS NOT NULL)",
            name="ck_review_decision_result_completed_shape",
        ),
        Index("ix_review_decision_result_task_version", "matter_analysis_task_version_id", "created_at"),
        Index("ix_review_decision_result_document", "matter_document_id", "created_at"),
        Index("ix_review_decision_result_workflow", "workflow_run_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_batch_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("review_batch_run.id", ondelete="RESTRICT"), index=True
    )
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="RESTRICT"), index=True
    )
    matter_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document.id", ondelete="RESTRICT"), index=True
    )
    matter_analysis_task_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_analysis_task_version.id", ondelete="RESTRICT"), index=True
    )
    definition_content_hash: Mapped[str] = mapped_column(String(64))
    specification_content_hash: Mapped[str] = mapped_column(String(64))
    source_artifact_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source_content_hash: Mapped[str] = mapped_column(String(64))
    state_content_hash: Mapped[str] = mapped_column(String(64))
    question_set_hash: Mapped[str] = mapped_column(String(64))
    decision_policy_hash: Mapped[str] = mapped_column(String(64))
    paragraph_map_version: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), index=True)
    coverage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    answers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recommendations: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    routes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_answer_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_evaluation_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    engine_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(500), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evaluation_skill_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("skill_run.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    evidence_skill_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("skill_run.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    model_invocation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("model_invocation.id", ondelete="RESTRICT"), nullable=True, unique=True, index=True
    )
    reused_from_result_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("review_decision_result.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MatterDefinition(TimestampMixin, Base):
    __tablename__ = "matter_definition"
    __table_args__ = (
        UniqueConstraint("matter_id", "key", name="uq_matter_definition_matter_key"),
        CheckConstraint("current_revision > 0", name="ck_matter_definition_current_revision"),
        CheckConstraint(
            "published_revision IS NULL OR (published_revision > 0 AND published_revision <= current_revision)",
            name="ck_matter_definition_published_revision",
        ),
        CheckConstraint("status IN ('ACTIVE', 'ARCHIVED')", name="ck_matter_definition_status"),
        Index("ix_matter_definition_matter_status", "matter_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(100), default="general_review")
    name: Mapped[str] = mapped_column(String(200), default="General Review Guidance")
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
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
            "'ASSESSMENT_REFINEMENT', 'CLONE')",
            name="ck_matter_definition_revision_source_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_definition.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    content_markdown: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(40))
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
    source_guidance_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter_definition.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    source_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter_definition_revision.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    source_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
            "guidance_refinement_status IN ('NOT_READY', 'QUEUED', 'RUNNING', 'COMPLETED', 'NOT_REQUIRED', 'FAILED')",
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
    initiated_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"))
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
            "status IN ('QUEUED', 'SUBMITTED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED', 'CANCELED')",
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
        CheckConstraint(
            "ordinal > 0 AND quota > 0 AND result_count >= 0", name="ck_definition_assessment_query_counts"
        ),
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
        UniqueConstraint("assessment_run_id", "matter_document_id", name="uq_definition_assessment_candidate_document"),
        CheckConstraint(
            "selection_order IS NULL OR selection_order > 0", name="ck_definition_assessment_selection_order"
        ),
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
    __table_args__ = (
        Index("ix_audit_record_tenant_created_id", "tenant_id", "created_at", "id"),
        Index("ix_audit_record_tenant_actor_created", "tenant_id", "actor_user_id", "created_at"),
        Index("ix_audit_record_tenant_action_created", "tenant_id", "action", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str] = mapped_column(String(100))
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class MatterEntity(TimestampMixin, Base):
    """A person or organization in a matter: a canonical entity (no parent) or a variant name/email of one."""

    __tablename__ = "matter_entity"
    __table_args__ = (
        CheckConstraint("entity_type IN ('PERSON', 'ORGANIZATION')", name="ck_matter_entity_type"),
        CheckConstraint(
            "(parent_id IS NULL AND status IN ('PROPOSED', 'CONFIRMED', 'REJECTED')) OR "
            "(parent_id IS NOT NULL AND status IS NULL AND title IS NULL AND organization_id IS NULL "
            "AND custodian_id IS NULL)",
            name="ck_matter_entity_canonical_shape",
        ),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_matter_entity_not_own_parent"),
        CheckConstraint(
            "(created_by_user_id IS NULL) <> (created_by_workflow_run_id IS NULL)",
            name="ck_matter_entity_creator",
        ),
        CheckConstraint("(email IS NULL) = (normalized_email IS NULL)", name="ck_matter_entity_email_pair"),
        CheckConstraint("lock_version > 0", name="ck_matter_entity_lock_version"),
        Index(
            "uq_matter_entity_matter_email",
            "matter_id",
            "normalized_email",
            unique=True,
            postgresql_where=text("normalized_email IS NOT NULL"),
            sqlite_where=text("normalized_email IS NOT NULL"),
        ),
        Index("ix_matter_entity_matter_name", "matter_id", "normalized_name"),
        Index("ix_matter_entity_matter_type_status", "matter_id", "entity_type", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter_entity.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(20))
    display_name: Mapped[str] = mapped_column(String(300))
    normalized_name: Mapped[str] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    normalized_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter_entity.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    custodian_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("custodian.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    created_by_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    lock_version: Mapped[int] = mapped_column(Integer, default=1)


class MatterEntityEvent(Base):
    """Append-only history of every entity change; the entity rows are its current projection."""

    __tablename__ = "matter_entity_event"
    __table_args__ = (
        CheckConstraint("actor_type IN ('HUMAN', 'AGENT', 'SYSTEM')", name="ck_matter_entity_event_actor_type"),
        Index("ix_matter_entity_event_entity_created", "entity_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter_entity.id", ondelete="CASCADE"))
    event_type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


FACT_DATE_PRECISIONS = "('DAY', 'MONTH', 'QUARTER', 'YEAR', 'RANGE', 'UNKNOWN')"
FACT_DATE_BASES = "('STATED_IN_TEXT', 'DOCUMENT_DATE', 'INFERRED')"


class MatterFact(TimestampMixin, Base):
    """Current projection of one fact; its history lives in matter_fact_event and matter_fact_revision."""

    __tablename__ = "matter_fact"
    __table_args__ = (
        CheckConstraint("status IN ('WORKING', 'CONFIRMED', 'REJECTED', 'MERGED')", name="ck_matter_fact_status"),
        CheckConstraint(f"date_precision IN {FACT_DATE_PRECISIONS}", name="ck_matter_fact_date_precision"),
        CheckConstraint(f"date_basis IS NULL OR date_basis IN {FACT_DATE_BASES}", name="ck_matter_fact_date_basis"),
        CheckConstraint(
            "(date_precision = 'UNKNOWN' AND event_date_start IS NULL AND event_date_end IS NULL) OR "
            "(date_precision = 'RANGE' AND event_date_start IS NOT NULL AND event_date_end IS NOT NULL "
            "AND event_date_end >= event_date_start) OR "
            "(date_precision IN ('DAY', 'MONTH', 'QUARTER', 'YEAR') AND event_date_start IS NOT NULL "
            "AND event_date_end IS NULL)",
            name="ck_matter_fact_date_shape",
        ),
        CheckConstraint(
            "(status = 'MERGED') = (merged_into_fact_id IS NOT NULL)",
            name="ck_matter_fact_merged_target",
        ),
        CheckConstraint(
            "(status = 'CONFIRMED') = (confirmed_at IS NOT NULL)",
            name="ck_matter_fact_confirmation",
        ),
        CheckConstraint("current_revision > 0 AND lock_version > 0", name="ck_matter_fact_versions"),
        CheckConstraint("evidence_count >= 0 AND document_count >= 0", name="ck_matter_fact_counts"),
        Index("ix_matter_fact_matter_status_date", "matter_id", "status", "event_date_start"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="WORKING")
    current_revision: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str] = mapped_column(Text)
    event_date_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    event_date_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_precision: Mapped[str] = mapped_column(String(20), default="UNKNOWN")
    date_basis: Mapped[str | None] = mapped_column(String(20), nullable=True)
    owner_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter_entity.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    key_fact: Mapped[bool] = mapped_column(Boolean, default=False)
    has_contradiction: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    merged_into_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter_fact.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    origin_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    lock_version: Mapped[int] = mapped_column(Integer, default=1)
    # Recall cache: valid only while summary_embedding_hash matches the current summary and embedding model.
    summary_embedding: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    summary_embedding_model: Mapped[str | None] = mapped_column(String(300), nullable=True)
    summary_embedding_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MatterFactRevision(Base):
    """Immutable snapshot of a fact's editable content after each content change."""

    __tablename__ = "matter_fact_revision"
    __table_args__ = (
        UniqueConstraint("fact_id", "revision", name="uq_matter_fact_revision"),
        CheckConstraint("revision > 0", name="ck_matter_fact_revision_positive"),
        CheckConstraint("author_type IN ('HUMAN', 'AGENT')", name="ck_matter_fact_revision_author_type"),
        CheckConstraint(f"date_precision IN {FACT_DATE_PRECISIONS}", name="ck_matter_fact_revision_precision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    fact_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter_fact.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    event_date_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    event_date_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_precision: Mapped[str] = mapped_column(String(20))
    date_basis: Mapped[str | None] = mapped_column(String(20), nullable=True)
    owner_entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    participants: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    author_type: Mapped[str] = mapped_column(String(20))
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MatterFactIssue(Base):
    __tablename__ = "matter_fact_issue"

    fact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_fact.id", ondelete="CASCADE"), primary_key=True
    )
    matter_analysis_task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_analysis_task.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    issue_key: Mapped[str] = mapped_column(String(100), primary_key=True, index=True)


class MatterFactEntity(Base):
    """A fact participant: the matched entity row (canonical or variant) and its role."""

    __tablename__ = "matter_fact_entity"
    __table_args__ = (
        CheckConstraint("role IN ('PARTICIPANT_ACTOR', 'PARTICIPANT_RECIPIENT')", name="ck_matter_fact_entity_role"),
    )

    fact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_fact.id", ondelete="CASCADE"), primary_key=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_entity.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(30), primary_key=True)


class MatterFactEvidence(Base):
    """One exact quote from one document that supports or contradicts a fact."""

    __tablename__ = "matter_fact_evidence"
    __table_args__ = (
        CheckConstraint("stance IN ('SUPPORTS', 'CONTRADICTS')", name="ck_matter_fact_evidence_stance"),
        CheckConstraint("status IN ('ACTIVE', 'REMOVED')", name="ck_matter_fact_evidence_status"),
        CheckConstraint("char_end > char_start AND char_start >= 0", name="ck_matter_fact_evidence_offsets"),
        CheckConstraint(
            "(added_by_user_id IS NULL) <> (workflow_run_id IS NULL)",
            name="ck_matter_fact_evidence_author",
        ),
        Index("ix_matter_fact_evidence_fact_status", "fact_id", "status"),
        Index("ix_matter_fact_evidence_document_status", "matter_document_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    fact_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter_fact.id", ondelete="CASCADE"))
    matter_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("matter_document.id", ondelete="RESTRICT")
    )
    source_artifact_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    source_content_hash: Mapped[str] = mapped_column(String(64))
    paragraph_map_version: Mapped[str] = mapped_column(String(100))
    paragraph_id: Mapped[str] = mapped_column(String(50))
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    quote: Mapped[str] = mapped_column(Text)
    quote_hash: Mapped[str] = mapped_column(String(64))
    stance: Mapped[str] = mapped_column(String(20), default="SUPPORTS")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MatterFactEvent(Base):
    """Append-only, authoritative history of every fact change."""

    __tablename__ = "matter_fact_event"
    __table_args__ = (
        CheckConstraint("actor_type IN ('HUMAN', 'AGENT', 'SYSTEM')", name="ck_matter_fact_event_actor_type"),
        Index("ix_matter_fact_event_fact_created", "fact_id", "created_at"),
        Index("ix_matter_fact_event_matter_created", "matter_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"))
    fact_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter_fact.id", ondelete="CASCADE"))
    event_type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MatterFactSuggestion(TimestampMixin, Base):
    """An agent's proposed change to a reviewer-owned (confirmed) fact, applied only when a reviewer accepts it.

    ``REVISION`` carries content changes in ``changes`` (the same field shapes as a fact revision) with the quotes
    that justify them. ``MERGE`` (used by the final duplicate sweep) proposes merging into ``target_fact_id``.
    """

    __tablename__ = "matter_fact_suggestion"
    __table_args__ = (
        CheckConstraint("kind IN ('REVISION', 'MERGE')", name="ck_matter_fact_suggestion_kind"),
        CheckConstraint("status IN ('PENDING', 'ACCEPTED', 'DISMISSED')", name="ck_matter_fact_suggestion_status"),
        CheckConstraint(
            "(kind = 'MERGE') = (target_fact_id IS NOT NULL)",
            name="ck_matter_fact_suggestion_merge_target",
        ),
        Index("ix_matter_fact_suggestion_fact_status", "fact_id", "status"),
        Index("ix_matter_fact_suggestion_matter_status", "matter_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"))
    fact_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter_fact.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    base_revision: Mapped[int] = mapped_column(Integer)
    changes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    target_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("matter_fact.id", ondelete="CASCADE"), nullable=True
    )
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_run.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class MatterFactExport(TimestampMixin, Base):
    """One completed CSV export of filtered facts: a facts file and an evidence file, stored by the Artifact Service.

    Exports run within the request, so a row exists only for an export whose files were stored.
    """

    __tablename__ = "matter_fact_export"
    __table_args__ = (
        CheckConstraint("fact_count >= 0 AND evidence_count >= 0", name="ck_matter_fact_export_counts"),
        UniqueConstraint(
            "matter_id",
            "created_by_user_id",
            "idempotency_key",
            name="uq_matter_fact_export_idempotency",
        ),
        Index("ix_matter_fact_export_matter_created", "matter_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenant.id", ondelete="RESTRICT"), index=True)
    matter_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("matter.id", ondelete="CASCADE"), index=True)
    filter_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    filter_hash: Mapped[str] = mapped_column(String(64))
    csv_policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    requested_filename: Mapped[str] = mapped_column(String(200))
    fact_count: Mapped[int] = mapped_column(Integer, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    facts_artifact_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    facts_content_hash: Mapped[str] = mapped_column(String(64))
    facts_byte_length: Mapped[int] = mapped_column(BigInteger)
    facts_filename: Mapped[str] = mapped_column(String(255))
    evidence_artifact_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    evidence_content_hash: Mapped[str] = mapped_column(String(64))
    evidence_byte_length: Mapped[int] = mapped_column(BigInteger)
    evidence_filename: Mapped[str] = mapped_column(String(255))
    snapshot_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="RESTRICT"), index=True
    )

    matter: Mapped["Matter"] = relationship()
