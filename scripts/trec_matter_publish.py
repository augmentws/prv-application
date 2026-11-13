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


@dataclass(frozen=True)
class SampledIdentity:
    identity: CollectionIdentity
    score: int


def _source_sha256(source: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sampling_score(seed: str, source_item_id: str) -> int:
    digest = hashlib.sha256(seed.encode("utf-8") + b"\0" + source_item_id.encode("utf-8")).digest()
    return int.from_bytes(digest, "big")


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
            yield CollectionIdentity(
                item_id=str(item["item_id"]),
                source_item_id=str(item["source_item_id"]),
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
    # Negative scores turn heapq into a bounded max-heap. The root is the
    # current least-desirable (largest hash) member of the sample.
    uncoded_heap: list[tuple[int, str, CollectionIdentity]] = []
    collection_count = 0
    uncoded_population_count = 0
    for identity in identities:
        collection_count += 1
        key = identity.source_item_id.casefold()
        if key in coded_by_source_id:
            coded_items[key] = identity
        else:
            uncoded_population_count += 1
            if uncoded_count:
                score = _sampling_score(seed, identity.source_item_id)
                candidate = (-score, identity.source_item_id, identity)
                if len(uncoded_heap) < uncoded_count:
                    heapq.heappush(uncoded_heap, candidate)
                elif score < -uncoded_heap[0][0]:
                    heapq.heapreplace(uncoded_heap, candidate)
        if collection_count % 50_000 == 0:
            progress(f"Scanned {collection_count:,} collection identities")

    missing = sorted(row.document_id for key, row in coded_by_source_id.items() if key not in coded_items)
    if missing:
        preview = ", ".join(missing[:10])
        suffix = "" if len(missing) <= 10 else f" (and {len(missing) - 10:,} more)"
        raise ValueError(f"Collection is missing {len(missing):,} coded Topic {topic} documents: {preview}{suffix}")
    if uncoded_population_count < uncoded_count:
        raise ValueError(
            f"Collection has only {uncoded_population_count:,} uncoded Topic {topic} "
            f"documents; {uncoded_count:,} were requested"
        )

    coded = sorted(
        [(coded_items[key], row) for key, row in coded_by_source_id.items()],
        key=lambda pair: pair[0].source_item_id,
    )
    sampled = sorted(
        (
            SampledIdentity(identity=identity, score=-negative_score)
            for negative_score, _source_item_id, identity in uncoded_heap
        ),
        key=lambda item: (item.score, item.identity.source_item_id),
    )
    selected_count = len(coded) + len(sampled)
    if selected_count > MAX_EXPLICIT_SELECTION:
        raise ValueError(
            f"Publication selects {selected_count:,} documents, exceeding the "
            f"{MAX_EXPLICIT_SELECTION:,}-document explicit-selection limit"
        )

    items: list[dict[str, Any]] = [
        {
            "collection_item_id": identity.item_id,
            "source_item_id": identity.source_item_id,
            "partition": "coded",
            "judgment": row.relevance,
        }
        for identity, row in coded
    ]
    items.extend(
        {
            "collection_item_id": item.identity.item_id,
            "source_item_id": item.identity.source_item_id,
            "partition": "uncoded_random",
            "random_rank": rank,
            "random_score_sha256": f"{item.score:064x}",
        }
        for rank, item in enumerate(sampled, start=1)
    )
    return {
        "schema_version": 1,
        "topic": topic,
        "collection_id": collection_id,
        "matter_id": matter_id,
        "qrels": {
            "source": str(source),
            "sha256": _source_sha256(source),
            "coded_count": len(coded),
            "relevant_count": sum(row.relevance == 1 for row in qrels),
            "not_relevant_count": sum(row.relevance == 0 for row in qrels),
            "unassessed_rows": skipped[topic],
        },
        "sampling": {
            "algorithm": "sha256(seed + NUL + source_item_id), lowest hashes",
            "seed": seed,
            "collection_document_count": collection_count,
            "uncoded_population_count": uncoded_population_count,
            "uncoded_selected_count": len(sampled),
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
    uncoded_count = manifest["sampling"]["uncoded_selected_count"]
    return api.request(
        CREATE_DOCUMENT_IMPORT,
        path_params={"matter_id": manifest["matter_id"]},
        json_body={
            "source_collection_id": manifest["collection_id"],
            "selection": {
                "mode": "EXPLICIT",
                "item_ids": [item["collection_item_id"] for item in manifest["items"]],
            },
            "selection_summary": (
                f"TREC Topic {topic}: {counts['coded_count']:,} coded documents plus "
                f"{uncoded_count:,} deterministic uncoded documents; "
                f"manifest sha256={manifest_sha256}"
            ),
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Publish all coded documents for one TREC topic plus a deterministic "
            "random sample of uncoded collection documents into a matter."
        )
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--topic", type=int, default=304)
    parser.add_argument("--matter-id", required=True)
    parser.add_argument("--collection-id", required=True)
    parser.add_argument("--uncoded-count", type=int, default=10_000)
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
