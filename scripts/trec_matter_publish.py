from __future__ import annotations

import argparse
import getpass
import hashlib
import heapq
import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.import_client.api import OpenApiClient
from scripts.trec_qrels_import import Qrel, load_qrels

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LIST_ITEM_IDENTITIES = "list_collection_item_identities_v1_collections__collection_id__item_identities_get"
CREATE_DOCUMENT_IMPORT = "create_document_import_v1_matters__matter_id__document_imports_post"
MAX_EXPLICIT_SELECTION = 25_000


@dataclass(frozen=True)
class CollectionIdentity:
    item_id: str
    source_item_id: str
    family_id: str | None = None


@dataclass(frozen=True)
class SampledFamily:
    key: str
    family_id: str | None
    members: tuple[CollectionIdentity, ...]
    score: int


def _source_sha256(source: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sampling_score(seed: str, family_key: str) -> int:
    digest = hashlib.sha256(seed.encode("utf-8") + b"\0" + family_key.encode("utf-8")).digest()
    return int.from_bytes(digest, "big")


def _family_key(identity: CollectionIdentity) -> str:
    return f"family:{identity.family_id}" if identity.family_id else f"item:{identity.item_id}"


def iter_collection_identities(
    api: OpenApiClient,
    collection_id: str,
) -> Iterator[CollectionIdentity]:
    cursor: str | None = None
    while True:
        page = api.request(
            LIST_ITEM_IDENTITIES,
            path_params={"collection_id": collection_id},
            params={"limit": 5000, "after_source_item_id": cursor},
        )
        items = page["items"]
        if not items:
            break
        for item in items:
            if "family_id" not in item:
                raise RuntimeError(
                    "Collection identity API does not expose family IDs; restart the updated backend before running"
                )
            yield CollectionIdentity(
                item_id=str(item["item_id"]),
                source_item_id=str(item["source_item_id"]),
                family_id=str(item["family_id"]) if item.get("family_id") else None,
            )
        next_cursor = page.get("next_after_source_item_id")
        if next_cursor is None:
            break
        if next_cursor == cursor:
            raise RuntimeError("Collection identity pagination did not advance")
        cursor = str(next_cursor)


def build_manifest(
    *,
    identities: Iterator[CollectionIdentity],
    qrels: list[Qrel],
    skipped: Counter[int],
    source: Path,
    topic: int,
    collection_id: str,
    matter_id: str,
    uncoded_count: int,
    seed: str,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    progress = progress or (lambda _message: None)
    coded_by_source_id = {row.document_id.casefold(): row for row in qrels}
    if len(coded_by_source_id) != len(qrels):
        raise ValueError("Coded Qrels document IDs are not unique")

    coded_items: dict[str, CollectionIdentity] = {}
    families: dict[str, list[CollectionIdentity]] = {}
    collection_count = 0
    for identity in identities:
        collection_count += 1
        families.setdefault(_family_key(identity), []).append(identity)
        key = identity.source_item_id.casefold()
        if key in coded_by_source_id:
            coded_items[key] = identity
        if collection_count % 50_000 == 0:
            progress(f"Scanned {collection_count:,} collection identities")

    missing = sorted(row.document_id for key, row in coded_by_source_id.items() if key not in coded_items)
    if missing:
        preview = ", ".join(missing[:10])
        suffix = "" if len(missing) <= 10 else f" (and {len(missing) - 10:,} more)"
        raise ValueError(f"Collection is missing {len(missing):,} coded Topic {topic} documents: {preview}{suffix}")
    coded_family_keys = {_family_key(identity) for identity in coded_items.values()}
    coded_expanded = sorted(
        (identity for family_key in coded_family_keys for identity in families[family_key]),
        key=lambda identity: identity.source_item_id,
    )
    uncoded_families = {
        family_key: tuple(sorted(members, key=lambda identity: identity.source_item_id))
        for family_key, members in families.items()
        if family_key not in coded_family_keys
    }
    uncoded_population_count = sum(len(members) for members in uncoded_families.values())
    if uncoded_population_count < uncoded_count:
        raise ValueError(
            f"Collection has only {uncoded_population_count:,} documents outside the coded "
            f"Topic {topic} families; approximately {uncoded_count:,} were requested"
        )

    # Negative scores turn heapq into a bounded max-heap. Keeping at most the
    # requested document count in families is sufficient because every family
    # contains at least one document.
    family_heap: list[tuple[int, str]] = []
    if uncoded_count:
        for family_key in uncoded_families:
            score = _sampling_score(seed, family_key)
            candidate = (-score, family_key)
            if len(family_heap) < uncoded_count:
                heapq.heappush(family_heap, candidate)
            elif score < -family_heap[0][0]:
                heapq.heapreplace(family_heap, candidate)
    ranked_families = sorted(
        (
            SampledFamily(
                key=family_key,
                family_id=members[0].family_id,
                members=members,
                score=-negative_score,
            )
            for negative_score, family_key in family_heap
            for members in [uncoded_families[family_key]]
        ),
        key=lambda family: (family.score, family.key),
    )
    sampled_families: list[SampledFamily] = []
    sampled_document_count = 0
    for family in ranked_families:
        if sampled_document_count >= uncoded_count:
            break
        sampled_families.append(family)
        sampled_document_count += len(family.members)

    selected_count = len(coded_expanded) + sampled_document_count
    if selected_count > MAX_EXPLICIT_SELECTION:
        raise ValueError(
            f"Publication selects {selected_count:,} documents, exceeding the "
            f"{MAX_EXPLICIT_SELECTION:,}-document explicit-selection limit"
        )

    items: list[dict[str, Any]] = []
    for identity in coded_expanded:
        judgment = coded_by_source_id.get(identity.source_item_id.casefold())
        family_members = families[_family_key(identity)]
        item: dict[str, Any] = {
            "collection_item_id": identity.item_id,
            "source_item_id": identity.source_item_id,
            "family_id": identity.family_id,
            "family_member_count": len(family_members),
            "partition": "coded" if judgment is not None else "coded_family_expansion",
        }
        if judgment is not None:
            item["judgment"] = judgment.relevance
        items.append(item)
    for family_rank, family in enumerate(sampled_families, start=1):
        for identity in family.members:
            items.append(
                {
                    "collection_item_id": identity.item_id,
                    "source_item_id": identity.source_item_id,
                    "family_id": identity.family_id,
                    "family_member_count": len(family.members),
                    "partition": "uncoded_random_family",
                    "random_family_rank": family_rank,
                    "random_family_score_sha256": f"{family.score:064x}",
                }
            )
    coded_seed_count = len(coded_items)
    coded_expanded_count = len(coded_expanded)
    return {
        "schema_version": 2,
        "topic": topic,
        "collection_id": collection_id,
        "matter_id": matter_id,
        "qrels": {
            "source": str(source),
            "sha256": _source_sha256(source),
            "coded_count": coded_seed_count,
            "relevant_count": sum(row.relevance == 1 for row in qrels),
            "not_relevant_count": sum(row.relevance == 0 for row in qrels),
            "unassessed_rows": skipped[topic],
        },
        "family_expansion": {
            "policy": "EXPAND_COMPLETE_FAMILIES_IN_COLLECTION",
            "coded_seed_document_count": coded_seed_count,
            "coded_family_count": len(coded_family_keys),
            "coded_added_family_document_count": coded_expanded_count - coded_seed_count,
            "coded_expanded_document_count": coded_expanded_count,
        },
        "sampling": {
            "algorithm": "sha256(seed + NUL + family_key), lowest family hashes until document target is met",
            "seed": seed,
            "collection_document_count": collection_count,
            "uncoded_population_count": uncoded_population_count,
            "eligible_uncoded_family_count": len(uncoded_families),
            "uncoded_target_document_count": uncoded_count,
            "uncoded_selected_family_count": len(sampled_families),
            "uncoded_selected_count": sampled_document_count,
            "uncoded_target_overshoot": sampled_document_count - uncoded_count,
        },
        "selected_count": selected_count,
        "items": items,
    }


def _manifest_bytes(manifest: dict[str, Any], *, pretty: bool) -> bytes:
    if pretty:
        return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8")


def write_manifest(path: Path, manifest: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_manifest_bytes(manifest, pretty=True))
    return hashlib.sha256(_manifest_bytes(manifest, pretty=False)).hexdigest()


def publish_manifest(
    api: OpenApiClient,
    manifest: dict[str, Any],
    manifest_sha256: str,
) -> dict[str, Any]:
    topic = manifest["topic"]
    counts = manifest["qrels"]
    coded_expanded_count = manifest.get("family_expansion", {}).get(
        "coded_expanded_document_count",
        counts["coded_count"],
    )
    uncoded_count = manifest["sampling"]["uncoded_selected_count"]
    return api.request(
        CREATE_DOCUMENT_IMPORT,
        path_params={"matter_id": manifest["matter_id"]},
        json_body={
            "source_collection_id": manifest["collection_id"],
            "selection": {
                "mode": "EXPLICIT",
                # The manifest already freezes complete families. Re-expansion
                # would make the published population differ from its hash.
                "include_complete_families": False,
                "item_ids": [item["collection_item_id"] for item in manifest["items"]],
            },
            "selection_summary": (
                f"TREC Topic {topic}: {counts['coded_count']:,} coded documents expanded to "
                f"{coded_expanded_count:,} family-complete documents plus {uncoded_count:,} "
                f"deterministic uncoded family documents; "
                f"manifest sha256={manifest_sha256}"
            ),
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Expand all coded documents for one TREC topic to complete families, then add "
            "a deterministic random sample of whole uncoded families into a matter."
        )
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--topic", type=int, default=304)
    parser.add_argument("--matter-id", required=True)
    parser.add_argument("--collection-id", required=True)
    parser.add_argument(
        "--uncoded-count",
        type=int,
        default=10_000,
        help="Approximate additional document target; whole families may make the result slightly larger.",
    )
    parser.add_argument("--seed", default="trec-topic-304-uncoded-v1")
    parser.add_argument("--manifest-out", type=Path, required=True)
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


def _authenticate(api: OpenApiClient, args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    access_token = os.getenv(args.access_token_env)
    password = os.getenv(args.password_env)
    if access_token:
        api.set_access_token(access_token)
        if args.email and password:
            api.set_reauthentication_credentials(args.email, password)
        return
    if not args.email:
        parser.error("--email is required unless an access token environment variable is set")
    password = password or getpass.getpass("Priv-View password: ")
    api.login(args.email, password)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.uncoded_count < 0:
        parser.error("--uncoded-count must be non-negative")
    try:
        qrels, skipped = load_qrels(args.source, {args.topic})
        api = OpenApiClient(args.base_url, args.openapi)
        try:
            _authenticate(api, args, parser)
            manifest = build_manifest(
                identities=iter_collection_identities(api, args.collection_id),
                qrels=qrels,
                skipped=skipped,
                source=args.source,
                topic=args.topic,
                collection_id=args.collection_id,
                matter_id=args.matter_id,
                uncoded_count=args.uncoded_count,
                seed=args.seed,
                progress=lambda message: print(message, file=sys.stderr),
            )
            manifest_sha256 = write_manifest(args.manifest_out, manifest)
            report: dict[str, Any] = {
                "manifest": str(args.manifest_out),
                "manifest_sha256": manifest_sha256,
                "topic": args.topic,
                "coded_count": manifest["qrels"]["coded_count"],
                "coded_expanded_count": manifest["family_expansion"]["coded_expanded_document_count"],
                "uncoded_count": manifest["sampling"]["uncoded_selected_count"],
                "selected_count": manifest["selected_count"],
                "dry_run": args.dry_run,
            }
            if not args.dry_run:
                report["document_import"] = publish_manifest(api, manifest, manifest_sha256)
            print(json.dumps(report, indent=2))
            return 0
        finally:
            api.close()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"TREC matter publication failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
