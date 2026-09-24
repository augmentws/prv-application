import re
from datetime import date, datetime, timezone
from typing import Any

from fastapi import HTTPException, status

from app.models import MetadataDefinition
from app.schemas import (
    DateHistogramInterval,
    MatterDateHistogramResponse,
    MatterFacetValuesResponse,
    MatterSearchFilter,
    MatterSearchRequest,
    MatterSearchResponse,
)
from app.search.client import OpenSearchClient
from app.search.mappings import metadata_query_path

DEFAULT_QUERY_FIELDS = [
    "original_filename^2",
    "email_subject^2",
    "email_from",
    "email_to",
    "email_cc",
    "email_bcc",
    "custodian_names",
    "source_path",
    "body_text",
]
RRF_SEARCH_PIPELINE = "pvr-hybrid-rrf-v1"
SEMANTIC_CANDIDATE_FLOOR = 100
DEFAULT_FACET_SIZE = 8


def _definition_map(definitions: list[MetadataDefinition]) -> dict[str, MetadataDefinition]:
    return {
        definition.key: definition
        for definition in definitions
        if definition.status == "ACTIVE" and definition.searchable
    }


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


def _typed_value(definition: MetadataDefinition, value: Any) -> Any:
    try:
        if definition.type in {"TEXT", "LONG_TEXT", "ENUM"}:
            if not isinstance(value, str):
                raise TypeError
            if definition.type == "ENUM":
                allowed = {item["key"] for item in definition.allowed_values or [] if item.get("active", True)}
                if value not in allowed:
                    raise _bad_request(f"Value '{value}' is not active for field '{definition.key}'")
            return value.lower() if definition.normalize_to_lowercase else value
        if definition.type == "INTEGER":
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError
            return value
        if definition.type == "DECIMAL":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError
            return value
        if definition.type == "BOOLEAN":
            if not isinstance(value, bool):
                raise TypeError
            return value
        if definition.type == "DATE":
            if isinstance(value, datetime):
                raise TypeError
            return value.isoformat() if isinstance(value, date) else date.fromisoformat(value).isoformat()
        if definition.type == "DATETIME":
            if isinstance(value, datetime):
                return value.isoformat()
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except (AttributeError, TypeError, ValueError) as exc:
        raise _bad_request(f"Invalid {definition.type} value for field '{definition.key}'") from exc
    raise _bad_request(f"Field '{definition.key}' does not support search values")


def _filter_clause(search_filter: MatterSearchFilter, definition: MetadataDefinition) -> dict[str, Any]:
    field_type = definition.type
    if field_type == "JSON":
        raise _bad_request(f"Field '{definition.key}' does not support search filters")
    exact_path = metadata_query_path(definition, exact=definition.facetable)
    if search_filter.operator == "EXISTS":
        return {"exists": {"field": metadata_query_path(definition)}}
    if search_filter.operator == "NOT_EXISTS":
        return {"bool": {"must_not": [{"exists": {"field": metadata_query_path(definition)}}]}}
    if search_filter.operator == "EQ":
        value = _typed_value(definition, search_filter.value)
        if field_type in {"TEXT", "LONG_TEXT"} and not definition.facetable:
            return {"match_phrase": {metadata_query_path(definition): value}}
        return {"term": {exact_path: value}}
    if search_filter.operator == "IN":
        if field_type in {"TEXT", "LONG_TEXT"} and not definition.facetable:
            raise _bad_request(f"Field '{definition.key}' must be facetable to use IN")
        clauses: list[dict[str, Any]] = []
        if search_filter.values:
            clauses.append({"terms": {exact_path: [_typed_value(definition, value) for value in search_filter.values]}})
        if search_filter.include_missing:
            clauses.append({"bool": {"must_not": [{"exists": {"field": metadata_query_path(definition)}}]}})
        if len(clauses) == 1:
            return clauses[0]
        return {"bool": {"should": clauses, "minimum_should_match": 1}}
    if search_filter.operator == "RANGE":
        if field_type not in {"INTEGER", "DECIMAL", "DATE", "DATETIME"}:
            raise _bad_request(f"Field '{definition.key}' does not support RANGE")
        bounds = {}
        if search_filter.from_value is not None:
            bounds["gte"] = _typed_value(definition, search_filter.from_value)
        if search_filter.to_value is not None:
            bounds["lte"] = _typed_value(definition, search_filter.to_value)
        return {"range": {metadata_query_path(definition): bounds}}
    raise _bad_request(f"Unsupported filter operator: {search_filter.operator}")


