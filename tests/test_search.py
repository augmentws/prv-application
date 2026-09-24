import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifact_gateway import SearchItemSnapshot
from app.config import Settings
from app.embeddings.chunking import TextChunk
from app.embeddings.parquet import write_chunk_set, write_vector_set
from app.models import (
    Client,
    Matter,
    MatterDocument,
    MatterDocumentImportJob,
    MatterEmbeddingBatch,
    MatterEmbeddingJob,
    MetadataDefinition,
    SearchIndexGeneration,
    SearchProjectionOperation,
    Tenant,
    User,
)
from app.schemas import MatterSearchRequest
from app.search.client import OpenSearchBulkError, OpenSearchError, is_retryable_opensearch_error
from app.search.mappings import (
    INDEX_ANALYZER,
    QUOTE_ANALYZER,
    SEARCH_ANALYZER,
    compile_document_index,
    schema_hash,
)
from app.search.query import (
    batch_topic_filter,
    compile_batch_topic_facet_request,
    compile_date_histogram_request,
    compile_facet_values_request,
    compile_search_request,
    execute_date_histogram,
    execute_facet_values,
    execute_search,
)
from app.search.schema import SearchReindexRequired
from app.search.service import SearchIndexManager, build_document_projection


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "error",
    [
        OpenSearchError("OpenSearch returned 429: too many requests", status_code=429),
        OpenSearchError("OpenSearch returned 507: insufficient storage", status_code=507),
        OpenSearchError("cluster_block_exception: disk usage exceeded flood-stage watermark"),
        OpenSearchError("circuit_breaking_exception: data too large"),
        OpenSearchError("OpenSearch request failed: connection reset"),
    ],
)
def test_opensearch_pressure_errors_are_retryable(error: OpenSearchError) -> None:
    assert is_retryable_opensearch_error(error)


def test_permanent_opensearch_errors_are_not_retried() -> None:
    assert not is_retryable_opensearch_error(OpenSearchError("mapper_parsing_exception: bad field"))
    assert not is_retryable_opensearch_error(ValueError("bad request"))


def definition(
    key: str,
    field_type: str,
    *,
    searchable: bool = True,
    facetable: bool = False,
    normalize_to_lowercase: bool = False,
) -> MetadataDefinition:
    return MetadataDefinition(
        id=uuid.uuid4(),
        matter_id=uuid.uuid4(),
        key=key,
        display_name=key,
        type=field_type,
        cardinality="SINGLE",
        value_source="ASSERTED",
        assertion_policy="IMMEDIATE",
        resolution_policy="EXPLICIT_ONLY",
        searchable=searchable,
        facetable=facetable,
        normalize_to_lowercase=normalize_to_lowercase,
        reviewable=True,
        ai_assignable=False,
        status="ACTIVE",
    )


def test_mapping_uses_versioned_ediscovery_analyzers_and_numeric_types() -> None:
    mapping = compile_document_index(
        [
            definition("notes", "LONG_TEXT"),
            definition("issue", "TEXT", facetable=True),
            definition("page_count", "INTEGER"),
            definition("confidence", "DECIMAL"),
            definition("private_value", "TEXT", searchable=False),
            definition("email_from", "TEXT", facetable=True, normalize_to_lowercase=True),
        ]
    )
    analyzers = mapping["settings"]["analysis"]["analyzer"]
    assert set(analyzers) == {INDEX_ANALYZER, SEARCH_ANALYZER, QUOTE_ANALYZER}
    assert all(value["tokenizer"] == "standard" for value in analyzers.values())
    assert all(value["filter"] == ["lowercase"] for value in analyzers.values())

    properties = mapping["mappings"]["properties"]["metadata"]["properties"]
    vector_method = mapping["mappings"]["properties"]["chunks"]["properties"]["embedding"]["method"]
    assert mapping["mappings"]["properties"]["batch_ids"] == {"type": "keyword"}
    assert mapping["mappings"]["properties"]["batch_topics"] == {
        "type": "nested",
        "properties": {
            "batch_id": {"type": "keyword"},
            "taxonomy_id": {"type": "keyword"},
            "topic_key": {"type": "keyword"},
        },
    }
    assert vector_method == {
        "name": "hnsw",
        "engine": "faiss",
        "space_type": "cosinesimil",
        "parameters": {"ef_construction": 128, "m": 16},
    }
    assert properties["notes"]["search_analyzer"] == SEARCH_ANALYZER
    assert properties["notes"]["search_quote_analyzer"] == QUOTE_ANALYZER
    assert properties["notes"]["index_options"] == "offsets"
    assert properties["issue"]["fields"]["exact"]["type"] == "keyword"
    assert properties["email_from"]["meta"] == {"pvr_normalize_to_lowercase": "true"}
    assert properties["page_count"] == {"type": "long"}
    assert properties["confidence"] == {"type": "double"}
    assert "private_value" not in properties


def test_query_compiler_injects_scope_and_validates_matter_fields() -> None:
    definitions = [
        definition("issue", "TEXT", facetable=True),
        definition("sent_at", "DATETIME", facetable=True),
    ]
    request = MatterSearchRequest.model_validate(
        {
            "query": '"price coordination"',
            "query_fields": ["issue"],
            "filters": [{"field": "issue", "operator": "IN", "values": ["conduct"]}],
            "facets": ["issue"],
        }
    )
    batch_filter = {"term": {"batch_ids": "batch-1"}}
    body = compile_search_request(
        request,
        definitions,
        tenant_id="tenant-1",
        matter_id="matter-1",
        required_filters=[batch_filter],
    )

    assert {"term": {"tenant_id": "tenant-1"}} in body["query"]["bool"]["filter"]
    assert {"term": {"matter_id": "matter-1"}} in body["query"]["bool"]["filter"]
    assert batch_filter in body["query"]["bool"]["filter"]
    assert {"terms": {"metadata.issue.exact": ["conduct"]}} in body["query"]["bool"]["filter"]
    assert body["aggs"]["issue"]["terms"]["field"] == "metadata.issue.exact"
    assert body["aggs"]["issue"]["terms"]["size"] == 8
    assert body["query"]["bool"]["must"][0]["simple_query_string"]["fields"] == ["metadata.issue"]
    assert body["highlight"] == {
        "pre_tags": ["<mark>"],
        "post_tags": ["</mark>"],
        "fragment_size": 180,
        "number_of_fragments": 2,
        "fields": {"metadata.issue": {}},
    }

    with pytest.raises(HTTPException, match="Unknown or non-searchable field"):
        compile_search_request(
            MatterSearchRequest.model_validate(
                {"filters": [{"field": "unknown", "operator": "EQ", "value": "x"}]}
            ),
            definitions,
            tenant_id="tenant-1",
            matter_id="matter-1",
        )

    with pytest.raises(HTTPException, match="Invalid DATETIME value"):
        compile_search_request(
            MatterSearchRequest.model_validate(
                {"filters": [{"field": "sent_at", "operator": "RANGE", "from": "not-a-date"}]}
            ),
            definitions,
            tenant_id="tenant-1",
            matter_id="matter-1",
        )


