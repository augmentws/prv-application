from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.document_metadata import value_columns
from app.models import (
    MetadataDefinition,
    ReviewBatchReviewerCoding,
    ReviewBatchReviewerDocument,
    ReviewBatchReviewerDocumentRevision,
    ReviewBatchReviewerFieldRevision,
    ReviewBatchReviewerRevision,
    ReviewBatchReviewerValue,
    ReviewBatchReviewerValueRevision,
    utcnow,
)

ReviewerCodingOrigin = Literal["REVIEW_UI", "IMPORT", "SYSTEM_MIGRATION"]


@dataclass(frozen=True)
class ReviewerFieldReplacement:
    definition: MetadataDefinition
    values: tuple[Any, ...]


@dataclass(frozen=True)
class ReviewerCodingMutationResult:
    revision: int
    created: bool


@dataclass(frozen=True)
class ReviewerCodingAsOf:
    documents: tuple[ReviewBatchReviewerDocumentRevision, ...]
    fields: tuple[ReviewBatchReviewerFieldRevision, ...]
    values: tuple[ReviewBatchReviewerValueRevision, ...]


def append_reviewer_coding_revision(
    db: Session,
    *,
    coding: ReviewBatchReviewerCoding,
    matter_document_id: uuid.UUID,
    status: Literal["IN_PROGRESS", "COMPLETED", "SKIPPED"],
    field_replacements: Sequence[ReviewerFieldReplacement],
    actor_user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    matter_id: uuid.UUID,
    origin: ReviewerCodingOrigin = "REVIEW_UI",
    request_key: str | None = None,
) -> ReviewerCodingMutationResult:
    """Append one immutable reviewer change and update its current projection.

    The caller must load ``coding`` with ``SELECT ... FOR UPDATE``. This function
    deliberately does not commit so the route can include all surrounding work in
    the same transaction.
    """

    if request_key:
        existing_revision = db.scalar(
            select(ReviewBatchReviewerRevision.revision).where(
                ReviewBatchReviewerRevision.reviewer_coding_id == coding.id,
                ReviewBatchReviewerRevision.request_key == request_key,
            )
        )
        if existing_revision is not None:
            return ReviewerCodingMutationResult(revision=existing_revision, created=False)

    prepared: list[tuple[MetadataDefinition, list[dict[str, Any]]]] = []
    seen_definition_ids: set[uuid.UUID] = set()
    for replacement in field_replacements:
        definition = replacement.definition
        if definition.id in seen_definition_ids:
            raise ValueError("Each coding field may appear only once per reviewer revision")
        seen_definition_ids.add(definition.id)
        if definition.cardinality == "SINGLE" and len(replacement.values) > 1:
            raise ValueError(f"{definition.display_name} accepts one value")
        prepared.append(
            (
                definition,
                [value_columns(value, definition) for value in replacement.values],
            )
        )

    revision = coding.revision + 1
    now = utcnow()
    coding.revision = revision
    db.add(
        ReviewBatchReviewerRevision(
            reviewer_coding_id=coding.id,
            revision=revision,
            actor_user_id=actor_user_id,
            origin=origin,
            request_key=request_key,
            created_at=now,
        )
    )
    db.add(
        ReviewBatchReviewerDocumentRevision(
            reviewer_coding_id=coding.id,
            revision=revision,
            matter_document_id=matter_document_id,
            status=status,
            started_at=now,
            completed_at=now if status in {"COMPLETED", "SKIPPED"} else None,
        )
    )

    for definition, typed_values in prepared:
        db.add(
            ReviewBatchReviewerFieldRevision(
                reviewer_coding_id=coding.id,
                revision=revision,
                matter_document_id=matter_document_id,
                metadata_definition_id=definition.id,
            )
        )
        db.execute(
            delete(ReviewBatchReviewerValue).where(
                ReviewBatchReviewerValue.reviewer_coding_id == coding.id,
                ReviewBatchReviewerValue.matter_document_id == matter_document_id,
                ReviewBatchReviewerValue.metadata_definition_id == definition.id,
            )
        )
        for ordinal, columns in enumerate(typed_values):
            db.add(
                ReviewBatchReviewerValueRevision(
                    reviewer_coding_id=coding.id,
                    revision=revision,
                    matter_document_id=matter_document_id,
                    metadata_definition_id=definition.id,
                    value_ordinal=ordinal,
                    **columns,
                )
            )
            db.add(
                ReviewBatchReviewerValue(
                    reviewer_coding_id=coding.id,
                    matter_document_id=matter_document_id,
                    metadata_definition_id=definition.id,
                    value_ordinal=ordinal,
                    revision=revision,
                    **columns,
                )
            )

    current_document = db.get(
        ReviewBatchReviewerDocument,
        (coding.id, matter_document_id),
    )
    if current_document is None:
        current_document = ReviewBatchReviewerDocument(
            reviewer_coding_id=coding.id,
            matter_document_id=matter_document_id,
            status=status,
            revision=revision,
            started_at=now,
            completed_at=now if status in {"COMPLETED", "SKIPPED"} else None,
        )
        db.add(current_document)
    else:
        current_document.status = status
        current_document.revision = revision
        current_document.completed_at = now if status in {"COMPLETED", "SKIPPED"} else None

    record_audit(
        db,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="review_batch.reviewer_coding_changed",
        target_type="review_batch_reviewer_coding",
        target_id=coding.id,
        details={
            "matter_id": str(matter_id),
            "review_batch_id": str(coding.review_batch_id),
            "matter_document_id": str(matter_document_id),
            "reviewer_user_id": str(coding.reviewer_user_id),
            "revision": revision,
            "origin": origin,
            "status": status,
            "metadata_definition_ids": [str(definition.id) for definition, _ in prepared],
        },
    )
    return ReviewerCodingMutationResult(revision=revision, created=True)


