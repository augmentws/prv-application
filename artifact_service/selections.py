import json
import uuid
from typing import Any

from sqlalchemy import delete, func, insert, literal, or_, select
from sqlalchemy.orm import Session

from artifact_service.models import (
    ClientCollection,
    CollectionItem,
    CollectionItemCustodian,
    CollectionSelection,
    CollectionSelectionItem,
)
from artifact_service.schemas import CollectionSelectionCreate
from artifact_service.search import collection_item_ids, normalize_extensions

MAX_SELECTION_METADATA_KEYS = 200
MAX_SELECTION_ITEM_METADATA_BYTES = 256_000


def create_collection_selection(
    db: Session,
    collection: ClientCollection,
    payload: CollectionSelectionCreate,
) -> CollectionSelection:
    existing = db.scalar(select(CollectionSelection).where(CollectionSelection.request_id == payload.request_id))
    if existing is not None:
        if existing.collection_id != collection.id:
            raise ValueError("Selection request ID is already used by another collection")
        return existing

    selection_data = payload.model_dump(mode="json")
    seed_ids = collection_item_ids(
        collection.id,
        search=payload.q.strip() if payload.q and payload.q.strip() else None,
        custodian_ids=list(dict.fromkeys(payload.custodian_ids)),
        extensions=normalize_extensions(payload.file_extensions),
        record_types=list(dict.fromkeys(payload.record_types)),
        processing_statuses=list(dict.fromkeys(payload.processing_statuses)),
        file_date_from=payload.file_date_from,
        file_date_to=payload.file_date_to,
        explicit_item_ids=list(dict.fromkeys(payload.item_ids)) if payload.mode == "EXPLICIT" else None,
    )
    seed_ids_subquery = seed_ids.subquery("collection_selection_seed_ids")
    seed_id_values = select(seed_ids_subquery.c.id)
    seed_count = db.scalar(select(func.count()).select_from(seed_ids_subquery)) or 0
    matched_ids = seed_id_values
    if payload.include_complete_families and seed_count:
        seed_family_ids = select(CollectionItem.family_id).where(
            CollectionItem.collection_id == collection.id,
            CollectionItem.id.in_(seed_id_values),
            CollectionItem.family_id.is_not(None),
        )
        seed_parent_ids = select(CollectionItem.parent_collection_item_id).where(
            CollectionItem.collection_id == collection.id,
            CollectionItem.id.in_(seed_id_values),
            CollectionItem.parent_collection_item_id.is_not(None),
        )
        matched_ids = select(CollectionItem.id).where(
            CollectionItem.collection_id == collection.id,
            or_(
                CollectionItem.id.in_(seed_id_values),
                CollectionItem.family_id.in_(seed_family_ids),
                # The edge checks preserve useful behavior for legacy rows that
                # predate family IDs. Modern imports use the family-ID branch.
                CollectionItem.id.in_(seed_parent_ids),
                CollectionItem.parent_collection_item_id.in_(seed_id_values),
            ),
        )
    total_count = db.scalar(select(func.count()).select_from(matched_ids.subquery())) or 0
    selection_data["family_expansion"] = {
        "policy": "EXPAND_COMPLETE_FAMILIES_IN_COLLECTION"
        if payload.include_complete_families
        else "SEED_DOCUMENTS_ONLY",
        "seed_document_count": seed_count,
        "added_document_count": total_count - seed_count,
        "final_document_count": total_count,
    }
    selection = CollectionSelection(
        request_id=payload.request_id,
        tenant_id=collection.tenant_id,
        client_id=collection.client_id,
        collection_id=collection.id,
        selection=selection_data,
        status="READY",
        total_count=total_count,
    )
    db.add(selection)
    db.flush()
    if total_count:
        ordered_matches = (
            select(
                literal(selection.id),
                CollectionItem.id,
                (func.row_number().over(order_by=(CollectionItem.created_at, CollectionItem.id)) - 1),
            )
            .where(CollectionItem.id.in_(matched_ids))
        )
        db.execute(
            insert(CollectionSelectionItem).from_select(
                ["selection_id", "collection_item_id", "ordinal"],
                ordered_matches,
            )
        )
    return selection


def get_collection_selection_batch(
    db: Session,
    selection: CollectionSelection,
    *,
    offset: int,
    limit: int,
) -> list[uuid.UUID]:
    return list(
        db.scalars(
            select(CollectionSelectionItem.collection_item_id)
            .where(CollectionSelectionItem.selection_id == selection.id)
            .order_by(CollectionSelectionItem.ordinal)
            .offset(offset)
            .limit(limit)
        )
    )


def get_collection_selection_batch_custodians(
    db: Session,
    item_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[tuple[uuid.UUID, str]]]:
    custodians = {item_id: [] for item_id in item_ids}
    if not item_ids:
        return custodians
    for item_id, custodian_id, relationship_type in db.execute(
        select(
            CollectionItemCustodian.collection_item_id,
            CollectionItemCustodian.custodian_id,
            CollectionItemCustodian.relationship_type,
        )
        .where(CollectionItemCustodian.collection_item_id.in_(item_ids))
        .order_by(CollectionItemCustodian.collection_item_id, CollectionItemCustodian.relationship_type.desc())
    ):
        custodians[item_id].append((custodian_id, relationship_type))
    return custodians


def get_collection_selection_batch_unmapped_metadata(
    db: Session,
    item_ids: list[uuid.UUID],
    metadata_keys: list[str],
) -> dict[uuid.UUID, dict[str, Any]]:
    requested = list(dict.fromkeys(key.strip() for key in metadata_keys if key.strip()))
    if len(requested) > MAX_SELECTION_METADATA_KEYS:
        raise ValueError(f"At most {MAX_SELECTION_METADATA_KEYS} metadata keys may be requested")
    if any(len(key) > 100 for key in requested):
        raise ValueError("Metadata keys cannot exceed 100 characters")
    values = {item_id: {} for item_id in item_ids}
    if not item_ids or not requested:
        return values
    requested_by_folded = {key.casefold(): key for key in requested}
    rows = db.execute(
        select(CollectionItem.id, CollectionItem.unmapped_metadata).where(CollectionItem.id.in_(item_ids))
    )
    for item_id, unmapped_metadata in rows:
        source = {
            str(key).casefold(): value
            for key, value in (unmapped_metadata or {}).items()
        }
        selected = {
            canonical: source[folded]
            for folded, canonical in requested_by_folded.items()
            if folded in source and source[folded] not in (None, "", [])
        }
        if len(json.dumps(selected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > (
            MAX_SELECTION_ITEM_METADATA_BYTES
        ):
            raise ValueError("Requested item metadata exceeds the selection payload limit")
        values[item_id] = selected
    return values


def delete_collection_selection(db: Session, selection: CollectionSelection) -> None:
    db.execute(delete(CollectionSelection).where(CollectionSelection.id == selection.id))
