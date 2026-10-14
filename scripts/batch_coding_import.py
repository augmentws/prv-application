from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from scripts.import_client.api import ApiError, OpenApiClient

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCUMENT_ID_COLUMN = "matter_document_id"
STATUS_COLUMN = "_review_status"
COMPLETED = "COMPLETED"
SKIPPED = "SKIPPED"

GET_CURRENT_USER = "me_v1_auth_me_get"
GET_BATCH = "get_review_batch_v1_matters__matter_id__review_batches__batch_id__get"
LIST_BATCH_DOCUMENTS = (
    "list_review_batch_documents_v1_matters__matter_id__review_batches__batch_id__documents_get"
)
GET_REVIEWER_CODING = (
    "get_or_create_review_batch_reviewer_coding_v1_matters__matter_id__review_batches__batch_id__"
    "reviewer_coding_post"
)
REPLACE_VALUES = (
    "replace_review_batch_reviewer_values_v1_matters__matter_id__review_batches__batch_id__reviewer_coding__"
    "documents__document_id__values_put"
)
SKIP_DOCUMENT = (
    "skip_review_batch_document_v1_matters__matter_id__review_batches__batch_id__reviewer_coding__"
    "documents__document_id__skip_post"
)


@dataclass(frozen=True)
class CodingField:
    metadata_definition_id: str
    key: str
    display_name: str
    value_type: str
    cardinality: str
    allowed_values: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class CodingImportRow:
    document_id: str
    review_status: str
    fields: tuple[dict[str, Any], ...]
    source_line: int


@dataclass
class CodingImportReport:
    batch_id: str
    reviewer_user_id: str
    reviewer_email: str
    source_rows: int
    reviewer_coding_id: str | None = None
    imported_documents: int = 0
    skipped_documents: int = 0
    failed_documents: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _field_catalog(batch: dict[str, Any]) -> tuple[dict[str, CodingField], dict[str, CodingField]]:
    by_key: dict[str, CodingField] = {}
    display_candidates: dict[str, list[CodingField]] = {}
    seen_ids: set[str] = set()
    for group in batch.get("coding_groups") or []:
        for item in group.get("fields") or []:
            definition_id = str(item["metadata_definition_id"])
            if definition_id in seen_ids:
                continue
            seen_ids.add(definition_id)
            snapshot = item.get("definition_snapshot") or {}
            coding_field = CodingField(
                metadata_definition_id=definition_id,
                key=str(snapshot.get("key") or ""),
                display_name=str(snapshot.get("display_name") or snapshot.get("key") or definition_id),
                value_type=str(snapshot.get("type") or ""),
                cardinality=str(snapshot.get("cardinality") or "SINGLE"),
                allowed_values=tuple(snapshot.get("allowed_values") or []),
            )
            if not coding_field.key:
                raise ValueError(f"Batch coding field {definition_id} has no stable key")
            if coding_field.key in by_key and by_key[coding_field.key] != coding_field:
                raise ValueError(f"Batch coding field key is ambiguous: {coding_field.key}")
            by_key[coding_field.key] = coding_field
            display_candidates.setdefault(coding_field.display_name.casefold(), []).append(coding_field)
    by_display = {
        label: candidates[0]
        for label, candidates in display_candidates.items()
        if len(candidates) == 1
    }
    return by_key, by_display


def _enum_value(raw: str, coding_field: CodingField) -> str:
    keys = {
        str(item["key"]): str(item["key"])
        for item in coding_field.allowed_values
        if item.get("active", True) and item.get("key") is not None
    }
    if raw in keys:
        return keys[raw]
    labels: dict[str, list[str]] = {}
    for item in coding_field.allowed_values:
        if not item.get("active", True) or item.get("key") is None:
            continue
        label = str(item.get("label") or item["key"]).casefold()
        labels.setdefault(label, []).append(str(item["key"]))
    matches = labels.get(raw.casefold(), [])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"enum label is ambiguous for {coding_field.key}: {raw}")
    raise ValueError(f"unknown enum key or label for {coding_field.key}: {raw}")


def _scalar_value(raw: str, coding_field: CodingField) -> Any:
    value_type = coding_field.value_type
    if value_type in {"TEXT", "LONG_TEXT"}:
        return raw
    normalized = raw.strip()
    if value_type == "ENUM":
        return _enum_value(normalized, coding_field)
    if value_type == "BOOLEAN":
        lowered = normalized.casefold()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
        raise ValueError(f"{coding_field.key} requires true/false, yes/no, or 1/0")
    if value_type == "INTEGER":
        try:
            return int(normalized)
        except ValueError as exc:
            raise ValueError(f"{coding_field.key} requires a whole number") from exc
    if value_type == "DECIMAL":
        try:
            return float(normalized)
        except ValueError as exc:
            raise ValueError(f"{coding_field.key} requires a number") from exc
    if value_type == "JSON":
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{coding_field.key} requires JSON") from exc
        if not isinstance(value, dict | list):
            raise ValueError(f"{coding_field.key} requires a JSON object or array")
        return value
    if value_type in {"DATE", "DATETIME"}:
        return normalized
    raise ValueError(f"Unsupported batch coding field type: {value_type}")


