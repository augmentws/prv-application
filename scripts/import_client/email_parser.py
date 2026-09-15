from __future__ import annotations

from datetime import timezone
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from scripts.import_client.models import EmailMetadata, EmailRecipient


def parse_message(content: bytes) -> Message:
    return BytesParser(policy=policy.default).parsebytes(content)


def _raw_header_values(message: Message, header: str) -> list[str]:
    expected = header.casefold()
    return [value for name, value in message.raw_items() if name.casefold() == expected]


def _decoded_header(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, TypeError, UnicodeError, ValueError):
        return value


def _first_header(message: Message, header: str) -> str | None:
    values = _raw_header_values(message, header)
    return _decoded_header(values[0]) if values else None


def _timestamp(message: Message, header: str):
    value = _first_header(message, header)
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def email_metadata(message: Message) -> EmailMetadata:
    recipients: list[EmailRecipient] = []
    for recipient_type, header in (("TO", "to"), ("CC", "cc"), ("BCC", "bcc")):
        values = [_decoded_header(value) for value in _raw_header_values(message, header)]
        for display_name, address in getaddresses(values):
            recipients.append(
                EmailRecipient(
                    recipient_type=recipient_type,
                    display_name=display_name or None,
                    email_address=address or None,
                )
            )
    return EmailMetadata(
        sender=_first_header(message, "from"),
        subject=_first_header(message, "subject"),
        sent_at=_timestamp(message, "date"),
        message_id=_first_header(message, "message-id"),
        recipients=tuple(recipients),
    )
