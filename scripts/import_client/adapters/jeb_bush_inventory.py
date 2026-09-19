from __future__ import annotations

import csv
import hashlib
import sys
import uuid
import warnings
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from scripts.import_client.adapters.base import DatasetAdapter
from scripts.import_client.models import CustodianSpec, ImportItem, SourceContainer

_IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jpe",
    ".jpeg",
    ".jpg",
    ".pcx",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
    ".xbm",
    ".xif",
}
_REQUIRED_COLUMNS = {
    "record_type",
    "eml_path",
    "eml_size_bytes",
    "eml_sha256",
    "attachment_index",
    "attachment_original_name",
    "attachment_saved_path",
    "attachment_content_type",
    "attachment_size_bytes",
    "attachment_sha256",
}


class MissingInventorySourceWarning(UserWarning):
    """An inventory EML is absent, so its family cannot be imported."""


@contextmanager
def _large_csv_fields() -> Iterator[None]:
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(sys.maxsize)
    try:
        yield
    finally:
        csv.field_size_limit(previous_limit)


def _bounded_source_id(prefix: str, relative_path: str) -> str:
    value = f"{prefix}:{relative_path}"
    if len(value) <= 500:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return f"{value[:478]}-{digest}"


def _family_id(email_source_item_id: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"priv-view:import-family:{email_source_item_id}",
        )
    )


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _normalized_relative_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _load_skip_paths(path: Path) -> frozenset[str]:
    if not path.is_file():
        return frozenset()
    paths: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.reader(handle):
            if not row:
                continue
            value = _normalized_relative_path(row[0])
            if value and value.casefold() not in {"path", "file", "filename"}:
                paths.add(value)
    return frozenset(paths)


