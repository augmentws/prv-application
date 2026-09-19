#!/usr/bin/env python3
"""Prepare, submit, monitor, and resolve Voyage embedding batches."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.voyage_batch import (
    VoyageBatchClient,
    prepare_batch_input,
    resolve_batch_output,
)
from embedding_service.config import EmbeddingSettings


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare_batch_input(
                args.source,
                args.requests,
                args.manifest,
                max_inputs_per_request=args.max_inputs,
                force=args.force,
            )
        elif args.command == "resolve":
            result = resolve_batch_output(args.output, args.manifest, args.resolved, force=args.force)
        else:
            client = VoyageBatchClient(EmbeddingSettings())
            if args.command == "submit":
                result = client.submit(args.requests, metadata=_metadata(args.metadata))
            elif args.command == "status":
                result = client.get_batch(args.batch_id)
            elif args.command == "cancel":
                result = client.cancel_batch(args.batch_id)
            elif args.command == "wait":
                batch = client.wait_for_batch(
                    args.batch_id,
                    poll_seconds=args.poll_seconds,
                    timeout_seconds=args.timeout_seconds,
                    on_update=lambda update: print(
                        f"Voyage batch {args.batch_id}: {update['status']}", file=sys.stderr
                    ),
                )
                status = batch["status"]
                if status not in {"completed", "partially_completed"}:
                    raise RuntimeError(f"Voyage batch ended with status {status}")
                output_file_id = batch.get("output_file_id")
                if not isinstance(output_file_id, str) or not output_file_id:
                    raise RuntimeError("Completed Voyage batch has no output_file_id")
                byte_count = client.download_file(output_file_id, args.output, force=args.force)
                result = {"batch": batch, "output": str(args.output), "bytes": byte_count}
            else:  # pragma: no cover - argparse prevents this branch
                parser.error(f"unknown command: {args.command}")
                return 2
    except Exception as exc:  # noqa: BLE001 - CLI converts failures to a nonzero exit
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="group source {id,text} JSONL into Voyage batch requests")
    prepare.add_argument("source", type=Path)
    prepare.add_argument("requests", type=Path)
    prepare.add_argument("manifest", type=Path)
    prepare.add_argument("--max-inputs", type=int, default=256)
    prepare.add_argument("--force", action="store_true")

    submit = subparsers.add_parser("submit", help="upload request JSONL and create a batch")
    submit.add_argument("requests", type=Path)
    submit.add_argument(
        "--metadata",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="batch metadata; may be repeated",
    )

    status = subparsers.add_parser("status", help="show batch status")
    status.add_argument("batch_id")

    wait = subparsers.add_parser("wait", help="wait for a batch and download its output JSONL")
    wait.add_argument("batch_id")
    wait.add_argument("output", type=Path)
    wait.add_argument("--poll-seconds", type=float, default=15.0)
    wait.add_argument("--timeout-seconds", type=float, default=43_200.0)
    wait.add_argument("--force", action="store_true")

    cancel = subparsers.add_parser("cancel", help="cancel a running batch")
    cancel.add_argument("batch_id")

    resolve = subparsers.add_parser("resolve", help="join unordered batch output to source IDs")
    resolve.add_argument("output", type=Path)
    resolve.add_argument("manifest", type=Path)
    resolve.add_argument("resolved", type=Path)
    resolve.add_argument("--force", action="store_true")
    return parser


def _metadata(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key or not item:
            raise ValueError(f"metadata must be KEY=VALUE: {value}")
        result[key] = item
    return result


if __name__ == "__main__":
    raise SystemExit(main())
