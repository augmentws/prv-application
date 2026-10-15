"""Code-enforced evidence for facts: a quote must be an exact substring of one cited paragraph.

Offsets are computed here from the document's current faithful text source, never taken from a caller or model,
and the source artifact and its content hash are recorded so a quote can later be checked against the exact text
it came from.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifact_gateway import get_preferred_text_source
from app.document_evidence import ParagraphMap, segment_paragraphs
from app.models import Matter, MatterDocument

MAX_QUOTE_CHARACTERS = 5_000
EvidenceStance = Literal["SUPPORTS", "CONTRADICTS"]


class FactEvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedEvidence:
    matter_document_id: uuid.UUID
    source_artifact_id: uuid.UUID
    source_content_hash: str
    paragraph_map_version: str
    paragraph_id: str
    char_start: int
    char_end: int
    quote: str
    quote_hash: str
    stance: EvidenceStance


def quote_hash(quote: str) -> str:
    return hashlib.sha256(quote.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DocumentText:
    """A document's current faithful text, its source artifact, and its paragraph map."""

    matter_document_id: uuid.UUID
    source_artifact_id: uuid.UUID
    source_content_hash: str
    text: str
    paragraph_map: ParagraphMap


def load_document_text(
    db: Session,
    *,
    matter: Matter,
    matter_document_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> DocumentText:
    document = db.scalar(
        select(MatterDocument).where(MatterDocument.id == matter_document_id, MatterDocument.matter_id == matter.id)
    )
    if document is None:
        raise FactEvidenceError("Document was not found in this matter")
    try:
        source = get_preferred_text_source(
            collection_item_id=document.collection_item_id,
            actor_user_id=actor_user_id,
            tenant_id=matter.client.tenant_id,
            client_id=matter.client_id,
        )
    except (ValueError, PermissionError) as exc:
        raise FactEvidenceError(f"Document text is unavailable: {exc}") from exc
    if source is None:
        raise FactEvidenceError("Document has no faithful text source")
    return DocumentText(
        matter_document_id=document.id,
        source_artifact_id=source.artifact_id,
        source_content_hash=source.content_hash,
        text=source.text,
        paragraph_map=segment_paragraphs(source.text),
    )


def verify_quote(
    document: DocumentText,
    *,
    paragraph_id: str,
    quote: str,
    occurrence: int = 1,
    stance: EvidenceStance = "SUPPORTS",
) -> VerifiedEvidence:
    """Locate ``quote`` (its ``occurrence``-th appearance) inside one paragraph of already-loaded text."""

    if not quote.strip():
        raise FactEvidenceError("A quote cannot be blank")
    if len(quote) > MAX_QUOTE_CHARACTERS:
        raise FactEvidenceError(f"A quote cannot exceed {MAX_QUOTE_CHARACTERS:,} characters")
    if occurrence < 1:
        raise FactEvidenceError("occurrence must be 1 or greater")
    paragraph = next(
        (item for item in document.paragraph_map.paragraphs if item.paragraph_id == paragraph_id),
        None,
    )
    if paragraph is None:
        raise FactEvidenceError(f"Paragraph {paragraph_id} does not exist in this document")
    position = -1
    for _ in range(occurrence):
        position = paragraph.text.find(quote, position + 1)
        if position < 0:
            break
    if position < 0:
        raise FactEvidenceError(
            f"The quote does not appear verbatim in paragraph {paragraph_id}"
            + ("" if occurrence == 1 else f" {occurrence} times")
        )
    char_start = paragraph.char_start + position
    char_end = char_start + len(quote)
    if document.text[char_start:char_end] != quote:
        raise FactEvidenceError("The quote could not be anchored to the document text")
    return VerifiedEvidence(
        matter_document_id=document.matter_document_id,
        source_artifact_id=document.source_artifact_id,
        source_content_hash=document.source_content_hash,
        paragraph_map_version=document.paragraph_map.version,
        paragraph_id=paragraph_id,
        char_start=char_start,
        char_end=char_end,
        quote=quote,
        quote_hash=quote_hash(quote),
        stance=stance,
    )


def verify_evidence(
    db: Session,
    *,
    matter: Matter,
    matter_document_id: uuid.UUID,
    paragraph_id: str,
    quote: str,
    occurrence: int = 1,
    stance: EvidenceStance = "SUPPORTS",
    actor_user_id: uuid.UUID,
) -> VerifiedEvidence:
    """Load the document's current text and verify one quote against it."""

    if not quote.strip():
        raise FactEvidenceError("A quote cannot be blank")
    if len(quote) > MAX_QUOTE_CHARACTERS:
        raise FactEvidenceError(f"A quote cannot exceed {MAX_QUOTE_CHARACTERS:,} characters")
    document = load_document_text(
        db, matter=matter, matter_document_id=matter_document_id, actor_user_id=actor_user_id
    )
    return verify_quote(document, paragraph_id=paragraph_id, quote=quote, occurrence=occurrence, stance=stance)
