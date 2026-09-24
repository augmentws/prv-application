"""Snapshot and materialize reusable matter configuration."""

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.metadata_profiles import MatterConfigurationRows
from app.models import MetadataDefinition, MetadataGroup, MetadataGroupField

CONFIGURATION_SCHEMA_VERSION = 1


def normalize_name(value: str) -> str:
    return " ".join(value.casefold().split())


def group_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", normalize_name(value)).strip("_")
    if not key:
        key = "group"
    if not key[0].isalpha():
        key = f"group_{key}"
    return key[:100]


def snapshot_matter_configuration(db: Session, matter_id: uuid.UUID) -> dict[str, Any]:
    definitions = list(
        db.scalars(
            select(MetadataDefinition)
            .where(MetadataDefinition.matter_id == matter_id, MetadataDefinition.status != "ARCHIVED")
            .order_by(MetadataDefinition.key)
        )
    )
    definition_keys = {definition.id: definition.key for definition in definitions}
    groups = list(
        db.scalars(
            select(MetadataGroup)
            .where(
                MetadataGroup.matter_id == matter_id,
                MetadataGroup.scope.in_(("SYSTEM", "MATTER")),
                MetadataGroup.status != "ARCHIVED",
            )
            .order_by(MetadataGroup.sort_order, MetadataGroup.display_name)
        )
    )
    group_ids = [group.id for group in groups]
    mappings = (
        list(
            db.scalars(
                select(MetadataGroupField)
                .where(MetadataGroupField.metadata_group_id.in_(group_ids))
                .order_by(MetadataGroupField.metadata_group_id, MetadataGroupField.sort_order)
            )
        )
        if group_ids
        else []
    )
    fields_by_group: dict[uuid.UUID, list[str]] = {group_id: [] for group_id in group_ids}
    for mapping in mappings:
        key = definition_keys.get(mapping.metadata_definition_id)
        if key is not None:
            fields_by_group[mapping.metadata_group_id].append(key)

    return {
        "schema_version": CONFIGURATION_SCHEMA_VERSION,
        "definitions": [
            {
                "key": definition.key,
                "display_name": definition.display_name,
                "description": definition.description,
                "type": definition.type,
                "cardinality": definition.cardinality,
                "allowed_values": definition.allowed_values,
                "value_source": definition.value_source,
                "reference_target": definition.reference_target,
                "template_key": definition.template_key,
                "template_version": definition.template_version,
                "assertion_policy": definition.assertion_policy,
                "resolution_policy": definition.resolution_policy,
                "searchable": definition.searchable,
                "facetable": definition.facetable,
                "normalize_to_lowercase": definition.normalize_to_lowercase,
                "reviewable": definition.reviewable,
                "ai_assignable": definition.ai_assignable,
                "status": definition.status,
            }
            for definition in definitions
        ],
        "groups": [
            {
                "key": group.key,
                "display_name": group.display_name,
                "description": group.description,
                "scope": group.scope,
                "sort_order": group.sort_order,
                "default_table_visible": group.default_table_visible,
                "default_document_visible": group.default_document_visible,
                "status": group.status,
                "template_key": group.template_key,
                "template_version": group.template_version,
                "field_keys": fields_by_group[group.id],
            }
            for group in groups
        ],
    }


def instantiate_configuration_snapshot(
    matter_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    configuration: dict[str, Any],
) -> MatterConfigurationRows:
    if configuration.get("schema_version") != CONFIGURATION_SCHEMA_VERSION:
        raise ValueError("Unsupported matter configuration schema version")

    definitions: list[MetadataDefinition] = []
    definitions_by_key: dict[str, MetadataDefinition] = {}
    for item in configuration.get("definitions", []):
        key = str(item["key"])
        if key in definitions_by_key:
            raise ValueError(f"Duplicate metadata definition key: {key}")
        definition = MetadataDefinition(
            id=uuid.uuid4(),
            matter_id=matter_id,
            key=key,
            display_name=item["display_name"],
            description=item.get("description"),
            type=item["type"],
            cardinality=item["cardinality"],
            allowed_values=item.get("allowed_values"),
            value_source=item["value_source"],
            reference_target=item.get("reference_target"),
            template_key=item.get("template_key"),
            template_version=item.get("template_version"),
            assertion_policy=item["assertion_policy"],
            resolution_policy=item["resolution_policy"],
            searchable=bool(item["searchable"]),
            facetable=bool(item["facetable"]),
            normalize_to_lowercase=bool(item.get("normalize_to_lowercase", False)),
            reviewable=bool(item["reviewable"]),
            ai_assignable=bool(item["ai_assignable"]),
            status=item["status"],
        )
        definitions.append(definition)
        definitions_by_key[key] = definition

    groups: list[MetadataGroup] = []
    group_fields: list[MetadataGroupField] = []
    seen_group_keys: set[str] = set()
    for item in configuration.get("groups", []):
        key = str(item["key"])
        if key in seen_group_keys:
            raise ValueError(f"Duplicate shared metadata group key: {key}")
        if item["scope"] not in {"SYSTEM", "MATTER"}:
            raise ValueError("Reusable configurations cannot contain personal metadata groups")
        group = MetadataGroup(
            id=uuid.uuid4(),
            matter_id=matter_id,
            scope=item["scope"],
            owner_user_id=None,
            created_by_user_id=created_by_user_id,
            key=key,
            display_name=item["display_name"],
            description=item.get("description"),
            sort_order=int(item["sort_order"]),
            default_table_visible=bool(item["default_table_visible"]),
            default_document_visible=bool(item["default_document_visible"]),
            status=item["status"],
            template_key=item.get("template_key"),
            template_version=item.get("template_version"),
        )
        groups.append(group)
        seen_group_keys.add(key)
        for index, field_key in enumerate(item.get("field_keys", []), start=1):
            definition = definitions_by_key.get(field_key)
            if definition is None:
                raise ValueError(f"Metadata group {key} references unknown field {field_key}")
            group_fields.append(
                MetadataGroupField(
                    metadata_group_id=group.id,
                    metadata_definition_id=definition.id,
                    sort_order=index * 10,
                )
            )

    if not definitions:
        raise ValueError("Matter configuration must contain at least one metadata definition")
    return MatterConfigurationRows(definitions=definitions, groups=groups, group_fields=group_fields)


def configuration_counts(configuration: dict[str, Any]) -> tuple[int, int]:
    return len(configuration.get("definitions", [])), len(configuration.get("groups", []))