def _cell_values(raw: str, coding_field: CodingField, delimiter: str | None) -> list[Any]:
    if raw == "":
        return []
    items: list[Any]
    stripped = raw.strip()
    if coding_field.cardinality == "MULTIPLE" and stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{coding_field.key} contains an invalid JSON array") from exc
        if not isinstance(parsed, list):
            raise ValueError(f"{coding_field.key} requires a JSON array for multiple values")
        items = parsed
    elif coding_field.cardinality == "MULTIPLE" and delimiter and delimiter in raw:
        items = [item.strip() for item in raw.split(delimiter) if item.strip()]
    else:
        items = [raw]
    if coding_field.cardinality == "SINGLE" and len(items) > 1:
        raise ValueError(f"{coding_field.key} accepts only one value")
    values: list[Any] = []
    for item in items:
        if not isinstance(item, str):
            if coding_field.value_type in {"BOOLEAN", "INTEGER", "DECIMAL", "JSON"}:
                values.append(item)
                continue
            item = str(item)
        values.append(_scalar_value(item, coding_field))
    return values


def load_coding_rows(
    source: Path,
    batch: dict[str, Any],
    *,
    multi_value_delimiter: str | None = None,
) -> list[CodingImportRow]:
    by_key, by_display = _field_catalog(batch)
    if not by_key:
        raise ValueError("The batch has no configured coding fields")
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        if DOCUMENT_ID_COLUMN not in headers:
            raise ValueError(f"CSV must contain a {DOCUMENT_ID_COLUMN} column")
        coding_columns = [header for header in headers if header not in {DOCUMENT_ID_COLUMN, STATUS_COLUMN}]
        if not coding_columns:
            raise ValueError("CSV must contain at least one batch coding field column")
        resolved_columns: dict[str, CodingField] = {}
        for column in coding_columns:
            coding_field = by_key.get(column) or by_display.get(column.casefold())
            if coding_field is None:
                raise ValueError(f"CSV column is not a uniquely configured batch coding field: {column}")
            if coding_field in resolved_columns.values():
                raise ValueError(f"CSV identifies the same batch coding field more than once: {column}")
            resolved_columns[column] = coding_field

        rows: list[CodingImportRow] = []
        seen_documents: set[str] = set()
        for line_number, source_row in enumerate(reader, start=2):
            raw_document_id = (source_row.get(DOCUMENT_ID_COLUMN) or "").strip()
            try:
                document_id = str(uuid.UUID(raw_document_id))
            except ValueError as exc:
                raise ValueError(f"Line {line_number}: invalid matter_document_id") from exc
            if document_id in seen_documents:
                raise ValueError(f"Line {line_number}: duplicate matter_document_id {document_id}")
            seen_documents.add(document_id)
            status = (source_row.get(STATUS_COLUMN) or COMPLETED).strip().upper()
            if status not in {COMPLETED, SKIPPED}:
                raise ValueError(f"Line {line_number}: {STATUS_COLUMN} must be COMPLETED or SKIPPED")
            fields: list[dict[str, Any]] = []
            for column, coding_field in resolved_columns.items():
                raw = source_row.get(column)
                if raw is None:
                    raise ValueError(f"Line {line_number}: missing value for {column}")
                try:
                    values = _cell_values(raw, coding_field, multi_value_delimiter)
                except ValueError as exc:
                    raise ValueError(f"Line {line_number}: {exc}") from exc
                fields.append({"metadata_definition_id": coding_field.metadata_definition_id, "values": values})
            if status == SKIPPED and any(item["values"] for item in fields):
                raise ValueError(f"Line {line_number}: skipped documents cannot contain coding values")
            rows.append(
                CodingImportRow(
                    document_id=document_id,
                    review_status=status,
                    fields=tuple(fields),
                    source_line=line_number,
                )
            )
    if not rows:
        raise ValueError("CSV contains no coding rows")
    return rows


