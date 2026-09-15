from __future__ import annotations

import hashlib
import mailbox
import mimetypes
import uuid
import zipfile
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from email import policy
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.import_client.adapters.base import DatasetAdapter
from scripts.import_client.api import OpenApiClient
from scripts.import_client.email_parser import email_metadata, parse_message
from scripts.import_client.models import CustodianSpec, EmailMetadata, ImportItem, SourceContainer


@dataclass
class ImportFailure:
    source_item_id: str
    message: str


@dataclass
class ImportReport:
    collection_id: str
    containers_uploaded: int = 0
    items_created: int = 0
    items_existing: int = 0
    bytes_submitted: int = 0
    failures: list[ImportFailure] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _email_payload(email: EmailMetadata | None) -> dict[str, Any] | None:
    if email is None:
        return None
    return {
        "sender": email.sender,
        "subject": email.subject,
        "sent_at": _datetime(email.sent_at),
        "received_at": _datetime(email.received_at),
        "message_id": email.message_id,
        "recipients": [asdict(recipient) for recipient in email.recipients],
    }


def _bounded_source_id(value: str) -> str:
    if len(value) <= 500:
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()[:20]
    return f"{value[:478]}-{digest}"


class BaseImporter:
    """Dataset-neutral orchestration for Artifact Service collection imports."""

    def __init__(
        self,
        api: OpenApiClient,
        *,
        tenant_id: str,
        tenant_slug: str,
        client_id: str,
        collection_name: str,
        collection_description: str | None = None,
        include_attachments: bool = True,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.api = api
        self.tenant_id = tenant_id
        self.tenant_slug = tenant_slug
        self.client_id = client_id
        self.collection_name = collection_name
        self.collection_description = collection_description
        self.include_attachments = include_attachments
        self.progress = progress or (lambda _: None)
        self._custodians: dict[str, dict[str, Any]] = {}

    def run(
        self,
        adapter: DatasetAdapter,
        *,
        limit: int | None = None,
        continue_on_error: bool = False,
    ) -> ImportReport:
        self.api.ensure_storage(self.tenant_id, self.tenant_slug)
        collection = self._resolve_collection()
        self._load_custodians()
        report = ImportReport(collection_id=collection["id"])
        source_artifacts: dict[str, str] = {}
        uploaded_items: dict[str, str] = {}
        submitted = 0

        for container in adapter.source_containers():
            self.progress(f"Preserving source container {container.key}")
            with container.path.open("rb") as content:
                response = self.api.upload_source_container(
                    collection["id"],
                    container.path.name,
                    content,
                    container.media_type,
                    container.original_source_path,
                )
            source_artifacts[container.key] = response["artifact"]["id"]
            report.containers_uploaded += 1

            for item in self.iter_preprocessed_items(
                adapter,
                container,
                include_attachments=self.include_attachments,
            ):
                if limit is not None and submitted >= limit:
                    break
                submitted += 1
                try:
                    response = self._upload_item(
                        collection["id"],
                        item,
                        source_artifacts,
                        uploaded_items,
                    )
                    uploaded_items[item.source_item_id] = response["item"]["id"]
                    report.bytes_submitted += len(item.content)
                    if response["created"]:
                        report.items_created += 1
                    else:
                        report.items_existing += 1
                    self.progress(f"Uploaded {item.source_item_id}")
                except Exception as exc:
                    if not continue_on_error:
                        raise
                    report.failures.append(ImportFailure(item.source_item_id, str(exc)))
                    self.progress(f"Failed {item.source_item_id}: {exc}")
            if limit is not None and submitted >= limit:
                break
        return report

    @classmethod
    def iter_preprocessed_items(
        cls,
        adapter: DatasetAdapter,
        source_container: SourceContainer,
        *,
        include_attachments: bool = True,
    ):
        """Enumerate a container and apply common email and file-relationship preprocessing."""
        if source_container.container_kind == "MBOX":
            source_items = cls._mbox_items(adapter, source_container)
        elif source_container.container_kind == "ZIP":
            source_items = cls._zip_items(adapter, source_container)
        elif source_container.container_kind == "CUSTOM":
            source_items = adapter.custom_items(source_container)
        else:
            raise ValueError(f"Unsupported source container kind: {source_container.container_kind}")
        for item in source_items:
            yield from cls._with_file_relationships(
                adapter,
                item,
                include_attachments=include_attachments,
            )

    @staticmethod
    def _mbox_items(adapter: DatasetAdapter, source_container: SourceContainer):
        seen_ids: Counter[str] = Counter()
        custodians = adapter.custodians_for(source_container, source_container.key)
        box = mailbox.mbox(source_container.path, create=False)
        try:
            for ordinal, message in enumerate(box, start=1):
                content = message.as_bytes(policy=policy.default)
                message_id = str(message.get("message-id") or "").strip()
                identity = message_id or hashlib.sha256(content).hexdigest()
                seen_ids[identity] += 1
                occurrence = f"-{seen_ids[identity]}" if seen_ids[identity] > 1 else ""
                source_item_id = _bounded_source_id(
                    f"{source_container.key}#message={identity}{occurrence}"
                )
                yield ImportItem(
                    source_container_key=source_container.key,
                    source_item_id=source_item_id,
                    record_type="EMAIL",
                    original_filename=f"message-{ordinal:04d}.eml",
                    original_source_path=f"{source_container.key}#message={ordinal}",
                    content=content,
                    media_type="message/rfc822",
                    custodians=custodians,
                    raw_metadata={
                        "source_mbox": source_container.key,
                        "message_ordinal": ordinal,
                        "message_id": message_id or None,
                    },
                )
        finally:
            box.close()

    @staticmethod
    def _zip_items(adapter: DatasetAdapter, source_container: SourceContainer):
        with zipfile.ZipFile(source_container.path) as archive:
            for entry in archive.infolist():
                pure_path = PurePosixPath(entry.filename)
                if (
                    entry.is_dir()
                    or "__MACOSX" in pure_path.parts
                    or pure_path.name == ".DS_Store"
                    or pure_path.name.startswith("._")
                ):
                    continue
                source_path = f"{source_container.key}!/{entry.filename}"
                timestamp = datetime(*entry.date_time, tzinfo=timezone.utc)
                yield ImportItem(
                    source_container_key=source_container.key,
                    source_item_id=_bounded_source_id(source_path),
                    record_type=adapter.record_type_for(pure_path.name),
                    original_filename=pure_path.name,
                    original_source_path=source_path,
                    content=archive.read(entry),
                    media_type=mimetypes.guess_type(pure_path.name)[0] or "application/octet-stream",
                    custodians=adapter.custodians_for(source_container, source_path),
                    source_created_at=timestamp,
                    source_modified_at=timestamp,
                    raw_metadata={
                        "source_zip": source_container.key,
                        "zip_entry": entry.filename,
                        "zip_crc32": f"{entry.CRC:08x}",
                        "zip_compressed_size": entry.compress_size,
                        "zip_uncompressed_size": entry.file_size,
                        "zip_timestamp_timezone": "UNSPECIFIED_ASSUMED_UTC",
                    },
                )

    @staticmethod
    def _with_file_relationships(
        adapter: DatasetAdapter,
        item: ImportItem,
        *,
        include_attachments: bool,
    ):
        if item.record_type != "EMAIL":
            yield item
            return
        parsed = parse_message(item.content)
        family_id = item.family_id or str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"priv-view:import-family:{item.source_item_id}")
        )
        parent = replace(
            item,
            family_id=family_id,
            email=item.email or email_metadata(parsed),
        )
        yield parent
        if not include_attachments:
            return
        for ordinal, attachment in enumerate(parsed.iter_attachments(), start=1):
            attachment_name = attachment.get_filename()
            if not attachment_name:
                continue
            content, transfer_metadata = adapter.decode_attachment(attachment)
            if content is None:
                continue
            filename = Path(attachment_name).name
            source_item_id = _bounded_source_id(
                f"{item.source_item_id}#attachment={ordinal}:{filename}"
            )
            yield ImportItem(
                source_container_key=item.source_container_key,
                source_item_id=source_item_id,
                record_type="FILE",
                original_filename=filename,
                original_source_path=source_item_id,
                content=content,
                media_type=attachment.get_content_type() or "application/octet-stream",
                custodians=item.custodians,
                primary_custodian_key=item.primary_custodian_key,
                parent_source_item_id=item.source_item_id,
                family_id=family_id,
                processing_status=item.processing_status,
                raw_metadata={
                    **item.raw_metadata,
                    "parent_source_item_id": item.source_item_id,
                    "attachment_ordinal": ordinal,
                    **transfer_metadata,
                },
            )

    def _resolve_collection(self) -> dict[str, Any]:
        collections = self.api.list_collections(self.tenant_id, self.client_id)
        for collection in collections:
            if collection["name"] == self.collection_name:
                if collection["status"] != "OPEN":
                    raise RuntimeError(f"Collection {self.collection_name!r} exists but is not OPEN")
                return collection
        self.progress(f"Creating collection {self.collection_name}")
        return self.api.create_collection(
            self.tenant_id,
            self.client_id,
            self.collection_name,
            self.collection_description,
        )

    def _load_custodians(self) -> None:
        self._custodians = {
            " ".join(custodian["display_name"].casefold().split()): custodian
            for custodian in self.api.list_custodians(self.client_id)
        }

    def _resolve_custodian(self, specification: CustodianSpec) -> str:
        existing = self._custodians.get(specification.cache_key)
        if existing is not None:
            if existing["status"] != "ACTIVE":
                raise RuntimeError(f"Custodian {specification.display_name!r} exists but is not ACTIVE")
            return existing["id"]
        self.progress(f"Creating custodian {specification.display_name}")
        created = self.api.create_custodian(
            self.client_id,
            specification.display_name,
            specification.email_addresses,
            specification.external_reference,
        )
        self._custodians[specification.cache_key] = created
        return created["id"]

    def _upload_item(
        self,
        collection_id: str,
        item: ImportItem,
        source_artifacts: dict[str, str],
        uploaded_items: dict[str, str],
    ) -> dict[str, Any]:
        try:
            source_artifact_id = source_artifacts[item.source_container_key]
        except KeyError as exc:
            raise RuntimeError(f"Unknown source container key: {item.source_container_key}") from exc
        custodian_ids = [self._resolve_custodian(custodian) for custodian in item.custodians]
        primary_id = None
        if item.primary_custodian_key is not None:
            for custodian, custodian_id in zip(item.custodians, custodian_ids, strict=True):
                if custodian.cache_key == item.primary_custodian_key:
                    primary_id = custodian_id
                    break
            if primary_id is None:
                raise RuntimeError("Primary custodian is not present in item custodians")
        parent_id = None
        if item.parent_source_item_id is not None:
            try:
                parent_id = uploaded_items[item.parent_source_item_id]
            except KeyError as exc:
                raise RuntimeError(
                    f"Parent item {item.parent_source_item_id!r} must be uploaded before its child"
                ) from exc
        metadata = {
            "source_item_id": item.source_item_id,
            "record_type": item.record_type,
            "original_filename": item.original_filename,
            "original_source_path": item.original_source_path,
            "custodian_ids": custodian_ids,
            "primary_custodian_id": primary_id or custodian_ids[0],
            "parent_collection_item_id": parent_id,
            "family_id": item.family_id,
            "source_created_at": _datetime(item.source_created_at),
            "source_modified_at": _datetime(item.source_modified_at),
            "processing_status": item.processing_status,
            "email": _email_payload(item.email),
            "raw_metadata": item.raw_metadata,
            "unmapped_metadata": item.unmapped_metadata,
            "source_container_artifact_id": source_artifact_id,
        }
        return self.api.upload_item(
            collection_id,
            item.original_filename,
            item.content,
            item.media_type,
            metadata,
        )
