import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import MatterDocument, MatterDocumentImportJob


@dataclass(frozen=True)
class CollectionDeletionBlockers:
    matter_document_count: int
    active_import_count: int

    @property
    def blocked(self) -> bool:
        return self.matter_document_count > 0 or self.active_import_count > 0

    def message(self) -> str:
        reasons: list[str] = []
        if self.matter_document_count:
            reasons.append(
                f"{self.matter_document_count} matter document"
                f"{'s' if self.matter_document_count != 1 else ''}"
            )
        if self.active_import_count:
            reasons.append(
                f"{self.active_import_count} active matter import"
                f"{'s' if self.active_import_count != 1 else ''}"
            )
        return "Collection deletion is blocked by " + " and ".join(reasons)


def collection_deletion_blockers(
    db: Session,
    collection_id: uuid.UUID,
) -> CollectionDeletionBlockers:
    matter_document_count = int(
        db.scalar(
            select(func.count())
            .select_from(MatterDocument)
            .where(MatterDocument.source_collection_id == collection_id)
        )
        or 0
    )
    active_import_count = int(
        db.scalar(
            select(func.count())
            .select_from(MatterDocumentImportJob)
            .where(
                MatterDocumentImportJob.source_collection_id == collection_id,
                MatterDocumentImportJob.status.in_(("QUEUED", "SNAPSHOTTING", "RUNNING")),
            )
        )
        or 0
    )
    return CollectionDeletionBlockers(
        matter_document_count=matter_document_count,
        active_import_count=active_import_count,
    )