def _keyword_query(filters: list[dict[str, Any]], query: str, query_fields: list[str]) -> dict[str, Any]:
    return {
        "bool": {
            "filter": filters,
            "must": [
                {
                    "simple_query_string": {
                        "query": query,
                        "fields": query_fields,
                        "default_operator": "and",
                        "analyze_wildcard": True,
                    }
                }
            ],
        }
    }


def _semantic_query(
    filters: list[dict[str, Any]],
    query_vector: list[float],
    *,
    candidate_count: int,
    minimum_similarity: float | None = None,
) -> dict[str, Any]:
    vector_query: dict[str, Any] = {
        "vector": query_vector,
        "filter": {"bool": {"filter": filters}},
    }
    if minimum_similarity is None:
        vector_query["k"] = candidate_count
    else:
        vector_query["min_score"] = cosine_similarity_to_opensearch_score(minimum_similarity)
    return {
        "nested": {
            "path": "chunks",
            "query": {
                "knn": {
                    "chunks.embedding": vector_query
                }
            },
            "score_mode": "max",
            "inner_hits": {
                "size": 1,
                "_source": {
                    "includes": [
                        "chunks.chunk_id",
                        "chunks.ordinal",
                        "chunks.char_start",
                        "chunks.char_end",
                        "chunks.text",
                    ]
                },
            },
        }
    }


def cosine_similarity_to_opensearch_score(similarity: float) -> float:
    """Convert cosine similarity to the OpenSearch 3 cosinesimil relevance score."""

    return (1.0 + similarity) / 2.0


