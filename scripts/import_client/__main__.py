from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections import Counter
from pathlib import Path

from scripts.import_client.adapters import Emc2Adapter, EnronCsvAdapter
from scripts.import_client.adapters.base import DatasetAdapter
from scripts.import_client.api import OpenApiClient
from scripts.import_client.importer import BaseImporter

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.import_client",
        description="Preprocess a supported local dataset and upload it through the OpenAPI-defined Core API.",
    )
    parser.add_argument("adapter", choices=("enron-csv", "emc2"))
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
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-attachments", action="store_true", help="Do not split named email attachments")
    return parser


def create_adapter(args: argparse.Namespace) -> DatasetAdapter:
    if args.adapter == "enron-csv":
        return EnronCsvAdapter(args.source)
    return Emc2Adapter(args.source)


def dry_run(adapter: DatasetAdapter, limit: int | None, *, include_attachments: bool) -> dict[str, object]:
    item_count = 0
    item_bytes = 0
    container_count = 0
    container_bytes = 0
    record_types: Counter[str] = Counter()
    custodians: set[str] = set()
    for container in adapter.source_containers():
        container_count += 1
        container_bytes += container.path.stat().st_size
        for item in BaseImporter.iter_preprocessed_items(
            adapter,
            container,
            include_attachments=include_attachments,
        ):
            if limit is not None and item_count >= limit:
                break
            item_count += 1
            item_bytes += len(item.content)
            record_types[item.record_type] += 1
            custodians.update(custodian.display_name for custodian in item.custodians)
        if limit is not None and item_count >= limit:
            break
    return {
        "dataset": adapter.dataset_name,
        "source_containers": container_count,
        "source_container_bytes": container_bytes,
        "items": item_count,
        "item_bytes": item_bytes,
        "record_types": dict(sorted(record_types.items())),
        "custodians": sorted(custodians),
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    try:
        adapter = create_adapter(args)
        if args.dry_run:
            print(json.dumps(dry_run(adapter, args.limit, include_attachments=not args.no_attachments), indent=2))
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
