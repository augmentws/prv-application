from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

RecordType = Literal["EMAIL", "FILE", "CHAT", "TRANSCRIPT", "OTHER"]


@dataclass(frozen=True)
class CustodianSpec:
    display_name: str
    email_addresses: tuple[str, ...] = ()
    external_reference: str | None = None

    @property
    def cache_key(self) -> str:
        return " ".join(self.display_name.casefold().split())


@dataclass(frozen=True)
class SourceContainer:
    key: str
    path: Path
    media_type: str = "application/octet-stream"
    original_source_path: str | None = None
    container_kind: Literal["CUSTOM", "MBOX", "ZIP"] = "CUSTOM"


@dataclass(frozen=True)
class EmailRecipient:
    recipient_type: Literal["TO", "CC", "BCC"]
    display_name: str | None = None
    email_address: str | None = None


@dataclass(frozen=True)
class EmailMetadata:
    sender: str | None = None
    subject: str | None = None
    sent_at: datetime | None = None
    received_at: datetime | None = None
    message_id: str | None = None
    recipients: tuple[EmailRecipient, ...] = ()


@dataclass(frozen=True)
class ImportItem:
    source_container_key: str
    source_item_id: str
    record_type: RecordType
    original_filename: str
    content: bytes
    custodians: tuple[CustodianSpec, ...]
    media_type: str = "application/octet-stream"
    original_source_path: str | None = None
    primary_custodian_key: str | None = None
    parent_source_item_id: str | None = None
    family_id: str | None = None
    source_created_at: datetime | None = None
    source_modified_at: datetime | None = None
    processing_status: Literal["NOT_PROCESSED", "METADATA_INCOMPLETE", "READY", "FAILED"] = "READY"
    email: EmailMetadata | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    unmapped_metadata: dict[str, Any] = field(default_factory=dict)
