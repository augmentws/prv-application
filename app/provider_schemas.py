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


def _gemini_schema(value: Any, *, map_value: bool = False) -> Any:
    if isinstance(value, list):
        return [_gemini_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    if map_value:
        return {key: _gemini_schema(item) for key, item in value.items()}
    if not value:
        return _unconstrained_json_value_schema()

    transformed: dict[str, Any] = {}
    const = value.get("const")
    if const is not None:
        transformed["enum"] = [const]
    for key, item in value.items():
        if key == "const" or key not in GEMINI_JSON_SCHEMA_KEYWORDS:
            continue
        transformed[key] = _gemini_schema(item, map_value=key in {"properties", "$defs"})
    return transformed or _unconstrained_json_value_schema()


def provider_output_schema(schema: dict[str, Any], *, provider: str | None) -> dict[str, Any]:
    """Return the provider-admission schema; callers still validate against the full stored schema."""
    if provider != "google":
        return schema
    return _gemini_schema(schema)
