from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DOCUMENT_IDENTIFIER_KEYS = ("BEGDOC", "BEGBATES", "CONTROL_NUMBER")


def metadata_value(
    raw_metadata: Mapping[str, Any] | None,
    unmapped_metadata: Mapping[str, Any] | None,
    *keys: str,
) -> Any:
    """Resolve a source metadata value case-insensitively using canonical precedence."""

    combined = {
        str(key).casefold(): value for key, value in {**(unmapped_metadata or {}), **(raw_metadata or {})}.items()
    }
    for key in keys:
        value = combined.get(key.casefold())
        if value not in (None, ""):
            return value
    return None


def document_identifier(
    source_item_id: str,
    raw_metadata: Mapping[str, Any] | None = None,
    unmapped_metadata: Mapping[str, Any] | None = None,
) -> str:
    """Return the review-facing identifier for one collection item."""

    value = metadata_value(raw_metadata, unmapped_metadata, *DOCUMENT_IDENTIFIER_KEYS)
    return str(value) if value not in (None, "") else source_item_id
