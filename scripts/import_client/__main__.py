from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections import Counter
from pathlib import Path

from scripts.import_client.adapters import (
    Athome4LoadfileAdapter,
    Emc2Adapter,
    EnronCsvAdapter,
    IndustryDocumentsLoadfileAdapter,
    JebBushInventoryAdapter,
)
from scripts.import_client.adapters.base import DatasetAdapter
from scripts.import_client.api import OpenApiClient
from scripts.import_client.importer import (
    SUPPORTED_RECORD_TYPES,
    BaseImporter,
    mime_type_matches,
    normalize_record_types,
    original_mime_type,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.import_client",
        description="Preprocess a supported local dataset and upload it through the OpenAPI-defined Core API.",
    )
    parser.add_argument(
        "adapter",
        choices=(
            "athome4-loadfile",
            "enron-csv",
            "emc2",
            "industrydocuments",
            "jeb-bush-inventory",
            "sf-walgreens-loadfile",
        ),
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--base-url", default=os.getenv("PVR_IMPORT_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--openapi", type=Path, default=REPOSITORY_ROOT / "web" / "openapi.json")
    parser.add_argument("--tenant-id", default=os.getenv("PVR_IMPORT_TENANT_ID"))
    parser.add_argument("--tenant-slug", default=os.getenv("PVR_IMPORT_TENANT_SLUG"))
    parser.add_argument("--client-id", default=os.getenv("PVR_IMPORT_CLIENT_ID"))
    parser.add_argument("--email", default=os.getenv("PVR_IMPORT_EMAIL"))
    parser.add_argument("--password-env", default="PVR_IMPORT_PASSWORD")
    parser.add_argument("--access-token-env", default="PVR_IMPORT_ACCESS_TOKEN")
    parser.add_argument("--collection-name")
    parser.add_argument("--collection-description")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Concurrent upload workers (default: 4)",
    )
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--include-record-type",
        action="append",
        default=[],
        type=str.upper,
        choices=SUPPORTED_RECORD_TYPES,
        metavar="TYPE",
        help=(
            "Load only this normalized record type. May be repeated; choices: "
            + ", ".join(SUPPORTED_RECORD_TYPES)
            + "."
        ),
    )
    parser.add_argument(
        "--exclude-mime-type",
        action="append",
        default=[],
        metavar="PATTERN",
        help=(
            "Exclude items by original document MIME type. May be repeated and "
            "supports shell wildcards such as 'image/*'."
        ),
    )
    parser.add_argument(
        "--store-source-containers",
        action="store_true",
        help=(
            "Store each original source CSV/MBOX/ZIP as an artifact. "
            "Disabled by default."
        ),
    )
    parser.add_argument(
        "--skip-malformed-records",
        action="store_true",
        help=(
            "Warn and skip an incomplete CSV record instead of stopping the "
            "Industry Documents import."
        ),
    )
    parser.add_argument(
        "--no-attachments",
        action="store_true",
        help="Do not split email attachments or load inventory text sidecars",
    )
    return parser


def create_adapter(args: argparse.Namespace) -> DatasetAdapter:
    if args.adapter == "athome4-loadfile":
        return Athome4LoadfileAdapter(args.source)
    if args.adapter == "enron-csv":
        return EnronCsvAdapter(args.source)
    if args.adapter == "emc2":
        return Emc2Adapter(args.source)
    if args.adapter in {"industrydocuments", "sf-walgreens-loadfile"}:
        return IndustryDocumentsLoadfileAdapter(
            args.source,
            skip_malformed_records=args.skip_malformed_records,
        )
    return JebBushInventoryAdapter(
        args.source,
        include_text_sidecars=not args.no_attachments,
    )