def resolve_reviewer_coding_as_of(
    db: Session,
    *,
    reviewer_coding_id: uuid.UUID,
    revision: int,
    document_ids: Sequence[uuid.UUID] | None = None,
    metadata_definition_ids: Sequence[uuid.UUID] | None = None,
) -> ReviewerCodingAsOf:
    """Resolve document and complete field states at one canonical revision."""

    document_latest = (
        select(
            ReviewBatchReviewerDocumentRevision.matter_document_id.label("matter_document_id"),
            func.max(ReviewBatchReviewerDocumentRevision.revision).label("revision"),
        )
        .where(
            ReviewBatchReviewerDocumentRevision.reviewer_coding_id == reviewer_coding_id,
            ReviewBatchReviewerDocumentRevision.revision <= revision,
        )
        .group_by(ReviewBatchReviewerDocumentRevision.matter_document_id)
    )
    if document_ids is not None:
        if not document_ids:
            return ReviewerCodingAsOf(documents=(), fields=(), values=())
        document_latest = document_latest.where(
            ReviewBatchReviewerDocumentRevision.matter_document_id.in_(document_ids)
        )
    document_latest_subquery = document_latest.subquery()
    documents = tuple(
        db.scalars(
            select(ReviewBatchReviewerDocumentRevision).join(
                document_latest_subquery,
                and_(
                    ReviewBatchReviewerDocumentRevision.reviewer_coding_id == reviewer_coding_id,
                    ReviewBatchReviewerDocumentRevision.matter_document_id
                    == document_latest_subquery.c.matter_document_id,
                    ReviewBatchReviewerDocumentRevision.revision == document_latest_subquery.c.revision,
                ),
            )
        )
    )

    field_latest = (
        select(
            ReviewBatchReviewerFieldRevision.matter_document_id.label("matter_document_id"),
            ReviewBatchReviewerFieldRevision.metadata_definition_id.label("metadata_definition_id"),
            func.max(ReviewBatchReviewerFieldRevision.revision).label("revision"),
        )
        .where(
            ReviewBatchReviewerFieldRevision.reviewer_coding_id == reviewer_coding_id,
            ReviewBatchReviewerFieldRevision.revision <= revision,
        )
        .group_by(
            ReviewBatchReviewerFieldRevision.matter_document_id,
            ReviewBatchReviewerFieldRevision.metadata_definition_id,
        )
    )
    if document_ids is not None:
        field_latest = field_latest.where(ReviewBatchReviewerFieldRevision.matter_document_id.in_(document_ids))
    if metadata_definition_ids is not None:
        if not metadata_definition_ids:
            return ReviewerCodingAsOf(documents=documents, fields=(), values=())
        field_latest = field_latest.where(
            ReviewBatchReviewerFieldRevision.metadata_definition_id.in_(metadata_definition_ids)
        )
    field_latest_subquery = field_latest.subquery()
    fields = tuple(
        db.scalars(
            select(ReviewBatchReviewerFieldRevision).join(
                field_latest_subquery,
                and_(
                    ReviewBatchReviewerFieldRevision.reviewer_coding_id == reviewer_coding_id,
                    ReviewBatchReviewerFieldRevision.matter_document_id
                    == field_latest_subquery.c.matter_document_id,
                    ReviewBatchReviewerFieldRevision.metadata_definition_id
                    == field_latest_subquery.c.metadata_definition_id,
                    ReviewBatchReviewerFieldRevision.revision == field_latest_subquery.c.revision,
                ),
            )
        )
    )
    if not fields:
        return ReviewerCodingAsOf(documents=documents, fields=(), values=())

    values = tuple(
        db.scalars(
            select(ReviewBatchReviewerValueRevision)
            .join(
                field_latest_subquery,
                and_(
                    ReviewBatchReviewerValueRevision.reviewer_coding_id == reviewer_coding_id,
                    ReviewBatchReviewerValueRevision.matter_document_id
                    == field_latest_subquery.c.matter_document_id,
                    ReviewBatchReviewerValueRevision.metadata_definition_id
                    == field_latest_subquery.c.metadata_definition_id,
                    ReviewBatchReviewerValueRevision.revision == field_latest_subquery.c.revision,
                ),
            )
            .order_by(
                ReviewBatchReviewerValueRevision.matter_document_id,
                ReviewBatchReviewerValueRevision.metadata_definition_id,
                ReviewBatchReviewerValueRevision.value_ordinal,
            )
        )
    )
    return ReviewerCodingAsOf(documents=documents, fields=fields, values=values)
