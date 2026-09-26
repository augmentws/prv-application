import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    MatterEmbeddingJob,
    MatterTopicCluster,
    MatterTopicJob,
    MetadataDefinition,
    SearchIndexGeneration,
)
from app.topic_clustering import (
    _automatic_kmeans_labels,
    _cluster_discriminative_terms,
    _evenly_spaced_indexes,
    _sample,
    _scoped_documents,
    _topic_sample_key,
    discover_topics,
)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _matter(client: TestClient, token: str) -> dict:
    headers = auth(token)
    tenant_id = client.get("/v1/auth/me", headers=headers).json()["tenant_id"]
    client_record = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=headers,
        json={"name": "Topic Client"},
    ).json()
    return client.post(
        f"/v1/clients/{client_record['id']}/matters",
        headers=headers,
        json={"name": "Topic Matter"},
    ).json()


def test_topic_job_requires_embeddings(client: TestClient, root_token: str) -> None:
    matter = _matter(client, root_token)
    response = client.post(
        f"/v1/matters/{matter['id']}/topic-jobs",
        headers=auth(root_token),
        json={"operating_mode": "AUTO", "sample_size": 100},
    )
    assert response.status_code == 409
    assert "embeddings" in response.json()["error"]["message"]


def test_create_list_and_cancel_topic_job(
    client: TestClient,
    root_token: str,
    root_admin,
    db: Session,
) -> None:
    matter = _matter(client, root_token)
    embedding_job = MatterEmbeddingJob(
        matter_id=uuid.UUID(matter["id"]),
        status="COMPLETED",
        workflow_id=f"test-embedding:{uuid.uuid4()}",
        configuration_hash="a" * 64,
        configuration={"embedding": {"dimensions": 8}},
        embedding_model="test-model",
        embedding_dimensions=8,
        embedding_normalized=True,
        total_count=1,
        processed_count=1,
        embedded_count=1,
        chunk_count=5,
        created_by_user_id=root_admin.id,
    )
    db.add(embedding_job)
    db.commit()

    created = client.post(
        f"/v1/matters/{matter['id']}/topic-jobs",
        headers=auth(root_token),
        json={
            "operating_mode": "FIXED",
            "sample_size": 500,
            "requested_topic_count": 8,
            "destination_mode": "NEW_FIELD",
            "new_field_key": "communication_topics",
            "new_field_name": "Communication topics",
        },
    )
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "QUEUED"
    assert created.json()["operating_mode"] == "FIXED"
    assert created.json()["sample_size"] == 500
    assert created.json()["requested_topic_count"] == 8
    assert created.json()["assignment_mode"] == "REPLACE"
    assert created.json()["scope_mode"] == "ENTIRE_MATTER"
    assert created.json()["saved_search_id"] is None
    assert created.json()["saved_search_name"] is None
    assert created.json()["destination_mode"] == "NEW_FIELD"
    assert created.json()["destination_metadata_definition_id"] is None
    assert created.json()["destination_field_key"] == "communication_topics"
    assert created.json()["destination_field_name"] == "Communication topics"
    assert db.query(MetadataDefinition).filter_by(
        matter_id=uuid.UUID(matter["id"]), key="communication_topics"
    ).first() is None

    duplicate = client.post(
        f"/v1/matters/{matter['id']}/topic-jobs",
        headers=auth(root_token),
        json={"operating_mode": "AUTO", "sample_size": 100},
    )
    assert duplicate.status_code == 409

    listed = client.get(f"/v1/matters/{matter['id']}/topic-jobs", headers=auth(root_token))
    assert listed.status_code == 200
    assert [job["id"] for job in listed.json()] == [created.json()["id"]]

    canceled = client.post(
        f"/v1/matters/{matter['id']}/topic-jobs/{created.json()['id']}/cancel",
        headers=auth(root_token),
    )
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "CANCELED"


