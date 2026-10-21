"""Parent/child system metadata definitions and legacy-matter upgrade."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.metadata_profiles import DEFAULT_MATTER_METADATA_PROFILE, MetadataDefinitionTemplate
from app.models import Matter, MetadataDefinition, MetadataGroup, MetadataGroupField

FAMILY_GROUP_KEY = "family"
FAMILY_RELATIONSHIP_KEYS = ("parent_id", "child_id")


class FamilyRelationshipUpdateConflict(ValueError):
    """A matter already uses a reserved relationship key incompatibly."""


def _templates() -> dict[str, MetadataDefinitionTemplate]:
    return {
        definition.key: definition
        for definition in DEFAULT_MATTER_METADATA_PROFILE.definitions
        if definition.key in FAMILY_RELATIONSHIP_KEYS
    }


def _validate_existing_definition(
    definition: MetadataDefinition,
    expected: MetadataDefinitionTemplate,
) -> None:
    actual = {
        "type": definition.type,
        "cardinality": definition.cardinality,
        "value_source": definition.value_source,
        "searchable": definition.searchable,
        "facetable": definition.facetable,
        "reviewable": definition.reviewable,
    }
    required = {
        "type": expected.type,
        "cardinality": expected.cardinality,
        "value_source": expected.value_source,
        "searchable": expected.searchable,
        "facetable": expected.facetable,
        "reviewable": expected.reviewable,
    }
    legacy_profile_definition = {
        **required,
        "facetable": False,
    }
    is_upgradeable_v6_definition = (
        actual == legacy_profile_definition
        and definition.template_key == DEFAULT_MATTER_METADATA_PROFILE.key
        and definition.template_version == 6
    )
    if actual != required and not is_upgradeable_v6_definition:
        raise FamilyRelationshipUpdateConflict(
            f"The {definition.key} key already exists with an incompatible definition; "
            "rename that field before installing the system family relationship fields"
        )


def _new_definition(matter_id: uuid.UUID, template: MetadataDefinitionTemplate) -> MetadataDefinition:
    return MetadataDefinition(
        matter_id=matter_id,
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


def ensure_family_relationship_metadata(
    db: Session,
    *,
    matter: Matter,
    actor_user_id: uuid.UUID,
) -> tuple[list[MetadataDefinition], bool]:
    """Install canonical Parent ID and Child ID definitions and group membership."""

    templates = _templates()
    definitions: list[MetadataDefinition] = []
    created = False
    for key in FAMILY_RELATIONSHIP_KEYS:
        template = templates[key]
        definition = db.scalar(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter.id,
                MetadataDefinition.key == key,
            )
        )
        if definition is None:
            definition = _new_definition(matter.id, template)
            db.add(definition)
            db.flush()
            created = True
        else:
            _validate_existing_definition(definition, template)
            definition.status = "ACTIVE"
            definition.facetable = template.facetable
            definition.template_version = DEFAULT_MATTER_METADATA_PROFILE.version
        definitions.append(definition)

    group = db.scalar(
        select(MetadataGroup).where(
            MetadataGroup.matter_id == matter.id,
            MetadataGroup.scope.in_(["SYSTEM", "MATTER"]),
            MetadataGroup.key == FAMILY_GROUP_KEY,
        )
    )
    group_template = next(item for item in DEFAULT_MATTER_METADATA_PROFILE.groups if item.key == FAMILY_GROUP_KEY)
    if group is None:
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
    else:
        group.status = "ACTIVE"
        if group.template_key == DEFAULT_MATTER_METADATA_PROFILE.key:
            group.template_version = DEFAULT_MATTER_METADATA_PROFILE.version

    existing_definitions = list(
        db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter.id,
                MetadataDefinition.key.in_(group_template.field_keys),
            )
        )
    )
    by_key = {definition.key: definition for definition in existing_definitions}
    for position, key in enumerate(group_template.field_keys, start=1):
        definition = by_key.get(key)
        if definition is None:
            continue
        membership = db.scalar(
            select(MetadataGroupField).where(
                MetadataGroupField.metadata_group_id == group.id,
                MetadataGroupField.metadata_definition_id == definition.id,
            )
        )
        if membership is None:
            membership = MetadataGroupField(
                metadata_group_id=group.id,
                metadata_definition_id=definition.id,
                sort_order=position * 10,
            )
            db.add(membership)
        else:
            membership.sort_order = position * 10

    return definitions, created
