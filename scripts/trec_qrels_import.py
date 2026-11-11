from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TypeVar

from scripts.import_client.api import OpenApiClient

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RESOLVE_SOURCE_IDS = (
    "resolve_collection_item_source_ids_"
    "v1_collections__collection_id__items_resolve_source_ids_post"
)
LIST_DEFINITIONS = (
    "list_metadata_definitions_v1_matters__matter_id__metadata_definitions_get"
)
CREATE_DEFINITION = (
    "create_metadata_definition_v1_matters__matter_id__metadata_definitions_post"
)
IMPORT_METADATA = (
    "import_matter_document_metadata_"
    "v1_matters__matter_id__document_metadata_imports_post"
)
_DOCUMENT_ID_PATTERN = re.compile(r"3\.\d+\.[A-Z0-9]+(?:\.\d+)?", re.IGNORECASE)
_JUDGMENT_VALUES = {
    0: "not_relevant",
    1: "relevant",
}
T = TypeVar("T")


@dataclass(frozen=True)
class Qrel:
    topic: int
    iteration: int
    document_id: str
    relevance: int
    sampling_probability: float
    source_line: int


@dataclass
class QrelsImportReport:
    matter_id: str
    collection_id: str
    topics: list[int]
    source_rows: int
    judged_rows: int
    unassessed_rows_skipped: int
    resolved_collection_items: int = 0
    imported_judgment_events: int = 0
    existing_judgment_events: int = 0
    imported_probability_events: int = 0
    existing_probability_events: int = 0
    missing_collection_document_ids: list[str] = field(default_factory=list)
    missing_matter_document_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


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
                    sampling_probability=probability,
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


def _definition_specs(topic: int, source_name: str) -> tuple[dict[str, object], dict[str, object]]:
    judgment = {
        "key": f"trec_topic_{topic}_judgment",
        "display_name": f"TREC Topic {topic} Judgment",
        "description": f"Imported gold judgment from {source_name}.",
        "type": "ENUM",
        "cardinality": "SINGLE",
        "allowed_values": [
            {"key": "relevant", "label": "Relevant", "active": True},
            {"key": "not_relevant", "label": "Not relevant", "active": True},
        ],
        "assertion_policy": "IMMEDIATE",
        "resolution_policy": "EXPLICIT_ONLY",
        "searchable": True,
        "facetable": True,
        "reviewable": True,
        "ai_assignable": False,
    }
    probability = {
        "key": f"trec_topic_{topic}_sampling_probability",
        "display_name": f"TREC Topic {topic} Sampling Probability",
        "description": f"Qrels assessment sampling probability imported from {source_name}.",
        "type": "DECIMAL",
        "cardinality": "SINGLE",
        "allowed_values": None,
        "assertion_policy": "IMMEDIATE",
        "resolution_policy": "EXPLICIT_ONLY",
        "searchable": False,
        "facetable": False,
        "reviewable": False,
        "ai_assignable": False,
    }
    return judgment, probability


def _chunks(values: list[T], size: int) -> list[list[T]]:
    return [values[start : start + size] for start in range(0, len(values), size)]


