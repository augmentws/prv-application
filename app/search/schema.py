from dataclasses import dataclass
from typing import Any, Literal

from app.search.mappings import schema_hash

SchemaChangeAction = Literal["NO_CHANGE", "IN_PLACE", "REINDEX_REQUIRED"]

# These root fields are additive and have a separate, bounded backfill path. Do
# not add metadata fields here: making existing metadata searchable requires a
# projection rebuild so historical values are populated.
IN_PLACE_ADDITIVE_ROOT_FIELDS = frozenset({"batch_ids", "batch_topics"})


@dataclass(frozen=True)
class SchemaChangePlan:
    action: SchemaChangeAction
    desired_schema_hash: str
    reasons: tuple[str, ...] = ()
    mapping_update: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "desired_schema_hash": self.desired_schema_hash,
            "reasons": list(self.reasons),
        }


class SearchReindexRequired(RuntimeError):
    def __init__(self, plan: SchemaChangePlan) -> None:
        self.plan = plan
        super().__init__("Search schema changes require a confirmed full reindex")


def plan_schema_change(current: dict[str, Any], desired: dict[str, Any]) -> SchemaChangePlan:
    desired_hash = schema_hash(desired)
    if current == desired:
        return SchemaChangePlan("NO_CHANGE", desired_hash)

    reasons: list[str] = []
    if current.get("settings") != desired.get("settings"):
        reasons.append("Index analysis, vector, or other creation-time settings changed.")

    current_mappings = current.get("mappings") if isinstance(current.get("mappings"), dict) else {}
    desired_mappings = desired.get("mappings") if isinstance(desired.get("mappings"), dict) else {}
    current_properties = current_mappings.get("properties") if isinstance(current_mappings.get("properties"), dict) else {}
    desired_properties = desired_mappings.get("properties") if isinstance(desired_mappings.get("properties"), dict) else {}
    current_options = {key: value for key, value in current_mappings.items() if key != "properties"}
    desired_options = {key: value for key, value in desired_mappings.items() if key != "properties"}
    if current_options != desired_options:
        reasons.append("Index mapping behavior changed.")

    removed = sorted(set(current_properties) - set(desired_properties))
    changed = sorted(
        key
        for key in set(current_properties) & set(desired_properties)
        if current_properties[key] != desired_properties[key]
    )
    added = sorted(set(desired_properties) - set(current_properties))
    if removed:
        reasons.append(f"Mapped fields were removed or renamed: {', '.join(removed)}.")
    if changed:
        reasons.append(f"Existing field mappings changed: {', '.join(changed)}.")

    unsafe_additions = sorted(set(added) - IN_PLACE_ADDITIVE_ROOT_FIELDS)
    if unsafe_additions:
        reasons.append(
            "New fields require historical document projection: " + ", ".join(unsafe_additions) + "."
        )

    if reasons:
        return SchemaChangePlan("REINDEX_REQUIRED", desired_hash, tuple(reasons))
    if added:
        return SchemaChangePlan(
            "IN_PLACE",
            desired_hash,
            ("Compatible additive fields: " + ", ".join(added) + ".",),
            {"properties": {key: desired_properties[key] for key in added}},
        )
    return SchemaChangePlan(
        "REINDEX_REQUIRED",
        desired_hash,
        ("The schema difference is not classified as a safe in-place update.",),
    )
