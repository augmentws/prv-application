"""Deterministic, citable document-record evidence supplied alongside source text."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.artifact_gateway import AnalysisInputItemSnapshot
from app.model_execution import content_hash


@dataclass(frozen=True)
class DocumentEvidenceContextItem:
    citation_id: str
    label: str
    value: str

    def as_model_input(self) -> dict[str, str]:
        return {
            "citation_id": self.citation_id,
            "label": self.label,
            "value": self.value,
        }


@dataclass(frozen=True)
class DocumentEvidenceContext:
    items: tuple[DocumentEvidenceContextItem, ...]
    context_hash: str

    def __post_init__(self) -> None:
        if len(self.citation_ids) != len(self.items):
            raise ValueError("Document evidence context citation IDs must be unique")
        if self.context_hash != content_hash(self.as_model_input()):
            raise ValueError("Document evidence context hash does not match its items")

    @property
    def citation_ids(self) -> frozenset[str]:
        return frozenset(item.citation_id for item in self.items)

    def as_model_input(self) -> list[dict[str, str]]:
        return [item.as_model_input() for item in self.items]


def _text(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    rendered = value.isoformat() if isinstance(value, datetime) else str(value).strip()
    return rendered or None


def build_document_evidence_context(snapshot: AnalysisInputItemSnapshot) -> DocumentEvidenceContext:
    """Render imported scalar/email metadata without placing its values in a plan artifact."""

    items: list[DocumentEvidenceContextItem] = []

    def add(citation_id: str, label: str, value: str | datetime | None) -> None:
        rendered = _text(value)
        if rendered is not None:
            items.append(DocumentEvidenceContextItem(citation_id, label, rendered))

    add("metadata:original_filename", "Document title / filename", snapshot.original_filename)
    add("metadata:email.sender", "Email sender", snapshot.email_sender)
    add("metadata:email.subject", "Email subject", snapshot.email_subject)
    for recipient_type in ("TO", "CC", "BCC"):
        for ordinal, value in enumerate(snapshot.email_recipients.get(recipient_type, ()), start=1):
            add(
                f"metadata:email.{recipient_type.lower()}:{ordinal}",
                f"Email {recipient_type} recipient {ordinal}",
                value,
            )
    payload = [item.as_model_input() for item in items]
    return DocumentEvidenceContext(items=tuple(items), context_hash=content_hash(payload))
