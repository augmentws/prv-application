"""Bulk, content-free item snapshots for freezing Core analysis input manifests.

Returns scalar item attributes and every finalized artifact that could supply the item's text, without reading
any content. Core applies its own text-source precedence to the candidates. Normalized text is offered only from
the collection's active normalized-text run, never from a running or superseded one.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from artifact_service.models import (
    Artifact,
    ClientCollection,
    CollectionItem,
    CollectionItemArtifact,
    CollectionItemEmail,
    CollectionItemEmailRecipient,
)
from artifact_service.schemas import (
    AnalysisInputEmail,
    CollectionItemAnalysisInputSnapshot,
    TextSourceCandidate,
)

TEXT_CANDIDATE_ROLES = ("NORMALIZED_TEXT", "EXTRACTED_TEXT", "OCR_TEXT", "NATIVE")


def _utc(value: datetime | None) -> datetime | None:
    # Stored timestamps are UTC; some databases return them without a zone.
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def collection_item_analysis_input_snapshots(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    item_ids: Sequence[uuid.UUID],
) -> list[CollectionItemAnalysisInputSnapshot]:
    """Snapshots in request order for the requested items owned by the tenant and client; others are omitted."""

    items = {
        item.id: item
        for item in db.scalars(
            select(CollectionItem).where(
                CollectionItem.id.in_(list(item_ids)),
                CollectionItem.tenant_id == tenant_id,
                CollectionItem.client_id == client_id,
            )
        )
    }
    if not items:
        return []
    active_runs = dict(
        db.execute(
            select(ClientCollection.id, ClientCollection.active_text_processing_run_id).where(
                ClientCollection.id.in_({item.collection_id for item in items.values()})
            )
        ).all()
    )
    candidates: dict[uuid.UUID, list[TextSourceCandidate]] = {item_id: [] for item_id in items}
    for item_id, role, processing_run_id, artifact in db.execute(
        select(
            CollectionItemArtifact.collection_item_id,
            CollectionItemArtifact.artifact_role,
            CollectionItemArtifact.processing_run_id,
            Artifact,
        )
        .join(Artifact, Artifact.id == CollectionItemArtifact.artifact_id)
        .where(
            CollectionItemArtifact.collection_item_id.in_(list(items)),
            CollectionItemArtifact.artifact_role.in_(TEXT_CANDIDATE_ROLES),
            Artifact.status == "FINALIZED",
        )
        .order_by(CollectionItemArtifact.collection_item_id, Artifact.created_at.desc(), Artifact.id)
    ):
        if role == "NORMALIZED_TEXT" and (
            processing_run_id is None or processing_run_id != active_runs.get(items[item_id].collection_id)
        ):
            continue
        candidates[item_id].append(
            TextSourceCandidate(
                artifact_id=artifact.id,
                role=role,
                media_type=artifact.media_type,
                original_filename=artifact.original_filename,
                content_hash=artifact.content_hash,
                byte_length=artifact.byte_length,
            )
        )
    emails = {
        email.collection_item_id: email
        for email in db.scalars(select(CollectionItemEmail).where(CollectionItemEmail.collection_item_id.in_(list(items))))
    }
    recipients: dict[uuid.UUID, dict[str, list[str]]] = {item_id: {"TO": [], "CC": [], "BCC": []} for item_id in emails}
    for recipient in db.scalars(
        select(CollectionItemEmailRecipient)
        .where(CollectionItemEmailRecipient.collection_item_id.in_(list(emails)))
        .order_by(
            CollectionItemEmailRecipient.collection_item_id,
            CollectionItemEmailRecipient.recipient_type,
            CollectionItemEmailRecipient.ordinal,
        )
    ):
        value = recipient.email_address or recipient.display_name
        if value:
            recipients[recipient.collection_item_id][recipient.recipient_type].append(value)
    snapshots = []
    for item_id in item_ids:
        item = items.get(item_id)
        if item is None:
            continue
        email = emails.get(item_id)
        snapshots.append(
            CollectionItemAnalysisInputSnapshot(
                item_id=item.id,
                family_id=item.family_id,
                record_type=item.record_type,
                original_filename=item.original_filename,
                file_date=_utc(item.file_date),
                email=(
                    AnalysisInputEmail(
                        sender=email.sender,
                        subject=email.subject,
                        sent_at=_utc(email.sent_at),
                        recipients=recipients[item_id],
                    )
                    if email is not None
                    else None
                ),
                text_candidates=candidates[item_id],
            )
        )
    return snapshots