def dry_run(
    adapter: DatasetAdapter,
    limit: int | None,
    *,
    include_attachments: bool,
    include_record_types: tuple[str, ...] = (),
    exclude_mime_types: tuple[str, ...] = (),
    store_source_containers: bool = False,
) -> dict[str, object]:
    item_count = 0
    item_bytes = 0
    container_count = 0
    container_bytes = 0
    record_types: Counter[str] = Counter()
    excluded_by_record_type: Counter[str] = Counter()
    excluded_by_mime_type: Counter[str] = Counter()
    excluded_due_to_parent = 0
    excluded_count = 0
    custodians: set[str] = set()
    included_record_types = normalize_record_types(include_record_types)
    for container in adapter.source_containers():
        excluded_parent_ids: set[str] = set()
        container_count += 1
        container_bytes += container.path.stat().st_size
        for item in BaseImporter.iter_preprocessed_items(
            adapter,
            container,
            include_attachments=include_attachments,
        ):
            if limit is not None and item_count >= limit:
                break
            excluded_by_parent = item.parent_source_item_id in excluded_parent_ids
            excluded_by_record = (
                bool(included_record_types)
                and item.record_type not in included_record_types
            )
            excluded_by_mime = mime_type_matches(item, exclude_mime_types)
            if excluded_by_parent or excluded_by_record or excluded_by_mime:
                excluded_parent_ids.add(item.source_item_id)
                excluded_count += 1
                if excluded_by_parent:
                    excluded_due_to_parent += 1
                elif excluded_by_record:
                    excluded_by_record_type[item.record_type] += 1
                else:
                    excluded_by_mime_type[original_mime_type(item)] += 1
                continue
            item_count += 1
            item_bytes += len(item.content)
            record_types[item.record_type] += 1
            custodians.update(custodian.display_name for custodian in item.custodians)
        if limit is not None and item_count >= limit:
            break
    source_container_upload_bytes = container_bytes if store_source_containers else 0
    total_upload_bytes = item_bytes + source_container_upload_bytes
    return {
        "dataset": adapter.dataset_name,
        "source_containers": container_count,
        "source_container_bytes": container_bytes,
        "store_source_containers": store_source_containers,
        "source_container_upload_bytes": source_container_upload_bytes,
        "source_container_upload_gb": source_container_upload_bytes / 1_000_000_000,
        "items": item_count,
        "items_excluded": excluded_count,
        "excluded_by_record_type": dict(sorted(excluded_by_record_type.items())),
        "excluded_by_mime_type": dict(sorted(excluded_by_mime_type.items())),
        "items_excluded_due_to_parent": excluded_due_to_parent,
        "item_bytes": item_bytes,
        "item_gb": item_bytes / 1_000_000_000,
        "total_upload_bytes": total_upload_bytes,
        "total_upload_gb": total_upload_bytes / 1_000_000_000,
        "record_types": dict(sorted(record_types.items())),
        "custodians": sorted(custodians),
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    try:
        adapter = create_adapter(args)
        if args.dry_run:
            print(
                json.dumps(
                    dry_run(
                        adapter,
                        args.limit,
                        include_attachments=not args.no_attachments,
                        include_record_types=tuple(args.include_record_type),
                        exclude_mime_types=tuple(args.exclude_mime_type),
                        store_source_containers=args.store_source_containers,
                    ),
                    indent=2,
                )
            )
            return 0
        missing = [name for name in ("tenant_id", "tenant_slug", "client_id") if not getattr(args, name)]
        if missing:
            parser.error(f"the following options are required for upload: {', '.join('--' + name.replace('_', '-') for name in missing)}")
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
                    parser.error("--email is required unless an access token environment variable is set")
                password = os.getenv(args.password_env) or getpass.getpass("Priv-View password: ")
                api.login(args.email, password)
            importer = BaseImporter(
                api,
                tenant_id=args.tenant_id,
                tenant_slug=args.tenant_slug,
                client_id=args.client_id,
                collection_name=args.collection_name or adapter.default_collection_name,
                collection_description=args.collection_description,
                include_attachments=not args.no_attachments,
                include_record_types=args.include_record_type,
                exclude_mime_types=args.exclude_mime_type,
                store_source_containers=args.store_source_containers,
                workers=args.workers,
                progress=lambda message: print(message, file=sys.stderr),
            )
            report = importer.run(
                adapter,
                limit=args.limit,
                continue_on_error=args.continue_on_error,
            )
            print(json.dumps(report.to_dict(), indent=2))
            return 1 if report.failures else 0
        finally:
            api.close()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