def test_query_compiler_normalizes_lowercase_filter_values() -> None:
    definitions = [definition("email_from", "TEXT", facetable=True, normalize_to_lowercase=True)]
    request = MatterSearchRequest.model_validate(
        {"filters": [{"field": "email_from", "operator": "IN", "values": ["Mixed.Case@Example.COM"]}]}
    )

    body = compile_search_request(request, definitions, tenant_id="tenant-1", matter_id="matter-1")

    assert {"terms": {"metadata.email_from.exact": ["mixed.case@example.com"]}} in body["query"]["bool"]["filter"]


def test_query_compiler_supports_missing_and_value_or_missing_filters() -> None:
    definitions = [definition("issue", "TEXT", facetable=True)]

    missing = compile_search_request(
        MatterSearchRequest.model_validate({"filters": [{"field": "issue", "operator": "NOT_EXISTS"}]}),
        definitions,
        tenant_id="tenant-1",
        matter_id="matter-1",
    )
    assert {
        "bool": {"must_not": [{"exists": {"field": "metadata.issue"}}]}
    } in missing["query"]["bool"]["filter"]

    value_or_missing = compile_search_request(
        MatterSearchRequest.model_validate(
            {
                "filters": [
                    {
                        "field": "issue",
                        "operator": "IN",
                        "values": ["pricing"],
                        "include_missing": True,
                    }
                ]
            }
        ),
        definitions,
        tenant_id="tenant-1",
        matter_id="matter-1",
    )
    assert {
        "bool": {
            "should": [
                {"terms": {"metadata.issue.exact": ["pricing"]}},
                {"bool": {"must_not": [{"exists": {"field": "metadata.issue"}}]}},
            ],
            "minimum_should_match": 1,
        }
    } in value_or_missing["query"]["bool"]["filter"]


def test_facet_value_query_is_limited_searchable_and_self_excluding() -> None:
    privilege = definition("privilege", "ENUM", facetable=True)
    privilege.allowed_values = [{"key": "privileged", "label": "Privileged", "active": True}]
    definitions = [definition("issue", "TEXT", facetable=True), privilege]
    request = MatterSearchRequest.model_validate(
        {
            "filters": [
                {"field": "issue", "operator": "IN", "values": ["pricing"]},
                {"field": "privilege", "operator": "IN", "values": ["privileged"]},
            ]
        }
    )
    body = compile_facet_values_request(
        request,
        definitions,
        field="issue",
        tenant_id="tenant-1",
        matter_id="matter-1",
        value_query="trade",
        size=20,
        required_filters=[{"term": {"batch_ids": "batch-1"}}],
    )

    assert body["size"] == 0
    assert "highlight" not in body
    assert {"terms": {"metadata.issue.exact": ["pricing"]}} not in body["query"]["bool"]["filter"]
    assert {"terms": {"metadata.privilege": ["privileged"]}} in body["query"]["bool"]["filter"]
    assert {"term": {"batch_ids": "batch-1"}} in body["query"]["bool"]["filter"]
    assert body["aggs"]["issue"]["terms"]["size"] == 20
    assert body["aggs"]["issue"]["terms"]["include"] == ".*trade.*"
    assert body["aggs"]["issue__missing"] == {"missing": {"field": "metadata.issue.exact"}}


def test_facet_value_query_normalizes_lowercase_search_text() -> None:
    email = definition("email_from", "TEXT", facetable=True, normalize_to_lowercase=True)

    body = compile_facet_values_request(
        MatterSearchRequest(),
        [email],
        field="email_from",
        tenant_id="tenant-1",
        matter_id="matter-1",
        value_query="Example.COM",
        size=20,
        include_values=None,
    )

    assert body["aggs"]["email_from"]["terms"]["include"] == ".*example\\.com.*"


def test_batch_topic_filter_and_facet_keep_batch_taxonomy_and_topic_in_one_nested_scope() -> None:
    topic_filter = batch_topic_filter(
        batch_id="batch-1",
        taxonomy_id="taxonomy-2",
        topic_keys=["nonrenewal", "mortgages"],
    )
    nested_filters = topic_filter["nested"]["query"]["bool"]["filter"]
    assert nested_filters == [
        {"term": {"batch_topics.batch_id": "batch-1"}},
        {"term": {"batch_topics.taxonomy_id": "taxonomy-2"}},
        {"terms": {"batch_topics.topic_key": ["nonrenewal", "mortgages"]}},
    ]

    body = compile_batch_topic_facet_request(
        MatterSearchRequest(),
        [],
        tenant_id="tenant-1",
        matter_id="matter-1",
        batch_id="batch-1",
        taxonomy_id="taxonomy-2",
        required_filters=[{"term": {"batch_ids": "batch-1"}}],
    )
    scoped = body["aggs"]["batch_topics"]["aggs"]["scope"]
    assert scoped["filter"]["bool"]["filter"] == [
        {"term": {"batch_topics.batch_id": "batch-1"}},
        {"term": {"batch_topics.taxonomy_id": "taxonomy-2"}},
    ]
    assert scoped["aggs"]["values"]["terms"]["field"] == "batch_topics.topic_key"


def test_date_histogram_is_calendar_bucketed_and_self_excluding() -> None:
    definitions = [definition("sent_at", "DATETIME"), definition("issue", "TEXT", facetable=True)]
    request = MatterSearchRequest.model_validate(
        {
            "filters": [
                {"field": "sent_at", "operator": "RANGE", "from": "2025-01-01T00:00:00Z"},
                {"field": "issue", "operator": "IN", "values": ["pricing"]},
            ]
        }
    )
    body = compile_date_histogram_request(
        request,
        definitions,
        field="sent_at",
        interval="month",
        tenant_id="tenant-1",
        matter_id="matter-1",
    )

    assert body["size"] == 0
    assert "highlight" not in body
    assert {"range": {"metadata.sent_at": {"gte": "2025-01-01T00:00:00+00:00"}}} not in body["query"]["bool"]["filter"]
    assert {"terms": {"metadata.issue.exact": ["pricing"]}} in body["query"]["bool"]["filter"]
    assert body["aggs"]["sent_at"]["date_histogram"] == {
        "field": "metadata.sent_at",
        "calendar_interval": "month",
        "time_zone": "UTC",
        "min_doc_count": 0,
    }


