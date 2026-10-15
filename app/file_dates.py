"""Canonical file-date metadata definition and legacy-matter upgrade."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.metadata_profiles import DEFAULT_MATTER_METADATA_PROFILE, MetadataDefinitionTemplate
from app.models import Matter, MetadataDefinition, MetadataGroup, MetadataGroupField

FILE_DATE_KEY = "file_date"
SOURCE_DATES_GROUP_KEY = "source_dates"


class FileDateUpdateConflict(ValueError):
    """The matter already uses ``file_date`` with an incompatible shape."""


def _file_date_template() -> MetadataDefinitionTemplate:
    return next(
        definition
        for definition in DEFAULT_MATTER_METADATA_PROFILE.definitions
        if definition.key == FILE_DATE_KEY
    )


def _validate_existing_definition(definition: MetadataDefinition) -> None:
    expected = _file_date_template()
    actual = {
        "type": definition.type,
        "cardinality": definition.cardinality,
        "value_source": definition.value_source,
        "searchable": definition.searchable,
        "facetable": definition.facetable,
    }
    required = {
        "type": expected.type,
        "cardinality": expected.cardinality,
        "value_source": expected.value_source,
        "searchable": expected.searchable,
        "facetable": expected.facetable,
    }
    if actual != required:
        raise FileDateUpdateConflict(
            "The file_date key already exists with an incompatible definition; "
            "rename that field before installing the system File date field"
        )


def ensure_file_date_metadata(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
) -> tuple[MetadataDefinition, bool]:
    """Install the canonical File date definition and Source Dates membership."""

    template = _file_date_template()
    definition = db.scalar(
        select(MetadataDefinition).where(
            MetadataDefinition.matter_id == matter.id,
            MetadataDefinition.key == FILE_DATE_KEY,
        )
    )
    created = definition is None
    if definition is None:
        definition = MetadataDefinition(
            matter_id=matter.id,
            key=template.key,
            display_name=template.display_name,
            description=template.description,
            type=template.type,
            cardinality=template.cardinality,
            allowed_values=None,
            value_source=template.value_source,
            reference_target=template.reference_target,
            template_key=DEFAULT_MATTER_METADATA_PROFILE.key,
            template_version=DEFAULT_MATTER_METADATA_PROFILE.version,
            assertion_policy=template.assertion_policy,
            resolution_policy=template.resolution_policy,
            searchable=template.searchable,
            facetable=template.facetable,
            normalize_to_lowercase=template.normalize_to_lowercase,
            hierarchy_separator=template.hierarchy_separator,
            reviewable=template.reviewable,
            ai_assignable=template.ai_assignable,
            status="ACTIVE",
        )
        db.add(definition)
        db.flush()
    else:
        _validate_existing_definition(definition)
        definition.status = "ACTIVE"

    group = db.scalar(
        select(MetadataGroup).where(
            MetadataGroup.matter_id == matter.id,
            MetadataGroup.scope.in_(["SYSTEM", "MATTER"]),
            MetadataGroup.key == SOURCE_DATES_GROUP_KEY,
        )
    )
    if group is None:
        group_template = next(
            item for item in DEFAULT_MATTER_METADATA_PROFILE.groups
            if item.key == SOURCE_DATES_GROUP_KEY
        )
        group = MetadataGroup(
            matter_id=matter.id,
            scope="SYSTEM",
            owner_user_id=None,
            created_by_user_id=actor_user_id,
            key=group_template.key,
            display_name=group_template.display_name,
            description=group_template.description,
            sort_order=group_template.sort_order,
            default_table_visible=group_template.default_table_visible,
            default_document_visible=group_template.default_document_visible,
            status="ACTIVE",
            template_key=DEFAULT_MATTER_METADATA_PROFILE.key,
            template_version=DEFAULT_MATTER_METADATA_PROFILE.version,
        )
        db.add(group)
        db.flush()

    membership = db.scalar(
        select(MetadataGroupField).where(
            MetadataGroupField.metadata_group_id == group.id,
            MetadataGroupField.metadata_definition_id == definition.id,
        )
    )
    if membership is None:
        minimum_sort_order = db.scalar(
            select(func.min(MetadataGroupField.sort_order)).where(
                MetadataGroupField.metadata_group_id == group.id
            )
        )
        db.add(
            MetadataGroupField(
                metadata_group_id=group.id,
                metadata_definition_id=definition.id,
                sort_order=int(minimum_sort_order or 10) - 10,
            )
        )

    return definition, created
