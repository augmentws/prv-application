from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Iterable
from email.message import Message
from pathlib import Path
from typing import Any

from scripts.import_client.adapters.base import DatasetAdapter
from scripts.import_client.models import CustodianSpec, RecordType, SourceContainer

_KNOWN_BASE64_REPAIRS: dict[str, int] = {
    # crisis_team_evaluation.txt contains one extra encoded character in the
    # published EMC-2 fixture. Removing it restores the UTF-8 attachment body.
    "e5b0bb29f31ed4d90b606a375b71cd2ad6e646e153bb5cf6496f1431a67d9373": 355,
}


def _file_record_type(filename: str) -> RecordType:
    lowered = filename.casefold()
    if any(marker in lowered for marker in ("chat", "talk_log", "irc_")):
        return "CHAT"
    if any(marker in lowered for marker in ("voicemail", "phone_log", "vm_")):
        return "TRANSCRIPT"
    return "FILE"


class Emc2Adapter(DatasetAdapter):
    dataset_name = "emc2"
    default_collection_name = "EMC-2"

    def __init__(self, source: Path) -> None:
        self.source = source.resolve()
        if not (self.source / "custodians").is_dir():
            raise ValueError(f"EMC-2 source must contain a custodians directory: {self.source}")

    def source_containers(self) -> Iterable[SourceContainer]:
        paths = sorted(self.source.glob("custodians/**/*.mbox"))
        paths.extend(sorted(self.source.glob("custodians/**/*.zip")))
        for path in paths:
            relative = path.relative_to(self.source).as_posix()
            media_type = "application/mbox" if path.suffix.casefold() == ".mbox" else "application/zip"
            yield SourceContainer(
                key=relative,
                path=path,
                media_type=media_type,
                original_source_path=relative,
                container_kind="MBOX" if path.suffix.casefold() == ".mbox" else "ZIP",
            )

    def custodians_for(self, source_container: SourceContainer, source_path: str) -> tuple[CustodianSpec, ...]:
        del source_path
        relative = source_container.path.relative_to(self.source)
        name = relative.parts[1]
        return (CustodianSpec(display_name=name, external_reference=f"emc2:{name}"),)

    def record_type_for(self, filename: str) -> RecordType:
        return _file_record_type(filename)

    def decode_attachment(self, attachment: Message) -> tuple[bytes | None, dict[str, Any]]:
        transfer_encoding = str(attachment.get("Content-Transfer-Encoding") or "").casefold().strip()
        raw_payload = attachment.get_payload()
        if transfer_encoding == "base64" and isinstance(raw_payload, str):
            compact_payload = re.sub(r"\s+", "", raw_payload)
            encoded_sha256 = hashlib.sha256(compact_payload.encode("ascii")).hexdigest()
            remove_index = _KNOWN_BASE64_REPAIRS.get(encoded_sha256)
            if remove_index is not None:
                repaired_payload = compact_payload[:remove_index] + compact_payload[remove_index + 1 :]
                content = base64.b64decode(repaired_payload, validate=True)
                return content, {
                    "mime_transfer_repair": {
                        "kind": "KNOWN_EMC2_MALFORMED_BASE64",
                        "encoded_payload_sha256": encoded_sha256,
                        "removed_character_offset": remove_index,
                    }
                }
        return super().decode_attachment(attachment)