def test_topic_job_snapshots_keyword_saved_search_scope(
    client: TestClient,
    root_token: str,
    root_admin,
    db: Session,
) -> None:
    matter = _matter(client, root_token)
    matter_id = uuid.UUID(matter["id"])
    embedding_job = MatterEmbeddingJob(
        matter_id=matter_id,
        status="COMPLETED",
        workflow_id=f"test-embedding:{uuid.uuid4()}",
        configuration_hash="b" * 64,
        configuration={"embedding": {"dimensions": 8}},
        embedding_model="test-model",
        embedding_dimensions=8,
        embedding_normalized=True,
        total_count=1,
        processed_count=1,
        embedded_count=1,
        chunk_count=5,
        created_by_user_id=root_admin.id,
    )
    generation = SearchIndexGeneration(
        matter_id=matter_id,
        generation=1,
        index_name=f"matter-{matter_id}-000001",
        alias_name=f"matter-{matter_id}",
        schema_hash="c" * 64,
        status="ACTIVE",
        schema_snapshot={},
        activated_at=datetime.now(timezone.utc),
    )
    db.add_all([embedding_job, generation])
    db.commit()

    saved = client.post(
        f"/v1/matters/{matter_id}/saved-searches",
        headers=auth(root_token),
        json={
            "name": "Form mail",
            "visibility": "PRIVATE",
            "search": {
                "query": "unsubscribe",
                "search_mode": "KEYWORD",
                "filters": [],
                "offset": 25,
                "size": 50,
            },
        },
    )
    assert saved.status_code == 201, saved.text

    created = client.post(
        f"/v1/matters/{matter_id}/topic-jobs",
        headers=auth(root_token),
        json={
            "operating_mode": "AUTO",
            "sample_size": 500,
            "scope_mode": "EXCLUDE_SAVED_SEARCH",
            "saved_search_id": saved.json()["id"],
        },
    )
    assert created.status_code == 202, created.text
    assert created.json()["scope_mode"] == "EXCLUDE_SAVED_SEARCH"
    assert created.json()["saved_search_id"] == saved.json()["id"]
    assert created.json()["saved_search_name"] == "Form mail"

    db.expire_all()
    job = db.get(MatterTopicJob, uuid.UUID(created.json()["id"]))
    assert job is not None
    scope = job.configuration["document_scope"]
    assert scope["mode"] == "EXCLUDE_SAVED_SEARCH"
    assert scope["saved_search_name"] == "Form mail"
    assert scope["search"]["query"] == "unsubscribe"
    assert scope["search"]["offset"] == 0
    assert scope["search_index_name"] == generation.index_name


@pytest.mark.parametrize(
    ("mode", "expected_indexes"),
    [
        ("INCLUDE_SAVED_SEARCH", [0, 2]),
        ("EXCLUDE_SAVED_SEARCH", [1]),
    ],
)
def test_saved_search_scope_filters_topic_documents(monkeypatch, mode, expected_indexes) -> None:
    document_ids = [uuid.uuid4() for _ in range(3)]
    documents = [SimpleNamespace(id=document_id) for document_id in document_ids]

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def scalars(self, statement):
            del statement
            self.calls += 1
            return documents if self.calls == 1 else []

    class FakeOpenSearchClient:
        def __init__(self, settings):
            del settings

        def search(self, index_name, body):
            assert index_name == "matter-snapshot-000001"
            assert body["sort"] == [{"document_id": "asc"}]
            return {
                "hits": {
                    "hits": [
                        {"_source": {"document_id": str(document_ids[0])}, "sort": [str(document_ids[0])]},
                        {"_source": {"document_id": str(document_ids[2])}, "sort": [str(document_ids[2])]},
                    ]
                }
            }

        def close(self):
            return None

    monkeypatch.setattr("app.topic_clustering.OpenSearchClient", FakeOpenSearchClient)
    monkeypatch.setattr(
        "app.topic_clustering.compile_search_request",
        lambda request, definitions, **kwargs: {"from": 0, "size": request.size},
    )
    job = SimpleNamespace(
        matter_id=uuid.uuid4(),
        matter=SimpleNamespace(client=SimpleNamespace(tenant_id=uuid.uuid4())),
        configuration={
            "document_scope": {
                "mode": mode,
                "search": {"query": "unsubscribe", "search_mode": "KEYWORD"},
                "search_index_name": "matter-snapshot-000001",
            }
        },
    )

    scoped = _scoped_documents(FakeSession(), job)

    assert [document.id for document in scoped] == [document_ids[index] for index in expected_indexes]


def test_fixed_topic_discovery_returns_named_clusters() -> None:
    vectors = [
        [1.0, 0.0],
        [0.95, 0.05],
        [0.9, 0.1],
        [0.0, 1.0],
        [0.05, 0.95],
        [0.1, 0.9],
    ]
    texts = [
        "executive compensation bonus plan",
        "annual executive bonus compensation",
        "incentive compensation for executives",
        "natural gas pipeline pricing",
        "energy market gas pipeline",
        "pipeline transportation and gas prices",
    ]
    topics = discover_topics(
        vectors,
        texts,
        operating_mode="FIXED",
        requested_topic_count=2,
        max_topics=50,
        random_seed=42,
    )
    assert len(topics) == 2
    assert all(
        topic.name and topic.keywords and topic.representative_excerpts and len(topic.centroid) == 2
        for topic in topics
    )


