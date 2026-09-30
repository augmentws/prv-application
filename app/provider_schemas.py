from __future__ import annotations

from typing import Any

GEMINI_JSON_SCHEMA_KEYWORDS = frozenset(
    {
        "$id",
        "$defs",
        "$ref",
        "$anchor",
        "type",
        "format",
        "title",
        "description",
        "enum",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "anyOf",
        "oneOf",
        "properties",
        "additionalProperties",
        "required",
        "propertyOrdering",
    }
)


def _unconstrained_json_value_schema() -> dict[str, Any]:
    scalar_types = [
        {"type": "string"},
        {"type": "number"},
        {"type": "integer"},
        {"type": "boolean"},
        {"type": "null"},
    ]
    return {
        "anyOf": [
            *scalar_types,
            {"type": "array", "items": {"anyOf": scalar_types}},
            {"type": "object", "additionalProperties": True},
        ]
    }


def _json_pointer(root: dict[str, Any], reference: str) -> Any:
    if not reference.startswith("#/"):
        raise ValueError(f"Only local JSON Schema references can be inlined: {reference}")
    value: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"JSON Schema reference does not exist: {reference}")
        value = value[part]
    return value


def _inline_local_references(
    value: Any,
    *,
    root: dict[str, Any],
    resolving: tuple[str, ...] = (),
) -> Any:
    if isinstance(value, list):
        return [_inline_local_references(item, root=root, resolving=resolving) for item in value]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if reference is not None:
        if not isinstance(reference, str):
            raise ValueError("JSON Schema $ref must be a string")
        if reference in resolving:
            raise ValueError(f"Recursive JSON Schema references cannot be inlined: {reference}")
        target = _inline_local_references(
            _json_pointer(root, reference),
            root=root,
            resolving=(*resolving, reference),
        )
        if not isinstance(target, dict):
            raise ValueError(f"JSON Schema reference must resolve to an object: {reference}")
        siblings = {
            key: _inline_local_references(item, root=root, resolving=resolving)
            for key, item in value.items()
            if key != "$ref"
        }
        conflicts = {key for key in siblings.keys() & target.keys() if siblings[key] != target[key]}
        if conflicts:
            raise ValueError(
                f"JSON Schema reference siblings conflict with the referenced schema: {', '.join(sorted(conflicts))}"
            )
        return {**target, **siblings}
    return {
        key: _inline_local_references(item, root=root, resolving=resolving)
        for key, item in value.items()
        if key != "$defs"
    }


def _gemini_schema(
    value: Any,
    *,
    map_value: bool = False,
    include_additional_properties: bool = True,
) -> Any:
    if isinstance(value, list):
        return [_gemini_schema(item, include_additional_properties=include_additional_properties) for item in value]
    if not isinstance(value, dict):
        return value
    if map_value:
        return {
            key: _gemini_schema(
                item,
                include_additional_properties=include_additional_properties,
            )
            for key, item in value.items()
        }
    if not value:
        return _unconstrained_json_value_schema()

    transformed: dict[str, Any] = {}
    const = value.get("const")
    if const is not None:
        transformed["enum"] = [const]
    for key, item in value.items():
        if (
            key == "const"
            or key not in GEMINI_JSON_SCHEMA_KEYWORDS
            or (key == "additionalProperties" and not include_additional_properties)
        ):
            continue
        transformed[key] = _gemini_schema(
            item,
            map_value=key in {"properties", "$defs"},
            include_additional_properties=include_additional_properties,
        )
    return transformed or _unconstrained_json_value_schema()


def provider_output_schema(
    schema: dict[str, Any],
    *,
    provider: str | None,
    inline_references: bool = False,
    include_additional_properties: bool = True,
) -> dict[str, Any]:
    """Return the provider-admission schema; callers still validate against the full stored schema."""
    if provider != "google":
        return schema
    admitted = _inline_local_references(schema, root=schema) if inline_references else schema
    return _gemini_schema(
        admitted,
        include_additional_properties=include_additional_properties,
    )