def test_query_compiler_builds_filtered_nested_semantic_and_hybrid_queries() -> None:
    definitions = [definition("issue", "TEXT", facetable=True)]
    query_vector = [0.25] * 32
    semantic = MatterSearchRequest.model_validate(
        {
            "query": "messages about concealed pricing discussions",
            "search_mode": "SEMANTIC",
            "filters": [{"field": "issue", "operator": "IN", "values": ["conduct"]}],
            "sort": [{"field": "_score", "direction": "DESC"}],
            "size": 25,
        }
    )
    semantic_body = compile_search_request(
        semantic,
        definitions,
        tenant_id="tenant-1",
        matter_id="matter-1",
        query_vector=query_vector,
        required_filters=[{"term": {"batch_ids": "batch-1"}}],
    )

    nested = semantic_body["query"]["nested"]
    knn = nested["query"]["knn"]["chunks.embedding"]
    assert knn["vector"] == query_vector
    assert knn["k"] == 100
    assert {"terms": {"metadata.issue.exact": ["conduct"]}} in knn["filter"]["bool"]["filter"]
    assert {"term": {"batch_ids": "batch-1"}} in knn["filter"]["bool"]["filter"]
    assert nested["score_mode"] == "max"
    assert nested["inner_hits"]["size"] == 1
    assert "highlight" not in semantic_body

    fixed_candidates = semantic.model_copy(update={"candidate_limit": 1600, "offset": 350})
    fixed_candidate_body = compile_search_request(
        fixed_candidates,
        definitions,
        tenant_id="tenant-1",
        matter_id="matter-1",
        query_vector=query_vector,
    )
    assert fixed_candidate_body["query"]["nested"]["query"]["knn"]["chunks.embedding"]["k"] == 1600

    thresholded = semantic.model_copy(update={"minimum_similarity": 0.75})
    thresholded_body = compile_search_request(
        thresholded,
        definitions,
        tenant_id="tenant-1",
        matter_id="matter-1",
        query_vector=query_vector,
    )
    thresholded_knn = thresholded_body["query"]["nested"]["query"]["knn"]["chunks.embedding"]
    assert thresholded_knn["min_score"] == pytest.approx(0.875)
    assert "k" not in thresholded_knn

    hybrid = semantic.model_copy(update={"search_mode": "HYBRID"})
    hybrid_body = compile_search_request(
        hybrid,
        definitions,
        tenant_id="tenant-1",
        matter_id="matter-1",
        query_vector=query_vector,
    )
    clauses = hybrid_body["query"]["hybrid"]["queries"]
    assert clauses[0]["bool"]["must"][0]["simple_query_string"]["query"] == hybrid.query
    assert clauses[1]["nested"]["query"]["knn"]["chunks.embedding"]["vector"] == query_vector

    with pytest.raises(ValueError, match="SEMANTIC search requires a query"):
        MatterSearchRequest.model_validate({"search_mode": "SEMANTIC"})
    with pytest.raises(ValueError, match="supported only for SEMANTIC"):
        MatterSearchRequest.model_validate(
            {"query": "pricing", "search_mode": "HYBRID", "minimum_similarity": 0.75}
        )
    with pytest.raises(ValueError, match="less than or equal to 1"):
        MatterSearchRequest.model_validate(
            {"query": "pricing", "search_mode": "SEMANTIC", "minimum_similarity": 1.1}
        )


class FakeSearchClient:
    def search(self, index: str, body: dict) -> dict:
        assert index == "matter-documents"
        assert body["track_total_hits"] is True
        return {
            "took": 7,
            "timed_out": False,
            "hits": {
                "total": {"value": 1, "relation": "eq"},
                "hits": [
                    {
                        "_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "_score": 2.5,
                        "_source": {"original_filename": "memo.txt"},
                        "highlight": {"email_subject": ["<em>price</em>"]},
                        "inner_hits": {
                            "chunks": {
                                "hits": {
                                    "hits": [
                                        {
                                            "_score": 0.91,
                                            "_source": {
                                                "chunk_id": "chunk-1",
                                                "ordinal": 0,
                                                "char_start": 10,
                                                "char_end": 58,
                                                "text": "The pricing discussion was moved off channel.",
                                            },
                                        }
                                    ]
                                }
                            }
                        },
                    }
                ],
            },
            "aggregations": {"issue": {"buckets": [{"key": "conduct", "doc_count": 1}]}},
        }


def test_search_response_hides_opensearch_shape() -> None:
    response = execute_search(
        FakeSearchClient(),  # type: ignore[arg-type]
        "matter-documents",
        MatterSearchRequest(query="price", facets=["issue"]),
        [definition("issue", "ENUM", facetable=True)],
        tenant_id="tenant-1",
        matter_id="matter-1",
    )
    assert response.total == 1
    assert response.hits[0].fields["original_filename"] == "memo.txt"
    assert response.hits[0].highlights == {"email_subject": ["<em>price</em>"]}
    assert response.hits[0].best_passage is not None
    assert response.hits[0].best_passage.text == "The pricing discussion was moved off channel."
    assert response.facets["issue"][0].value == "conduct"


class FakeBooleanFacetClient:
    def search(self, index: str, body: dict) -> dict:
        return {
            "hits": {"total": {"value": 0}, "hits": []},
            "aggregations": {
                "key_document": {
                    "buckets": [
                        {"key": 1, "key_as_string": "true", "doc_count": 4},
                        {"key": 0, "key_as_string": "false", "doc_count": 2},
                    ]
                },
                "key_document__missing": {"doc_count": 3},
            },
        }


def test_boolean_facet_values_use_boolean_bucket_labels() -> None:
    response = execute_facet_values(
        FakeBooleanFacetClient(),  # type: ignore[arg-type]
        "matter-documents",
        MatterSearchRequest(),
        [definition("key_document", "BOOLEAN", facetable=True)],
        field="key_document",
        tenant_id="tenant-1",
        matter_id="matter-1",
        value_query=None,
        size=8,
    )

    assert [(item.value, item.count) for item in response.values] == [(True, 4), (False, 2)]
    assert response.missing_count == 3


