from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

import regex as re
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from artifact_service.database import ArtifactSessionLocal
from artifact_service.derived import store_derived_artifact
from artifact_service.models import (
    Artifact,
    ClientCollection,
    CollectionItem,
    CollectionItemArtifact,
    CollectionTextProcessingRun,
    ContentBlob,
)
from artifact_service.schemas import TextProcessingChange, TextProcessingRule
from artifact_service.storage import BlobStorage, get_storage

PROCESSOR_VERSION = "collection-text-v7"
MAX_TEST_TEXT_CHARS = 100_000
MAX_PROCESSING_BYTES = 20 * 1024 * 1024
RULE_REGEX_TIMEOUT_SECONDS = 0.1

DEFAULT_RULES = [
    {
        "id": "system-lotus-forward-envelope",
        "name": "Lotus Notes forwarding envelope",
        "description": "Removes generated forwarding separators and their From, To, cc, bcc, Sent, and Date routing blocks while preserving the forwarded message body and a non-duplicated Subject title. Single-line separators are supported, and headerless calendar forwards lose only the separator.",
        "action": "UNWRAP_ENVELOPE",
        "match_description": "Lotus Notes separators containing 'Forwarded by'; supports single-line, split, whitespace-separated, and headerless calendar variants and retains Subject when the body does not repeat it.",
    },
    {
        "id": "system-reply-history",
        "name": "Quoted reply history",
        "description": "Removes quoted history beginning at a standard Original Message or Forwarded Message separator.",
        "action": "REMOVE_TRAILING_BLOCK",
        "match_description": "Dashed 'Original Message' or 'Forwarded Message' markers.",
    },
    {
        "id": "system-attachment-placeholder",
        "name": "Attachment placeholder",
        "description": "Removes standalone transport-generated attachment placeholder lines without removing ordinary attachment references in prose.",
        "action": "REMOVE_LINE",
        "match_description": "Standalone '- winmail.dat' and '- smime.p7s' lines.",
    },
    {
        "id": "system-whitespace",
        "name": "Whitespace normalization",
        "description": "Normalizes line endings, removes NUL characters and trailing spaces, and collapses runs of blank lines.",
        "action": "NORMALIZE",
        "match_description": "Applied to every supported text source.",
    },
]


@dataclass(frozen=True)
class SourceText:
    artifact_id: uuid.UUID
    role: str
    content_hash: str
    text: str


