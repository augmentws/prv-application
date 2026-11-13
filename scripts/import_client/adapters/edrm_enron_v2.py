from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from pathlib import Path

from scripts.import_client.adapters.base import DatasetAdapter
from scripts.import_client.models import CustodianSpec, ImportItem, SourceContainer

_PACKAGE_PATTERN = re.compile(
    r"^edrm-enron-v2_(?P<custodian>.+?)_xml(?:_(?P<part>\d+)of(?P<parts>\d+))?\.zip$",
    re.IGNORECASE,
)
_DOCUMENT_PATTERN = re.compile(
    r"^(?P<message>3\.\d+\.[A-Z0-9]+)(?:\.(?P<attachment>\d+))?\.txt$",
    re.IGNORECASE,
)


def _document_sort_key(path: Path) -> tuple[str, int, str]:
    match = _DOCUMENT_PATTERN.fullmatch(path.name)
    if match is None:
        return (path.name.casefold(), -1, path.as_posix().casefold())
    attachment = match.group("attachment")
    return (
        match.group("message").casefold(),
        int(attachment) if attachment is not None else -1,
        path.as_posix().casefold(),
    )


class EdrmEnronV2Adapter(DatasetAdapter):
    """Import the extracted, de-duplicated TREC EDRM Enron v2 text corpus."""

    dataset_name = "edrm-enron-v2"
    default_collection_name = "EDRM Enron v2"
    expand_email_attachments = False
    store_source_containers_supported = False

    def __init__(self, source: Path) -> None:
        self.source = source.expanduser().resolve()
        if not self.source.is_dir():
            raise ValueError(
                f"EDRM Enron v2 source must be an extracted corpus directory: {self.source}"
            )
        self._packages = tuple(
            path
            for path in sorted(self.source.iterdir(), key=lambda item: item.name.casefold())
            if path.is_dir() and _PACKAGE_PATTERN.fullmatch(path.name)
        )
        if not self._packages:
            raise ValueError(
                "EDRM Enron v2 source contains no extracted "
                "edrm-enron-v2_*_xml*.zip package directories"
            )

    def source_containers(self) -> Iterable[SourceContainer]:
        for package in self._packages:
            yield SourceContainer(
                key=package.name,
                path=package,
                media_type="inode/directory",
                original_source_path=package.name,
                container_kind="CUSTOM",
            )

    def source_container_size(self, source_container: SourceContainer) -> int:
        return sum(
            path.stat().st_size
            for path in source_container.path.rglob("*.txt")
            if path.is_file()
        )

    def custom_items(self, source_container: SourceContainer) -> Iterable[ImportItem]:
        package_match = _PACKAGE_PATTERN.fullmatch(source_container.path.name)
        if package_match is None:
            raise ValueError(
                f"Invalid EDRM Enron v2 package directory: {source_container.path.name}"
            )
        custodian_key = package_match.group("custodian")
        custodian = CustodianSpec(
            display_name=custodian_key,
            external_reference=f"edrm-enron-v2:{custodian_key.casefold()}",
        )
        paths = sorted(
            (
                path
                for path in source_container.path.rglob("*.txt")
                if path.is_file()
            ),
            key=_document_sort_key,
        )
        available_document_ids = {
            path.name.removesuffix(".txt") for path in paths
        }
        for path in paths:
            match = _DOCUMENT_PATTERN.fullmatch(path.name)
            if match is None:
                raise ValueError(f"Invalid EDRM Enron v2 document filename: {path}")
            message_id = match.group("message")
            attachment = match.group("attachment")
            document_id = path.name.removesuffix(".txt")
            relative_path = path.relative_to(self.source).as_posix()
            family_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"priv-view:import-family:{message_id}",
                )
            )
            raw_metadata: dict[str, object] = {
                "dataset": self.dataset_name,
                "trec_document_id": document_id,
                "trec_message_id": message_id,
                "source_package": source_container.path.name,
                "source_text_path": relative_path,
            }
            if attachment is not None:
                raw_metadata["attachment_ordinal"] = int(attachment)
                if message_id not in available_document_ids:
                    raw_metadata["parent_document_missing"] = True
            yield ImportItem(
                source_container_key=source_container.key,
                source_item_id=document_id,
                record_type="EMAIL" if attachment is None else "FILE",
                original_filename=path.name,
                original_source_path=relative_path,
                content=path.read_bytes(),
                media_type="message/rfc822" if attachment is None else "text/plain",
                custodians=(custodian,),
                primary_custodian_key=custodian.cache_key,
                parent_source_item_id=(
                    message_id
                    if attachment is not None
                    and message_id in available_document_ids
                    else None
                ),
                family_id=family_id,
                raw_metadata=raw_metadata,
            )