class FakeDateHistogramClient:
    def search(self, index: str, body: dict) -> dict:
        assert index == "matter-documents"
        assert body["aggs"]["sent_at"]["date_histogram"]["calendar_interval"] == "year"
        return {
            "aggregations": {
                "sent_at": {
                    "buckets": [
                        {"key": 1_735_689_600_000, "doc_count": 4},
                        {"key": 1_767_225_600_000, "doc_count": 2},
                    ]
                }
            }
        }


def test_date_histogram_response_hides_opensearch_shape() -> None:
    response = execute_date_histogram(
        FakeDateHistogramClient(),  # type: ignore[arg-type]
        "matter-documents",
        MatterSearchRequest(),
        [definition("sent_at", "DATETIME")],
        field="sent_at",
        interval="year",
        tenant_id="tenant-1",
        matter_id="matter-1",
    )

    assert response.field == "sent_at"
    assert [(bucket.start.date().isoformat(), bucket.count) for bucket in response.buckets] == [
        ("2025-01-01", 4),
        ("2026-01-01", 2),
    ]


class FakeHybridSearchClient(FakeSearchClient):
    def __init__(self) -> None:
        self.pipeline_id: str | None = None

    def ensure_rrf_search_pipeline(self, pipeline_id: str) -> None:
        self.pipeline_id = pipeline_id

    def search(self, index: str, body: dict, *, search_pipeline: str | None = None) -> dict:
        assert search_pipeline == self.pipeline_id == "pvr-hybrid-rrf-v1"
        assert "hybrid" in body["query"]
        return super().search(index, body)


def test_hybrid_search_uses_rrf_pipeline() -> None:
    client = FakeHybridSearchClient()
    response = execute_search(
        client,  # type: ignore[arg-type]
        "matter-documents",
        MatterSearchRequest(query="price coordination", search_mode="HYBRID"),
        [],
        tenant_id="tenant-1",
        matter_id="matter-1",
        query_vector=[0.1] * 32,
    )

    assert client.pipeline_id == "pvr-hybrid-rrf-v1"
    assert response.total == 1


def test_document_projection_includes_artifact_body_text(monkeypatch: pytest.MonkeyPatch) -> None:
    matter_id = uuid.uuid4()
    client_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    job_id = uuid.uuid4()
    document = SimpleNamespace(
        id=uuid.uuid4(),
        matter_id=matter_id,
        source_collection_id=uuid.uuid4(),
        collection_item_id=uuid.uuid4(),
        added_by_import_job_id=job_id,
        created_at=SimpleNamespace(isoformat=lambda: "2026-09-14T12:00:00+00:00"),
    )
    matter = SimpleNamespace(id=matter_id, client_id=client_id, client=SimpleNamespace(tenant_id=tenant_id))
    import_job = SimpleNamespace(created_by_user_id=actor_id)
    snapshot = SearchItemSnapshot(
        item_id=document.collection_item_id,
        source_item_id="email-1",
        record_type="EMAIL",
        original_filename="email-1.eml",
        original_extension=".eml",
        original_source_path="mailbox/email-1.eml",
        source_created_at=None,
        source_modified_at=None,
        family_id=None,
        processing_status="READY",
        custodian_ids=[],
        email_sender="Sender@Example.COM",
        email_subject="Project update",
        email_sent_at=None,
        email_received_at=None,
        email_recipients={"TO": ["Recipient@Example.COM"], "CC": [], "BCC": []},
        native_sha256="a" * 64,
        native_byte_length=100,
        page_count=None,
        raw_metadata={},
        unmapped_metadata={},
        body_text="The confidential project is Juniper.",
    )
    db = Mock()
    db.get.side_effect = lambda model, identifier: (
        matter if model is Matter and identifier == matter_id else import_job if model is MatterDocumentImportJob else None
    )
    db.scalars.return_value = []
    monkeypatch.setattr("app.search.service.get_search_item_snapshot", lambda **_: snapshot)
    monkeypatch.setattr("app.search.service.load_current_chunk_artifacts", lambda **_: None)
    monkeypatch.setattr("app.search.service.current_metadata_values", lambda *_: {})

    batch_ids = [uuid.uuid4(), uuid.uuid4()]
    definitions = [
        definition("email_from", "TEXT", facetable=True, normalize_to_lowercase=True),
        definition("email_to", "TEXT", facetable=True, normalize_to_lowercase=True),
    ]
    definitions[1].cardinality = "MULTIPLE"
    projection = build_document_projection(db, document, definitions, batch_ids=batch_ids)

    assert projection["body_text"] == "The confidential project is Juniper."
    assert projection["batch_ids"] == [str(value) for value in batch_ids]
    assert projection["email_from"] == "sender@example.com"
    assert projection["email_to"] == ["recipient@example.com"]
    assert projection["metadata"]["email_from"] == "sender@example.com"
    assert projection["metadata"]["email_to"] == ["recipient@example.com"]


def test_document_projection_attaches_nested_chunk_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    matter_id = uuid.uuid4()
    client_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    job_id = uuid.uuid4()
    document = SimpleNamespace(
        id=uuid.uuid4(),
        matter_id=matter_id,
        source_collection_id=uuid.uuid4(),
        collection_item_id=uuid.uuid4(),
        added_by_import_job_id=job_id,
        created_at=SimpleNamespace(isoformat=lambda: "2026-09-14T12:00:00+00:00"),
    )
    matter = SimpleNamespace(id=matter_id, client_id=client_id, client=SimpleNamespace(tenant_id=tenant_id))
    import_job = SimpleNamespace(created_by_user_id=actor_id)
    snapshot = SearchItemSnapshot(
        item_id=document.collection_item_id,
        source_item_id="memo-1",
        record_type="FILE",
        original_filename="memo.txt",
        original_extension=".txt",
        original_source_path=None,
        source_created_at=None,
        source_modified_at=None,
        family_id=None,
        processing_status="READY",
        custodian_ids=[],
        email_sender=None,
        email_subject=None,
        email_sent_at=None,
        email_received_at=None,
        email_recipients={"TO": [], "CC": [], "BCC": []},
        native_sha256="a" * 64,
        native_byte_length=50,
        page_count=None,
        raw_metadata={},
        unmapped_metadata={},
        body_text="A short legal memorandum.",
    )
    chunk = TextChunk("c" * 64, 0, 0, 25, "A short legal memorandum.")
    chunk_set = write_chunk_set([chunk], {})
    vector_set = write_vector_set([chunk], [[0.5] * 1024], dimensions=1024, metadata={})
    db = Mock()
    db.get.side_effect = lambda model, identifier: (
        matter if model is Matter and identifier == matter_id else import_job if model is MatterDocumentImportJob else None
    )
    db.scalars.return_value = []
    monkeypatch.setattr("app.search.service.get_search_item_snapshot", lambda **_: snapshot)
    monkeypatch.setattr("app.search.service.load_current_chunk_artifacts", lambda **_: (chunk_set, vector_set))
    monkeypatch.setattr("app.search.service.current_metadata_values", lambda *_: {})

    projection = build_document_projection(db, document, [])

    assert projection["chunks"][0]["chunk_id"] == "c" * 64
    assert projection["chunks"][0]["text"] == "A short legal memorandum."
    assert len(projection["chunks"][0]["embedding"]) == 1024


