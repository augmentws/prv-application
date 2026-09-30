import hashlib
import json
from collections.abc import Iterable
from typing import Any

from app.models import MetadataDefinition

ANALYZER_VERSION = 1
INDEX_ANALYZER = f"pvr_edisc_index_v{ANALYZER_VERSION}"
SEARCH_ANALYZER = f"pvr_edisc_search_v{ANALYZER_VERSION}"
QUOTE_ANALYZER = f"pvr_edisc_quote_v{ANALYZER_VERSION}"


def _text(*, exact: bool = False) -> dict[str, Any]:
    mapping: dict[str, Any] = {
        "type": "text",
        "analyzer": INDEX_ANALYZER,
        "search_analyzer": SEARCH_ANALYZER,
        "search_quote_analyzer": QUOTE_ANALYZER,
        "index_options": "offsets",
    }
    if exact:
        mapping["fields"] = {"exact": {"type": "keyword", "ignore_above": 4096}}
    return mapping


def metadata_field_mapping(definition: MetadataDefinition) -> dict[str, Any]:
    if definition.type == "TEXT":
        mapping = _text(exact=definition.facetable)
        if definition.normalize_to_lowercase:
            mapping["meta"] = {"pvr_normalize_to_lowercase": "true"}
        return mapping
    if definition.type == "LONG_TEXT":
        mapping = _text()
        if definition.normalize_to_lowercase:
            mapping["meta"] = {"pvr_normalize_to_lowercase": "true"}
        return mapping
    if definition.type == "INTEGER":
        return {"type": "long"}
    if definition.type == "DECIMAL":
        return {"type": "double"}
    if definition.type == "BOOLEAN":
        return {"type": "boolean"}
    if definition.type == "DATE":
        return {"type": "date", "format": "strict_date"}
    if definition.type == "DATETIME":
        return {"type": "date", "format": "strict_date_optional_time||epoch_millis"}
    if definition.type == "ENUM":
        return {"type": "keyword", "ignore_above": 1024}
    if definition.type == "JSON":
        return {"type": "object", "enabled": False}
    raise ValueError(f"Unsupported metadata type: {definition.type}")


def compile_document_index(
    definitions: Iterable[MetadataDefinition],
    *,
    embedding_dimensions: int = 1024,
) -> dict[str, Any]:
    searchable = [definition for definition in definitions if definition.status == "ACTIVE" and definition.searchable]
    metadata_properties = {definition.key: metadata_field_mapping(definition) for definition in searchable}
    path_hierarchies = {
        definition.key: definition.hierarchy_separator
        for definition in searchable
        if definition.type == "TEXT" and definition.hierarchy_separator is not None
    }
    return {
        "settings": {
            "index": {"knn": True},
            "analysis": {
                "analyzer": {
                    INDEX_ANALYZER: {"type": "custom", "tokenizer": "standard", "filter": ["lowercase"]},
                    SEARCH_ANALYZER: {"type": "custom", "tokenizer": "standard", "filter": ["lowercase"]},
                    QUOTE_ANALYZER: {"type": "custom", "tokenizer": "standard", "filter": ["lowercase"]},
                }
            },
        },
        "mappings": {
            "_meta": {"path_hierarchies": path_hierarchies},
            "dynamic": "strict",
            "properties": {
                "document_id": {"type": "keyword"},
                "tenant_id": {"type": "keyword"},
                "client_id": {"type": "keyword"},
                "matter_id": {"type": "keyword"},
                "source_collection_id": {"type": "keyword"},
                "collection_item_id": {"type": "keyword"},
                "batch_ids": {"type": "keyword"},
                "batch_topics": {
                    "type": "nested",
                    "properties": {
                        "batch_id": {"type": "keyword"},
                        "taxonomy_id": {"type": "keyword"},
                        "topic_key": {"type": "keyword"},
                    },
                },
                "batch_coding": {
                    "type": "nested",
                    "properties": {
                        "batch_id": {"type": "keyword"},
                        "run_id": {"type": "keyword"},
                        "field_id": {"type": "keyword"},
                        "field_key": {"type": "keyword"},
                        "value_type": {"type": "keyword"},
                        "value_keyword": {"type": "keyword", "ignore_above": 4096},
                        "value_text": _text(exact=True),
                        "value_long": {"type": "long"},
                        "value_double": {"type": "double"},
                        "value_boolean": {"type": "boolean"},
                        "value_date": {"type": "date", "format": "strict_date"},
                        "value_datetime": {
                            "type": "date",
                            "format": "strict_date_optional_time||epoch_millis",
                        },
                        "confidence": {"type": "double"},
                        "confidence_kind": {"type": "keyword"},
                        "question_key": {"type": "keyword", "ignore_above": 4096},
                    },
                },
                "hierarchy_facets": {
                    "type": "nested",
                    "properties": {
                        "field": {"type": "keyword"},
                        "node_id": {"type": "keyword"},
                        "parent_id": {"type": "keyword"},
                        "depth": {"type": "integer"},
                        "has_children": {"type": "boolean"},
                    },
                },
                "created_at": {"type": "date", "format": "strict_date_optional_time"},
                "record_type": {"type": "keyword"},
                "processing_status": {"type": "keyword"},
                "original_filename": _text(exact=True),
                "source_path": _text(exact=True),
                "family_id": {"type": "keyword"},
                "custodian_ids": {"type": "keyword"},
                "custodian_names": _text(exact=True),
                "email_from": _text(exact=True),
                "email_to": _text(exact=True),
                "email_cc": _text(exact=True),
                "email_bcc": _text(exact=True),
                "email_subject": _text(exact=True),
                "body_text": _text(),
                "chunks": {
                    "type": "nested",
                    "properties": {
                        "chunk_id": {"type": "keyword"},
                        "ordinal": {"type": "integer"},
                        "char_start": {"type": "long"},
                        "char_end": {"type": "long"},
                        "text": _text(),
                        "embedding": {
                            "type": "knn_vector",
                            "dimension": embedding_dimensions,
                            "method": {
                                "name": "hnsw",
                                "engine": "faiss",
                                "space_type": "cosinesimil",
                                "parameters": {"ef_construction": 128, "m": 16},
                            },
                        },
                    },
                },
                "metadata": {"type": "object", "dynamic": "strict", "properties": metadata_properties},
            },
        },
    }


def schema_hash(index_body: dict[str, Any]) -> str:
    canonical = json.dumps(index_body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def metadata_query_path(definition: MetadataDefinition, *, exact: bool = False) -> str:
    path = f"metadata.{definition.key}"
    if exact and definition.type == "TEXT":
        return f"{path}.exact"
    return path
