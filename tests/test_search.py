import uuid
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
from app.models import Client, Matter, MatterDocumentImportJob, MetadataDefinition, SearchIndexGeneration, Tenant
from app.schemas import MatterSearchRequest
from app.search.mappings import (
    INDEX_ANALYZER,
    QUOTE_ANALYZER,
    SEARCH_ANALYZER,
    compile_document_index,
)
from app.search.query import compile_facet_values_request, compile_search_request, execute_facet_values, execute_search
from app.search.service import SearchIndexManager, build_document_projection


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def definition(
    key: str,
    field_type: str,
    *,
    searchable: bool = True,
    facetable: bool = False,
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
        ]
    )
    analyzers = mapping["settings"]["analysis"]["analyzer"]
    assert set(analyzers) == {INDEX_ANALYZER, SEARCH_ANALYZER, QUOTE_ANALYZER}
    assert all(value["tokenizer"] == "standard" for value in analyzers.values())
    assert all(value["filter"] == ["lowercase"] for value in analyzers.values())

    properties = mapping["mappings"]["properties"]["metadata"]["properties"]
    assert mapping["mappings"]["properties"]["batch_ids"] == {"type": "keyword"}
    assert properties["notes"]["search_analyzer"] == SEARCH_ANALYZER
    assert properties["notes"]["search_quote_analyzer"] == QUOTE_ANALYZER
    assert properties["notes"]["index_options"] == "offsets"
    assert properties["issue"]["fields"]["exact"]["type"] == "keyword"
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
                }
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
        email_sender="sender@example.com",
        email_subject="Project update",
        email_sent_at=None,
        email_received_at=None,
        email_recipients={"TO": [], "CC": [], "BCC": []},
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
    projection = build_document_projection(db, document, [], batch_ids=batch_ids)

    assert projection["body_text"] == "The confidential project is Juniper."
    assert projection["batch_ids"] == [str(value) for value in batch_ids]


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

    def create_index(self, name: str, body: dict) -> None:
        assert body["mappings"]["dynamic"] == "strict"
        self.created.append(name)
        self.existing.add(name)

    def index_exists(self, name: str) -> bool:
        return name in self.existing

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


def test_index_manager_versions_mapping_changes_and_retires_without_deleting(db: Session) -> None:
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
    second = manager.ensure(matter.id)

    assert second.generation == 2
    assert first.status == "RETIRED"
    assert len(fake.created) == 2
    assert fake.alias_actions[-1] == [
        {"remove": {"index": first.index_name, "alias": first.alias_name}},
        {"add": {"index": second.index_name, "alias": second.alias_name}},
    ]
    generations = list(
        db.scalars(select(SearchIndexGeneration).where(SearchIndexGeneration.matter_id == matter.id))
    )
    assert {generation.status for generation in generations} == {"ACTIVE", "RETIRED"}


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