class FakeIndexClient:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.existing: set[str] = set()
        self.alias_actions: list[list[dict]] = []
        self.aliases: dict[str, set[str]] = {}
        self.refreshed: list[str] = []
        self.mapping_updates: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.indexed_document_count = 0

    def close(self) -> None:
        pass

    def create_index(self, name: str, body: dict) -> None:
        assert body["mappings"]["dynamic"] == "strict"
        self.created.append(name)
        self.existing.add(name)

    def index_exists(self, name: str) -> bool:
        return name in self.existing

    def update_mapping(self, name: str, body: dict) -> None:
        self.mapping_updates.append((name, body))

    def delete_index(self, name: str) -> None:
        self.deleted.append(name)
        self.existing.discard(name)

    def resolve_indices(self, pattern: str) -> list[str]:
        prefix = pattern.removesuffix("*")
        return sorted(name for name in self.existing if name.startswith(prefix))

    def update_aliases(self, actions: list[dict]) -> None:
        self.alias_actions.append(actions)
        for action in actions:
            if "remove" in action:
                value = action["remove"]
                self.aliases.setdefault(value["alias"], set()).discard(value["index"])
            else:
                value = action["add"]
                self.aliases.setdefault(value["alias"], set()).add(value["index"])

    def alias_indices(self, alias: str) -> list[str]:
        return sorted(self.aliases.get(alias, set()))

    def bulk(self, index: str, operations) -> None:
        assert list(operations) == []

    def refresh(self, index: str) -> None:
        self.refreshed.append(index)

    def count(self, index: str) -> int:
        return self.indexed_document_count


