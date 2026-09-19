import uuid
from datetime import datetime

from sqlalchemy import or_, select

from artifact_service.models import CollectionItem, CollectionItemCustodian


def search_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def normalize_extensions(values: list[str]) -> list[str | None]:
    normalized: list[str | None] = []
    for value in values:
        cleaned = value.strip().lower()
        if not cleaned:
            continue
        extension = None if cleaned == "__none__" else f".{cleaned.lstrip('.')}"
        if extension not in normalized:
            normalized.append(extension)
    return normalized


def collection_item_ids(
    collection_id: uuid.UUID,
    *,
    search: str | None,
    custodian_ids: list[uuid.UUID],
    extensions: list[str | None],
    record_types: list[str],
    processing_statuses: list[str],
    source_created_from: datetime | None = None,
    source_created_to: datetime | None = None,
    file_date_from: datetime | None = None,
    file_date_to: datetime | None = None,
    explicit_item_ids: list[uuid.UUID] | None = None,
    exclude_facet: str | None = None,
):
    query = select(CollectionItem.id).where(CollectionItem.collection_id == collection_id)
    if explicit_item_ids is not None:
        return query.where(CollectionItem.id.in_(explicit_item_ids))
    if search:
        pattern = search_pattern(search)
        query = query.where(
            or_(
                CollectionItem.original_filename.ilike(pattern, escape="\\"),
                CollectionItem.original_source_path.ilike(pattern, escape="\\"),
            )
        )
    if custodian_ids and exclude_facet != "custodians":
        query = query.where(
            select(CollectionItemCustodian.collection_item_id)
            .where(
                CollectionItemCustodian.collection_item_id == CollectionItem.id,
                CollectionItemCustodian.custodian_id.in_(custodian_ids),
            )
            .exists()
        )
    if extensions and exclude_facet != "file_extensions":
        concrete_extensions = [extension for extension in extensions if extension is not None]
        extension_conditions = []
        if concrete_extensions:
            extension_conditions.append(CollectionItem.original_extension.in_(concrete_extensions))
        if None in extensions:
            extension_conditions.append(CollectionItem.original_extension.is_(None))
        query = query.where(or_(*extension_conditions))
    if record_types and exclude_facet != "record_types":
        query = query.where(CollectionItem.record_type.in_(record_types))
    if processing_statuses and exclude_facet != "processing_statuses":
        query = query.where(CollectionItem.processing_status.in_(processing_statuses))
    if source_created_from is not None:
        query = query.where(CollectionItem.source_created_at >= source_created_from)
    if source_created_to is not None:
        query = query.where(CollectionItem.source_created_at <= source_created_to)
    if file_date_from is not None:
        query = query.where(CollectionItem.file_date >= file_date_from)
    if file_date_to is not None:
        query = query.where(CollectionItem.file_date <= file_date_to)
    return query
