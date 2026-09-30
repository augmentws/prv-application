import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditRecord, MatterAnalysisTask, MatterDefinition
from tests.test_agents_and_matter_definitions import auth, create_tenant_context


def _specification(task_version_id: str) -> dict:
    return {
        "schema_version": "review-decision-specification-v1",
        "questions": {
            "responsiveness.issue_one": {
                "type": "noul",
                "instructions": {
                    "question": "Does the document concern the first issue?",
                    "state_path": "document.paragraphs",
                },
                "criteria": {
                    "true": "The first issue is present.",
                    "false": "The first issue is absent.",
                },
                "source_refs": [
                    {
                        "task_version_id": task_version_id,
                        "heading": "Issue One",
                        "excerpt_hash": "b" * 64,
                    }
                ],
                "aggregation": {"operator": "ANY_WINDOW"},
                "evidence": {"required": True, "minimum_exists_probability": 0.7},
                "field_mapping": {
                    "metadata_definition_key": "responsive",
                    "value": "responsive",
                    "uncertainty": {
                        "kind": "DERIVED_PROBABILITY",
                        "source": "MAPPED_BOOLEAN_PROBABILITY",
                    },
                },
            }
        },
        "decision_policy": {
            "version": "decision-policy-v1",
            "recommendations": {},
            "routes": {},
        },
        "state_contract": {
            "builder_version": "document-review-state-v1",
            "required_paths": ["document.id", "document.paragraphs"],
        },
    }


def _create_task(client: TestClient, token: str, matter_id: str) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "first_pass_review",
            "name": "First-pass issue review",
            "description": "Apply the reviewed issue guidance.",
            "task_type": "QUESTION_ANSWERING",
            "definition_markdown": "# Issue One\nReview documents about coverage cancellation.",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_analysis_task_definition_and_specification_publish_atomically(
    client: TestClient,
    root_token: str,
    db: Session,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _create_task(client, token, matter_id)
    assert task["workflow_key"] == "question_answering_v1"
    assert task["published_version"] is None
    assert task["version"]["compilation_status"] == "NOT_GENERATED"
    assert db.scalar(select(MatterDefinition).where(MatterDefinition.matter_id == uuid.UUID(matter_id))) is None

    publish_before_ready = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/publish",
        headers=auth(token),
    )
    assert publish_before_ready.status_code == 409

    specification_response = client.put(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/specification",
        headers=auth(token),
        json={
            "decision_specification": _specification(task["version"]["id"]),
            "input_contract": {"type": "document-review-state-v1"},
            "output_contract": {"type": "decision-result-v1"},
            "evidence_policy": {"required": True},
            "routing_policy": {"low_probability": "HUMAN_REVIEW"},
            "validation_report": {"errors": [], "warnings": []},
        },
    )
    assert specification_response.status_code == 200, specification_response.text
    ready = specification_response.json()
    assert ready["version"]["compilation_status"] == "READY"
    assert len(ready["version"]["specification_content_hash"]) == 64

    publish_response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/publish",
        headers=auth(token),
    )
    assert publish_response.status_code == 200, publish_response.text
    published = publish_response.json()
    assert published["published_version"] == 1
    assert published["version"]["status"] == "PUBLISHED"
    actions = list(
        db.scalars(
            select(AuditRecord.action)
            .where(AuditRecord.target_id == uuid.UUID(task["id"]))
            .order_by(AuditRecord.created_at)
        )
    )
    assert actions == [
        "matter_analysis_task.created",
        "matter_analysis_task.specification.updated",
        "matter_analysis_task.version.published",
    ]


def test_edit_creates_new_uncompiled_version_and_rejects_stale_writes(
    client: TestClient,
    root_token: str,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _create_task(client, token, matter_id)

    edit_response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions",
        headers=auth(token),
        json={
            "based_on_version": 1,
            "definition_markdown": "# Issue One\nReview cancellations and threatened non-renewals.",
        },
    )
    assert edit_response.status_code == 201, edit_response.text
    edited = edit_response.json()
    assert edited["current_version"] == 2
    assert edited["version"]["decision_specification"] is None
    assert edited["version"]["compilation_status"] == "NOT_GENERATED"

    stale_response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions",
        headers=auth(token),
        json={"based_on_version": 1, "definition_markdown": "stale edit"},
    )
    assert stale_response.status_code == 409

    versions_response = client.get(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions",
        headers=auth(token),
    )
    assert versions_response.status_code == 200
    versions = versions_response.json()
    assert [version["version"] for version in versions] == [2, 1]
    assert versions[1]["status"] == "RETIRED"


def test_specification_sources_must_reference_the_enclosing_task_version(
    client: TestClient,
    root_token: str,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _create_task(client, token, matter_id)
    response = client.put(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/specification",
        headers=auth(token),
        json={"decision_specification": _specification(str(uuid.uuid4()))},
    )
    assert response.status_code == 409
    assert "source references" in response.json()["error"]["message"]


def test_analysis_task_keys_are_unique_within_a_matter(
    client: TestClient,
    root_token: str,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    _create_task(client, token, matter_id)
    duplicate = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "first_pass_review",
            "name": "Duplicate",
            "task_type": "QUESTION_ANSWERING",
            "definition_markdown": "# Privilege",
        },
    )
    assert duplicate.status_code == 409

    tasks = db_tasks = client.get(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
    )
    assert db_tasks.status_code == 200
    assert len(tasks.json()) == 1
    assert tasks.json()[0]["key"] == "first_pass_review"


def test_analysis_tasks_do_not_cross_matter_boundaries(client: TestClient, root_token: str) -> None:
    tenant_id, token, matter_id = create_tenant_context(client, root_token)
    task = _create_task(client, token, matter_id)
    second_client = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(token),
        json={"name": "Second Client"},
    )
    second_matter = client.post(
        f"/v1/clients/{second_client.json()['id']}/matters",
        headers=auth(token),
        json={"name": "Second Matter"},
    )
    response = client.get(
        f"/v1/matters/{second_matter.json()['id']}/analysis-tasks/{task['id']}",
        headers=auth(token),
    )
    assert response.status_code == 404


def test_analysis_task_row_uses_code_owned_workflow_key(
    client: TestClient,
    root_token: str,
    db: Session,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _create_task(client, token, matter_id)
    row = db.get(MatterAnalysisTask, uuid.UUID(task["id"]))
    assert row is not None
    assert row.workflow_key == "question_answering_v1"


def test_matter_definition_remains_separate_from_question_answering_tasks(
    client: TestClient,
    root_token: str,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    first = client.post(
        f"/v1/matters/{matter_id}/definition/revisions",
        headers=auth(token),
        json={
            "content_markdown": "# Responsiveness\n\nReview documents against the matter-wide issues.",
            "source_kind": "PASTE",
        },
    )
    assert first.status_code == 201, first.text

    tasks = client.get(f"/v1/matters/{matter_id}/analysis-tasks", headers=auth(token))
    assert tasks.status_code == 200, tasks.text
    assert tasks.json() == []

    unsupported = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "matter_definition",
            "name": "Matter Definition",
            "task_type": "MATTER_DEFINITION",
            "definition_markdown": "# Responsiveness\n\nReview the matter-wide issues.",
        },
    )
    assert unsupported.status_code == 422