class JebBushInventoryAdapter(DatasetAdapter):
    """Import Jeb Bush EMLs and extracted-text sidecars from an inventory CSV."""

    dataset_name = "jeb-bush-inventory"
    default_collection_name = "Jeb Bush Emails"
    expand_email_attachments = False

    def __init__(self, source: Path, *, include_text_sidecars: bool = True) -> None:
        self.source = source.expanduser().resolve()
        self.root = self.source.parent
        self.skip_file = self.root / "skip.csv"
        self.skip_paths = _load_skip_paths(self.skip_file)
        self.include_text_sidecars = include_text_sidecars
        self.custodian = CustodianSpec(
            display_name="Jeb Bush",
            email_addresses=("jeb@jeb.org",),
            external_reference="jeb-bush-email-archive",
        )
        if not self.source.is_file():
            raise ValueError(f"Jeb Bush source must be an inventory CSV: {self.source}")
        with (
            _large_csv_fields(),
            self.source.open("r", encoding="utf-8-sig", newline="") as handle,
        ):
            fieldnames = set(csv.DictReader(handle).fieldnames or [])
        missing = _REQUIRED_COLUMNS.difference(fieldnames)
        if missing:
            raise ValueError(
                "Jeb Bush inventory is missing columns: "
                + ", ".join(sorted(missing))
            )

    def source_containers(self) -> Iterable[SourceContainer]:
        yield SourceContainer(
            key=self.source.name,
            path=self.source,
            media_type="text/csv",
            original_source_path=self.source.name,
            container_kind="CUSTOM",
        )

    def custom_items(self, source_container: SourceContainer) -> Iterable[ImportItem]:
        current_email_path: str | None = None
        current_email_exists = False
        with (
            _large_csv_fields(),
            source_container.path.open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle,
        ):
            for row_number, row in enumerate(csv.DictReader(handle), start=2):
                record_type = (row.get("record_type") or "").strip().casefold()
                if record_type == "eml":
                    current_email_path = (row.get("eml_path") or "").strip()
                    current_email_exists = False
                    if not current_email_path:
                        continue
                    if _normalized_relative_path(current_email_path) in self.skip_paths:
                        print(
                            f"Skipped by skip.csv: {current_email_path}",
                            file=sys.stderr,
                        )
                        continue
                    item = self._email_item(
                        source_container,
                        row,
                        row_number,
                        current_email_path,
                    )
                    if item is None:
                        warnings.warn(
                            "Skipping missing inventory EML and its attachment rows "
                            f"at row {row_number}: {current_email_path}",
                            MissingInventorySourceWarning,
                            stacklevel=2,
                        )
                        continue
                    current_email_exists = True
                    yield item
                elif record_type == "attachment" and self.include_text_sidecars:
                    email_path = (row.get("eml_path") or "").strip()
                    if not email_path:
                        continue
                    if email_path != current_email_path:
                        raise ValueError(
                            "Inventory attachment row does not immediately follow its "
                            f"parent EML at row {row_number}: {email_path}"
                        )
                    if not current_email_exists:
                        continue
                    attachment_path = (
                        row.get("attachment_saved_path") or ""
                    ).strip()
                    normalized_attachment_path = _normalized_relative_path(
                        attachment_path
                    )
                    if (
                        normalized_attachment_path in self.skip_paths
                        or f"{normalized_attachment_path}.txt" in self.skip_paths
                    ):
                        print(
                            f"Skipped by skip.csv: {attachment_path}",
                            file=sys.stderr,
                        )
                        continue
                    item = self._text_sidecar_item(
                        source_container,
                        row,
                        row_number,
                        email_path,
                    )
                    if item is not None:
                        yield item

    def _email_item(
        self,
        source_container: SourceContainer,
        row: dict[str, str],
        row_number: int,
        relative_path: str,
    ) -> ImportItem | None:
        path = self._safe_path(relative_path, row_number)
        if not path.is_file():
            return None
        source_item_id = _bounded_source_id("jeb-bush-eml", relative_path)
        return ImportItem(
            source_container_key=source_container.key,
            source_item_id=source_item_id,
            record_type="EMAIL",
            original_filename=path.name,
            original_source_path=relative_path,
            content=path.read_bytes(),
            media_type="message/rfc822",
            custodians=(self.custodian,),
            primary_custodian_key=self.custodian.cache_key,
            family_id=_family_id(source_item_id),
            raw_metadata={
                "dataset": self.dataset_name,
                "inventory_row_number": row_number,
                "inventory_path": self.source.name,
                "inventory_eml_size_bytes": _optional_int(row.get("eml_size_bytes")),
                "inventory_eml_sha256": (row.get("eml_sha256") or "").strip() or None,
            },
        )

    def _text_sidecar_item(
        self,
        source_container: SourceContainer,
        row: dict[str, str],
        row_number: int,
        email_path: str,
    ) -> ImportItem | None:
        attachment_path = (row.get("attachment_saved_path") or "").strip()
        if not attachment_path:
            return None
        content_type = (row.get("attachment_content_type") or "").strip()
        extension = Path(attachment_path).suffix.casefold()
        if extension in _IMAGE_EXTENSIONS or content_type.casefold().startswith("image/"):
            return None

        sidecar_relative_path = f"{attachment_path}.txt"
        sidecar = self._safe_path(sidecar_relative_path, row_number)
        if not sidecar.is_file() or sidecar.stat().st_size == 0:
            return None

        parent_source_item_id = _bounded_source_id("jeb-bush-eml", email_path)
        source_item_id = _bounded_source_id("jeb-bush-text", attachment_path)
        original_name = (row.get("attachment_original_name") or "").strip()
        content = sidecar.read_bytes()
        return ImportItem(
            source_container_key=source_container.key,
            source_item_id=source_item_id,
            record_type="FILE",
            original_filename=sidecar.name,
            original_source_path=sidecar_relative_path,
            content=content,
            media_type="text/plain; charset=utf-8",
            custodians=(self.custodian,),
            primary_custodian_key=self.custodian.cache_key,
            parent_source_item_id=parent_source_item_id,
            family_id=_family_id(parent_source_item_id),
            raw_metadata={
                "dataset": self.dataset_name,
                "inventory_row_number": row_number,
                "inventory_path": self.source.name,
                "source_attachment_path": attachment_path,
                "source_attachment_filename": original_name or Path(attachment_path).name,
                "source_attachment_index": _optional_int(row.get("attachment_index")),
                "source_attachment_content_type": content_type or None,
                "source_attachment_size_bytes": _optional_int(
                    row.get("attachment_size_bytes")
                ),
                "source_attachment_sha256": (
                    row.get("attachment_sha256") or ""
                ).strip()
                or None,
                "text_sidecar_path": sidecar_relative_path,
                "text_sidecar_sha256": hashlib.sha256(content).hexdigest(),
            },
        )

    def _safe_path(self, relative_path: str, row_number: int) -> Path:
        path = (self.root / relative_path).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                f"Inventory path escapes its root at row {row_number}: {relative_path}"
            ) from exc
        return path