def test_embedding_job_indexing_checkpoints_and_retries_only_failed_documents(
    db: Session,
    root_admin: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_record = Client(tenant_id=root_admin.tenant_id, name="Embedding Index Client")
    db.add(client_record)
    db.flush()
    matter = Matter(client_id=client_record.id, name="Embedding Index Matter")
    db.add(matter)
    db.flush()
    source_collection_id = uuid.uuid4()
    import_job = MatterDocumentImportJob(
        matter_id=matter.id,
        source_collection_id=source_collection_id,
        selection_type="EXPLICIT",
        selection={},
        selection_summary="Embedding index test",
        status="COMPLETED",
        workflow_id=f"import:{uuid.uuid4()}",
        created_by_user_id=root_admin.id,
    )
    db.add(import_job)
    db.flush()
    documents = [
        MatterDocument(
            matter_id=matter.id,
            source_collection_id=source_collection_id,
            collection_item_id=uuid.uuid4(),
            added_by_import_job_id=import_job.id,
        )
        for _ in range(3)
    ]
    db.add_all(documents)
    embedding_job = MatterEmbeddingJob(
        matter_id=matter.id,
        status="RUNNING",
        workflow_id=f"embedding:{uuid.uuid4()}",
        configuration_hash="f" * 64,
        configuration={},
        embedding_model="test-model",
        embedding_model_revision="revision-1",
        embedding_dimensions=32,
        embedding_normalized=True,
        created_by_user_id=root_admin.id,
    )
    db.add(embedding_job)
    db.flush()
    db.add_all(
        [
            MatterEmbeddingBatch(
                job_id=embedding_job.id,
                batch_number=0,
                document_ids=[str(document.id) for document in documents[:2]],
                status="COMPLETED",
                item_count=2,
                processed_count=2,
                embedded_count=2,
            ),
            MatterEmbeddingBatch(
                job_id=embedding_job.id,
                batch_number=1,
                document_ids=[str(documents[2].id)],
                status="COMPLETED",
                item_count=1,
                processed_count=1,
                embedded_count=1,
            ),
        ]
    )
    operation = SearchProjectionOperation(
        matter_id=matter.id,
        kind="DOCUMENT_UPSERT",
        payload={"embedding_job_id": str(embedding_job.id)},
        status="RUNNING",
        workflow_id=f"embedding-index-job:{embedding_job.id}",
        created_by_user_id=root_admin.id,
    )
    db.add(operation)
    db.commit()

    fake = Mock()
    fake.count.return_value = 3
    generation = SimpleNamespace(index_name="matter-embedding-index", document_count=0)
    manager = SearchIndexManager(db, fake, Settings(search_bulk_batch_size=2))
    monkeypatch.setattr(manager, "ensure", Mock(return_value=generation))
    bulk_upsert = Mock(
        side_effect=[
            None,
            OpenSearchBulkError(
                "cluster_block_exception: disk usage exceeded flood-stage watermark",
                failed_document_ids=[str(documents[2].id)],
            ),
        ]
    )
    monkeypatch.setattr(manager, "_bulk_upsert", bulk_upsert)

    with pytest.raises(OpenSearchBulkError):
        manager.upsert_embedding_job(matter.id, embedding_job.id, operation=operation)

    assert bulk_upsert.call_count == 2
    assert [len(call.args[1]) for call in bulk_upsert.call_args_list] == [2, 1]
    assert operation.payload["next_document_offset"] == 3
    assert operation.payload["retry_document_ids"] == [str(documents[2].id)]
    fake.refresh.assert_not_called()

    bulk_upsert.reset_mock(side_effect=True)
    indexed_count = manager.upsert_embedding_job(matter.id, embedding_job.id, operation=operation)

    assert indexed_count == 3
    bulk_upsert.assert_called_once()
    assert {document.id for document in bulk_upsert.call_args.args[1]} == {documents[2].id}
    assert operation.payload["next_document_offset"] == 3
    assert operation.payload["retry_document_ids"] == []
    fake.refresh.assert_called_once_with("matter-embedding-index")
    fake.count.assert_called_once_with("matter-embedding-index")
    assert generation.document_count == 3


def test_full_rebuild_records_progress_after_each_document_batch(
    db: Session,
    root_admin: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_record = Client(tenant_id=root_admin.tenant_id, name="Rebuild Progress Client")
    db.add(client_record)
    db.flush()
    matter = Matter(client_id=client_record.id, name="Rebuild Progress Matter")
    db.add(matter)
    db.flush()
    source_collection_id = uuid.uuid4()
    import_job = MatterDocumentImportJob(
        matter_id=matter.id,
        source_collection_id=source_collection_id,
        selection_type="EXPLICIT",
        selection={},
        selection_summary="Rebuild progress test",
        status="COMPLETED",
        workflow_id=f"import:{uuid.uuid4()}",
        created_by_user_id=root_admin.id,
    )
    db.add(import_job)
    db.flush()
    db.add_all(
        [
            MatterDocument(
                matter_id=matter.id,
                source_collection_id=source_collection_id,
                collection_item_id=uuid.uuid4(),
                added_by_import_job_id=import_job.id,
            )
            for _ in range(3)
        ]
    )
    db.commit()

    fake = Mock()
    manager = SearchIndexManager(db, fake, Settings(search_bulk_batch_size=2))
    bulk_upsert = Mock()
    monkeypatch.setattr(manager, "_bulk_upsert", bulk_upsert)
    progress: list[dict] = []
    monkeypatch.setattr(
        "app.search.service._save_rebuild_progress",
        lambda _operation_id, **values: progress.append(values),
    )

    count = manager._index_all_documents(
        "matter-rebuild-index",
        matter,
        [],
        operation_id=uuid.uuid4(),
        total_documents=3,
    )

    assert count == 3
    assert [len(call.args[1]) for call in bulk_upsert.call_args_list] == [2, 1]
    assert [(item["phase"], item["processed_documents"]) for item in progress] == [
        ("INDEXING_DOCUMENTS", 2),
        ("INDEXING_DOCUMENTS", 3),
        ("REFRESHING_INDEX", 3),
    ]
    fake.refresh.assert_called_once_with("matter-rebuild-index")


def test_bulk_projection_hydrates_documents_concurrently_in_input_order(
    db: Session,
    root_admin: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_record = Client(tenant_id=root_admin.tenant_id, name="Concurrent Projection Client")
    db.add(client_record)
    db.flush()
    matter = Matter(client_id=client_record.id, name="Concurrent Projection Matter")
    db.add(matter)
    db.flush()
    source_collection_id = uuid.uuid4()
    import_job = MatterDocumentImportJob(
        matter_id=matter.id,
        source_collection_id=source_collection_id,
        selection_type="EXPLICIT",
        selection={},
        selection_summary="Concurrent projection test",
        status="COMPLETED",
        workflow_id=f"import:{uuid.uuid4()}",
        created_by_user_id=root_admin.id,
    )
    db.add(import_job)
    db.flush()
    documents = [
        MatterDocument(
            matter_id=matter.id,
            source_collection_id=source_collection_id,
            collection_item_id=uuid.uuid4(),
            added_by_import_job_id=import_job.id,
        )
        for _ in range(6)
    ]
    db.add_all(documents)
    db.commit()

    lock = threading.Lock()
    active_workers = 0
    maximum_workers = 0

    def projection(_db, document, _definitions, **_kwargs):
        nonlocal active_workers, maximum_workers
        with lock:
            active_workers += 1
            maximum_workers = max(maximum_workers, active_workers)
        time.sleep(0.02)
        with lock:
            active_workers -= 1
        return {"document_id": str(document.id)}

    monkeypatch.setattr("app.search.service.build_document_projection", projection)
    documents_by_id = {document.id: document for document in documents}

    class WorkerSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _model, document_id):
            return documents_by_id.get(document_id)

    monkeypatch.setattr(
        "app.search.service.sessionmaker",
        lambda **_kwargs: WorkerSession,
    )
    real_get_bind = db.get_bind

    def projection_bind(*args, **kwargs):
        if args or kwargs:
            return real_get_bind(*args, **kwargs)
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    monkeypatch.setattr(db, "get_bind", projection_bind)
    fake = Mock()
    captured_operations: list[tuple[str, str, dict]] = []
    fake.bulk.side_effect = lambda _index, operations: captured_operations.extend(operations)
    manager = SearchIndexManager(
        db,
        fake,
        Settings(search_bulk_batch_size=10, search_projection_document_concurrency=3),
    )

    manager._bulk_upsert("concurrent-index", documents, [])

    assert maximum_workers >= 2
    assert [operation[1] for operation in captured_operations] == [str(document.id) for document in documents]


def test_index_manager_adds_metadata_mapping_and_backfills_in_place(db: Session) -> None:
    tenant = Tenant(slug="search-tenant", name="Search Tenant", status="ACTIVE", is_root=True)
    db.add(tenant)
    db.flush()
    client_record = Client(tenant_id=tenant.id, name="Search Client", status="ACTIVE")
    db.add(client_record)
    db.flush()
    matter = Matter(client_id=client_record.id, name="Search Matter", status="ACTIVE")
    db.add(matter)
    db.flush()
    notes = definition("notes", "LONG_TEXT")
    notes.matter_id = matter.id
    db.add(notes)
    db.commit()

    fake = FakeIndexClient()
    manager = SearchIndexManager(db, fake, Settings(opensearch_index_prefix="test"))  # type: ignore[arg-type]
    first = manager.ensure(matter.id)
    assert first.generation == 1
    assert first.status == "ACTIVE"

    added = definition("issue", "ENUM", facetable=True)
    added.matter_id = matter.id
    added.allowed_values = [{"key": "conduct", "label": "Conduct", "active": True}]
    db.add(added)
    db.commit()
    backfill = Mock(wraps=manager._backfill_metadata_fields)
    manager._backfill_metadata_fields = backfill

    same = manager.ensure(matter.id)

    assert same.id == first.id
    assert len(fake.created) == 1
    assert fake.mapping_updates == [
        (
            first.index_name,
            {"properties": {"metadata": {"properties": {"issue": {"type": "keyword", "ignore_above": 1024}}}}},
        )
    ]
    backfill.assert_called_once()
    assert backfill.call_args.args[0] == first.index_name
    assert backfill.call_args.args[1] == matter
    assert {definition.key for definition in backfill.call_args.args[2]} == {"issue", "notes"}
    assert backfill.call_args.args[3] == ("issue",)
    assert fake.deleted == []


def test_index_manager_requires_confirmation_for_existing_metadata_mapping_change(db: Session) -> None:
    tenant = Tenant(slug="changed-metadata-tenant", name="Changed Metadata Tenant", status="ACTIVE", is_root=True)
    db.add(tenant)
    db.flush()
    client_record = Client(tenant_id=tenant.id, name="Changed Metadata Client", status="ACTIVE")
    db.add(client_record)
    db.flush()
    matter = Matter(client_id=client_record.id, name="Changed Metadata Matter", status="ACTIVE")
    db.add(matter)
    db.flush()
    issue = definition("issue", "TEXT")
    issue.matter_id = matter.id
    db.add(issue)
    db.commit()

    fake = FakeIndexClient()
    manager = SearchIndexManager(db, fake, Settings(opensearch_index_prefix="test"))  # type: ignore[arg-type]
    manager.ensure(matter.id)
    issue.normalize_to_lowercase = True
    db.commit()

    with pytest.raises(SearchReindexRequired) as required:
        manager.ensure(matter.id)

    assert "metadata.issue" in " ".join(required.value.plan.reasons)


def test_index_manager_applies_allowlisted_additive_mapping_in_place(db: Session) -> None:
    tenant = Tenant(slug="in-place-tenant", name="In-place Tenant", status="ACTIVE", is_root=True)
    db.add(tenant)
    db.flush()
    client_record = Client(tenant_id=tenant.id, name="In-place Client", status="ACTIVE")
    db.add(client_record)
    db.flush()
    matter = Matter(client_id=client_record.id, name="In-place Matter", status="ACTIVE")
    db.add(matter)
    db.commit()

    fake = FakeIndexClient()
    manager = SearchIndexManager(db, fake, Settings(opensearch_index_prefix="test"))  # type: ignore[arg-type]
    active = manager.ensure(matter.id)
    legacy = deepcopy(active.schema_snapshot)
    legacy["mappings"]["properties"].pop("batch_ids")
    active.schema_snapshot = legacy
    active.schema_hash = schema_hash(legacy)
    db.commit()

    same = manager.ensure(matter.id)

    assert same.id == active.id
    assert len(fake.created) == 1
    assert fake.mapping_updates == [(active.index_name, {"properties": {"batch_ids": {"type": "keyword"}}})]


def test_index_manager_repairs_an_alias_with_multiple_generations(db: Session) -> None:
    tenant = Tenant(slug="repair-tenant", name="Repair Tenant", status="ACTIVE", is_root=True)
    db.add(tenant)
    db.flush()
    client_record = Client(tenant_id=tenant.id, name="Repair Client", status="ACTIVE")
    db.add(client_record)
    db.flush()
    matter = Matter(client_id=client_record.id, name="Repair Matter", status="ACTIVE")
    db.add(matter)
    db.flush()
    notes = definition("notes", "LONG_TEXT")
    notes.matter_id = matter.id
    db.add(notes)
    db.commit()

    fake = FakeIndexClient()
    manager = SearchIndexManager(db, fake, Settings(opensearch_index_prefix="test"))  # type: ignore[arg-type]
    active = manager.ensure(matter.id)
    stale_index = f"{active.alias_name}-v000000"
    fake.aliases[active.alias_name].add(stale_index)

    same = manager.ensure(matter.id)

    assert same.id == active.id
    assert fake.aliases[active.alias_name] == {active.index_name}
    assert fake.alias_actions[-1] == [
        {"remove": {"index": stale_index, "alias": active.alias_name}},
        {"remove": {"index": active.index_name, "alias": active.alias_name}},
        {"add": {"index": active.index_name, "alias": active.alias_name}},
    ]


def test_index_manager_skips_an_orphaned_physical_index(db: Session) -> None:
    tenant = Tenant(slug="orphan-tenant", name="Orphan Tenant", status="ACTIVE", is_root=True)
    db.add(tenant)
    db.flush()
    client_record = Client(tenant_id=tenant.id, name="Orphan Client", status="ACTIVE")
    db.add(client_record)
    db.flush()
    matter = Matter(client_id=client_record.id, name="Orphan Matter", status="ACTIVE")
    db.add(matter)
    db.flush()
    notes = definition("notes", "LONG_TEXT")
    notes.matter_id = matter.id
    db.add(notes)
    db.commit()

    fake = FakeIndexClient()
    orphaned = f"test-matter-{matter.id.hex}-documents-v000001"
    fake.existing.add(orphaned)
    manager = SearchIndexManager(db, fake, Settings(opensearch_index_prefix="test"))  # type: ignore[arg-type]

    active = manager.ensure(matter.id)

    assert active.generation == 2
    assert active.index_name.endswith("-v000002")
    assert fake.created == [active.index_name]
    assert fake.deleted == [orphaned]


def test_matter_creation_queues_index_and_search_waits_for_active_generation(
    client: TestClient,
    root_token: str,
) -> None:
    headers = auth(root_token)
    tenant_id = client.get("/v1/auth/me", headers=headers).json()["tenant_id"]
    created_client = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=headers,
        json={"name": "Search Test Client"},
    ).json()
    matter = client.post(
        f"/v1/clients/{created_client['id']}/matters",
        headers=headers,
        json={"name": "Search Test Matter"},
    ).json()

    operations = client.get(f"/v1/matters/{matter['id']}/search-operations", headers=headers)
    assert operations.status_code == 200
    assert [(item["kind"], item["status"]) for item in operations.json()] == [("SCHEMA_SYNC", "QUEUED")]

    search_response = client.post(
        f"/v1/matters/{matter['id']}/search",
        headers=headers,
        json={"query": "price coordination"},
    )
    assert search_response.status_code == 409
    assert search_response.json()["error"]["message"] == "Matter search index is not ready"


def test_search_index_status_reports_missing_index_on_configured_server(
    client: TestClient,
    root_token: str,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = auth(root_token)
    tenant_id = client.get("/v1/auth/me", headers=headers).json()["tenant_id"]
    created_client = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=headers,
        json={"name": "Missing Index Client"},
    ).json()
    matter = client.post(
        f"/v1/clients/{created_client['id']}/matters",
        headers=headers,
        json={"name": "Missing Index Matter"},
    ).json()
    generation = SearchIndexGeneration(
        matter_id=uuid.UUID(matter["id"]),
        generation=1,
        index_name="pvr-missing-v000001",
        alias_name="pvr-missing",
        schema_hash="a" * 64,
        schema_snapshot={},
        status="ACTIVE",
        document_count=178_093,
        activated_at=datetime.now(timezone.utc),
    )
    db.add(generation)
    db.commit()
    fake = FakeIndexClient()
    monkeypatch.setattr("app.routers.search.OpenSearchClient", lambda _settings: fake)

    response = client.get(f"/v1/matters/{matter['id']}/search-indexes", headers=headers)

    assert response.status_code == 200, response.text
    [reported] = response.json()
    assert reported["document_count"] == 178_093
    assert reported["physical_index_exists"] is False
    assert reported["alias_points_to_index"] is False
    assert reported["live_document_count"] == 0
    assert reported["verification_error"] is None


def test_reindex_confirmation_requeues_the_schema_operation(
    client: TestClient,
    root_token: str,
    db: Session,
) -> None:
    headers = auth(root_token)
    tenant_id = client.get("/v1/auth/me", headers=headers).json()["tenant_id"]
    created_client = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=headers,
        json={"name": "Confirmation Client"},
    ).json()
    matter = client.post(
        f"/v1/clients/{created_client['id']}/matters",
        headers=headers,
        json={"name": "Confirmation Matter"},
    ).json()
    db.expire_all()
    operation = db.scalar(
        select(SearchProjectionOperation).where(SearchProjectionOperation.matter_id == uuid.UUID(matter["id"]))
    )
    assert operation is not None
    operation.status = "AWAITING_USER"
    operation.payload = {
        "schema_change": {
            "action": "REINDEX_REQUIRED",
            "desired_schema_hash": "a" * 64,
            "reasons": ["Existing field mappings changed: metadata."],
        }
    }
    db.commit()

    response = client.post(
        f"/v1/matters/{matter['id']}/search-operations/{operation.id}/confirm-reindex",
        headers=headers,
    )

    assert response.status_code == 202, response.text
    confirmed = response.json()
    assert confirmed["kind"] == "REBUILD"
    assert confirmed["status"] == "QUEUED"
    assert confirmed["payload"]["confirmation"]["confirmed_by_user_id"]


