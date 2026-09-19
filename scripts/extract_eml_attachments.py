#!/usr/bin/env python3
"""Extract EML attachments and create a CSV inventory."""

from __future__ import annotations

import argparse
import csv
import hashlib
import mimetypes
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path

MARKER_NAME = ".eml-attachment-output"
EXCLUDED_FILENAMES = {"rtf-body.rtf"}
INVENTORY_FIELDS = [
    "record_type",
    "eml_path",
    "eml_size_bytes",
    "eml_sha256",
    "message_id",
    "date",
    "from",
    "to",
    "cc",
    "subject",
    "attachment_count",
    "attachment_index",
    "attachment_original_name",
    "attachment_saved_path",
    "attachment_content_type",
    "attachment_size_bytes",
    "attachment_sha256",
    "status",
    "error",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively extract attachments from EML files into sibling "
            "folders and create a CSV inventory. Generated rtf-body.rtf "
            "duplicates are ignored."
        )
    )
    parser.add_argument("directory", type=Path, help="Directory containing EML files")
    parser.add_argument(
        "--inventory",
        type=Path,
        help="Inventory CSV path (default: <directory>/eml_attachment_inventory.csv)",
    )
    return parser.parse_args()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def display_header(message: Message, name: str) -> str:
    value = message.get(name, "")
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def find_eml_files(root: Path) -> list[Path]:
    """Find input EMLs while excluding attachment folders made by this tool."""
    files: list[Path] = []
    for current_dir, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_dir)
        dirnames[:] = [
            name for name in dirnames if not (current / name / MARKER_NAME).is_file()
        ]
        for filename in filenames:
            if filename.lower().endswith(".eml"):
                files.append(current / filename)
    return sorted(files, key=lambda path: str(path).casefold())


def iter_attachments(message: Message) -> Iterator[Message]:
    """Yield attached MIME parts, including named inline parts."""
    for part in message.walk():
        if part is message:
            continue
        disposition = part.get_content_disposition()
        if disposition == "attachment" or part.get_filename() is not None:
            yield part


def attachment_basename(part: Message) -> str:
    filename = part.get_filename() or ""
    return filename.replace("\\", "/").split("/")[-1].strip()


def is_excluded_attachment(part: Message) -> bool:
    return attachment_basename(part).casefold() in EXCLUDED_FILENAMES


def attachment_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if payload is not None:
        return payload

    # message/rfc822 attachments commonly contain a nested Message rather than
    # a directly decodable byte payload.
    nested = part.get_payload()
    if isinstance(nested, list) and nested and isinstance(nested[0], Message):
        return nested[0].as_bytes(policy=policy.default)
    if isinstance(nested, str):
        return nested.encode(part.get_content_charset() or "utf-8", errors="replace")
    return b""


def sanitize_filename(filename: str | None, index: int, content_type: str) -> str:
    if filename:
        filename = filename.replace("\\", "/").split("/")[-1]
        filename = re.sub(r"[\x00-\x1f\x7f]", "_", filename)
        filename = re.sub(r'[:*?"<>|]', "_", filename).strip(" .")
    if not filename:
        extension = mimetypes.guess_extension(content_type, strict=False) or ".bin"
        filename = f"attachment_{index:03d}{extension}"
    if len(filename) > 180:
        suffix = Path(filename).suffix
        filename = f"{Path(filename).stem[: 180 - len(suffix)]}{suffix}"
    return filename or f"attachment_{index:03d}.bin"


def choose_destination(folder: Path, filename: str, data_hash: str) -> tuple[Path, str]:
    """Choose a non-destructive path, reusing an existing identical file."""
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = folder / filename
    number = 1
    while candidate.exists():
        if candidate.is_file():
            try:
                if sha256_bytes(candidate.read_bytes()) == data_hash:
                    return candidate, "reused"
            except OSError:
                pass
        number += 1
        candidate = folder / f"{stem}_{number}{suffix}"
    return candidate, "extracted"


