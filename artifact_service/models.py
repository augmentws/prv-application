import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from artifact_service.database import ArtifactBase


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TenantStorage(ArtifactBase):
    __tablename__ = "tenant_storage"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'ARCHIVED')", name="ck_tenant_storage_status"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_slug_snapshot: Mapped[str] = mapped_column(String(80))
    bucket_name: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ClientCollection(TimestampMixin, ArtifactBase):
    __tablename__ = "client_collection"
    __table_args__ = (
        UniqueConstraint("client_id", "name", name="uq_artifact_collection_client_name"),
        CheckConstraint("status IN ('OPEN', 'SEALED', 'ARCHIVED')", name="ck_artifact_collection_status"),
        Index("ix_artifact_collection_tenant_client", "tenant_id", "client_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)


class CollectionItem(TimestampMixin, ArtifactBase):
    __tablename__ = "collection_item"
    __table_args__ = (
        UniqueConstraint("collection_id", "source_item_id", name="uq_collection_item_source"),
        CheckConstraint(
            "record_type IN ('EMAIL', 'FILE', 'CHAT', 'TRANSCRIPT', 'OTHER')",
            name="ck_collection_item_record_type",
        ),
        CheckConstraint(
            "processing_status IN ('NOT_PROCESSED', 'METADATA_INCOMPLETE', 'READY', 'FAILED')",
            name="ck_collection_item_processing_status",
        ),
        Index("ix_collection_item_collection_type", "collection_id", "record_type"),
        Index("ix_collection_item_source_created", "collection_id", "source_created_at"),
        Index("ix_collection_item_source_modified", "collection_id", "source_modified_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    collection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("client_collection.id", ondelete="CASCADE"), index=True
    )
    source_item_id: Mapped[str] = mapped_column(String(500))
    record_type: Mapped[str] = mapped_column(String(30))
    original_filename: Mapped[str] = mapped_column(String(500))
    original_extension: Mapped[str | None] = mapped_column(String(100))
    original_source_path: Mapped[str | None] = mapped_column(Text)
    source_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    family_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    parent_collection_item_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("collection_item.id", ondelete="RESTRICT"), index=True
    )
    processing_status: Mapped[str] = mapped_column(String(30), default="NOT_PROCESSED")
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    unmapped_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    collection: Mapped[ClientCollection] = relationship()
    parent: Mapped["CollectionItem | None"] = relationship(remote_side="CollectionItem.id")


class CollectionItemCustodian(ArtifactBase):
    __tablename__ = "collection_item_custodian"
    __table_args__ = (
        CheckConstraint("relationship_type IN ('PRIMARY', 'COMMON')", name="ck_item_custodian_relationship"),
    )

    collection_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("collection_item.id", ondelete="CASCADE"), primary_key=True
    )
    custodian_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, index=True)
    relationship_type: Mapped[str] = mapped_column(String(20), default="PRIMARY")


class CollectionSelection(ArtifactBase):
    __tablename__ = "collection_selection"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_collection_selection_request"),
        CheckConstraint("status IN ('READY', 'DISPATCHED')", name="ck_collection_selection_status"),
        CheckConstraint("total_count >= 0", name="ck_collection_selection_total"),
        Index("ix_collection_selection_collection_created", "collection_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    collection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("client_collection.id", ondelete="CASCADE"), index=True
    )
    selection: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="READY")
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CollectionSelectionItem(ArtifactBase):
    __tablename__ = "collection_selection_item"
    __table_args__ = (
        UniqueConstraint("selection_id", "ordinal", name="uq_collection_selection_item_ordinal"),
    )

    selection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("collection_selection.id", ondelete="CASCADE"), primary_key=True
    )
    collection_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("collection_item.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)


class CollectionItemEmail(ArtifactBase):
    __tablename__ = "collection_item_email"

    collection_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("collection_item.id", ondelete="CASCADE"), primary_key=True
    )
    sender: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    message_id: Mapped[str | None] = mapped_column(String(1000), index=True)


