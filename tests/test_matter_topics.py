import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import MatterEmbeddingJob
from app.topic_clustering import discover_topics


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
        json={"operating_mode": "FIXED", "sample_size": 500, "requested_topic_count": 8},
    )
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "QUEUED"
    assert created.json()["operating_mode"] == "FIXED"
    assert created.json()["sample_size"] == 500
    assert created.json()["requested_topic_count"] == 8
    assert created.json()["assignment_mode"] == "REPLACE"

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
    assert all(topic.name and topic.keywords and len(topic.centroid) == 2 for topic in topics)