class BatchCodingImporter:
    def __init__(self, api: OpenApiClient, *, matter_id: str, batch_id: str) -> None:
        self.api = api
        self.matter_id = str(uuid.UUID(matter_id))
        self.batch_id = str(uuid.UUID(batch_id))
        self.path_params = {"matter_id": self.matter_id, "batch_id": self.batch_id}

    def load_scope(self) -> tuple[dict[str, Any], dict[str, Any]]:
        reviewer = self.api.request(GET_CURRENT_USER)
        batch = self.api.request(GET_BATCH, path_params=self.path_params)
        if batch.get("status") != "READY":
            raise ValueError("The review batch must be ready")
        assigned = batch.get("assigned_user")
        if assigned and str(assigned.get("id")) != str(reviewer.get("id")):
            raise ValueError(
                f"The batch is assigned to {assigned.get('email') or assigned.get('display_name')}, "
                f"not {reviewer.get('email')}"
            )
        return reviewer, batch

    def validate_membership(self, rows: list[CodingImportRow]) -> None:
        requested = {row.document_id for row in rows}
        found: set[str] = set()
        ordered = sorted(requested)
        for start in range(0, len(ordered), 100):
            document_ids = ordered[start : start + 100]
            documents = self.api.request(
                LIST_BATCH_DOCUMENTS,
                path_params=self.path_params,
                params={"document_id": document_ids, "offset": 0, "limit": 500},
            )
            found.update(str(item["matter_document_id"]) for item in documents)
        missing = sorted(requested - found)
        if missing:
            preview = ", ".join(missing[:5])
            suffix = "…" if len(missing) > 5 else ""
            raise ValueError(f"{len(missing)} CSV documents are not in this batch: {preview}{suffix}")

    def import_rows(
        self,
        reviewer: dict[str, Any],
        rows: list[CodingImportRow],
        *,
        continue_on_error: bool,
        progress: Any = None,
    ) -> CodingImportReport:
        coding = self.api.request(GET_REVIEWER_CODING, path_params=self.path_params)
        report = CodingImportReport(
            batch_id=self.batch_id,
            reviewer_user_id=str(reviewer["id"]),
            reviewer_email=str(reviewer["email"]),
            source_rows=len(rows),
            reviewer_coding_id=str(coding["id"]),
        )
        for index, row in enumerate(rows, start=1):
            path_params = {
                **self.path_params,
                "document_id": row.document_id,
            }
            request_key = "batch-coding-import:" + hashlib.sha256(
                json.dumps(
                    {
                        "batch_id": self.batch_id,
                        "document_id": row.document_id,
                        "review_status": row.review_status,
                        "fields": row.fields,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            try:
                if row.review_status == SKIPPED:
                    self.api.request(
                        SKIP_DOCUMENT,
                        path_params=path_params,
                        params={"origin": "IMPORT", "request_key": request_key},
                    )
                    report.skipped_documents += 1
                else:
                    self.api.request(
                        REPLACE_VALUES,
                        path_params=path_params,
                        json_body={
                            "matter_document_id": row.document_id,
                            "fields": list(row.fields),
                            "origin": "IMPORT",
                            "request_key": request_key,
                        },
                    )
                    report.imported_documents += 1
            except (ApiError, ValueError, RuntimeError) as exc:
                report.failed_documents += 1
                report.errors.append(
                    {"line": row.source_line, "matter_document_id": row.document_id, "error": str(exc)}
                )
                if not continue_on_error:
                    raise RuntimeError(
                        f"Import stopped at CSV line {row.source_line}; correct the row and rerun the import"
                    ) from exc
            if progress:
                progress(
                    f"Processed {index}/{len(rows)} rows "
                    f"({report.imported_documents} coded, {report.skipped_documents} skipped, "
                    f"{report.failed_documents} failed)"
                )
        return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.batch_coding_import",
        description="Import CSV coding into the authenticated reviewer's canonical batch coding through the Core API.",
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--matter-id", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--base-url", default=os.getenv("PVR_BATCH_IMPORT_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--openapi", type=Path, default=REPOSITORY_ROOT / "web" / "openapi.json")
    parser.add_argument("--email", default=os.getenv("PVR_BATCH_IMPORT_EMAIL"))
    parser.add_argument("--password-env", default="PVR_BATCH_IMPORT_PASSWORD")
    parser.add_argument("--access-token-env", default="PVR_BATCH_IMPORT_ACCESS_TOKEN")
    parser.add_argument("--multi-value-delimiter", help="Optional delimiter for MULTIPLE cells; JSON arrays are preferred")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        api = OpenApiClient(args.base_url, args.openapi)
        try:
            token = os.getenv(args.access_token_env)
            if token:
                api.set_access_token(token)
                password = os.getenv(args.password_env)
                if args.email and password:
                    api.set_reauthentication_credentials(args.email, password)
            else:
                if not args.email:
                    parser.error("--email is required unless an access-token environment variable is set")
                password = os.getenv(args.password_env) or getpass.getpass("Priv-View password: ")
                api.login(args.email, password)
            importer = BatchCodingImporter(api, matter_id=args.matter_id, batch_id=args.batch_id)
            reviewer, batch = importer.load_scope()
            rows = load_coding_rows(
                args.source,
                batch,
                multi_value_delimiter=args.multi_value_delimiter,
            )
            importer.validate_membership(rows)
            if args.dry_run:
                print(
                    json.dumps(
                        {
                            "dry_run": True,
                            "batch_id": importer.batch_id,
                            "batch_name": batch.get("name"),
                            "reviewer_user_id": reviewer.get("id"),
                            "reviewer_email": reviewer.get("email"),
                            "source_rows": len(rows),
                            "coding_field_count": len(rows[0].fields),
                            "skipped_rows": sum(row.review_status == SKIPPED for row in rows),
                        },
                        indent=2,
                    )
                )
                return 0
            report = importer.import_rows(
                reviewer,
                rows,
                continue_on_error=args.continue_on_error,
                progress=lambda message: print(message, file=sys.stderr),
            )
            print(json.dumps(report.to_dict(), indent=2))
            return 1 if report.errors else 0
        finally:
            api.close()
    except (ApiError, OSError, ValueError, RuntimeError) as exc:
        print(f"Batch coding import failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
