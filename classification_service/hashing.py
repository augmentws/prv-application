import hashlib
import json
from typing import Any

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


def canonical_value_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def classification_output_artifact_derivation_key(
    output_derivation_key: str,
    artifact_type: str,
) -> str:
    return canonical_value_hash(
        {
            "contract_version": 1,
            "output_derivation_key": output_derivation_key,
            "artifact_type": artifact_type,
        }
    )
