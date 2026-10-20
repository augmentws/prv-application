import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ReviewBatch, ReviewBatchDocument
from tests.test_agents_and_matter_definitions import auth, create_tenant_context
from tests.test_analysis_tasks import _specification
from tests.test_review_batches import add_documents


def _published_partitioned_task(client: TestClient, token: str, matter_id: str) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "partitioned_review",
            "name": "Partitioned review",
            "task_type": "QUESTION_ANSWERING",
            "definition_markdown": "# Issue\nReview the issue.",
            "scope_policy": {
                "allowed_modes": ["AD_HOC_BATCH", "PARTITIONED_SET"],
                "default_mode": "PARTITIONED_SET",
            },
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    response = client.put(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/specification",
        headers=auth(token),
        json={"decision_specification": _specification(task["version"]["id"])},
    )
    assert response.status_code == 200, response.text
    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/publish",
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_partitioned_batch_set_materializes_exact_non_overlapping_children(
    client: TestClient,
    root_token: str,
    db: Session,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _published_partitioned_task(client, token, matter_id)
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    document_ids = add_documents(matter_id, user_id, count=5)

    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets",
        headers=auth(token),
        json={
            "name": "First partition",
            "population_source_type": "ALL_MATTER",
            "target_batch_size": 2,
        },
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == "READY"
    assert payload["population_count"] == 5
    assert payload["batch_count"] == 3
    assert [child["document_count"] for child in payload["children"]] == [2, 2, 1]
    assert task["version"]["scope_policy"]["default_mode"] == "PARTITIONED_SET"

    batch_set_id = uuid.UUID(payload["id"])
    memberships = list(
        db.execute(
            select(
                ReviewBatchDocument.matter_document_id,
                func.count(ReviewBatchDocument.review_batch_id),
            )
            .where(ReviewBatchDocument.analysis_task_batch_set_id == batch_set_id)
            .group_by(ReviewBatchDocument.matter_document_id)
        )
    )
    assert {str(document_id) for document_id, _ in memberships} == set(document_ids)
    assert {count for _, count in memberships} == {1}
    children = list(
        db.scalars(
            select(ReviewBatch)
            .where(ReviewBatch.analysis_task_batch_set_id == batch_set_id)
            .order_by(ReviewBatch.batch_set_ordinal)
        )
    )
    assert [child.selection_type for child in children] == ["TASK_BATCH_PARTITION"] * 3
    first_document_id = uuid.UUID(document_ids[0])
    owning_child_id = db.scalar(
        select(ReviewBatchDocument.review_batch_id).where(
            ReviewBatchDocument.analysis_task_batch_set_id == batch_set_id,
            ReviewBatchDocument.matter_document_id == first_document_id,
        )
    )
    other_child = next(child for child in children if child.id != owning_child_id)
    db.add(
        ReviewBatchDocument(
            review_batch_id=other_child.id,
            matter_document_id=first_document_id,
            analysis_task_batch_set_id=batch_set_id,
            sequence_number=99,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_partitioned_batch_set_requires_opted_in_published_version(
    client: TestClient,
    root_token: str,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "ordinary_review",
            "name": "Ordinary review",
            "task_type": "QUESTION_ANSWERING",
            "definition_markdown": "# Issue\nReview the issue.",
        },
    )
    assert response.status_code == 201
    task = response.json()
    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/batch-sets",
        headers=auth(token),
        json={
            "name": "Not allowed",
            "population_source_type": "ALL_MATTER",
            "target_batch_size": 100,
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "Analysis Task has no published version"