def relative_text(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def base_row(eml: Path, root: Path, raw: bytes, message: Message) -> dict[str, object]:
    return {
        "eml_path": relative_text(eml, root),
        "eml_size_bytes": len(raw),
        "eml_sha256": sha256_bytes(raw),
        "message_id": display_header(message, "Message-ID"),
        "date": display_header(message, "Date"),
        "from": display_header(message, "From"),
        "to": display_header(message, "To"),
        "cc": display_header(message, "Cc"),
        "subject": display_header(message, "Subject"),
    }


def attachment_inventory_row(
    common: dict[str, object], part: Message, index: int, count: int
) -> tuple[dict[str, object], bytes, str]:
    data = attachment_bytes(part)
    data_hash = sha256_bytes(data)
    row = {
        **common,
        "record_type": "attachment",
        "attachment_count": count,
        "attachment_index": index,
        "attachment_original_name": part.get_filename() or "",
        "attachment_content_type": part.get_content_type(),
        "attachment_size_bytes": len(data),
        "attachment_sha256": data_hash,
    }
    return row, data, data_hash


def process_eml(eml: Path, root: Path) -> tuple[list[dict[str, object]], int]:
    try:
        raw = eml.read_bytes()
        message = BytesParser(policy=policy.default).parsebytes(raw)
        attachments = list(iter_attachments(message))
        common = base_row(eml, root, raw, message)
    except Exception as exc:  # noqa: BLE001 - one malformed message must not stop a corpus run
        return [
            {
                "record_type": "eml",
                "eml_path": relative_text(eml, root),
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        ], 0

    extractable = [part for part in attachments if not is_excluded_attachment(part)]
    rows: list[dict[str, object]] = [
        {
            **common,
            "record_type": "eml",
            "attachment_count": len(extractable),
            "status": "processed",
        }
    ]
    if not extractable:
        return rows, 0

    output_folder = eml.with_suffix("")
    try:
        output_folder.mkdir(parents=False, exist_ok=True)
        if not output_folder.is_dir():
            raise NotADirectoryError(f"Output path is not a directory: {output_folder}")
        (output_folder / MARKER_NAME).touch(exist_ok=True)
    except OSError as exc:
        rows[0]["status"] = "error"
        rows[0]["error"] = f"Could not create attachment folder: {exc}"
        return rows, 0

    extracted_count = 0
    for index, part in enumerate(extractable, start=1):
        try:
            row, data, data_hash = attachment_inventory_row(
                common, part, index, len(extractable)
            )
            safe_name = sanitize_filename(
                part.get_filename(), index, part.get_content_type()
            )
            destination, status = choose_destination(output_folder, safe_name, data_hash)
            if status == "extracted":
                destination.write_bytes(data)
            row.update(
                {
                    "attachment_saved_path": relative_text(destination, root),
                    "status": status,
                }
            )
            rows.append(row)
            extracted_count += 1
        except Exception as exc:  # noqa: BLE001 - record per-attachment failures in the inventory
            rows.append(
                {
                    **common,
                    "record_type": "attachment",
                    "attachment_count": len(extractable),
                    "attachment_index": index,
                    "attachment_original_name": part.get_filename() or "",
                    "attachment_content_type": part.get_content_type(),
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return rows, extracted_count


def write_inventory(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8-sig",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)
        writer = csv.DictWriter(
            temp_file, fieldnames=INVENTORY_FIELDS, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(path)


def main() -> int:
    args = parse_args()
    root = args.directory.expanduser().resolve()
    if not root.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        return 2

    inventory = (
        args.inventory.expanduser().resolve()
        if args.inventory
        else root / "eml_attachment_inventory.csv"
    )
    eml_files = find_eml_files(root)
    rows: list[dict[str, object]] = []
    extracted_count = 0
    for eml in eml_files:
        eml_rows, extracted = process_eml(eml, root)
        rows.extend(eml_rows)
        extracted_count += extracted

    try:
        write_inventory(inventory, rows)
    except OSError as exc:
        print(f"Error writing inventory: {exc}", file=sys.stderr)
        return 1

    errors = sum(1 for row in rows if row.get("status") == "error")
    print(f"EML files inventoried: {len(eml_files)}")
    print(f"Attachments extracted or reused: {extracted_count}")
    print(f"Errors: {errors}")
    print(f"Inventory: {inventory}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
