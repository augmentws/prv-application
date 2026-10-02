import hashlib
import json

from pydantic import BaseModel


def canonical_model_json(model: BaseModel) -> str:
    """Serialize a contract deterministically for hashing and derivation keys."""
    return json.dumps(
        model.model_dump(by_alias=True, exclude_none=True, mode="json", round_trip=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_model_hash(model: BaseModel) -> str:
    return hashlib.sha256(canonical_model_json(model).encode("utf-8")).hexdigest()