class TrecQrelsImporter:
    def __init__(
        self,
        api: OpenApiClient,
        *,
        matter_id: str,
        collection_id: str,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.api = api
        self.matter_id = matter_id
        self.collection_id = collection_id
        self.progress = progress or (lambda _message: None)

    def _ensure_definition(self, specification: dict[str, object]) -> dict[str, object]:
        definitions = self.api.request(
            LIST_DEFINITIONS,
            path_params={"matter_id": self.matter_id},
        )
        existing = next(
            (
                definition
                for definition in definitions
                if definition["key"] == specification["key"]
            ),
            None,
        )
        if existing is None:
            return self.api.request(
                CREATE_DEFINITION,
                path_params={"matter_id": self.matter_id},
                json_body=specification,
            )
        expected_enum_keys = [
            value["key"] for value in (specification.get("allowed_values") or [])
        ]
        actual_enum_keys = [
            value["key"]
            for value in (existing.get("allowed_values") or [])
            if value.get("active", True)
        ]
        if (
            existing.get("status") != "ACTIVE"
            or existing.get("value_source") != "ASSERTED"
            or existing.get("type") != specification["type"]
            or existing.get("cardinality") != specification["cardinality"]
            or actual_enum_keys != expected_enum_keys
        ):
            raise ValueError(
                f"Existing matter field {specification['key']!r} is incompatible "
                "with the TREC Qrels importer"
            )
        return existing

    def run(
        self,
        source: Path,
        qrels: list[Qrel],
        skipped: Counter[int],
    ) -> QrelsImportReport:
        topics = sorted({row.topic for row in qrels} | set(skipped))
        report = QrelsImportReport(
            matter_id=self.matter_id,
            collection_id=self.collection_id,
            topics=topics,
            source_rows=len(qrels) + sum(skipped.values()),
            judged_rows=len(qrels),
            unassessed_rows_skipped=sum(skipped.values()),
        )
        definitions: dict[int, tuple[dict[str, object], dict[str, object]]] = {}
        for topic in topics:
            judgment_spec, probability_spec = _definition_specs(topic, source.name)
            definitions[topic] = (
                self._ensure_definition(judgment_spec),
                self._ensure_definition(probability_spec),
            )

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
            resolved = {
                item["source_item_id"]: item["collection_item_id"]
                for item in resolution["items"]
            }
            report.missing_collection_document_ids.extend(
                resolution["missing_source_item_ids"]
            )
            report.resolved_collection_items += len(resolved)
            for topic in sorted({row.topic for row in qrel_batch}):
                topic_rows = [
                    row
                    for row in qrel_batch
                    if row.topic == topic and row.document_id in resolved
                ]
                if not topic_rows:
                    continue
                judgment_definition, probability_definition = definitions[topic]
                judgment_result = self._import_values(
                    str(judgment_definition["id"]),
                    [
                        {
                            "collection_item_id": resolved[row.document_id],
                            "value": _JUDGMENT_VALUES[row.relevance],
                        }
                        for row in topic_rows
                    ],
                )
                probability_result = self._import_values(
                    str(probability_definition["id"]),
                    [
                        {
                            "collection_item_id": resolved[row.document_id],
                            "value": row.sampling_probability,
                        }
                        for row in topic_rows
                    ],
                )
                report.imported_judgment_events += judgment_result["created_event_count"]
                report.existing_judgment_events += judgment_result["existing_event_count"]
                report.imported_probability_events += probability_result["created_event_count"]
                report.existing_probability_events += probability_result["existing_event_count"]
                missing_item_ids = {
                    *judgment_result["missing_collection_item_ids"],
                    *probability_result["missing_collection_item_ids"],
                }
                by_item_id = {
                    resolved[row.document_id]: row.document_id for row in topic_rows
                }
                report.missing_matter_document_ids.extend(
                    by_item_id[item_id]
                    for item_id in missing_item_ids
                    if item_id in by_item_id
                )
                report.warnings.extend(judgment_result["warnings"])
                report.warnings.extend(probability_result["warnings"])
            self.progress(
                f"Processed {report.resolved_collection_items} Qrels collection items"
            )
        report.missing_collection_document_ids = sorted(
            set(report.missing_collection_document_ids)
        )
        report.missing_matter_document_ids = sorted(
            set(report.missing_matter_document_ids)
        )
        return report

    def _import_values(
        self,
        definition_id: str,
        items: list[dict[str, object]],
    ) -> dict[str, object]:
        return self.api.request(
            IMPORT_METADATA,
            path_params={"matter_id": self.matter_id},
            json_body={
                "metadata_definition_id": definition_id,
                "items": items,
            },
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Import TREC Legal Qrels as matter metadata without modifying the "
            "source collection."
        )
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--topic", type=int, action="append", required=True)
    parser.add_argument("--matter-id", required=True)
    parser.add_argument("--collection-id", required=True)
    parser.add_argument(
        "--base-url",
        default=os.getenv("PVR_IMPORT_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument(
        "--openapi",
        type=Path,
        default=REPOSITORY_ROOT / "web" / "openapi.json",
    )
    parser.add_argument("--email", default=os.getenv("PVR_IMPORT_EMAIL"))
    parser.add_argument("--password-env", default="PVR_IMPORT_PASSWORD")
    parser.add_argument("--access-token-env", default="PVR_IMPORT_ACCESS_TOKEN")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    topics = set(args.topic)
    try:
        qrels, skipped = load_qrels(args.source, topics)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "source": str(args.source),
                        "topics": sorted(topics),
                        "judged_rows": len(qrels),
                        "relevant_rows": sum(row.relevance == 1 for row in qrels),
                        "not_relevant_rows": sum(row.relevance == 0 for row in qrels),
                        "unassessed_rows_skipped": sum(skipped.values()),
                    },
                    indent=2,
                )
            )
            return 0
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
                progress=lambda message: print(message, file=sys.stderr),
            )
            report = importer.run(args.source, qrels, skipped)
            print(json.dumps(report.to_dict(), indent=2))
            return 1 if (
                report.missing_collection_document_ids
                or report.missing_matter_document_ids
                or report.warnings
            ) else 0
        finally:
            api.close()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Qrels import failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
