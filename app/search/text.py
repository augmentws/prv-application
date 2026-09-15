from __future__ import annotations

from email import policy
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

DERIVED_TEXT_ROLES = ("EXTRACTED_TEXT", "OCR_TEXT")
SEARCH_TEXT_ROLES = (*DERIVED_TEXT_ROLES, "NATIVE")

_TEXT_EXTENSIONS = {
    ".csv",
    ".eml",
    ".htm",
    ".html",
    ".json",
    ".log",
    ".md",
    ".text",
    ".txt",
    ".xml",
}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in {"br", "div", "p", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in {"div", "p", "li", "tr"}:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self._parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self._parts).split())


def native_can_supply_search_text(*, media_type: str, filename: str, record_type: str) -> bool:
    normalized_media_type = media_type.split(";", 1)[0].strip().lower()
    return (
        record_type == "EMAIL"
        or normalized_media_type == "message/rfc822"
        or normalized_media_type.startswith("text/")
        or Path(filename).suffix.lower() in _TEXT_EXTENSIONS
    )


def extract_search_text(
    content: bytes,
    *,
    role: str,
    media_type: str,
    filename: str,
    record_type: str,
) -> str | None:
    if role == "NATIVE" and not native_can_supply_search_text(
        media_type=media_type,
        filename=filename,
        record_type=record_type,
    ):
        return None

    normalized_media_type = media_type.split(";", 1)[0].strip().lower()
    is_email = role == "NATIVE" and (
        record_type == "EMAIL" or normalized_media_type == "message/rfc822" or Path(filename).suffix.lower() == ".eml"
    )
    if is_email:
        return _email_body(content)

    text = _decode_text(content, media_type)
    if normalized_media_type == "text/html" or Path(filename).suffix.lower() in {".htm", ".html"}:
        text = _html_text(text)
    return _normalize(text) or None


def read_search_text_bytes(stream, max_bytes: int) -> bytes:
    try:
        return stream.read(max_bytes)
    finally:
        stream.close()


def _email_body(content: bytes) -> str | None:
    message = BytesParser(policy=policy.default).parsebytes(content)
    preferred = message.get_body(preferencelist=("plain", "html"))
    if preferred is not None:
        return _part_text(preferred)

    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() in {"text/plain", "text/html"}:
            value = _part_text(part)
            if value:
                return value
    return None


def _part_text(part: Message) -> str | None:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw_payload = part.get_payload()
        text = raw_payload if isinstance(raw_payload, str) else ""
    else:
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        text = _html_text(text)
    return _normalize(text) or None


def _decode_text(content: bytes, media_type: str) -> str:
    header = Message()
    header["content-type"] = media_type
    return content.decode(header.get_content_charset() or "utf-8", errors="replace")


def _html_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text()


def _normalize(value: str) -> str:
    return value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
