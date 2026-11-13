from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from scripts.batch_coding_import import (
    COMPLETED,
    LIST_BATCH_DOCUMENTS,
    BatchCodingImporter,
    CodingImportReport,
    CodingImportRow,
)
from scripts.import_client.api import OpenApiClient

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RESOLVE_SOURCE_IDS = (
    "resolve_collection_item_source_ids_"
    "v1_collections__collection_id__items_resolve_source_ids_post"
)
_DOCUMENT_ID_PATTERN = re.compile(r"3\.\d+\.[A-Z0-9]+(?:\.\d+)?", re.IGNORECASE)
_JUDGMENT_VALUES = {
    0: False,
    1: True,
}
T = TypeVar("T")


@dataclass(frozen=True)
class Qrel:
    topic: int
    iteration: int
    document_id: str
    relevance: int
    source_line: int


@dataclass(frozen=True)
class QrelsReviewerImportPlan:
    reviewer: dict[str, Any]
    batch: dict[str, Any]
    rows: list[CodingImportRow]
    source_rows: int
    judged_rows: int
    unassessed_rows_skipped: int


def load_qrels(source: Path, topics: set[int]) -> tuple[list[Qrel], Counter[int]]:
    if not source.is_file():
        raise ValueError(f"Qrels source must be a file: {source}")
    rows: list[Qrel] = []
    skipped: Counter[int] = Counter()
    seen: set[tuple[int, str]] = set()
    with source.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            columns = line.split()
            if len(columns) != 5:
                raise ValueError(
                    f"Invalid Qrels row at line {line_number}: expected 5 columns, "
                    f"received {len(columns)}"
                )
            raw_topic, raw_iteration, document_id, raw_relevance, raw_probability = columns
            try:
                topic = int(raw_topic)
                iteration = int(raw_iteration)
                relevance = int(raw_relevance)
                probability = float(raw_probability)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid numeric Qrels value at line {line_number}"
                ) from exc
            if topic not in topics:
                continue
            if iteration != 0:
                raise ValueError(
                    f"Unsupported Qrels iteration at line {line_number}: {iteration}"
                )
            if _DOCUMENT_ID_PATTERN.fullmatch(document_id) is None:
                raise ValueError(
                    f"Invalid EDRM Enron v2 document ID at line {line_number}: "
                    f"{document_id!r}"
                )
            if not 0 <= probability <= 1:
                raise ValueError(
                    f"Qrels sampling probability must be between 0 and 1 at line "
                    f"{line_number}"
                )
            identity = (topic, document_id.casefold())
            if identity in seen:
                raise ValueError(
                    f"Duplicate Qrels document for topic {topic} at line {line_number}: "
                    f"{document_id}"
                )
            seen.add(identity)
            if relevance not in _JUDGMENT_VALUES:
                if relevance not in {-1, -2}:
                    raise ValueError(
                        f"Unsupported Qrels relevance at line {line_number}: {relevance}"
                    )
                skipped[topic] += 1
                continue
            rows.append(
                Qrel(
                    topic=topic,
                    iteration=iteration,
                    document_id=document_id,
                    relevance=relevance,
                    source_line=line_number,
                )
            )
    found_topics = {row.topic for row in rows} | set(skipped)
    missing_topics = topics - found_topics
    if missing_topics:
        raise ValueError(
            "Qrels source contains no rows for topic(s): "
            + ", ".join(str(topic) for topic in sorted(missing_topics))
        )
    return rows, skipped


def _chunks(values: list[T], size: int) -> list[list[T]]:
    return [values[start : start + size] for start in range(0, len(values), size)]