def test_automatic_topic_discovery_rejects_a_single_cluster(monkeypatch) -> None:
    class SingleCluster:
        def __init__(self, **kwargs):
            del kwargs

        def fit_predict(self, values):
            return np.zeros(len(values), dtype=np.int32)

    class PassthroughUMAP:
        def __init__(self, **kwargs):
            del kwargs

        def fit_transform(self, values):
            return values

    monkeypatch.setattr("app.topic_clustering.HDBSCAN", SingleCluster)
    monkeypatch.setattr("app.topic_clustering.UMAP", PassthroughUMAP)
    vectors = [[float(index), float(index % 3), 1.0] for index in range(12)]
    texts = [f"Substantive sample content for automatic topic number {index} " * 3 for index in range(12)]

    with pytest.raises(ValueError, match="fewer than two useful topics"):
        discover_topics(
            vectors,
            texts,
            operating_mode="AUTO",
            requested_topic_count=None,
            max_topics=50,
            random_seed=42,
            clustering_version=2,
        )


def test_legacy_automatic_topic_discovery_keeps_single_cluster_behavior(monkeypatch) -> None:
    class SingleCluster:
        def __init__(self, **kwargs):
            del kwargs

        def fit_predict(self, values):
            return np.zeros(len(values), dtype=np.int32)

    monkeypatch.setattr("app.topic_clustering.HDBSCAN", SingleCluster)
    vectors = [[float(index), float(index % 3), 1.0] for index in range(12)]
    texts = [f"Legacy sample content number {index} " * 4 for index in range(12)]

    topics = discover_topics(
        vectors,
        texts,
        operating_mode="AUTO",
        requested_topic_count=None,
        max_topics=50,
        random_seed=42,
        clustering_version=1,
    )
    assert len(topics) == 1


def test_topic_names_use_cluster_discriminative_terms() -> None:
    texts = [
        "Enron ECT subject thanks natural gas pipeline capacity",
        "Enron ECT subject thanks pipeline transportation capacity",
        "Enron ECT subject thanks executive compensation bonus",
        "Enron ECT subject thanks annual incentive compensation",
    ]
    labels = np.asarray([0, 0, 1, 1])

    terms = _cluster_discriminative_terms(texts, labels, [0, 1])

    assert "enron" not in terms[0]
    assert "thanks" not in terms[1]
    assert any("pipeline" in term for term in terms[0])
    assert any("compensation" in term for term in terms[1])


def test_automatic_topic_count_uses_best_scored_candidate(monkeypatch) -> None:
    class CandidateKMeans:
        def __init__(self, *, n_clusters, **kwargs):
            del kwargs
            self.n_clusters = n_clusters

        def fit_predict(self, values):
            return np.arange(len(values), dtype=np.int32) % self.n_clusters

    scores = {10: 0.10, 20: 0.40, 30: 0.20}
    monkeypatch.setattr("app.topic_clustering.MiniBatchKMeans", CandidateKMeans)
    monkeypatch.setattr(
        "app.topic_clustering.silhouette_score",
        lambda values, labels, metric: scores[len(np.unique(labels))],
    )
    reduced = np.asarray([[float(index), float(index % 7)] for index in range(300)])

    labels = _automatic_kmeans_labels(reduced, max_topics=30, random_seed=42)

    assert len(np.unique(labels)) == 20


def test_topic_sample_filters_short_and_duplicate_chunks(monkeypatch) -> None:
    repeated = "Repeated substantive content " * 5
    documents = [SimpleNamespace(id="first"), SimpleNamespace(id="second")]
    chunks_by_document = {
        "first": [
            "too short",
            repeated,
            "First unique substantive section " * 5,
            "Middle unique substantive section " * 5,
            "Final unique substantive section " * 5,
        ],
        "second": [
            repeated.upper(),
            "Second document substantive content " * 5,
        ],
    }

    def load_chunks(job, document):
        del job
        texts = chunks_by_document[document.id]
        return [], texts, [[float(index), 1.0] for index in range(len(texts))]

    monkeypatch.setattr("app.topic_clustering._load_document_chunks", load_chunks)
    job = SimpleNamespace(sample_size=10, configuration={"random_seed": 42})
    vectors, texts = _sample(job, documents)

    assert len(vectors) == len(texts) == 5
    assert "too short" not in texts
    assert sum(_topic_sample_key(text) == _topic_sample_key(repeated) for text in texts) == 1
    assert any(text.startswith("Final unique") for text in texts)


def test_evenly_spaced_topic_chunk_indexes() -> None:
    assert _evenly_spaced_indexes(10, 4) == [0, 3, 6, 9]
    assert _evenly_spaced_indexes(10, 1) == [5]
    assert _evenly_spaced_indexes(3, 5) == [0, 1, 2]


