from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis_task_playground import execute_analysis_task_playground
from app.analysis_task_skills import ensure_standard_analysis_task_skills
from app.decision_engine import DecisionEngineRegistry, DecisionEnvelope
from app.models import (
    ExternalProviderUsage,
    ReviewBatchRun,
    ReviewDecisionResult,
    SkillDefinition,
    SkillRun,
    Tenant,
    WorkflowRun,
)
from tests.test_agents_and_matter_definitions import auth, create_tenant_context
from tests.test_review_batches import add_documents


class _FakeDecisionEngine:
    async def evaluate(self, request) -> DecisionEnvelope:
        now = datetime.now(UTC)
        return DecisionEnvelope.model_validate(
            {
                "answers": {
                    "privilege.legal_advice": {"type": "noul", "noul": 0.93},
                },
                "provider": "typesafe",
                "model": "jev-test",
                "provider_request_id": "req_playground",
                "usage": {"request_count": 1, "input_tokens": 42, "output_tokens": 5},
                "latency_ms": 120,
                "started_at": now,
                "completed_at": now,
                "attempts": 1,
            }
        )


def _bootstrap_skills(db: Session, root_admin) -> None:
    root = db.get(Tenant, root_admin.tenant_id)
    assert root is not None
    assert ensure_standard_analysis_task_skills(db, root, root_admin) is True
    db.commit()


def _create_published_task(client: TestClient, token: str, matter_id: str) -> dict:
    created = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "privilege_review",
            "name": "Privilege review",
            "task_type": "PRIVILEGE_REVIEW",
            "definition_markdown": "# Legal advice\n\nReview requests for or provision of legal advice.",
        },
    )
    assert created.status_code == 201, created.text
    task = created.json()
    task_version_id = task["version"]["id"]
    specification = {
        "questions": {
            "privilege.legal_advice": {
                "type": "noul",
                "instructions": "Does the document request or provide legal advice?",
                "criteria": {"true": "Legal advice is present", "false": "It is absent"},
                "source_refs": [
                    {
                        "task_version_id": task_version_id,
                        "heading": "Legal advice",
                        "excerpt_hash": "a" * 64,
                    }
                ],
                "aggregation": {"operator": "ANY_WINDOW"},
                "evidence": {"required": True, "minimum_exists_probability": 0.7},
            }
        },
        "decision_policy": {
            "recommendations": {
                "potentially_privileged": {
                    "operator": "PREDICATE",
                    "predicate": {
                        "question_key": "privilege.legal_advice",
                        "measure": "noul",
                        "comparator": "GTE",
                        "threshold": 0.7,
                    },
                }
            }
        },
        "state_contract": {
            "builder_version": "document-review-state-v1",
            "required_paths": ["document.id", "document.metadata", "document.paragraphs"],
        },
    }
    updated = client.put(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/specification",
        headers=auth(token),
        json={"decision_specification": specification},
    )
    assert updated.status_code == 200, updated.text
    published = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/publish",
        headers=auth(token),
    )
    assert published.status_code == 200, published.text
    return published.json()


def test_playground_queues_managed_skill_and_persists_isolated_typed_result(
    client: TestClient,
    root_token: str,
    root_admin,
    db: Session,
    monkeypatch,
) -> None:
    _bootstrap_skills(db, root_admin)
    _, token, matter_id = create_tenant_context(client, root_token)
    document_id = add_documents(matter_id, root_admin.id, count=1)[0]
    batch_response = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(token),
        json={"name": "Playground documents", "selection_type": "ALL_MATTER"},
    )
    assert batch_response.status_code == 202, batch_response.text
    batch_id = batch_response.json()["id"]
    task = _create_published_task(client, token, matter_id)

    queued = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/playground-runs",
        headers=auth(token),
        json={"review_batch_id": batch_id, "matter_document_id": document_id},
    )
    assert queued.status_code == 202, queued.text
    queued_data = queued.json()
    assert queued_data["status"] == "QUEUED"
    assert queued_data["review_batch_id"] == batch_id
    assert queued_data["result_id"] is None

    with TestingSessionLocal() as execution_db:
        workflow = execution_db.get(WorkflowRun, uuid.UUID(queued_data["workflow_run_id"]))
        assert workflow is not None
        assert workflow.binding_snapshot["decision_evaluation"]["skill_key"] == (
            "evaluate_analysis_task_decision_specification"
        )
        monkeypatch.setattr(
            "app.analysis_task_playground.get_preferred_text_source",
            lambda **_: SimpleNamespace(
                artifact_id=uuid.uuid4(),
                content_hash="f" * 64,
                text="Counsel provided legal advice to the client.\n\nThe communication was confidential.",
            ),
        )
        registry = DecisionEngineRegistry()
        registry.register("jev", _FakeDecisionEngine())
        result = execute_analysis_task_playground(
            execution_db,
            workflow.id,
            registry=registry,
        )
        assert result.status == "PARTIAL"
        assert result.coverage["question_count"] == 1
        assert result.coverage["question_complete"] is True
        assert result.coverage["complete"] is False
        assert result.coverage["evidence_complete"] is False
        assert result.answers["privilege.legal_advice"]["noul"] == 0.93
        assert result.recommendations["potentially_privileged"]["matched"] is True
        assert result.provider == "typesafe"
        assert result.model == "jev-test"
        assert execution_db.scalar(
            select(SkillRun).where(SkillRun.id == result.evaluation_skill_run_id)
        ).status == "COMPLETED"
        assert execution_db.scalar(
            select(ExternalProviderUsage).where(
                ExternalProviderUsage.job_type == "MATTER_ANALYSIS_TASK_PLAYGROUND"
            )
        ).input_tokens == 42
        review_run = execution_db.get(ReviewBatchRun, result.review_batch_run_id)
        assert review_run is not None
        assert review_run.result_policy == "ISOLATED"
        assert review_run.status == "COMPLETED"
        result_id = result.id

    fetched = client.get(
        f"/v1/matters/{matter_id}/review-batches/{batch_id}/runs/"
        f"{queued_data['review_batch_run_id']}/documents/{document_id}/decision-result",
        headers=auth(token),
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["id"] == str(result_id)
    assert fetched.json()["coverage"]["evidence_complete"] is False

    run_status = client.get(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/playground-runs/"
        f"{queued_data['workflow_run_id']}",
        headers=auth(token),
    )
    assert run_status.status_code == 200, run_status.text
    assert run_status.json()["status"] == "COMPLETED"
    assert run_status.json()["result_id"] == str(result_id)

    with TestingSessionLocal() as check_db:
        assert check_db.scalar(select(ReviewDecisionResult).where(ReviewDecisionResult.id == result_id)) is not None
        assert check_db.scalar(
            select(SkillDefinition).where(
                SkillDefinition.key == "evaluate_analysis_task_decision_specification"
            )
        ) is not None