def test_failed_document_upserts_can_be_requeued_in_bulk(
    client: TestClient,
    root_token: str,
    db: Session,
) -> None:
    headers = auth(root_token)
    tenant_id = client.get("/v1/auth/me", headers=headers).json()["tenant_id"]
    created_client = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=headers,
        json={"name": "Retry Client"},
    ).json()
    matter = client.post(
        f"/v1/clients/{created_client['id']}/matters",
        headers=headers,
        json={"name": "Retry Matter"},
    ).json()
    matter_id = uuid.UUID(matter["id"])
    failed = SearchProjectionOperation(
        matter_id=matter_id,
        kind="DOCUMENT_UPSERT",
        payload={"document_ids": [str(uuid.uuid4()), str(uuid.uuid4())]},
        status="FAILED",
        workflow_id=f"search-projection:{uuid.uuid4()}",
        created_by_user_id=None,
        attempt_count=5,
        error_message="unknown encoding: windows-3839",
    )
    unrelated = SearchProjectionOperation(
        matter_id=matter_id,
        kind="REBUILD",
        payload={},
        status="FAILED",
        workflow_id=f"search-projection:{uuid.uuid4()}",
        created_by_user_id=None,
        attempt_count=5,
        error_message="rebuild failed",
    )
    db.add_all([failed, unrelated])
    db.commit()
    failed_id = failed.id
    original_workflow_id = failed.workflow_id

    response = client.post(
        f"/v1/matters/{matter_id}/search-operations/retry-failed",
        headers=headers,
    )

    assert response.status_code == 202, response.text
    assert response.json() == {
        "requeued_operation_count": 1,
        "requeued_document_count": 2,
    }
    db.expire_all()
    retried = db.get(SearchProjectionOperation, failed_id)
    assert retried is not None
    assert retried.status == "QUEUED"
    assert retried.workflow_id != original_workflow_id
    assert retried.error_message is None
    assert retried.started_at is None
    assert retried.payload["retry_history"][-1]["workflow_id"] == original_workflow_id
    assert retried.payload["retry_history"][-1]["attempt_count"] == 5
    assert db.get(SearchProjectionOperation, unrelated.id).status == "FAILED"

    repeated = client.post(
        f"/v1/matters/{matter_id}/search-operations/retry-failed",
        headers=headers,
    )
    assert repeated.status_code == 202
    assert repeated.json() == {
        "requeued_operation_count": 0,
        "requeued_document_count": 0,
    }


