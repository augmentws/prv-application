from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from pathlib import Path

from scripts.import_client.adapters.base import DatasetAdapter
from scripts.import_client.email_parser import email_metadata, parse_message
from scripts.import_client.models import CustodianSpec, ImportItem, SourceContainer

_REQUIRED_COLUMNS = {
    "docid_2016",
    "docid_2015",
    "path",
    "tr2016_labels",
    "tr2016_important",
    "tr2016_facets",
    "tr2015_labels",
}
_DOCID_2016_PATTERN = re.compile(r"\d{6}")
_DOCID_2015_PATTERN = re.compile(r"\d{4,6}-\d+")


def _multiple_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(";") if part.strip()]


class Athome4LoadfileAdapter(DatasetAdapter):
    """Import the TREC Athome4 load file and its flattened Jeb Bush emails."""

    dataset_name = "athome4-loadfile"
    default_collection_name = "TREC Athome4"
    expand_email_attachments = False

    def __init__(self, source: Path) -> None:
        self.source = source.expanduser().resolve()
        self.custodian = CustodianSpec(
            display_name="Jeb Bush",
            email_addresses=("jeb@jeb.org",),
            external_reference="jeb-bush-email-archive",
        )
        if not self.source.is_file():
            raise ValueError(f"Athome4 source must be a CSV load file: {self.source}")
        with self.source.open("r", encoding="utf-8-sig", newline="") as handle:
            fieldnames = set(csv.DictReader(handle).fieldnames or [])
        missing = _REQUIRED_COLUMNS.difference(fieldnames)
        if missing:
            raise ValueError(
                "Athome4 load file is missing columns: "
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
        seen_docid_2016: set[str] = set()
        seen_docid_2015: set[str] = set()
        seen_paths: set[Path] = set()
        corpus_root: Path | None = None

        with source_container.path.open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            for row_number, row in enumerate(csv.DictReader(handle), start=2):
                docid_2016 = (row.get("docid_2016") or "").strip()
                docid_2015 = (row.get("docid_2015") or "").strip()
                source_path_value = (row.get("path") or "").strip()
                self._validate_identifier(
                    docid_2016,
                    _DOCID_2016_PATTERN,
                    "docid_2016",
                    row_number,
                )
                self._validate_identifier(
                    docid_2015,
                    _DOCID_2015_PATTERN,
                    "docid_2015",
                    row_number,
                )
                self._require_unique(
                    docid_2016,
                    seen_docid_2016,
                    "docid_2016",
                    row_number,
                )
                self._require_unique(
                    docid_2015,
                    seen_docid_2015,
                    "docid_2015",
                    row_number,
                )

                source_path = self._resolve_source_path(source_path_value, row_number)
                if corpus_root is None:
                    corpus_root = source_path.parent.parent
                try:
                    relative_path = source_path.relative_to(corpus_root)
                except ValueError as exc:
                    raise ValueError(
                        f"Athome4 path escapes corpus root at row {row_number}: "
                        f"{source_path_value}"
                    ) from exc
                if source_path in seen_paths:
                    raise ValueError(
                        f"Duplicate Athome4 path at row {row_number}: {source_path_value}"
                    )
                seen_paths.add(source_path)
                if source_path.name != docid_2016:
                    raise ValueError(
                        f"Athome4 path basename does not match docid_2016 at row "
                        f"{row_number}: {source_path.name!r} != {docid_2016!r}"
                    )
                expected_bucket = f"{int(docid_2016) // 1000:03d}"
                if source_path.parent.name != expected_bucket:
                    raise ValueError(
                        f"Athome4 path bucket does not match docid_2016 at row "
                        f"{row_number}: {source_path.parent.name!r} != "
                        f"{expected_bucket!r}"
                    )

                content = source_path.read_bytes()
                if not content:
                    raise ValueError(
                        f"Athome4 source file is empty at row {row_number}: "
                        f"{source_path_value}"
                    )
                message = parse_message(content)
                sent_header = str(
                    message.get("Sent") or message.get("Date") or ""
                ).strip()
                yield ImportItem(
                    source_container_key=source_container.key,
                    source_item_id=f"athome4:{docid_2016}",
                    record_type="EMAIL",
                    original_filename=source_path.name,
                    original_source_path=relative_path.as_posix(),
                    content=content,
                    media_type="message/rfc822",
                    custodians=(self.custodian,),
                    primary_custodian_key=self.custodian.cache_key,
                    email=email_metadata(
                        message,
                        sent_headers=("sent", "date"),
                    ),
                    raw_metadata={
                        "dataset": self.dataset_name,
                        "loadfile": self.source.name,
                        "loadfile_row_number": row_number,
                        "loadfile_source_path": source_path_value,
                        "source_sent_header": sent_header or None,
                    },
                    unmapped_metadata={
                        "CONTROL_NUMBER": docid_2016,
                        "docid_2016": docid_2016,
                        "docid_2015": docid_2015,
                        "tr2016_labels": _multiple_values(
                            row.get("tr2016_labels")
                        ),
                        "tr2016_important": _multiple_values(
                            row.get("tr2016_important")
                        ),
                        "tr2016_facets": _multiple_values(
                            row.get("tr2016_facets")
                        ),
                        "tr2015_labels": _multiple_values(
                            row.get("tr2015_labels")
                        ),
                    },
                )

    @staticmethod
    def _validate_identifier(
        value: str,
        pattern: re.Pattern[str],
        field: str,
        row_number: int,
    ) -> None:
        if pattern.fullmatch(value) is None:
            raise ValueError(
                f"Invalid Athome4 {field} at row {row_number}: {value!r}"
            )

    @staticmethod
    def _require_unique(
        value: str,
        seen: set[str],
        field: str,
        row_number: int,
    ) -> None:
        if value in seen:
            raise ValueError(
                f"Duplicate Athome4 {field} at row {row_number}: {value!r}"
            )
        seen.add(value)

    @staticmethod
    def _resolve_source_path(value: str, row_number: int) -> Path:
        if not value:
            raise ValueError(f"Missing Athome4 path at row {row_number}")
        path = Path(value).expanduser()
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(
                f"Missing Athome4 source file at row {row_number}: {value}"
            ) from exc
        if not resolved.is_file():
            raise ValueError(
                f"Athome4 source is not a regular file at row {row_number}: {value}"
            )
        return resolved