class TrecQrelsImporter:
    def __init__(
        self,
        api: OpenApiClient,
        *,
        matter_id: str,
        collection_id: str,
        batch_id: str,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.api = api
        self.matter_id = matter_id
        self.collection_id = collection_id
        self.batch_id = batch_id
        self.progress = progress or (lambda _message: None)
        self.batch_importer = BatchCodingImporter(
            api,
            matter_id=matter_id,
            batch_id=batch_id,
        )

    @staticmethod
    def _coding_field(batch: dict[str, Any], topic: int) -> dict[str, Any]:
        field_key = "trec_priv_coding" if topic == 304 else f"trec_topic_{topic}_relevant"
        matches: dict[str, dict[str, Any]] = {}
        for group in batch.get("coding_groups") or []:
            for field in group.get("fields") or []:
                snapshot = field.get("definition_snapshot") or {}
                if snapshot.get("key") == field_key:
                    matches[str(field["metadata_definition_id"])] = field
        if not matches:
            raise ValueError(
                f"Review batch coding configuration does not contain {field_key!r}"
            )
        if len(matches) != 1:
            raise ValueError(f"Review batch contains duplicate {field_key!r} fields")
        field = next(iter(matches.values()))
        snapshot = field.get("definition_snapshot") or {}
        if snapshot.get("type") != "BOOLEAN" or snapshot.get("cardinality") != "SINGLE":
            raise ValueError(f"Review batch field {field_key!r} must be a single Boolean")
        return field

    def _resolve_collection_items(self, qrels: list[Qrel]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        missing: list[str] = []
        for qrel_batch in _chunks(qrels, 1000):
            resolution = self.api.request(
                RESOLVE_SOURCE_IDS,
                path_params={"collection_id": self.collection_id},
                json_body={
                    "source_item_ids": list(
                        dict.fromkeys(row.document_id for row in qrel_batch)
                    )
                },
            )
            resolved.update(
                {
                    item["source_item_id"]: str(item["collection_item_id"])
                    for item in resolution["items"]
                }
            )
            missing.extend(resolution["missing_source_item_ids"])
        if missing:
            preview = ", ".join(sorted(missing)[:10])
            suffix = "" if len(missing) <= 10 else f" (and {len(missing) - 10:,} more)"
            raise ValueError(
                f"Collection is missing {len(missing):,} coded Qrels documents: "
                f"{preview}{suffix}"
            )
        return resolved

    def _batch_documents_by_collection_item(self) -> dict[str, str]:
        documents: dict[str, str] = {}
        offset = 0
        while True:
            page = self.api.request(
                LIST_BATCH_DOCUMENTS,
                path_params={
                    "matter_id": self.batch_importer.matter_id,
                    "batch_id": self.batch_importer.batch_id,
                },
                params={"offset": offset, "limit": 500},
            )
            for document in page:
                documents[str(document["collection_item_id"])] = str(
                    document["matter_document_id"]
                )
            if len(page) < 500:
                return documents
            offset += len(page)

    def prepare(
        self,
        qrels: list[Qrel],
        skipped: Counter[int],
    ) -> QrelsReviewerImportPlan:
        topics = {row.topic for row in qrels} | set(skipped)
        if len(topics) != 1:
            raise ValueError("Reviewer coding import supports exactly one TREC topic")
        topic = next(iter(topics))
        reviewer, batch = self.batch_importer.load_scope()
        field = self._coding_field(batch, topic)
        definition_id = str(field["metadata_definition_id"])
        resolved = self._resolve_collection_items(qrels)
        batch_documents = self._batch_documents_by_collection_item()
        missing_batch_documents = sorted(
            row.document_id
            for row in qrels
            if resolved[row.document_id] not in batch_documents
        )
        if missing_batch_documents:
            preview = ", ".join(missing_batch_documents[:10])
            suffix = (
                ""
                if len(missing_batch_documents) <= 10
                else f" (and {len(missing_batch_documents) - 10:,} more)"
            )
            raise ValueError(
                f"Review batch is missing {len(missing_batch_documents):,} coded "
                f"Qrels documents: {preview}{suffix}"
            )
        rows = [
            CodingImportRow(
                document_id=batch_documents[resolved[row.document_id]],
                review_status=COMPLETED,
                fields=(
                    {
                        "metadata_definition_id": definition_id,
                        "values": [_JUDGMENT_VALUES[row.relevance]],
                    },
                ),
                source_line=row.source_line,
            )
            for row in qrels
        ]
        return QrelsReviewerImportPlan(
            reviewer=reviewer,
            batch=batch,
            rows=rows,
            source_rows=len(qrels) + sum(skipped.values()),
            judged_rows=len(qrels),
            unassessed_rows_skipped=sum(skipped.values()),
        )

    def run(
        self,
        qrels: list[Qrel],
        skipped: Counter[int],
        *,
        continue_on_error: bool = False,
    ) -> tuple[QrelsReviewerImportPlan, CodingImportReport]:
        plan = self.prepare(qrels, skipped)
        report = self.batch_importer.import_rows(
            plan.reviewer,
            plan.rows,
            continue_on_error=continue_on_error,
            progress=self.progress,
        )
        return plan, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Import TREC Legal Qrels into the authenticated user's canonical "
            "reviewer coding for a ready review batch."
        )
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--topic", type=int, required=True)
    parser.add_argument("--matter-id", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--collection-id", required=True)
    parser.add_argument(
        "--base-url",
        default=os.getenv("PVR_BATCH_IMPORT_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument(
        "--openapi",
        type=Path,
        default=REPOSITORY_ROOT / "web" / "openapi.json",
    )
    parser.add_argument("--email", default=os.getenv("PVR_BATCH_IMPORT_EMAIL"))
    parser.add_argument("--password-env", default="PVR_BATCH_IMPORT_PASSWORD")
    parser.add_argument("--access-token-env", default="PVR_BATCH_IMPORT_ACCESS_TOKEN")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    topics = {args.topic}
    try:
        qrels, skipped = load_qrels(args.source, topics)
        api = OpenApiClient(args.base_url, args.openapi)
        try:
            access_token = os.getenv(args.access_token_env)
            if access_token:
                api.set_access_token(access_token)
                password = os.getenv(args.password_env)
                if args.email and password:
                    api.set_reauthentication_credentials(args.email, password)
            else:
                if not args.email:
                    parser.error(
                        "--email is required unless an access token environment "
                        "variable is set"
                    )
                password = os.getenv(args.password_env) or getpass.getpass(
                    "Priv-View password: "
                )
                api.login(args.email, password)
            importer = TrecQrelsImporter(
                api,
                matter_id=args.matter_id,
                collection_id=args.collection_id,
                batch_id=args.batch_id,
                progress=lambda message: print(message, file=sys.stderr),
            )
            if args.dry_run:
                plan = importer.prepare(qrels, skipped)
                print(
                    json.dumps(
                        {
                            "dry_run": True,
                            "source": str(args.source),
                            "topic": args.topic,
                            "batch_id": args.batch_id,
                            "batch_name": plan.batch.get("name"),
                            "reviewer_user_id": plan.reviewer.get("id"),
                            "reviewer_email": plan.reviewer.get("email"),
                            "source_rows": plan.source_rows,
                            "judged_rows": plan.judged_rows,
                            "privileged_rows": sum(row.relevance == 1 for row in qrels),
                            "not_privileged_rows": sum(row.relevance == 0 for row in qrels),
                            "unassessed_rows_skipped": plan.unassessed_rows_skipped,
                        },
                        indent=2,
                    )
                )
                return 0
            plan, report = importer.run(
                qrels,
                skipped,
                continue_on_error=args.continue_on_error,
            )
            print(
                json.dumps(
                    {
                        "source": str(args.source),
                        "topic": args.topic,
                        "source_rows": plan.source_rows,
                        "judged_rows": plan.judged_rows,
                        "unassessed_rows_skipped": plan.unassessed_rows_skipped,
                        "reviewer_coding": report.to_dict(),
                    },
                    indent=2,
                )
            )
            return 1 if report.errors else 0
        finally:
            api.close()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Qrels import failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