def test_durable_workflow_errors_can_be_requeued_when_operation_is_stranded_running(
    client: TestClient,
    root_token: str,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = auth(root_token)
    tenant_id = client.get("/v1/auth/me", headers=headers).json()["tenant_id"]
    created_client = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=headers,
        json={"name": "Durable Retry Client"},
    ).json()
    matter = client.post(
        f"/v1/clients/{created_client['id']}/matters",
        headers=headers,
        json={"name": "Durable Retry Matter"},
    ).json()
    matter_id = uuid.UUID(matter["id"])
    errored = SearchProjectionOperation(
        matter_id=matter_id,
        kind="DOCUMENT_UPSERT",
        payload={"document_ids": [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]},
        status="RUNNING",
        workflow_id=f"search-projection:{uuid.uuid4()}",
        created_by_user_id=None,
        attempt_count=1,
        started_at=datetime.now(timezone.utc),
    )
    active = SearchProjectionOperation(
        matter_id=matter_id,
        kind="DOCUMENT_UPSERT",
        payload={"document_ids": [str(uuid.uuid4())]},
        status="RUNNING",
        workflow_id=f"search-projection:{uuid.uuid4()}",
        created_by_user_id=None,
        attempt_count=1,
        started_at=datetime.now(timezone.utc),
    )
    db.add_all([errored, active])
    db.commit()
    errored_id = errored.id
    active_id = active.id
    original_workflow_id = errored.workflow_id

    monkeypatch.setattr(
        "app.routers.search._durable_error_workflow_statuses",
        lambda _settings, workflow_ids: {
            workflow_id: "ERROR" for workflow_id in workflow_ids if workflow_id == original_workflow_id
        },
    )

    summary = client.get(
        f"/v1/matters/{matter_id}/search-operations/retryable",
        headers=headers,
    )
    assert summary.status_code == 200, summary.text
    assert summary.json() == {
        "requeued_operation_count": 1,
        "requeued_document_count": 3,
    }

    response = client.post(
        f"/v1/matters/{matter_id}/search-operations/retry-failed",
        headers=headers,
    )
    assert response.status_code == 202, response.text
    assert response.json() == {
        "requeued_operation_count": 1,
        "requeued_document_count": 3,
    }
    db.expire_all()
    retried = db.get(SearchProjectionOperation, errored_id)
    assert retried is not None
    assert retried.status == "QUEUED"
    assert retried.workflow_id != original_workflow_id
    assert retried.payload["retry_history"][-1]["durable_workflow_status"] == "ERROR"
    assert db.get(SearchProjectionOperation, active_id).status == "RUNNING"