class CollectionItemEmailRecipient(ArtifactBase):
    __tablename__ = "collection_item_email_recipient"
    __table_args__ = (
        CheckConstraint("recipient_type IN ('TO', 'CC', 'BCC')", name="ck_email_recipient_type"),
        UniqueConstraint(
            "collection_item_id", "recipient_type", "ordinal", name="uq_email_recipient_item_type_ordinal"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    collection_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("collection_item.id", ondelete="CASCADE"), index=True
    )
    recipient_type: Mapped[str] = mapped_column(String(10))
    display_name: Mapped[str | None] = mapped_column(String(500))
    email_address: Mapped[str | None] = mapped_column(String(500), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)


class ContentBlob(ArtifactBase):
    __tablename__ = "content_blob"
    __table_args__ = (UniqueConstraint("tenant_id", "sha256", name="uq_content_blob_tenant_sha256"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    bucket_name: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(1000))
    byte_length: Mapped[int] = mapped_column(BigInteger)
    media_type: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Artifact(ArtifactBase):
    __tablename__ = "artifact"
    __table_args__ = (
        CheckConstraint("artifact_class IN ('EVIDENCE', 'DERIVED')", name="ck_artifact_class"),
        CheckConstraint(
            "artifact_type IN ('SOURCE_CONTAINER', 'NATIVE_FILE', 'EXTRACTED_TEXT', 'OCR_TEXT', "
            "'DOCUMENT_IMAGE', 'CHUNK_SET', 'DOCUMENT_VECTOR', 'CHUNK_VECTOR_SET', 'SUMMARY', 'THUMBNAIL')",
            name="ck_artifact_type",
        ),
        CheckConstraint("status IN ('UPLOADING', 'FINALIZED', 'QUARANTINED')", name="ck_artifact_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    artifact_class: Mapped[str] = mapped_column(String(30))
    artifact_type: Mapped[str] = mapped_column(String(40))
    content_blob_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("content_blob.id", ondelete="RESTRICT"))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    media_type: Mapped[str] = mapped_column(String(255))
    byte_length: Mapped[int] = mapped_column(BigInteger)
    original_filename: Mapped[str] = mapped_column(String(500))
    source_reference: Mapped[str | None] = mapped_column(Text)
    artifact_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(30), default="FINALIZED")

    content_blob: Mapped[ContentBlob] = relationship()


class CollectionArtifact(ArtifactBase):
    __tablename__ = "collection_artifact"
    __table_args__ = (
        CheckConstraint("artifact_role = 'SOURCE_CONTAINER'", name="ck_collection_artifact_role"),
    )

    artifact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("artifact.id", ondelete="CASCADE"), primary_key=True
    )
    collection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("client_collection.id", ondelete="CASCADE"), index=True
    )
    artifact_role: Mapped[str] = mapped_column(String(40), default="SOURCE_CONTAINER")


class CollectionItemArtifact(ArtifactBase):
    __tablename__ = "collection_item_artifact"
    __table_args__ = (
        CheckConstraint(
            "artifact_role IN ('NATIVE', 'EXTRACTED_TEXT', 'OCR_TEXT', 'DOCUMENT_IMAGE', 'CHUNK_SET', "
            "'DOCUMENT_VECTOR', 'CHUNK_VECTOR_SET', 'SUMMARY', 'THUMBNAIL')",
            name="ck_collection_item_artifact_role",
        ),
        UniqueConstraint(
            "collection_item_id",
            "artifact_role",
            "derivation_key",
            name="uq_item_artifact_derivation",
        ),
    )

    artifact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("artifact.id", ondelete="CASCADE"), primary_key=True
    )
    collection_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("collection_item.id", ondelete="CASCADE"), index=True
    )
    artifact_role: Mapped[str] = mapped_column(String(40))
    page_count: Mapped[int | None] = mapped_column(Integer)
    processing_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    derivation_key: Mapped[str | None] = mapped_column(String(64), index=True)


class ArtifactLineage(ArtifactBase):
    __tablename__ = "artifact_lineage"
    __table_args__ = (
        CheckConstraint(
            "relationship IN ('DERIVED_FROM', 'EXTRACTED_FROM', 'CHUNKED_FROM', 'EMBEDDED_FROM', "
            "'EXTRACTED_FROM_CONTAINER')",
            name="ck_artifact_lineage_relationship",
        ),
    )

    artifact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("artifact.id", ondelete="CASCADE"), primary_key=True
    )
    source_artifact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("artifact.id", ondelete="RESTRICT"), primary_key=True
    )
    relationship: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
