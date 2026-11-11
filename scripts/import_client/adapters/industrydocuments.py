from __future__ import annotations

import csv
import re
import sys
import warnings
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.import_client.adapters.base import DatasetAdapter
from scripts.import_client.models import CustodianSpec, EmailMetadata, ImportItem, SourceContainer

LOADFILE_COLUMNS = (
    "id",
    "tid",
    "bates",
    "type",
    "description",
    "title",
    "author",
    "mentioned",
    "attending",
    "copied",
    "recipient",
    "redacted",
    "collection_name",
    "pages",
    "exhibit_number",
    "document_date",
    "date_added_ucsf",
    "date_modified_ucsf",
    "date_added_industry",
    "date_modified_industry",
    "date_produced",
    "date_shipped",
    "deposition_date",
    "date_privilege_logged",
    "case",
    "industry",
    "drug",
    "adverse_ruling",
    "area",
    "bates_alternate",
    "box",
    "brand",
    "country",
    "language",
    "court",
    "format",
    "express_waiver",
    "file",
    "genre",
    "keywords",
    "bates_master",
    "other_number",
    "request_number",
    "minnesota_request_number",
    "privilege_code",
    "topic",
    "witness",
    "cited",
    "availability",
    "grant_number",
    "source",
    "folder",
    "series",
    "chemical",
    "food",
    "rights",
    "attachment",
    "attachmentnum",
    "conversation",
    "conversationid",
    "custodian",
    "datereceived",
    "datesent",
    "filename",
    "filepath",
    "messageid",
    "subject",
    "timereceived",
    "timesent",
    "redaction",
    "ocr_text",
)

_HEAD_BANNER = re.compile(r"^==> .+ <==\s*$")
_NATURAL_SORT_PART = re.compile(r"(\d+)")
_SOURCE_TIMESTAMP = re.compile(
    r"^(?P<weekday>[A-Z][a-z]{2}) (?P<month>[A-Z][a-z]{2}) "
    r"(?P<day>\d{2}) (?P<clock>\d{2}:\d{2}:\d{2}) "
    r"(?P<zone>PST|PDT) (?P<year>\d{4})$"
)
_ZONE_OFFSETS = {"PST": "-0800", "PDT": "-0700"}
_LOS_ANGELES = ZoneInfo("America/Los_Angeles")


class MalformedLoadfileWarning(UserWarning):
    """A malformed load-file record was explicitly skipped."""


def _source_sort_key(path: Path) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in _NATURAL_SORT_PART.split(path.name)
    )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_source_timestamp(value: str | None) -> datetime | None:
    cleaned = _clean(value)
    if cleaned is None or "," in cleaned:
        return None
    match = _SOURCE_TIMESTAMP.fullmatch(cleaned)
    if match is None:
        return None
    local = datetime.strptime(
        " ".join(
            (
                match["weekday"],
                match["month"],
                match["day"],
                match["clock"],
                match["year"],
                _ZONE_OFFSETS[match["zone"]],
            )
        ),
        "%a %b %d %H:%M:%S %Y %z",
    )
    return local.astimezone(timezone.utc)


def _parse_sent_at(date_value: str | None, time_value: str | None) -> datetime | None:
    cleaned_date = _clean(date_value)
    if cleaned_date is None:
        return None
    try:
        sent_date = datetime.strptime(
            f"{cleaned_date} +0000", "%Y %B %d %z"
        ).date()
        sent_time = time.fromisoformat(_clean(time_value) or "00:00:00")
    except ValueError:
        return None
    sent_at = datetime.combine(sent_date, sent_time)
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=_LOS_ANGELES)
    return sent_at.astimezone(timezone.utc)


@contextmanager
def _loadfile_rows(source: Path) -> Iterator[csv.DictReader]:
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(max(previous_limit, min(source.stat().st_size, sys.maxsize)))
    try:
        with source.open("r", encoding="utf-8-sig", errors="strict", newline="") as handle:
            first_line = handle.readline()
            if not _HEAD_BANNER.fullmatch(first_line):
                handle.seek(0)
            rows = csv.DictReader(
                handle,
                delimiter="|",
                quotechar='"',
                strict=True,
            )
            if rows.fieldnames != list(LOADFILE_COLUMNS):
                actual = rows.fieldnames or []
                missing = sorted(set(LOADFILE_COLUMNS).difference(actual))
                extra = sorted(set(actual).difference(LOADFILE_COLUMNS))
                details = []
                if missing:
                    details.append("missing: " + ", ".join(missing))
                if extra:
                    details.append("unexpected: " + ", ".join(extra))
                if not details:
                    details.append("column order differs")
                raise ValueError(
                    f"Industry Documents load file schema mismatch for {source} ("
                    + "; ".join(details)
                    + ")"
                )
            yield rows
    finally:
        csv.field_size_limit(previous_limit)