@dataclass(frozen=True)
class ProcessingResult:
    text: str
    changes: list[TextProcessingChange]
    warnings: list[str]


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in {"br", "div", "p", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in {"div", "p", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def configuration_hash(rules: list[TextProcessingRule]) -> str:
    payload = {
        "processor_version": PROCESSOR_VERSION,
        "rules": [rule.model_dump(mode="json") for rule in rules],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_rules(rules: list[TextProcessingRule]) -> None:
    for rule in rules:
        flags = 0 if rule.case_sensitive else re.IGNORECASE
        try:
            re.compile(rule.pattern, flags)
            if rule.end_pattern:
                re.compile(rule.end_pattern, flags)
        except re.error as exc:
            raise ValueError(f"Rule '{rule.name}' has an invalid regular expression: {exc}") from exc


def load_source_text(db: Session, storage: BlobStorage, item: CollectionItem) -> SourceText | None:
    role_order = case(
        (CollectionItemArtifact.artifact_role == "EXTRACTED_TEXT", 0),
        (CollectionItemArtifact.artifact_role == "OCR_TEXT", 1),
        (CollectionItemArtifact.artifact_role == "NATIVE", 2),
        else_=3,
    )
    rows = db.execute(
        select(CollectionItemArtifact.artifact_role, Artifact, ContentBlob)
        .join(Artifact, Artifact.id == CollectionItemArtifact.artifact_id)
        .join(ContentBlob, ContentBlob.id == Artifact.content_blob_id)
        .where(
            CollectionItemArtifact.collection_item_id == item.id,
            CollectionItemArtifact.artifact_role.in_(("EXTRACTED_TEXT", "OCR_TEXT", "NATIVE")),
            Artifact.status == "FINALIZED",
        )
        .order_by(role_order, Artifact.created_at.desc())
    ).all()
    for role, artifact, blob in rows:
        if role == "NATIVE" and not _native_can_supply_text(item, artifact):
            continue
        stream = storage.open(blob.bucket_name, blob.storage_key)
        try:
            content = stream.read(MAX_PROCESSING_BYTES + 1)
        finally:
            stream.close()
        if len(content) > MAX_PROCESSING_BYTES:
            raise ValueError("Source text exceeds the 20 MiB processing limit")
        text = _extract_text(content, item, role, artifact)
        if text and text.strip():
            return SourceText(artifact.id, role, artifact.content_hash, text)
    return None


def process_text(
    value: str,
    rules: list[TextProcessingRule],
    *,
    processor_version: str = PROCESSOR_VERSION,
) -> ProcessingResult:
    validate_rules(rules)
    if processor_version not in {
        "collection-text-v1",
        "collection-text-v2",
        "collection-text-v3",
        "collection-text-v4",
        "collection-text-v5",
        "collection-text-v6",
        "collection-text-v7",
    }:
        raise ValueError(f"Unsupported collection text processor version: {processor_version}")
    original_length = len(value)
    text = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    changes: list[TextProcessingChange] = []

    # Conservative system defaults: unwrap common mail-system envelopes, normalize
    # whitespace, and remove unmistakable attachment placeholders.
    before = text
    if processor_version == "collection-text-v7":
        text, forwarded_count = _remove_lotus_forwarding_envelopes_v7(text)
    elif processor_version == "collection-text-v6":
        text, forwarded_count = _remove_lotus_forwarding_envelopes_v6(text)
    elif processor_version == "collection-text-v5":
        text, forwarded_count = _remove_lotus_forwarding_envelopes_v5(text)
    elif processor_version == "collection-text-v4":
        text, forwarded_count = _remove_lotus_forwarding_envelopes_v4(text)
    elif processor_version == "collection-text-v3":
        text, forwarded_count = _remove_lotus_forwarding_envelopes_v3(text)
    elif processor_version == "collection-text-v2":
        text, forwarded_count = _remove_lotus_forwarding_envelopes_v2(text)
    else:
        forwarded_count = 0
    if forwarded_count:
        changes.append(
            TextProcessingChange(
                rule_id="system-lotus-forward-envelope",
                rule_name="Lotus Notes forwarding envelope",
                match_count=forwarded_count,
            )
        )
    text, reply_count = re.subn(
        r"(?ims)^\s*-{2,}\s*(?:original message|forwarded message)\s*-{2,}\s*$.*\Z",
        "",
        text,
    )
    if reply_count:
        changes.append(TextProcessingChange(rule_id="system-reply-history", rule_name="Quoted reply history", match_count=reply_count))
    if processor_version in {
        "collection-text-v2",
        "collection-text-v3",
        "collection-text-v4",
        "collection-text-v5",
        "collection-text-v6",
        "collection-text-v7",
    }:
        text, attachment_count = re.subn(
            r"(?im)^\s*-\s*(?:winmail\.dat|smime\.p7s)\s*$",
            "",
            text,
        )
    else:
        attachment_count = 0
    if attachment_count:
        changes.append(
            TextProcessingChange(
                rule_id="system-attachment-placeholder",
                rule_name="Attachment placeholder",
                match_count=attachment_count,
            )
        )
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if text != before and not (forwarded_count or reply_count or attachment_count):
        changes.append(TextProcessingChange(rule_id="system-whitespace", rule_name="Whitespace normalization", match_count=1))

    for rule in rules:
        if not rule.enabled:
            continue
        flags = 0 if rule.case_sensitive else re.IGNORECASE
        if rule.action == "REMOVE_LINE":
            pattern = re.compile(rule.pattern, flags)
            kept: list[str] = []
            count = 0
            for line in text.splitlines():
                if pattern.search(line, timeout=RULE_REGEX_TIMEOUT_SECONDS):
                    count += 1
                else:
                    kept.append(line)
            text = "\n".join(kept)
        elif rule.action == "REMOVE_BLOCK":
            start = re.compile(rule.pattern, flags)
            end = re.compile(rule.end_pattern or "", flags)
            kept = []
            count = 0
            removing = False
            for line in text.splitlines():
                if not removing and start.search(line, timeout=RULE_REGEX_TIMEOUT_SECONDS):
                    removing = True
                    count += 1
                    continue
                if removing:
                    if end.search(line, timeout=RULE_REGEX_TIMEOUT_SECONDS):
                        removing = False
                    continue
                kept.append(line)
            text = "\n".join(kept)
        else:
            text, count = re.subn(
                rule.pattern,
                rule.replacement,
                text,
                flags=flags,
                timeout=RULE_REGEX_TIMEOUT_SECONDS,
            )
        if count:
            changes.append(TextProcessingChange(rule_id=rule.id, rule_name=rule.name, match_count=count))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

    warnings: list[str] = []
    if original_length and len(text) < original_length * 0.5:
        warnings.append("More than half of the source text was removed; review this result before running the collection.")
    if not text:
        warnings.append("The processor produced empty text.")
    return ProcessingResult(text=text, changes=changes, warnings=warnings)


def _remove_lotus_forwarding_envelopes_v2(value: str) -> tuple[str, int]:
    """Remove Lotus Notes forwarding metadata while retaining the forwarded body."""
    lines = value.splitlines()
    output: list[str] = []
    index = 0
    count = 0
    marker = re.compile(r"-{5,}\s*Forwarded by\b", re.IGNORECASE)
    envelope_header = re.compile(r"^(?:to|cc|bcc|subject):", re.IGNORECASE)

    while index < len(lines):
        if not marker.search(lines[index], timeout=RULE_REGEX_TIMEOUT_SECONDS):
            output.append(lines[index])
            index += 1
            continue

        start = index
        index += 1
        # The separator commonly wraps after the date; consume through the line
        # containing the closing run of dashes.
        while index < len(lines) and not re.search(r"-{5,}\s*$", lines[index]):
            index += 1
        if index < len(lines):
            index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1

        # The sender/timestamp and "Please respond" lines precede the RFC-like
        # To/cc/Subject block. Require a Subject header before treating it as an
        # envelope so a prose occurrence of "Forwarded by" is left untouched.
        scan = index
        subject_index: int | None = None
        while scan < len(lines) and scan - index < 30:
            if re.match(r"^subject:", lines[scan].strip(), re.IGNORECASE):
                subject_index = scan
                break
            if scan > index and not lines[scan].strip() and not any(
                envelope_header.match(line.strip()) for line in lines[index:scan]
            ):
                break
            scan += 1
        if subject_index is None:
            output.extend(lines[start:index])
            continue

        index = subject_index + 1
        # Consume folded Subject continuations and the blank separator before body.
        while index < len(lines) and lines[index].strip():
            index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        count += 1

    return "\n".join(output), count


def _remove_lotus_forwarding_envelopes_v3(value: str) -> tuple[str, int]:
    """Remove full or minimal Lotus Notes forwarding metadata."""
    lines = value.splitlines()
    output: list[str] = []
    index = 0
    count = 0
    marker = re.compile(r"-{5,}\s*Forwarded by\b", re.IGNORECASE)
    envelope_header = re.compile(r"^(?:from|to|cc|bcc|subject|sent|date):", re.IGNORECASE)

    while index < len(lines):
        if not marker.search(lines[index], timeout=RULE_REGEX_TIMEOUT_SECONDS):
            output.append(lines[index])
            index += 1
            continue

        start = index
        index += 1
        while index < len(lines) and not re.search(r"-{5,}\s*$", lines[index]):
            index += 1
        if index < len(lines):
            index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1

        scan = index
        saw_header = False
        body_index: int | None = None
        while scan < len(lines) and scan - index < 30:
            stripped = lines[scan].strip()
            if envelope_header.match(stripped):
                saw_header = True
            if not stripped and saw_header:
                body_index = scan + 1
                while body_index < len(lines) and not lines[body_index].strip():
                    body_index += 1
                break
            scan += 1

        if body_index is None:
            output.extend(lines[start:index])
            continue
        index = body_index
        count += 1

    return "\n".join(output), count


def _remove_lotus_forwarding_envelopes_v4(value: str) -> tuple[str, int]:
    """Remove Lotus envelopes whose header groups are separated by whitespace."""
    lines = value.splitlines()
    output: list[str] = []
    index = 0
    count = 0
    marker = re.compile(r"-{5,}\s*Forwarded by\b", re.IGNORECASE)
    envelope_header = re.compile(r"^(?:from|to|cc|bcc|subject|sent|date):", re.IGNORECASE)

    while index < len(lines):
        if not marker.search(lines[index], timeout=RULE_REGEX_TIMEOUT_SECONDS):
            output.append(lines[index])
            index += 1
            continue

        start = index
        index += 1
        while index < len(lines) and not re.search(r"-{5,}\s*$", lines[index]):
            index += 1
        if index < len(lines):
            index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1

        scan = index
        saw_header = False
        body_index: int | None = None
        while scan < len(lines) and scan - index < 50:
            stripped = lines[scan].strip()
            if envelope_header.match(stripped):
                saw_header = True
            if not stripped and saw_header:
                next_nonblank = scan + 1
                while next_nonblank < len(lines) and not lines[next_nonblank].strip():
                    next_nonblank += 1
                if next_nonblank < len(lines) and envelope_header.match(lines[next_nonblank].strip()):
                    scan = next_nonblank
                    continue
                body_index = next_nonblank
                break
            scan += 1

        if body_index is None:
            output.extend(lines[start:index])
            continue
        index = body_index
        count += 1

    return "\n".join(output), count


def _remove_lotus_forwarding_envelopes_v5(
    value: str,
    *,
    remove_headerless_marker: bool = False,
    support_single_line_marker: bool = False,
) -> tuple[str, int]:
    """Remove Lotus envelopes while retaining a subject absent from the body."""
    lines = value.splitlines()
    output: list[str] = []
    index = 0
    count = 0
    marker = re.compile(r"-{5,}\s*Forwarded by\b", re.IGNORECASE)
    envelope_header = re.compile(r"^(from|to|cc|bcc|subject|sent|date):\s*(.*)$", re.IGNORECASE)

    def normalized(value: str) -> str:
        return " ".join(value.casefold().split())

    def body_repeats_subject(subject: str, body_index: int) -> bool:
        expected = normalized(subject)
        for line in lines[body_index : body_index + 12]:
            candidate = normalized(line)
            if candidate == expected:
                return True
            if (
                ":" in candidate
                and candidate.split(":", 1)[0] in {"subject", "topic"}
                and candidate.split(":", 1)[1].strip() == expected
            ):
                return True
        return False

    while index < len(lines):
        if not marker.search(lines[index], timeout=RULE_REGEX_TIMEOUT_SECONDS):
            output.append(lines[index])
            index += 1
            continue

        start = index
        marker_ends_on_same_line = bool(re.search(r"-{5,}\s*$", lines[index]))
        index += 1
        if not (support_single_line_marker and marker_ends_on_same_line):
            while index < len(lines) and not re.search(r"-{5,}\s*$", lines[index]):
                index += 1
            if index < len(lines):
                index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1

        scan = index
        saw_header = False
        subject_parts: list[str] = []
        collecting_subject = False
        body_index: int | None = None
        while scan < len(lines) and scan - index < 50:
            stripped = lines[scan].strip()
            header_match = envelope_header.match(stripped)
            if header_match:
                saw_header = True
                collecting_subject = header_match.group(1).casefold() == "subject"
                if collecting_subject and header_match.group(2).strip():
                    subject_parts = [header_match.group(2).strip()]
            elif stripped and collecting_subject:
                subject_parts.append(stripped)
            if not stripped and saw_header:
                collecting_subject = False
                next_nonblank = scan + 1
                while next_nonblank < len(lines) and not lines[next_nonblank].strip():
                    next_nonblank += 1
                if next_nonblank < len(lines) and envelope_header.match(lines[next_nonblank].strip()):
                    scan = next_nonblank
                    continue
                body_index = next_nonblank
                break
            scan += 1

        if body_index is None:
            if remove_headerless_marker:
                count += 1
                continue
            output.extend(lines[start:index])
            continue

        subject = " ".join(subject_parts).strip()
        last_output_line = next((line.strip() for line in reversed(output) if line.strip()), "")
        if subject and normalized(subject) != normalized(last_output_line) and not body_repeats_subject(subject, body_index):
            output.append(f"Subject: {subject}")
            output.append("")
        index = body_index
        count += 1

    return "\n".join(output), count


def _remove_lotus_forwarding_envelopes_v6(value: str) -> tuple[str, int]:
    """Also remove an unambiguous forwarding marker before calendar content."""
    return _remove_lotus_forwarding_envelopes_v5(value, remove_headerless_marker=True)


def _remove_lotus_forwarding_envelopes_v7(value: str) -> tuple[str, int]:
    """Also recognize a forwarding separator completed on its opening line."""
    return _remove_lotus_forwarding_envelopes_v5(
        value,
        remove_headerless_marker=True,
        support_single_line_marker=True,
    )


def process_run(run_id: uuid.UUID) -> None:
    storage = get_storage()
    with ArtifactSessionLocal() as db:
        run = db.get(CollectionTextProcessingRun, run_id)
        if run is None:
            raise ValueError("Collection text processing run not found")
        collection = db.get(ClientCollection, run.collection_id)
        if collection is None:
            raise ValueError("Collection not found")
        if run.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}:
            return
        rules = [TextProcessingRule.model_validate(value) for value in run.rules_snapshot]
        run.status = "RUNNING"
        run.started_at = run.started_at or utcnow()
        # A DBOS step may restart after a worker interruption. Recompute counters from
        # the full frozen collection scope while deterministic artifact keys prevent duplicates.
        run.processed_count = 0
        run.created_count = 0
        run.reused_count = 0
        run.skipped_count = 0
        run.failed_count = 0
        run.error_message = None
        run.total_count = db.scalar(
            select(func.count()).select_from(CollectionItem).where(CollectionItem.collection_id == collection.id)
        ) or 0
        db.commit()

        item_ids = list(
            db.scalars(
                select(CollectionItem.id)
                .where(CollectionItem.collection_id == collection.id)
                .order_by(CollectionItem.created_at, CollectionItem.id)
            )
        )

    for item_id in item_ids:
        with ArtifactSessionLocal() as db:
            run = db.get(CollectionTextProcessingRun, run_id)
            item = db.get(CollectionItem, item_id)
            if run is None or item is None:
                continue
            try:
                source = load_source_text(db, storage, item)
                if source is None:
                    run.skipped_count += 1
                else:
                    result = process_text(
                        source.text,
                        rules,
                        processor_version=run.processor_version,
                    )
                    derivation_key = hashlib.sha256(
                        f"{source.content_hash}:{run.configuration_hash}".encode()
                    ).hexdigest()
                    _, created = store_derived_artifact(
                        db,
                        storage,
                        item=item,
                        content=result.text.encode("utf-8"),
                        media_type="text/plain; charset=utf-8",
                        original_filename=f"{Path(item.original_filename).stem}.normalized.txt",
                        artifact_type="NORMALIZED_TEXT",
                        source_artifact_id=source.artifact_id,
                        relationship="NORMALIZED_FROM",
                        processing_run_id=run.id,
                        derivation_key=derivation_key,
                        artifact_metadata={
                            "processor_version": run.processor_version,
                            "configuration_hash": run.configuration_hash,
                            "source_role": source.role,
                            "changes": [change.model_dump(mode="json") for change in result.changes],
                            "warnings": result.warnings,
                        },
                        actor_user_id=run.requested_by_user_id,
                    )
                    if created:
                        run.created_count += 1
                    else:
                        run.reused_count += 1
            except Exception as exc:  # noqa: BLE001 - item failures must not abort the collection
                run.failed_count += 1
                run.error_message = str(exc)[:4000]
            run.processed_count += 1
            db.commit()

    with ArtifactSessionLocal() as db:
        run = db.get(CollectionTextProcessingRun, run_id)
        collection = db.get(ClientCollection, run.collection_id) if run else None
        if run is None or collection is None:
            return
        run.status = "COMPLETED_WITH_ERRORS" if run.failed_count else "COMPLETED"
        run.completed_at = utcnow()
        # Successful outputs become preferred; items without output continue to fall back to faithful source text.
        collection.active_text_processing_run_id = run.id
        db.commit()


def fail_run(run_id: uuid.UUID, message: str) -> None:
    with ArtifactSessionLocal() as db:
        run = db.get(CollectionTextProcessingRun, run_id)
        if run is None:
            return
        run.status = "FAILED"
        run.error_message = message[:4000]
        run.completed_at = utcnow()
        db.commit()


def _native_can_supply_text(item: CollectionItem, artifact: Artifact) -> bool:
    media_type = artifact.media_type.split(";", 1)[0].lower()
    return (
        item.record_type == "EMAIL"
        or media_type == "message/rfc822"
        or media_type.startswith("text/")
        or Path(artifact.original_filename).suffix.lower() in {".eml", ".txt", ".text", ".md", ".html", ".htm", ".csv", ".json", ".xml"}
    )


def _extract_text(content: bytes, item: CollectionItem, role: str, artifact: Artifact) -> str:
    media_type = artifact.media_type.split(";", 1)[0].lower()
    if role == "NATIVE" and (
        item.record_type == "EMAIL" or media_type == "message/rfc822" or artifact.original_filename.lower().endswith(".eml")
    ):
        message = BytesParser(policy=policy.default).parsebytes(content)
        body = message.get_body(preferencelist=("plain", "html"))
        return _message_part_text(body) if body is not None else ""
    charset = "utf-8"
    if "charset=" in artifact.media_type.lower():
        charset = artifact.media_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip()
    text = content.decode(charset, errors="replace")
    if media_type == "text/html" or artifact.original_filename.lower().endswith((".html", ".htm")):
        parser = _HTMLTextExtractor()
        parser.feed(text)
        parser.close()
        text = " ".join(parser.parts)
    return text


def _message_part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        text = raw if isinstance(raw, str) else ""
    else:
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        parser = _HTMLTextExtractor()
        parser.feed(text)
        parser.close()
        text = " ".join(parser.parts)
    return text
