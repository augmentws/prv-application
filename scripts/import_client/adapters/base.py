from __future__ import annotations

import base64
import binascii
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from email.message import Message
from typing import Any

from scripts.import_client.models import CustodianSpec, ImportItem, RecordType, SourceContainer


class DatasetAdapter(ABC):
    dataset_name: str
    default_collection_name: str

    @abstractmethod
    def source_containers(self) -> Iterable[SourceContainer]:
        """Return original aggregate files that must be preserved before extracted items."""

    def custom_items(self, source_container: SourceContainer) -> Iterable[ImportItem]:
        """Yield raw items only for a source shape the base importer cannot enumerate."""
        del source_container
        return ()

    def custodians_for(
        self,
        source_container: SourceContainer,
        source_path: str,
    ) -> tuple[CustodianSpec, ...]:
        """Map a generic container member to dataset-specific custodian metadata."""
        raise NotImplementedError(f"{type(self).__name__} does not define container custodians")

    def record_type_for(self, filename: str) -> RecordType:
        """Classify a generic file extracted by the base importer."""
        del filename
        return "FILE"

    def decode_attachment(self, attachment: Message) -> tuple[bytes | None, dict[str, Any]]:
        """Decode a MIME attachment and return any dataset-specific repair metadata."""
        transfer_encoding = str(attachment.get("Content-Transfer-Encoding") or "").casefold().strip()
        raw_payload = attachment.get_payload()
        if transfer_encoding != "base64" or not isinstance(raw_payload, str):
            return attachment.get_payload(decode=True), {}

        compact_payload = re.sub(r"\s+", "", raw_payload)
        if not compact_payload:
            return b"", {}
        if len(compact_payload) % 4 == 1:
            raise ValueError(
                f"Malformed base64 MIME attachment {attachment.get_filename()!r}: "
                "encoded length cannot be decoded"
            )
        padded_payload = compact_payload + ("=" * (-len(compact_payload) % 4))
        try:
            return base64.b64decode(padded_payload, validate=True), {}
        except (ValueError, binascii.Error) as exc:
            raise ValueError(
                f"Malformed base64 MIME attachment {attachment.get_filename()!r}"
            ) from exc
