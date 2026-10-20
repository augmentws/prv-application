from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from artifact_service.document_identifiers import document_identifier
from artifact_service.models import CollectionItem
from artifact_service.schemas import CollectionItemRelationshipSnapshot


def default_family_id(parent_source_item_id: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"priv-view:import-family:{parent_source_item_id}")


def collection_item_relationship_snapshots(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    item_ids: list[uuid.UUID],
) -> list[CollectionItemRelationshipSnapshot]:
    """Read direct family edges with deterministic, review-facing identifiers."""

    items = list(
        db.scalars(
            select(CollectionItem).where(
                CollectionItem.tenant_id == tenant_id,
                CollectionItem.client_id == client_id,
                CollectionItem.id.in_(item_ids),
            )
        )
    )
    by_id = {item.id: item for item in items}
    parent_ids = {item.parent_collection_item_id for item in items if item.parent_collection_item_id is not None}
    parents = (
        list(
            db.scalars(
                select(CollectionItem).where(
                    CollectionItem.tenant_id == tenant_id,
                    CollectionItem.client_id == client_id,
                    CollectionItem.id.in_(parent_ids),
                )
            )
        )
        if parent_ids
        else []
    )
    parents_by_id = {item.id: item for item in parents}
    children = list(
        db.scalars(
            select(CollectionItem).where(
                CollectionItem.tenant_id == tenant_id,
                CollectionItem.client_id == client_id,
                CollectionItem.parent_collection_item_id.in_(item_ids),
            )
        )
    )
    children_by_parent: dict[uuid.UUID, list[CollectionItem]] = {}
    for child in children:
        parent = by_id.get(child.parent_collection_item_id)
        if parent is not None and child.collection_id == parent.collection_id:
            children_by_parent.setdefault(parent.id, []).append(child)

    snapshots: list[CollectionItemRelationshipSnapshot] = []
    for item_id in item_ids:
        item = by_id.get(item_id)
        if item is None:
            continue
        parent = parents_by_id.get(item.parent_collection_item_id)
        if parent is not None and parent.collection_id != item.collection_id:
            parent = None
        ordered_children = sorted(
            children_by_parent.get(item.id, []),
            key=lambda child: (
                document_identifier(
                    child.source_item_id,
                    child.raw_metadata,
                    child.unmapped_metadata,
                ),
                str(child.id),
            ),
        )
        snapshots.append(
            CollectionItemRelationshipSnapshot(
                item_id=item.id,
                family_id=item.family_id,
                parent_item_id=parent.id if parent is not None else None,
                parent_source_item_id=parent.source_item_id if parent is not None else None,
                parent_identifier=(
                    document_identifier(parent.source_item_id, parent.raw_metadata, parent.unmapped_metadata)
                    if parent is not None
                    else None
                ),
                child_item_ids=[child.id for child in ordered_children],
                child_source_item_ids=[child.source_item_id for child in ordered_children],
                child_identifiers=[
                    document_identifier(child.source_item_id, child.raw_metadata, child.unmapped_metadata)
                    for child in ordered_children
                ],
            )
        )
    return snapshots
