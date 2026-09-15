from __future__ import annotations

import csv
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from scripts.import_client.adapters.base import DatasetAdapter
from scripts.import_client.models import CustodianSpec, ImportItem, SourceContainer


@contextmanager
def _allow_fields_as_large_as(source: Path) -> Iterator[None]:
    """Temporarily permit a CSV field to occupy the entire source file."""

    previous_limit = csv.field_size_limit()
    required_limit = min(source.stat().st_size, sys.maxsize)
    csv.field_size_limit(max(previous_limit, required_limit))
    try:
        yield
    finally:
        csv.field_size_limit(previous_limit)


class EnronCsvAdapter(DatasetAdapter):
    """Adapter for the full Enron file/message CSV schema and smaller samples of it."""

    dataset_name = "enron-csv"
    default_collection_name = "Enron CSV"

    def __init__(self, source: Path) -> None:
        self.source = source.resolve()
        if not self.source.is_file():
            raise ValueError(f"Enron source must be a CSV file: {self.source}")

    def source_containers(self) -> Iterable[SourceContainer]:
        yield SourceContainer(
            key=self.source.name,
            path=self.source,
            media_type="text/csv",
            original_source_path=self.source.name,
            container_kind="CUSTOM",
        )

    def custom_items(self, source_container: SourceContainer) -> Iterable[ImportItem]:
        with (
            _allow_fields_as_large_as(source_container.path),
            source_container.path.open(newline="", encoding="utf-8-sig", errors="replace") as handle,
        ):
            rows = csv.DictReader(handle)
            if rows.fieldnames is None or not {"file", "message"}.issubset(rows.fieldnames):
                raise ValueError("Enron CSV must contain file and message columns")
            for row_number, row in enumerate(rows, start=2):
                source_item_id = row["file"].strip()
                if not source_item_id:
                    continue
                content = row["message"].encode("utf-8")
                owner = source_item_id.split("/", 1)[0]
                custodian = CustodianSpec(
                    display_name=owner,
                    external_reference=f"enron-maildir:{owner}",
                )
                filename = Path(source_item_id.rstrip("./")).name or f"row-{row_number}"
                if not filename.casefold().endswith(".eml"):
                    filename = f"{filename}.eml"
                yield ImportItem(
                    source_container_key=source_container.key,
                    source_item_id=source_item_id,
                    record_type="EMAIL",
                    original_filename=filename,
                    original_source_path=source_item_id,
                    content=content,
                    media_type="message/rfc822",
                    custodians=(custodian,),
                    raw_metadata={"file": source_item_id, "source_row_number": row_number},
                )
