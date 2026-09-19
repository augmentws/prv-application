#!/usr/bin/env python3
"""Create UTF-8 text sidecars for text-extractable files in an EML inventory.

By default, only attachment rows are processed. Each source file produces a
sidecar beside it named ``<original-filename>.txt`` (for example,
``brief.pdf.txt``). A separate CSV report records every attempted source.

The script uses Python's standard library for email, HTML, and plain-text
formats, and installed command-line tools for other formats:

* pdftotext (Poppler): PDF
* textutil (macOS): DOC, DOCX, RTF, ODT, and related word-processing files
* soffice (LibreOffice): XLS/XLSX, PPT/PPTX, WPS, WPD, and other legacy files
* tesseract: scanned-PDF OCR when --ocr is supplied
* Apache Tika App: optional fallback when --tika-jar is supplied
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar

DIRECT_TEXT_EXTENSIONS = {
    ".asc",
    ".asp",
    ".aspx",
    ".bat",
    ".cfg",
    ".cgi",
    ".cmd",
    ".conf",
    ".css",
    ".csv",
    ".ics",
    ".ini",
    ".js",
    ".json",
    ".log",
    ".md",
    ".php",
    ".phtml",
    ".pl",
    ".ps1",
    ".sig",
    ".shtml",
    ".sql",
    ".text",
    ".tsv",
    ".txt",
    ".url",
    ".vcf",
    ".xml",
    ".yaml",
    ".yml",
}
HTML_EXTENSIONS = {".htm", ".html", ".mht", ".mhtml"}
EMAIL_EXTENSIONS = {".eml"}
PDF_EXTENSIONS = {".pdf"}
TEXTUTIL_EXTENSIONS = {
    ".doc",
    ".docx",
    ".dot",
    ".dotx",
    ".odt",
    ".rtf",
    ".rtfd",
    ".webarchive",
    ".wordml",
}
SOFFICE_EXTENSIONS = {
    ".cdr",
    ".cwk",
    ".lwp",
    ".odp",
    ".ods",
    ".pps",
    ".ppsx",
    ".ppt",
    ".pptx",
    ".pub",
    ".sdw",
    ".sxw",
    ".wbk",
    ".wk1",
    ".wks",
    ".wp",
    ".wpd",
    ".wps",
    ".wri",
    ".xl",
    ".xls",
    ".xlsb",
    ".xlsm",
    ".xlsx",
}
IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jpe",
    ".jpeg",
    ".jpg",
    ".pcx",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
    ".xbm",
    ".xif",
}

REPORT_FIELDS = [
    "record_type",
    "parent_eml_path",
    "source_path",
    "source_extension",
    "source_content_type",
    "source_size_bytes",
    "source_sha256",
    "output_path",
    "output_size_bytes",
    "output_sha256",
    "extractor",
    "status",
    "elapsed_ms",
    "error",
]


@dataclass(frozen=True)
class Job:
    record_type: str
    parent_eml_path: str
    source_relative_path: str
    source_content_type: str
    inventory_size: str
    inventory_sha256: str


@dataclass(frozen=True)
class Settings:
    root: Path
    overwrite: bool
    ocr: bool
    ocr_language: str
    ocr_max_pages: int
    timeout: int
    tika_jar: Path | None


class VisibleHTMLParser(HTMLParser):
    """Conservatively retain visible HTML text and useful line boundaries."""

    BLOCK_TAGS: ClassVar[set[str]] = {
        "address",
        "article",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read an EML attachment inventory and create UTF-8 .txt sidecars "
            "for text-extractable attachments."
        )
    )
    parser.add_argument("inventory", type=Path, help="Inventory CSV to process")
    parser.add_argument(
        "--root",
        type=Path,
        help="Root for inventory-relative paths (default: inventory directory)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Extraction report path (default: <inventory-stem>_text_extraction.csv)",
    )
    parser.add_argument(
        "--include-eml",
        action="store_true",
        help="Also produce text sidecars for EML message bodies",
    )
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="OCR PDFs that contain no extractable text (slow)",
    )
    parser.add_argument(
        "--ocr-language",
        default="eng",
        help="Tesseract language(s), such as eng or eng+spa (default: eng)",
    )
    parser.add_argument(
        "--ocr-max-pages",
        type=int,
        default=100,
        help="Maximum pages to OCR per scanned PDF (default: 100)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="Concurrent extraction workers (default: up to 4)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Seconds allowed for each external extraction step (default: 180)",
    )
    parser.add_argument(
        "--tika-jar",
        type=Path,
        help="Optional Apache Tika App JAR used as a fallback extractor",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing .txt sidecars instead of reporting them as existing",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Process at most this many eligible inventory rows (for testing)",
    )
    return parser.parse_args()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decode_bytes(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return data.decode("utf-32", errors="replace")
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def normalize_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def html_to_text(text: str) -> str:
    parser = VisibleHTMLParser()
    parser.feed(text)
    parser.close()
    return parser.text()


def message_part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        text = raw if isinstance(raw, str) else ""
    else:
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        return html_to_text(text)
    return text


def extract_email(source: Path) -> str:
    message = BytesParser(policy=policy.default).parsebytes(source.read_bytes())
    header_lines = []
    for name in ("From", "To", "Cc", "Bcc", "Date", "Subject", "Message-ID"):
        value = message.get(name)
        if value:
            header_lines.append(f"{name}: {str(value).strip()}")

    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None:
        body_text = message_part_text(body)
    else:
        body_text = ""
        for part in message.walk():
            if part.is_multipart() or part.get_content_disposition() == "attachment":
                continue
            if part.get_content_type() in {"text/plain", "text/html"}:
                body_text = message_part_text(part)
                if body_text:
                    break
    return "\n".join(header_lines) + "\n\n" + body_text


def run_command(command: list[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def command_text(result: subprocess.CompletedProcess[bytes], tool: str) -> str:
    text = decode_bytes(result.stdout)
    if result.returncode and not normalize_text(text):
        error = decode_bytes(result.stderr).strip()
        raise RuntimeError(f"{tool} exited {result.returncode}: {error[:1000]}")
    return text


def extract_pdf(source: Path, settings: Settings) -> tuple[str, str]:
    result = run_command(
        ["pdftotext", "-layout", "-enc", "UTF-8", str(source), "-"],
        settings.timeout,
    )
    text = command_text(result, "pdftotext")
    if normalize_text(text) or not settings.ocr:
        return text, "pdftotext"
    return ocr_pdf(source, settings), "pdftotext+tesseract"


def ocr_image(source: Path, settings: Settings) -> str:
    result = run_command(
        [
            "tesseract",
            str(source),
            "stdout",
            "-l",
            settings.ocr_language,
            "--psm",
            "6",
        ],
        settings.timeout,
    )
    return command_text(result, "tesseract")


def ocr_pdf(source: Path, settings: Settings) -> str:
    with tempfile.TemporaryDirectory(prefix="eml-pdf-ocr-") as temp_dir:
        prefix = Path(temp_dir) / "page"
        render = run_command(
            [
                "pdftoppm",
                "-png",
                "-r",
                "300",
                "-f",
                "1",
                "-l",
                str(settings.ocr_max_pages),
                str(source),
                str(prefix),
            ],
            settings.timeout,
        )
        if render.returncode:
            raise RuntimeError(
                f"pdftoppm exited {render.returncode}: "
                f"{decode_bytes(render.stderr).strip()[:1000]}"
            )
        pages = sorted(Path(temp_dir).glob("page-*.png"))
        if not pages:
            raise RuntimeError("pdftoppm rendered no pages")
        return "\n\n".join(ocr_image(page, settings) for page in pages)


def extract_textutil(source: Path, settings: Settings) -> str:
    result = run_command(
        ["textutil", "-convert", "txt", "-stdout", "--", str(source)],
        settings.timeout,
    )
    return command_text(result, "textutil")


def extract_soffice(source: Path, settings: Settings) -> tuple[str, str]:
    with tempfile.TemporaryDirectory(prefix="eml-soffice-") as temp_dir:
        temp_root = Path(temp_dir)
        output_dir = temp_root / "output"
        profile_dir = temp_root / "profile"
        output_dir.mkdir()
        profile_dir.mkdir()
        result = run_command(
            [
                "soffice",
                "--headless",
                f"-env:UserInstallation={profile_dir.as_uri()}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(output_dir),
                str(source),
            ],
            settings.timeout,
        )
        pdfs = list(output_dir.glob("*.pdf"))
        if result.returncode or not pdfs:
            error = decode_bytes(result.stderr).strip()
            raise RuntimeError(
                f"LibreOffice conversion failed ({result.returncode}): {error[:1000]}"
            )
        pdf_result = run_command(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(pdfs[0]), "-"],
            settings.timeout,
        )
        text = command_text(pdf_result, "pdftotext")
        if normalize_text(text) or not settings.ocr:
            return text, "soffice+pdftotext"
        return ocr_pdf(pdfs[0], settings), "soffice+tesseract"


def extract_tika(source: Path, settings: Settings) -> str:
    assert settings.tika_jar is not None
    result = run_command(
        ["java", "-jar", str(settings.tika_jar), "-t", str(source)],
        settings.timeout,
    )
    return command_text(result, "Apache Tika")


def select_extractor(job: Job, source: Path, settings: Settings) -> str | None:
    extension = source.suffix.casefold()
    media_type = job.source_content_type.split(";", 1)[0].strip().casefold()
    if extension in PDF_EXTENSIONS:
        return "pdf"
    # Standalone image attachments are intentionally skipped. Tesseract is used
    # only for page images rendered from otherwise text-empty PDFs.
    if extension in IMAGE_EXTENSIONS:
        return "skip_image"
    if media_type == "application/pdf":
        return "pdf"
    if media_type.startswith("image/"):
        return "skip_image"
    if extension in EMAIL_EXTENSIONS or media_type == "message/rfc822":
        return "email"
    if extension in HTML_EXTENSIONS or media_type == "text/html":
        return "html"
    if extension in DIRECT_TEXT_EXTENSIONS or media_type.startswith("text/"):
        return "text"
    if extension in TEXTUTIL_EXTENSIONS:
        return "textutil"
    if extension in SOFFICE_EXTENSIONS:
        return "soffice"
    return "tika" if settings.tika_jar is not None else None


def extract_source(job: Job, source: Path, settings: Settings) -> tuple[str, str]:
    extractor = select_extractor(job, source, settings)
    if extractor is None:
        raise LookupError("No enabled extractor for this file type")
    if extractor == "skip_image":
        raise LookupError("Standalone image attachments are skipped")
    if extractor == "email":
        return extract_email(source), extractor
    if extractor == "html":
        return html_to_text(decode_bytes(source.read_bytes())), extractor
    if extractor == "text":
        return decode_bytes(source.read_bytes()), extractor
    if extractor == "pdf":
        return extract_pdf(source, settings)
    if extractor == "textutil":
        try:
            text = extract_textutil(source, settings)
            if normalize_text(text):
                return text, extractor
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass
        # LibreOffice is a useful fallback for word-processing formats that
        # textutil recognizes incompletely.
        text, office_extractor = extract_soffice(source, settings)
        return text, f"{office_extractor}-fallback"
    if extractor == "soffice":
        return extract_soffice(source, settings)
    return extract_tika(source, settings), extractor


def safe_source_path(root: Path, relative_path: str) -> Path:
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Inventory path escapes the selected root") from exc
    return candidate


def relative_text(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def write_text_atomic(path: Path, text: str) -> bytes:
    data = (text + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)
        temp_file.write(data)
    temp_path.replace(path)
    return data


def base_result(job: Job, source: Path, settings: Settings) -> dict[str, object]:
    return {
        "record_type": job.record_type,
        "parent_eml_path": job.parent_eml_path,
        "source_path": relative_text(source, settings.root),
        "source_extension": source.suffix.casefold(),
        "source_content_type": job.source_content_type,
        "source_size_bytes": job.inventory_size,
        "source_sha256": job.inventory_sha256,
    }


def process_job(job: Job, settings: Settings) -> dict[str, object]:
    started = time.monotonic()
    try:
        source = safe_source_path(settings.root, job.source_relative_path)
    except ValueError as exc:
        return {
            "record_type": job.record_type,
            "parent_eml_path": job.parent_eml_path,
            "source_path": job.source_relative_path,
            "status": "invalid_path",
            "error": str(exc),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }

    result = base_result(job, source, settings)
    output = Path(f"{source}.txt")
    if not source.is_file():
        result.update(status="missing", error="Source file does not exist")
    else:
        extractor = select_extractor(job, source, settings)
        if extractor == "skip_image":
            result.update(
                status="skipped_image",
                error="Standalone image attachments are intentionally skipped",
            )
        elif extractor is None:
            result.update(
                status="unsupported",
                error="No enabled extractor for this extension/content type",
            )
        elif output.exists() and not settings.overwrite:
            try:
                result.update(
                    status="existing",
                    extractor="existing",
                    output_path=relative_text(output, settings.root),
                    output_size_bytes=output.stat().st_size,
                    output_sha256=sha256_file(output),
                )
            except OSError as exc:
                result.update(
                    status="error", error=f"Could not read existing output: {exc}"
                )
        else:
            try:
                text, used_extractor = extract_source(job, source, settings)
                text = normalize_text(text)
                result["extractor"] = used_extractor
                if not text:
                    result.update(status="no_text", error="Extractor returned no text")
                else:
                    data = write_text_atomic(output, text)
                    result.update(
                        status="extracted",
                        output_path=relative_text(output, settings.root),
                        output_size_bytes=len(data),
                        output_sha256=sha256_bytes(data),
                    )
            except subprocess.TimeoutExpired:
                result.update(
                    status="error",
                    extractor=extractor,
                    error=f"Extraction exceeded {settings.timeout} seconds",
                )
            except Exception as exc:  # noqa: BLE001 - isolate heterogeneous file failures
                if settings.tika_jar is not None and extractor != "tika":
                    try:
                        text = normalize_text(extract_tika(source, settings))
                        result["extractor"] = "tika-fallback"
                        if text:
                            data = write_text_atomic(output, text)
                            result.update(
                                status="extracted",
                                output_path=relative_text(output, settings.root),
                                output_size_bytes=len(data),
                                output_sha256=sha256_bytes(data),
                            )
                        else:
                            result.update(status="no_text", error="Tika returned no text")
                    except Exception as tika_exc:  # noqa: BLE001 - report fallback failure
                        result.update(
                            status="error",
                            extractor=extractor,
                            error=(
                                f"{type(exc).__name__}: {exc}; Tika fallback: "
                                f"{type(tika_exc).__name__}: {tika_exc}"
                            )[:4000],
                        )
                else:
                    result.update(
                        status="error",
                        extractor=extractor,
                        error=f"{type(exc).__name__}: {exc}"[:4000],
                    )
    result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return result


@contextmanager
def allow_large_csv_fields() -> Iterator[None]:
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(sys.maxsize)
    try:
        yield
    finally:
        csv.field_size_limit(previous_limit)


def iter_jobs(
    inventory: Path, include_eml: bool, limit: int | None
) -> Iterator[Job]:
    yielded = 0
    with allow_large_csv_fields(), inventory.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"record_type", "eml_path", "attachment_saved_path"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Inventory is missing columns: {', '.join(sorted(missing))}")

        for row in reader:
            record_type = (row.get("record_type") or "").strip().casefold()
            if record_type == "attachment":
                source_path = (row.get("attachment_saved_path") or "").strip()
                if not source_path:
                    continue
                job = Job(
                    record_type="attachment",
                    parent_eml_path=(row.get("eml_path") or "").strip(),
                    source_relative_path=source_path,
                    source_content_type=(
                        row.get("attachment_content_type") or ""
                    ).strip(),
                    inventory_size=(row.get("attachment_size_bytes") or "").strip(),
                    inventory_sha256=(row.get("attachment_sha256") or "").strip(),
                )
            elif record_type == "eml" and include_eml:
                source_path = (row.get("eml_path") or "").strip()
                if not source_path:
                    continue
                job = Job(
                    record_type="eml",
                    parent_eml_path=source_path,
                    source_relative_path=source_path,
                    source_content_type="message/rfc822",
                    inventory_size=(row.get("eml_size_bytes") or "").strip(),
                    inventory_sha256=(row.get("eml_sha256") or "").strip(),
                )
            else:
                continue

            yield job
            yielded += 1
            if limit is not None and yielded >= limit:
                return


def completed_results(
    jobs: Iterable[Job], settings: Settings, workers: int
) -> Iterator[dict[str, object]]:
    if workers == 1:
        for job in jobs:
            yield process_job(job, settings)
        return

    max_pending = workers * 2
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending: set[Future[dict[str, object]]] = set()
        for job in jobs:
            pending.add(executor.submit(process_job, job, settings))
            if len(pending) >= max_pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    yield future.result()
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                yield future.result()


def validate_args(args: argparse.Namespace) -> tuple[Path, Path, Path, Settings]:
    inventory = args.inventory.expanduser().resolve()
    if not inventory.is_file():
        raise ValueError(f"Inventory does not exist: {inventory}")
    root = (args.root or inventory.parent).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Root is not a directory: {root}")
    report = (
        args.report.expanduser().resolve()
        if args.report
        else inventory.with_name(f"{inventory.stem}_text_extraction.csv")
    )
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")
    if args.timeout < 1:
        raise ValueError("--timeout must be at least 1")
    if args.ocr_max_pages < 1:
        raise ValueError("--ocr-max-pages must be at least 1")
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be at least 1")
    tika_jar = args.tika_jar.expanduser().resolve() if args.tika_jar else None
    if tika_jar is not None and not tika_jar.is_file():
        raise ValueError(f"Tika JAR does not exist: {tika_jar}")
    settings = Settings(
        root=root,
        overwrite=args.overwrite,
        ocr=args.ocr,
        ocr_language=args.ocr_language,
        ocr_max_pages=args.ocr_max_pages,
        timeout=args.timeout,
        tika_jar=tika_jar,
    )
    return inventory, root, report, settings


def missing_tools(settings: Settings) -> list[str]:
    required = {"pdftotext", "textutil", "soffice"}
    if settings.ocr:
        required.update({"pdftoppm", "tesseract"})
    if settings.tika_jar is not None:
        required.add("java")
    return sorted(tool for tool in required if shutil.which(tool) is None)


def main() -> int:
    args = parse_args()
    try:
        inventory, root, report, settings = validate_args(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    unavailable = missing_tools(settings)
    if unavailable:
        print(
            "Warning: unavailable extractors: " + ", ".join(unavailable),
            file=sys.stderr,
        )

    report.parent.mkdir(parents=True, exist_ok=True)
    partial_report = report.with_name(f".{report.name}.partial")
    counts: dict[str, int] = {}
    processed = 0
    started = time.monotonic()
    try:
        jobs = iter_jobs(inventory, args.include_eml, args.limit)
        with partial_report.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=REPORT_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for result in completed_results(jobs, settings, args.workers):
                writer.writerow(result)
                processed += 1
                status = str(result.get("status") or "unknown")
                counts[status] = counts.get(status, 0) + 1
                if processed % 1000 == 0:
                    output.flush()
                    print(f"Processed {processed:,} files...", file=sys.stderr)
        partial_report.replace(report)
    except KeyboardInterrupt:
        print(f"Interrupted. Partial report retained at: {partial_report}", file=sys.stderr)
        return 130
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    print(f"Inventory: {inventory}")
    print(f"Root: {root}")
    print(f"Files processed: {processed:,}")
    for status, count in sorted(counts.items()):
        print(f"{status}: {count:,}")
    print(f"Elapsed seconds: {elapsed:.1f}")
    print(f"Report: {report}")
    return 1 if counts.get("error", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