class IndustryDocumentsLoadfileAdapter(DatasetAdapter):
    """Import one or more UCSF Industry Documents pipe-delimited load files."""

    dataset_name = "industrydocuments"
    default_collection_name = "Industry Documents"
    expand_email_attachments = False

    def __init__(
        self,
        source: Path,
        *,
        skip_malformed_records: bool = False,
    ) -> None:
        self.source = source.expanduser().resolve()
        self.skip_malformed_records = skip_malformed_records
        if self.source.is_file():
            if self.source.suffix.casefold() != ".csv":
                raise ValueError(
                    "Industry Documents source file must have a .csv extension: "
                    f"{self.source}"
                )
            self.sources = (self.source,)
        elif self.source.is_dir():
            self.sources = tuple(
                sorted(
                    (
                        candidate
                        for candidate in self.source.iterdir()
                        if candidate.is_file()
                        and candidate.suffix.casefold() == ".csv"
                    ),
                    key=_source_sort_key,
                )
            )
            if not self.sources:
                raise ValueError(
                    "Industry Documents source directory contains no CSV files: "
                    f"{self.source}"
                )
        else:
            raise ValueError(
                "Industry Documents source must be a load-file CSV or directory: "
                f"{self.source}"
            )
        self.unknown_custodian = CustodianSpec(
            display_name="Unknown - Industry Documents collection",
            external_reference="industrydocuments:unknown-custodian",
        )
        for loadfile in self.sources:
            with _loadfile_rows(loadfile):
                pass

    def source_containers(self) -> Iterable[SourceContainer]:
        for loadfile in self.sources:
            yield SourceContainer(
                key=loadfile.name,
                path=loadfile,
                media_type="text/csv",
                original_source_path=loadfile.name,
                container_kind="CUSTOM",
            )

    def custom_items(self, source_container: SourceContainer) -> Iterable[ImportItem]:
        seen_ids: set[str] = set()
        with _loadfile_rows(source_container.path) as rows:
            row_number = 1
            while True:
                try:
                    row = next(rows)
                except StopIteration:
                    break
                except csv.Error as exc:
                    message = (
                        f"Malformed CSV in {source_container.path} after "
                        f"{row_number - 1} complete records "
                        f"(physical line {rows.line_num}): {exc}"
                    )
                    if not self.skip_malformed_records:
                        raise ValueError(message) from exc
                    warnings.warn(
                        message + "; skipped incomplete record and continued",
                        MalformedLoadfileWarning,
                        stacklevel=2,
                    )
                    break
                row_number += 1
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(
                        f"Malformed load-file row {row_number}: expected "
                        f"{len(LOADFILE_COLUMNS)} columns"
                    )
                document_id = _clean(row["id"])
                if document_id is None:
                    raise ValueError(f"Missing id at load-file row {row_number}")
                if document_id in seen_ids:
                    raise ValueError(
                        f"Duplicate id at load-file row {row_number}: {document_id}"
                    )
                seen_ids.add(document_id)

                ocr_text = row["ocr_text"]
                has_ocr = bool(ocr_text.strip())
                source_type = (_clean(row["type"]) or "unknown").casefold()
                record_type = "EMAIL" if source_type == "email" else "FILE"
                filename = _clean(row["filename"])
                if filename is None:
                    filename = f"{document_id}.{'txt' if has_ocr else 'bin'}"
                source_path = (
                    _clean(row["filepath"])
                    or _clean(row["filename"])
                    or f"{source_container.path.name}#row={row_number}"
                )
                document_timestamp = _parse_source_timestamp(row["document_date"])
                sent_at = _parse_sent_at(row["datesent"], row["timesent"])
                if sent_at is None:
                    sent_at = document_timestamp

                imported_metadata = {
                    key: cleaned
                    for key, value in row.items()
                    if key != "ocr_text" and (cleaned := _clean(value)) is not None
                }
                yield ImportItem(
                    source_container_key=source_container.key,
                    source_item_id=f"industrydocuments:{document_id}",
                    record_type=record_type,
                    original_filename=filename,
                    original_source_path=source_path,
                    content=ocr_text.encode("utf-8") if has_ocr else b"",
                    media_type="text/plain" if has_ocr else "application/octet-stream",
                    custodians=(self.unknown_custodian,),
                    primary_custodian_key=self.unknown_custodian.cache_key,
                    source_created_at=document_timestamp,
                    source_modified_at=document_timestamp,
                    processing_status="READY" if has_ocr else "METADATA_INCOMPLETE",
                    email=(
                        EmailMetadata(
                            sender=_clean(row["author"]),
                            subject=_clean(row["title"]),
                            sent_at=sent_at,
                        )
                        if record_type == "EMAIL"
                        else None
                    ),
                    raw_metadata={
                        "dataset": self.dataset_name,
                        "loadfile": source_container.path.name,
                        "loadfile_row_number": row_number,
                        "source_record_type": source_type,
                        "content_representation": (
                            "OCR_TEXT_FROM_LOADFILE" if has_ocr else "NO_CONTENT_IN_LOADFILE"
                        ),
                        "native_file_available": False,
                    },
                    unmapped_metadata={
                        "CONTROL_NUMBER": document_id,
                        **imported_metadata,
                    },
                )