def compile_search_request(
    request: MatterSearchRequest,
    definitions: list[MetadataDefinition],
    *,
    tenant_id: str,
    matter_id: str,
    query_vector: list[float] | None = None,
    required_filters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    catalog = _definition_map(definitions)
    filters: list[dict[str, Any]] = [
        {"term": {"tenant_id": tenant_id}},
        {"term": {"matter_id": matter_id}},
        *(required_filters or []),
    ]
    for search_filter in request.filters:
        definition = catalog.get(search_filter.field)
        if definition is None:
            raise _bad_request(f"Unknown or non-searchable field: {search_filter.field}")
        filters.append(_filter_clause(search_filter, definition))

    if request.query_fields:
        query_fields = []
        for key in request.query_fields:
            definition = catalog.get(key)
            if definition is None or definition.type not in {"TEXT", "LONG_TEXT"}:
                raise _bad_request(f"Field '{key}' is not a searchable text field")
            query_fields.append(metadata_query_path(definition))
    else:
        query_fields = [
            *DEFAULT_QUERY_FIELDS,
            *[
                metadata_query_path(definition)
                for definition in catalog.values()
                if definition.type in {"TEXT", "LONG_TEXT"}
            ],
        ]

    if request.search_mode != "KEYWORD" and query_vector is None:
        raise _bad_request(f"{request.search_mode} search requires a query vector")

    if not request.query:
        query: dict[str, Any] = {"bool": {"filter": filters}}
    elif request.search_mode == "KEYWORD":
        query = _keyword_query(filters, request.query, query_fields)
    else:
        semantic_query = _semantic_query(
            filters,
            query_vector or [],
            candidate_count=request.candidate_limit
            or min(10_000, max(SEMANTIC_CANDIDATE_FLOOR, request.offset + request.size)),
            minimum_similarity=request.minimum_similarity,
        )
        if request.search_mode == "SEMANTIC":
            query = semantic_query
        else:
            query = {
                "hybrid": {
                    "queries": [
                        _keyword_query(filters, request.query, query_fields),
                        semantic_query,
                    ]
                }
            }

    aggregations: dict[str, Any] = {}
    for key in request.facets:
        definition = catalog.get(key)
        if definition is None or not definition.facetable:
            raise _bad_request(f"Unknown or non-facetable field: {key}")
        aggregations[key] = {
            "terms": {"field": metadata_query_path(definition, exact=True), "size": DEFAULT_FACET_SIZE}
        }

    sort: list[dict[str, Any]] = []
    for item in request.sort:
        if item.field in {"_score", "created_at"}:
            path = item.field
        else:
            definition = catalog.get(item.field)
            if definition is None or definition.type in {"LONG_TEXT", "JSON"}:
                raise _bad_request(f"Field '{item.field}' is not sortable")
            if definition.type == "TEXT" and not definition.facetable:
                raise _bad_request(f"Text field '{item.field}' must be facetable to sort")
            path = metadata_query_path(definition, exact=definition.type == "TEXT")
        if path == "_score":
            sort.append({path: item.direction.lower()})
        else:
            sort.append({path: {"order": item.direction.lower(), "missing": "_last"}})

    body: dict[str, Any] = {
        "from": request.offset,
        "size": request.size,
        "track_total_hits": True,
        "_source": {"excludes": ["chunks.embedding"]},
        "query": query,
    }
    if request.query and request.search_mode != "SEMANTIC":
        body["highlight"] = {
            "pre_tags": ["<mark>"],
            "post_tags": ["</mark>"],
            "fragment_size": 180,
            "number_of_fragments": 2,
            "fields": {field.split("^")[0]: {} for field in query_fields},
        }
    if aggregations:
        body["aggs"] = aggregations
    if sort:
        body["sort"] = sort
    return body


def compile_facet_values_request(
    request: MatterSearchRequest,
    definitions: list[MetadataDefinition],
    *,
    field: str,
    tenant_id: str,
    matter_id: str,
    value_query: str | None,
    size: int,
    include_values: list[str] | None = None,
    query_vector: list[float] | None = None,
    required_filters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    catalog = _definition_map(definitions)
    definition = catalog.get(field)
    if definition is None or not definition.facetable:
        raise _bad_request(f"Unknown or non-facetable field: {field}")
    scoped = request.model_copy(
        update={
            "filters": [item for item in request.filters if item.field != field],
            "facets": [field],
            "offset": 0,
            "size": 1,
        }
    )
    body = compile_search_request(
        scoped,
        definitions,
        tenant_id=tenant_id,
        matter_id=matter_id,
        query_vector=query_vector,
        required_filters=required_filters,
    )
    body["size"] = 0
    body.pop("highlight", None)
    terms = body["aggs"][field]["terms"]
    body["aggs"][f"{field}__missing"] = {
        "missing": {"field": metadata_query_path(definition, exact=True)}
    }
    terms["size"] = size
    if include_values is not None:
        terms["include"] = [
            value.lower() if definition.normalize_to_lowercase else value
            for value in include_values
        ]
    elif value_query:
        normalized_query = value_query.lower() if definition.normalize_to_lowercase else value_query
        terms["include"] = f".*{re.escape(normalized_query)}.*"
    return body


def batch_topic_filter(*, batch_id: str, taxonomy_id: str, topic_keys: list[str]) -> dict[str, Any]:
    return {
        "nested": {
            "path": "batch_topics",
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"batch_topics.batch_id": batch_id}},
                        {"term": {"batch_topics.taxonomy_id": taxonomy_id}},
                        {"terms": {"batch_topics.topic_key": topic_keys}},
                    ]
                }
            },
        }
    }


