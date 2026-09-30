from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.analysis_task_batch import (
    complete_analysis_task_batch,
    fail_analysis_task_batch_document,
    prepare_analysis_task_batch,
    refresh_analysis_task_batch_progress,
)
from app.analysis_task_playground import (
    execute_analysis_task_document,
    execute_analysis_task_playground,
    materialize_decision_recommendations,
)
from app.analysis_task_skills import ensure_standard_analysis_task_skills
from app.decision_engine import DecisionEngineRegistry, DecisionEnvelope
from app.models import (
    ExternalProviderUsage,
    MetadataDefinition,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewBatchRunValue,
    ReviewDecisionResult,
    SkillDefinition,
    SkillRun,
    Tenant,
    WorkflowRun,
    WorkflowSkillBinding,
)
from app.workflow_specs import MATTER_ANALYSIS_TASK_BATCH_SPEC, MATTER_ANALYSIS_TASK_PLAYGROUND_SPEC
from tests.test_agents_and_matter_definitions import auth, create_tenant_context
from tests.test_review_batches import add_documents


class _FakeDecisionEngine:
    def __init__(self) -> None:
        self.requests = []

    async def evaluate(self, request) -> DecisionEnvelope:
        self.requests.append(request)
        now = datetime.now(UTC)
        answers = {
            "privilege.legal_advice": {"type": "noul", "noul": 0.93},
        }
        for key, question in request.questions.items():
            if key.endswith(".exists"):
                answers[key] = {"type": "noul", "noul": 0.96}
            elif key.endswith(".location"):
                probabilities = {option: 0.0 for option in question.criteria}
                selected = next(option for option in probabilities if option != "no_support")
                probabilities[selected] = 1.0
                answers[key] = {
                    "type": "choice",
                    "choice": selected,
                    "confidence": 1.0,
                    "probabilities": probabilities,
                }
        return DecisionEnvelope.model_validate(
            {
                "answers": answers,
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
            "task_type": "QUESTION_ANSWERING",
            "definition_markdown": "# Legal advice\n\nReview requests for or provision of legal advice.",
        },
    )
    assert created.status_code == 201, created.text
    task = created.json()
    task_version_id = task["version"]["id"]
    specification = {
        "decision_context": {
            "summary": "Review communications for requests for or provision of legal advice.",
            "controlling_guidance": ["The communication must concern legal rather than business advice."],
            "inclusion_criteria": ["Requests for legal advice", "Provision of legal advice"],
            "exclusion_criteria": ["Pure business advice"],
        },
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
            "required_paths": [
                "matter.decision_context",
                "document.id",
                "document.metadata",
                "document.paragraphs",
            ],
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
        engine = _FakeDecisionEngine()
        registry.register("jev", engine)
        result = execute_analysis_task_playground(
            execution_db,
            workflow.id,
            registry=registry,
        )
        assert result.status == "COMPLETED"
        assert result.coverage["question_count"] == 1
        assert result.coverage["question_complete"] is True
        assert result.coverage["complete"] is True
        assert result.coverage["evidence_complete"] is True
        assert result.evidence["privilege.legal_advice"]["paragraph_ids"] == ["¶1"]
        assert result.evidence["privilege.legal_advice"]["exists_probability"] == 0.96
        assert result.answers["privilege.legal_advice"]["noul"] == 0.93
        assert result.recommendations["potentially_privileged"]["matched"] is True
        assert engine.requests[0].state["matter"]["decision_context"] == {
            "summary": "Review communications for requests for or provision of legal advice.",
            "controlling_guidance": [
                "The communication must concern legal rather than business advice."
            ],
            "responsiveness_scope": [],
            "inclusion_criteria": ["Requests for legal advice", "Provision of legal advice"],
            "exclusion_criteria": ["Pure business advice"],
            "issue_definitions": {},
            "key_entities": [],
            "date_scope": [],
            "terminology": {},
            "examples": [],
            "source_material": [],
            "reviewed_task_definition": {
                "content_hash": task["version"]["definition_content_hash"],
                "markdown": "# Legal advice\n\nReview requests for or provision of legal advice.",
            },
        }
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
        definition = MetadataDefinition(
            matter_id=uuid.UUID(matter_id),
            key="jev_privilege",
            display_name="Jev privilege",
            type="BOOLEAN",
            cardinality="SINGLE",
            value_source="ASSERTED",
            assertion_policy="IMMEDIATE",
            resolution_policy="EXPLICIT_ONLY",
            searchable=True,
            facetable=True,
            normalize_to_lowercase=False,
            reviewable=True,
            ai_assignable=True,
            status="ACTIVE",
        )
        execution_db.add(definition)
        execution_db.flush()
        materialize_decision_recommendations(
            execution_db,
            matter_id=uuid.UUID(matter_id),
            review_run_id=review_run.id,
            document_id=uuid.UUID(document_id),
            decision_result=result,
            recommendations={
                "jev_privilege": {
                    "matched": True,
                    "metadata_definition_key": "jev_privilege",
                    "value": True,
                    "probability": 0.93,
                    "question_key": "privilege.legal_advice",
                }
            },
        )
        execution_db.commit()
        materialized = execution_db.scalar(
            select(ReviewBatchRunValue).where(
                ReviewBatchRunValue.review_batch_run_id == review_run.id,
                ReviewBatchRunValue.metadata_definition_id == definition.id,
            )
        )
        assert materialized is not None
        assert materialized.value_boolean is True
        assert materialized.confidence == 0.93
        assert materialized.confidence_kind == "SELECTED_PROBABILITY"
        assert materialized.review_decision_result_id == result.id
        result_id = result.id

    fetched = client.get(
        f"/v1/matters/{matter_id}/review-batches/{batch_id}/runs/"
        f"{queued_data['review_batch_run_id']}/documents/{document_id}/decision-result",
        headers=auth(token),
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["id"] == str(result_id)
    assert fetched.json()["coverage"]["evidence_complete"] is True

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


def test_published_analysis_task_runs_against_every_document_in_batch(
    client: TestClient,
    root_token: str,
    root_admin,
    db: Session,
    monkeypatch,
) -> None:
    _bootstrap_skills(db, root_admin)
    # Simulate an installation bootstrapped before batch analysis had its own binding.
    db.execute(
        delete(WorkflowSkillBinding).where(
            WorkflowSkillBinding.workflow_key == MATTER_ANALYSIS_TASK_BATCH_SPEC.key
        )
    )
    db.commit()
    _, token, matter_id = create_tenant_context(client, root_token)
    document_ids = add_documents(matter_id, root_admin.id, count=2)
    batch_response = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(token),
        json={"name": "Jev batch", "selection_type": "ALL_MATTER"},
    )
    assert batch_response.status_code == 202, batch_response.text
    batch_id = batch_response.json()["id"]
    task = _create_published_task(client, token, matter_id)

    queued = client.post(
        f"/v1/matters/{matter_id}/review-batches/{batch_id}/analysis-runs",
        headers=auth(token),
        json={"matter_analysis_task_id": task["id"]},
    )
    assert queued.status_code == 202, queued.text
    queued_data = queued.json()
    assert queued_data["status"] == "QUEUED"
    assert queued_data["result_policy"] == "ISOLATED"
    assert queued_data["configuration_snapshot"]["mode"] == "BATCH"
    assert queued_data["configuration_snapshot"]["task_name"] == "Privilege review"

    monkeypatch.setattr(
        "app.analysis_task_playground.get_preferred_text_source",
        lambda **_: SimpleNamespace(
            artifact_id=uuid.uuid4(),
            content_hash="d" * 64,
            text="Counsel provided legal advice to the client in a confidential communication.",
        ),
    )
    registry = DecisionEngineRegistry()
    registry.register("jev", _FakeDecisionEngine())
    with TestingSessionLocal() as execution_db:
        review_run = execution_db.get(ReviewBatchRun, uuid.UUID(queued_data["id"]))
        assert review_run is not None and review_run.workflow_run_record_id is not None
        workflow_id = review_run.workflow_run_record_id
        workflow = execution_db.get(WorkflowRun, workflow_id)
        assert workflow is not None
        assert workflow.binding_snapshot["decision_evaluation"]["binding_workflow_key"] == (
            MATTER_ANALYSIS_TASK_PLAYGROUND_SPEC.key
        )
        prepared = prepare_analysis_task_batch(execution_db, workflow_id)
        assert {str(value) for value in prepared} == set(document_ids)
        for document_id in prepared:
            result = execute_analysis_task_document(
                execution_db,
                workflow_id,
                document_id,
                complete_run=False,
                registry=registry,
            )
            assert result.status == "COMPLETED"
        execution_db.expire_all()
        running_workflow = execution_db.get(WorkflowRun, workflow_id)
        assert running_workflow is not None
        assert running_workflow.progress == {
            "stage": "EVALUATING",
            "total": 2,
            "completed": 0,
            "failed": 0,
        }
        raced_document = execution_db.get(
            ReviewBatchRunDocument,
            (review_run.id, prepared[0]),
        )
        assert raced_document is not None
        raced_document.status = "FAILED"
        execution_db.commit()
        fail_analysis_task_batch_document(
            execution_db,
            workflow_id,
            prepared[0],
            "late duplicate failure",
        )
        progress = refresh_analysis_task_batch_progress(execution_db, workflow_id)
        assert progress == {"completed": 2, "failed": 0, "skipped": 0, "processed": 2}
        complete_analysis_task_batch(execution_db, workflow_id)
        completed_workflow = execution_db.get(WorkflowRun, workflow_id)
        assert completed_workflow is not None
        assert completed_workflow.request_count == 2
        assert completed_workflow.input_tokens == 84
        assert completed_workflow.output_tokens == 10

    progress_response = client.get(
        f"/v1/matters/{matter_id}/review-batches/{batch_id}/runs/{queued_data['id']}/progress",
        headers=auth(token),
    )
    assert progress_response.status_code == 200, progress_response.text
    assert progress_response.json() == {
        "review_batch_run_id": queued_data["id"],
        "document_count": 2,
        "not_started_count": 0,
        "in_progress_count": 0,
        "completed_count": 2,
        "skipped_count": 0,
        "failed_count": 0,
    }
    for document_id in document_ids:
        result_response = client.get(
            f"/v1/matters/{matter_id}/review-batches/{batch_id}/runs/{queued_data['id']}"
            f"/documents/{document_id}/decision-result",
            headers=auth(token),
        )
        assert result_response.status_code == 200, result_response.text
        assert result_response.json()["provider"] == "typesafe"

    with TestingSessionLocal() as check_db:
        run = check_db.get(ReviewBatchRun, uuid.UUID(queued_data["id"]))
        assert run is not None
        assert run.status == "COMPLETED"
        assert run.processed_document_count == 2
        assert check_db.scalar(
            select(func.count())
            .select_from(ReviewBatchRunDocument)
            .where(ReviewBatchRunDocument.review_batch_run_id == run.id)
        ) == 2