@pytest.mark.parametrize("destination_mode", ["TOPICS", "EXISTING_FIELD", "NEW_FIELD"])
def test_reviewed_topics_are_published_only_after_apply(
    client: TestClient,
    root_token: str,
    root_admin,
    db: Session,
    destination_mode: str,
) -> None:
    matter = _matter(client, root_token)
    matter_id = uuid.UUID(matter["id"])
    embedding_job = MatterEmbeddingJob(
        matter_id=matter_id,
        status="COMPLETED",
        workflow_id=f"test-embedding:{uuid.uuid4()}",
        configuration_hash="b" * 64,
        configuration={"embedding": {"dimensions": 2}},
        embedding_model="test-model",
        embedding_dimensions=2,
        embedding_normalized=True,
        total_count=1,
        processed_count=1,
        embedded_count=1,
        chunk_count=5,
        created_by_user_id=root_admin.id,
    )
    db.add(embedding_job)
    db.flush()
    job = MatterTopicJob(
        matter_id=matter_id,
        embedding_job_id=embedding_job.id,
        status="AWAITING_REVIEW",
        workflow_id=f"matter-topics:{uuid.uuid4()}",
        operating_mode="FIXED",
        sample_size=100,
        requested_topic_count=2,
        assignment_mode="REPLACE",
        configuration_hash="c" * 64,
        configuration={"max_topics_per_document": 3, "minimum_assignment_confidence": 0.35},
        document_count=0,
        sampled_chunk_count=10,
        topic_count=2,
        created_by_user_id=root_admin.id,
    )
    db.add(job)
    db.flush()
    clusters = [
        MatterTopicCluster(
            job_id=job.id,
            ordinal=ordinal,
            topic_key=f"topic_{job.id.hex[:12]}_{ordinal + 1}",
            name=name,
            description=None,
            keywords=[name.lower()],
            centroid=centroid,
            representative_excerpts=[f"Example for {name}"],
        )
        for ordinal, (name, centroid) in enumerate((("Finance", [1.0, 0.0]), ("Energy", [0.0, 1.0])))
    ]
    db.add_all(clusters)
    existing_definition = None
    if destination_mode == "EXISTING_FIELD":
        existing_definition = MetadataDefinition(
            matter_id=matter_id,
            key="case_themes",
            display_name="Case themes",
            description="Existing case themes.",
            type="ENUM",
            cardinality="MULTIPLE",
            allowed_values=[{"key": "legacy", "label": "Legacy", "description": None, "active": True}],
            value_source="ASSERTED",
            assertion_policy="IMMEDIATE",
            resolution_policy="LATEST_VALID",
            searchable=True,
            facetable=True,
            reviewable=True,
            ai_assignable=True,
            status="ACTIVE",
        )
        db.add(existing_definition)
    db.flush()
    destination_snapshot = {
        "mode": destination_mode,
        "metadata_definition_id": str(existing_definition.id) if existing_definition else None,
        "field_key": {
            "TOPICS": "topics",
            "EXISTING_FIELD": "case_themes",
            "NEW_FIELD": "communication_topics",
        }[destination_mode],
        "field_name": {
            "TOPICS": "Topics",
            "EXISTING_FIELD": "Case themes",
            "NEW_FIELD": "Communication topics",
        }[destination_mode],
    }
    job.configuration = {**job.configuration, "topic_destination": destination_snapshot}
    db.commit()

    assert db.query(MetadataDefinition).filter_by(matter_id=matter_id, key="topics").first() is None
    expected_key = "topics"
    if destination_mode == "EXISTING_FIELD":
        expected_key = "case_themes"
    elif destination_mode == "NEW_FIELD":
        expected_key = "communication_topics"
    response = client.post(
        f"/v1/matters/{matter['id']}/topic-jobs/{job.id}/apply",
        headers=auth(root_token),
        json={
            "topics": [
                {
                    "id": str(clusters[0].id),
                    "name": "Financial transactions",
                    "description": "Payments and financial reporting.",
                    "included": True,
                },
                {
                    "id": str(clusters[1].id),
                    "name": "Energy",
                    "description": None,
                    "included": False,
                },
            ],
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "PUBLISHING"
    assert body["topic_count"] == 1
    assert body["reviewed_by_user_id"] == str(root_admin.id)
    assert body["reviewed_at"] is not None
    assert [cluster["included"] for cluster in body["clusters"]] == [True, False]
    db.expire_all()
    definition = db.query(MetadataDefinition).filter_by(matter_id=matter_id, key=expected_key).one()
    expected_values = [
        {
            "key": clusters[0].topic_key,
            "label": "Financial transactions",
            "description": "Payments and financial reporting.",
            "active": True,
        }
    ]
    if destination_mode == "EXISTING_FIELD":
        expected_values.insert(0, {"key": "legacy", "label": "Legacy", "description": None, "active": True})
    assert definition.allowed_values == expected_values
    assert body["metadata_definition_id"] == str(definition.id)
    db.refresh(job)
    assert job.configuration["topic_destination"] == {
        "mode": destination_mode,
        "metadata_definition_id": str(definition.id),
        "field_key": expected_key,
        "field_name": definition.display_name,
    }