def compile_batch_topic_facet_request(
    request: MatterSearchRequest,
    definitions: list[MetadataDefinition],
    *,
    tenant_id: str,
    matter_id: str,
    batch_id: str,
    taxonomy_id: str,
    query_vector: list[float] | None = None,
    required_filters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body = compile_search_request(
        request.model_copy(update={"facets": [], "offset": 0, "size": 1}),
        definitions,
        tenant_id=tenant_id,
        matter_id=matter_id,
        query_vector=query_vector,
        required_filters=required_filters,
    )
    body["size"] = 0
    body.pop("highlight", None)
    body["aggs"] = {
        "batch_topics": {
            "nested": {"path": "batch_topics"},
            "aggs": {
                "scope": {
                    "filter": {
                        "bool": {
                            "filter": [
                                {"term": {"batch_topics.batch_id": batch_id}},
                                {"term": {"batch_topics.taxonomy_id": taxonomy_id}},
                            ]
                        }
                    },
                    "aggs": {"values": {"terms": {"field": "batch_topics.topic_key", "size": 200}}},
                }
            },
        }
    }
    return body


def compile_date_histogram_request(
    request: MatterSearchRequest,
    definitions: list[MetadataDefinition],
    *,
    field: str,
    interval: DateHistogramInterval,
    tenant_id: str,
    matter_id: str,
    query_vector: list[float] | None = None,
    required_filters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    definition = _definition_map(definitions).get(field)
    if definition is None or definition.type not in {"DATE", "DATETIME"}:
        raise _bad_request(f"Unknown or non-date field: {field}")
    scoped = request.model_copy(
        update={
            "filters": [item for item in request.filters if item.field != field],
            "facets": [],
            "offset": 0,
            "size": 1,
        }
    )
    body = compile_search_request(
        scoped,
        definitions,
        tenant_id=tenant_id,
        matter_id=matter_id,
        query_vector=query_vector,
        required_filters=required_filters,
    )
    body["size"] = 0
    body.pop("highlight", None)
    body["aggs"] = {
        field: {
            "date_histogram": {
                "field": metadata_query_path(definition),
                "calendar_interval": interval,
                "time_zone": "UTC",
                "min_doc_count": 0,
            }
        }
    }
    return body


def _best_passage(hit: dict[str, Any]) -> dict[str, Any] | None:
    nested_hits = hit.get("inner_hits", {}).get("chunks", {}).get("hits", {}).get("hits", [])
    if not nested_hits:
        return None
    nested_hit = nested_hits[0]
    source = nested_hit.get("_source") or {}
    if isinstance(source.get("chunks"), dict):
        source = source["chunks"]
    required = {"chunk_id", "ordinal", "char_start", "char_end", "text"}
    if not required.issubset(source):
        return None
    return {
        "chunk_id": source["chunk_id"],
        "ordinal": source["ordinal"],
        "char_start": source["char_start"],
        "char_end": source["char_end"],
        "text": source["text"],
        "score": nested_hit.get("_score"),
    }


def _hit_highlights(hit: dict[str, Any]) -> dict[str, list[str]]:
    highlights = hit.get("highlight") or {}
    if not isinstance(highlights, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for field, fragments in highlights.items():
        if isinstance(fragments, str):
            fragments = [fragments]
        if not isinstance(fragments, list):
            continue
        values = [fragment for fragment in fragments if isinstance(fragment, str)]
        if values:
            normalized[str(field)] = values
    return normalized


def _facet_bucket_value(definition: MetadataDefinition | None, bucket: dict[str, Any]) -> Any:
    if definition is not None and definition.type == "BOOLEAN":
        key_as_string = bucket.get("key_as_string")
        if isinstance(key_as_string, str):
            return key_as_string.casefold() == "true"
        return bucket.get("key") in {True, "1", "true"}
    return bucket["key"]


def execute_search(
    client: OpenSearchClient,
    alias_name: str,
    request: MatterSearchRequest,
    definitions: list[MetadataDefinition],
    *,
    tenant_id: str,
    matter_id: str,
    query_vector: list[float] | None = None,
    required_filters: list[dict[str, Any]] | None = None,
) -> MatterSearchResponse:
    body = compile_search_request(
        request,
        definitions,
        tenant_id=tenant_id,
        matter_id=matter_id,
        query_vector=query_vector,
        required_filters=required_filters,
    )
    if request.search_mode == "HYBRID":
        client.ensure_rrf_search_pipeline(RRF_SEARCH_PIPELINE)
        raw = client.search(alias_name, body, search_pipeline=RRF_SEARCH_PIPELINE)
    else:
        raw = client.search(alias_name, body)
    total = raw.get("hits", {}).get("total", 0)
    if isinstance(total, dict):
        total = total.get("value", 0)
    hits = [
        {
            "document_id": hit["_id"],
            "score": hit.get("_score"),
            "fields": hit.get("_source") or {},
            "highlights": _hit_highlights(hit),
            "best_passage": _best_passage(hit),
        }
        for hit in raw.get("hits", {}).get("hits", [])
    ]
    catalog = _definition_map(definitions)
    facets = {
        key: [
            {"value": _facet_bucket_value(catalog.get(key), bucket), "count": bucket["doc_count"]}
            for bucket in value.get("buckets", [])
        ]
        for key, value in raw.get("aggregations", {}).items()
    }
    return MatterSearchResponse(
        total=int(total),
        took_ms=int(raw.get("took", 0)),
        timed_out=bool(raw.get("timed_out", False)),
        hits=hits,
        facets=facets,
    )


def execute_facet_values(
    client: OpenSearchClient,
    alias_name: str,
    request: MatterSearchRequest,
    definitions: list[MetadataDefinition],
    *,
    field: str,
    tenant_id: str,
    matter_id: str,
    value_query: str | None,
    size: int,
    include_values: list[str] | None = None,
    query_vector: list[float] | None = None,
    required_filters: list[dict[str, Any]] | None = None,
) -> MatterFacetValuesResponse:
    if include_values == []:
        return MatterFacetValuesResponse(field=field, values=[], missing_count=0)
    body = compile_facet_values_request(
        request,
        definitions,
        field=field,
        tenant_id=tenant_id,
        matter_id=matter_id,
        value_query=value_query,
        size=size,
        include_values=include_values,
        query_vector=query_vector,
        required_filters=required_filters,
    )
    if request.search_mode == "HYBRID":
        client.ensure_rrf_search_pipeline(RRF_SEARCH_PIPELINE)
        raw = client.search(alias_name, body, search_pipeline=RRF_SEARCH_PIPELINE)
    else:
        raw = client.search(alias_name, body)
    buckets = raw.get("aggregations", {}).get(field, {}).get("buckets", [])
    missing_count = raw.get("aggregations", {}).get(f"{field}__missing", {}).get("doc_count", 0)
    definition = _definition_map(definitions).get(field)
    return MatterFacetValuesResponse(
        field=field,
        values=[
            {"value": _facet_bucket_value(definition, bucket), "count": bucket["doc_count"]}
            for bucket in buckets
        ],
        missing_count=int(missing_count),
    )


def execute_batch_topic_facets(
    client: OpenSearchClient,
    alias_name: str,
    request: MatterSearchRequest,
    definitions: list[MetadataDefinition],
    *,
    tenant_id: str,
    matter_id: str,
    batch_id: str,
    taxonomy_id: str,
    query_vector: list[float] | None = None,
    required_filters: list[dict[str, Any]] | None = None,
) -> MatterFacetValuesResponse:
    body = compile_batch_topic_facet_request(
        request,
        definitions,
        tenant_id=tenant_id,
        matter_id=matter_id,
        batch_id=batch_id,
        taxonomy_id=taxonomy_id,
        query_vector=query_vector,
        required_filters=required_filters,
    )
    if request.search_mode == "HYBRID":
        client.ensure_rrf_search_pipeline(RRF_SEARCH_PIPELINE)
        raw = client.search(alias_name, body, search_pipeline=RRF_SEARCH_PIPELINE)
    else:
        raw = client.search(alias_name, body)
    buckets = (
        raw.get("aggregations", {})
        .get("batch_topics", {})
        .get("scope", {})
        .get("values", {})
        .get("buckets", [])
    )
    return MatterFacetValuesResponse(
        field="batch_topic",
        values=[{"value": bucket["key"], "count": int(bucket["doc_count"])} for bucket in buckets],
    )


def execute_date_histogram(
    client: OpenSearchClient,
    alias_name: str,
    request: MatterSearchRequest,
    definitions: list[MetadataDefinition],
    *,
    field: str,
    interval: DateHistogramInterval,
    tenant_id: str,
    matter_id: str,
    query_vector: list[float] | None = None,
    required_filters: list[dict[str, Any]] | None = None,
) -> MatterDateHistogramResponse:
    body = compile_date_histogram_request(
        request,
        definitions,
        field=field,
        interval=interval,
        tenant_id=tenant_id,
        matter_id=matter_id,
        query_vector=query_vector,
        required_filters=required_filters,
    )
    if request.search_mode == "HYBRID":
        client.ensure_rrf_search_pipeline(RRF_SEARCH_PIPELINE)
        raw = client.search(alias_name, body, search_pipeline=RRF_SEARCH_PIPELINE)
    else:
        raw = client.search(alias_name, body)
    buckets = raw.get("aggregations", {}).get(field, {}).get("buckets", [])
    return MatterDateHistogramResponse(
        field=field,
        interval=interval,
        buckets=[
            {
                "start": datetime.fromtimestamp(float(bucket["key"]) / 1000, tz=timezone.utc),
                "count": int(bucket["doc_count"]),
            }
            for bucket in buckets
        ],
    )
