#!/usr/bin/env python3
"""Safely remove obsolete physical search indexes and generation records."""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.database import SessionLocal
from app.search.cleanup import (
    SearchIndexCleanupPlan,
    active_matter_ids,
    execute_search_index_cleanup,
    plan_search_index_cleanup,
)
from app.search.client import OpenSearchClient


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    settings = get_settings()
    client = OpenSearchClient(settings)
    failures = 0

    try:
        with SessionLocal() as db:
            matter_ids = args.matter_id or active_matter_ids(db)
            if not matter_ids:
                print("No matters with active search indexes were found.")
                return 0

            mode = "EXECUTE" if args.execute else "DRY RUN"
            print(f"Search index cleanup: {mode}")
            print("Active and alias-targeted indexes are always preserved.\n")

            for matter_id in matter_ids:
                try:
                    if args.execute:
                        plan = execute_search_index_cleanup(db, client, matter_id)
                    else:
                        plan = plan_search_index_cleanup(db, client, matter_id)
                    _print_plan(plan, executed=args.execute)
                except Exception as exc:  # noqa: BLE001 - continue reporting other matters
                    db.rollback()
                    failures += 1
                    print(f"ERROR matter_id={matter_id}: {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - CLI converts connection failures to a nonzero exit
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()

    if failures:
        print(f"Cleanup finished with {failures} matter error(s).", file=sys.stderr)
        return 1
    if not args.execute:
        print("Dry run only. Re-run with --execute to apply this exact cleanup policy.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--matter-id",
        action="append",
        type=uuid.UUID,
        help="clean only this matter UUID; may be repeated (default: all matters with an ACTIVE generation)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="delete planned OpenSearch indexes and prune obsolete database generation rows",
    )
    return parser


def _print_plan(plan: SearchIndexCleanupPlan, *, executed: bool) -> None:
    action = "deleted" if executed else "would delete"
    print(f"Matter: {plan.matter_name} ({plan.matter_id})")
    print(f"  keep: {plan.active_index}")
    print(f"  {action}: {len(plan.delete_indexes)} physical index(es)")
    for index_name in plan.delete_indexes:
        marker = " [orphan: no database record]" if index_name in plan.orphan_indexes else ""
        print(f"    - {index_name}{marker}")
    print(f"  {'pruned' if executed else 'would prune'}: {len(plan.stale_generation_ids)} database record(s)")
    if plan.missing_physical_indexes:
        print(
            "  database records whose physical indexes are already absent: "
            f"{len(plan.missing_physical_indexes)}"
        )
    print()


if __name__ == "__main__":
    raise SystemExit(main())
