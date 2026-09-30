from collections.abc import Iterable
from typing import Any

from app.models import MetadataDefinition

HIERARCHY_ROOT = "__root__"
MAX_HIERARCHY_DEPTH = 32
MAX_HIERARCHY_PATH_LENGTH = 4096
MAX_HIERARCHY_SEGMENT_LENGTH = 200


def enum_option_map(definition: MetadataDefinition) -> dict[str, dict[str, Any]]:
    return {
        str(option["key"]): option
        for option in definition.allowed_values or []
        if isinstance(option, dict) and option.get("key") is not None
    }


def is_hierarchical_enum(definition: MetadataDefinition) -> bool:
    return definition.type == "ENUM" and any(
        option.get("parent_key") is not None for option in definition.allowed_values or []
    )


def is_hierarchical_field(definition: MetadataDefinition) -> bool:
    return is_hierarchical_enum(definition) or (
        definition.type == "TEXT" and definition.hierarchy_separator is not None
    )


def parse_hierarchy_path(value: Any, separator: str) -> list[str]:
    if not isinstance(value, str):
        raise TypeError("Hierarchy path values must be strings")
    if len(value) > MAX_HIERARCHY_PATH_LENGTH:
        raise ValueError(f"Hierarchy path exceeds the {MAX_HIERARCHY_PATH_LENGTH}-character limit")
    segments = [segment.strip() for segment in value.split(separator)]
    if not segments or any(not segment for segment in segments):
        raise ValueError("Hierarchy paths cannot contain empty segments")
    if len(segments) > MAX_HIERARCHY_DEPTH:
        raise ValueError(f"Hierarchy path exceeds the {MAX_HIERARCHY_DEPTH}-level limit")
    if any(len(segment) > MAX_HIERARCHY_SEGMENT_LENGTH for segment in segments):
        raise ValueError(f"Hierarchy path segments cannot exceed {MAX_HIERARCHY_SEGMENT_LENGTH} characters")
    return segments


def hierarchy_path_parent(value: str, separator: str) -> str | None:
    segments = parse_hierarchy_path(value, separator)
    return separator.join(segments[:-1]) or None


def hierarchy_path_label(value: str, separator: str) -> str:
    return parse_hierarchy_path(value, separator)[-1]


def hierarchy_depths(definition: MetadataDefinition) -> dict[str, int]:
    options = enum_option_map(definition)
    depths: dict[str, int] = {}

    def depth(key: str, trail: frozenset[str] = frozenset()) -> int:
        if key in depths:
            return depths[key]
        if key in trail:
            raise ValueError(f"Enum hierarchy for '{definition.key}' contains a cycle")
        option = options.get(key)
        if option is None:
            raise ValueError(f"Unknown enum value '{key}' for field '{definition.key}'")
        parent_key = option.get("parent_key")
        result = 0 if parent_key is None else depth(str(parent_key), trail | {key}) + 1
        depths[key] = result
        return result

    for option_key in options:
        depth(option_key)
    return depths


def hierarchy_ancestor_keys(definition: MetadataDefinition, value: str) -> list[str]:
    options = enum_option_map(definition)
    ancestors: list[str] = []
    current: str | None = value
    visited: set[str] = set()
    while current is not None:
        if current in visited:
            raise ValueError(f"Enum hierarchy for '{definition.key}' contains a cycle")
        visited.add(current)
        option = options.get(current)
        if option is None:
            raise ValueError(f"Unknown enum value '{current}' for field '{definition.key}'")
        ancestors.append(current)
        parent_key = option.get("parent_key")
        current = str(parent_key) if parent_key is not None else None
    return ancestors


def hierarchy_entries(
    definitions: Iterable[MetadataDefinition],
    metadata_values: dict[str, Any],
) -> list[dict[str, Any]]:
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for definition in definitions:
        if definition.type == "TEXT" and definition.hierarchy_separator is not None:
            raw_value = metadata_values.get(definition.key)
            values = raw_value if isinstance(raw_value, list) else [raw_value]
            for value in values:
                if value is None:
                    continue
                segments = parse_hierarchy_path(value, definition.hierarchy_separator)
                for depth in range(len(segments)):
                    node_id = definition.hierarchy_separator.join(segments[: depth + 1])
                    parent_id = definition.hierarchy_separator.join(segments[:depth]) if depth else HIERARCHY_ROOT
                    key = (definition.key, node_id)
                    candidate = {
                        "field": definition.key,
                        "node_id": node_id,
                        "parent_id": parent_id,
                        "depth": depth,
                        "has_children": depth < len(segments) - 1,
                    }
                    existing = entries.get(key)
                    if existing is None:
                        entries[key] = candidate
                    elif candidate["has_children"]:
                        existing["has_children"] = True
            continue
        # Project every enum value so an existing flat enum can gain its first
        # child without requiring a special backfill at that moment. The root
        # hierarchy_facets mapping itself is introduced through a confirmed
        # full reindex, and new searchable definitions already require one.
        if definition.type != "ENUM":
            continue
        raw_value = metadata_values.get(definition.key)
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        depths = hierarchy_depths(definition)
        options = enum_option_map(definition)
        parents = {
            str(option["parent_key"])
            for option in options.values()
            if option.get("parent_key") is not None and option.get("active", True)
        }
        for value in values:
            if value is None:
                continue
            for node_id in hierarchy_ancestor_keys(definition, str(value)):
                option = options[node_id]
                entries[(definition.key, node_id)] = {
                    "field": definition.key,
                    "node_id": node_id,
                    "parent_id": option.get("parent_key") or HIERARCHY_ROOT,
                    "depth": depths[node_id],
                    "has_children": node_id in parents,
                }
    return list(entries.values())
