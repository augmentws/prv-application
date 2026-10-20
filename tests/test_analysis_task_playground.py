from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
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
    _classification_state,
    _runtime_decision_context,
    execute_analysis_task_document,
    execute_analysis_task_playground,
    materialize_decision_recommendations,
)
from app.analysis_task_promotions import (
    apply_analysis_task_promotion_chunk,
    complete_analysis_task_promotion,
    prepare_analysis_task_promotion,
)
from app.analysis_task_skills import ensure_standard_analysis_task_skills
from app.analysis_tasks import MatterAnalysisTaskConflict
from app.artifact_gateway import AnalysisInputItemSnapshot, TextSourceReference, WorkflowArtifactReference
from app.decision_engine import DecisionEngineRegistry, DecisionEnvelope
from app.models import (
    AnalysisInputManifest,
    DocumentMetadataCurrent,
    ExternalProviderUsage,
    MatterAnalysisTaskVersion,
    MetadataDefinition,
    MetadataEvent,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewBatchRunPromotion,
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
        answers = {}
        for key, question in request.questions.items():
            if key == "privilege.legal_advice":
                answers[key] = {"type": "noul", "noul": 0.93}
            elif key.endswith(".exists"):
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
                "provider_request_id": f"req_playground_{len(self.requests)}",
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


def test_runtime_decision_context_omits_source_material_when_compiled_context_exists() -> None:
    original = {
        "summary": "Apply the compiled review standard.",
        "issue_definitions": {"topic_401": "Olympics"},
        "source_material": [{"heading": "Issues", "text": "Full reviewed source"}],
    }

    runtime = _runtime_decision_context(original)

    assert "source_material" not in runtime
    assert runtime["issue_definitions"] == {"topic_401": "Olympics"}
    assert "source_material" in original


def test_runtime_decision_context_retains_source_material_without_compiled_context() -> None:
    original = {
        "summary": "Sparse legacy context.",
        "source_material": [{"heading": "Guidance", "text": "Controlling reviewed source"}],
    }

    assert _runtime_decision_context(original) == original


@pytest.mark.parametrize(
    ("required_paths", "expected_document_keys"),
    [
        (["document.text"], {"id", "text"}),
        (["document.paragraphs"], {"id", "paragraphs"}),
        (["document.text", "document.paragraphs"], {"id", "text", "paragraphs"}),
    ],
)
def test_classification_state_sends_only_the_required_document_representation(
    required_paths: list[str],
    expected_document_keys: set[str],
) -> None:
    original = {
        "document": {
            "id": "document-id",
            "text": "First paragraph.\n\nSecond paragraph.",
            "paragraphs": [{"id": "¶1", "text": "First paragraph."}],
        }
    }

    projected = _classification_state(original, required_paths=required_paths)

    assert set(projected["document"]) == expected_document_keys
    assert set(original["document"]) == {"id", "text", "paragraphs"}


def _create_published_task(
    client: TestClient,
    token: str,
    matter_id: str,
    *,
    decision_provider: str = "JEV",
) -> dict:
    created = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "privilege_review",
            "name": "Privilege review",
            "task_type": "QUESTION_ANSWERING",
            "decision_provider": decision_provider,
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
        assert result.coverage["evidence_target_question_count"] == 1
        assert result.coverage["evidence_localization_request_count"] == 1
        assert result.evidence["privilege.legal_advice"]["paragraph_ids"] == ["¶1"]
        assert result.evidence["privilege.legal_advice"]["exists_probability"] == 0.96
        assert result.answers["privilege.legal_advice"]["noul"] == 0.93
        assert result.recommendations["potentially_privileged"]["matched"] is True
        assert len(engine.requests) == 2
        assert set(engine.requests[0].questions) == {"privilege.legal_advice"}
        assert set(engine.requests[1].questions) == {
            "internal_evidence.q1.exists",
            "internal_evidence.q1.location",
        }
        assert set(engine.requests[1].state) == {"document", "classification"}
        assert "text" not in engine.requests[1].state["document"]
        assert engine.requests[1].state["classification"]["answers"] == {
            "privilege.legal_advice": {"type": "noul", "noul": 0.93}
        }
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
        assert result.evidence_skill_run_id is not None
        assert execution_db.scalar(
            select(SkillRun).where(SkillRun.id == result.evidence_skill_run_id)
        ).status == "COMPLETED"
        usage_rows = list(
            execution_db.scalars(
                select(ExternalProviderUsage).where(
                    ExternalProviderUsage.job_type == "MATTER_ANALYSIS_TASK_PLAYGROUND",
                    ExternalProviderUsage.job_id == workflow.id,
                )
            )
        )
        assert len(usage_rows) == 2
        assert sum(row.input_tokens for row in usage_rows) == 84
        assert {row.details["phase"] for row in usage_rows} == {
            "CLASSIFICATION",
            "EVIDENCE_LOCALIZATION",
        }
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

        issue_definition = MetadataDefinition(
            matter_id=uuid.UUID(matter_id),
            key="issue_coding",
            display_name="Issue coding",
            type="ENUM",
            cardinality="MULTIPLE",
            allowed_values=[
                {"key": "topic_401", "label": "Olympics", "active": True},
                {"key": "topic_408", "label": "Invasive Species", "active": True},
            ],
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
        execution_db.add(issue_definition)
        execution_db.flush()
        materialize_decision_recommendations(
            execution_db,
            matter_id=uuid.UUID(matter_id),
            review_run_id=review_run.id,
            document_id=uuid.UUID(document_id),
            decision_result=result,
            recommendations={
                "issue_coding.topic_401": {
                    "matched": True,
                    "metadata_definition_key": "issue_coding",
                    "value": "topic_401",
                    "confidence": 0.93,
                    "confidence_kind": "DERIVED_PROBABILITY",
                    "question_key": "issues.topic_401",
                },
                "issue_coding.topic_408": {
                    "matched": True,
                    "metadata_definition_key": "issue_coding",
                    "value": "topic_408",
                    "confidence": 0.87,
                    "confidence_kind": "DERIVED_PROBABILITY",
                    "question_key": "issues.topic_408",
                },
                "issue_coding.topic_401_duplicate": {
                    "matched": True,
                    "metadata_definition_key": "issue_coding",
                    "value": "topic_401",
                    "confidence": 0.89,
                    "confidence_kind": "DERIVED_PROBABILITY",
                    "question_key": "issues.topic_401_duplicate",
                },
            },
        )
        execution_db.commit()
        issue_values = list(
            execution_db.scalars(
                select(ReviewBatchRunValue)
                .where(
                    ReviewBatchRunValue.review_batch_run_id == review_run.id,
                    ReviewBatchRunValue.metadata_definition_id == issue_definition.id,
                )
                .order_by(ReviewBatchRunValue.value_ordinal)
            )
        )
        assert [(value.value_ordinal, value.value_text) for value in issue_values] == [
            (0, "topic_401"),
            (1, "topic_408"),
        ]
        assert all(value.confidence_kind == "DERIVED_PROBABILITY" for value in issue_values)
        result_id = result.id
        with pytest.raises(MatterAnalysisTaskConflict, match="conflicting values"):
            materialize_decision_recommendations(
                execution_db,
                matter_id=uuid.UUID(matter_id),
                review_run_id=review_run.id,
                document_id=uuid.UUID(document_id),
                decision_result=result,
                recommendations={
                    "jev_privilege.true": {
                        "matched": True,
                        "metadata_definition_key": "jev_privilege",
                        "value": True,
                    },
                    "jev_privilege.false": {
                        "matched": True,
                        "metadata_definition_key": "jev_privilege",
                        "value": False,
                    },
                },
            )
        execution_db.rollback()

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


def test_question_answering_batch_run_can_be_canceled(
    client: TestClient,
    root_token: str,
    root_admin,
    db: Session,
    monkeypatch,
) -> None:
    _bootstrap_skills(db, root_admin)
    _, token, matter_id = create_tenant_context(client, root_token)
    document_ids = add_documents(matter_id, root_admin.id, count=2)
    batch_response = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(token),
        json={"name": "Cancelable analysis", "selection_type": "ALL_MATTER"},
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
    run_id = queued.json()["id"]
    with TestingSessionLocal() as check_db:
        queued_run = check_db.get(ReviewBatchRun, uuid.UUID(run_id))
        assert queued_run is not None and queued_run.dbos_workflow_id is not None
        dbos_workflow_id = queued_run.dbos_workflow_id
    canceled_workflows: list[str] = []
    monkeypatch.setattr(
        "app.routers.job_executions.cancel_analysis_task_batch",
        canceled_workflows.append,
    )

    canceled = client.post(
        f"/v1/jobs/ANALYSIS_TASK_BATCH_RUN/{run_id}/cancel",
        headers=auth(token),
    )

    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "CANCELED"
    assert canceled.json()["current_stage"] == "CANCELED"
    assert canceled.json()["progress_completed"] == 2
    assert canceled.json()["progress"] == {
        "stage": "CANCELED",
        "total": 2,
        "completed": 0,
        "failed": 0,
        "skipped": 2,
    }
    assert canceled_workflows == [dbos_workflow_id]

    documents = client.get(
        f"/v1/matters/{matter_id}/review-batches/{batch_id}/documents",
        headers=auth(token),
        params={"run_id": run_id},
    )
    assert documents.status_code == 200, documents.text
    assert {item["matter_document_id"] for item in documents.json()} == set(document_ids)
    assert {item["review_status"] for item in documents.json()} == {"SKIPPED"}


def test_question_answering_job_lists_and_retries_retryable_document_failures(
    client: TestClient,
    root_token: str,
    root_admin,
    db: Session,
    monkeypatch,
) -> None:
    _bootstrap_skills(db, root_admin)
    _, token, matter_id = create_tenant_context(client, root_token)
    document_ids = add_documents(matter_id, root_admin.id, count=2)
    batch_response = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(token),
        json={"name": "Failure diagnostics", "selection_type": "ALL_MATTER"},
    )
    assert batch_response.status_code == 202, batch_response.text
    task = _create_published_task(client, token, matter_id)
    queued = client.post(
        f"/v1/matters/{matter_id}/review-batches/{batch_response.json()['id']}/analysis-runs",
        headers=auth(token),
        json={"matter_analysis_task_id": task["id"]},
    )
    assert queued.status_code == 202, queued.text
    run_id = uuid.UUID(queued.json()["id"])

    with TestingSessionLocal() as execution_db:
        run = execution_db.get(ReviewBatchRun, run_id)
        assert run is not None and run.workflow_run_record_id is not None
        workflow_run_id = run.workflow_run_record_id
        prepared = prepare_analysis_task_batch(execution_db, workflow_run_id)
        assert {str(value) for value in prepared} == set(document_ids)
        fail_analysis_task_batch_document(
            execution_db,
            workflow_run_id,
            prepared[0],
            "Provider unavailable",
            failure_code="OPENAI_DECISIONS_HTTP_503",
            retryable=True,
        )
        fail_analysis_task_batch_document(
            execution_db,
            workflow_run_id,
            prepared[1],
            "Document has no faithful text source",
            failure_code="ANALYSIS_DOCUMENT_CONFLICT",
            retryable=False,
        )
        complete_analysis_task_batch(execution_db, workflow_run_id)

    failures = client.get(
        f"/v1/jobs/ANALYSIS_TASK_BATCH_RUN/{run_id}/failed-documents",
        headers=auth(token),
    )
    assert failures.status_code == 200, failures.text
    failure_data = failures.json()
    assert failure_data["total"] == 2
    assert failure_data["retryable_count"] == 1
    assert failure_data["items"][0]["failure_code"] == "OPENAI_DECISIONS_HTTP_503"
    assert failure_data["items"][0]["failure_retriable"] is True
    retryable_document_id = failure_data["items"][0]["matter_document_id"]
    nonretryable_document_id = next(
        item["matter_document_id"]
        for item in failure_data["items"]
        if not item["failure_retriable"]
    )

    rejected = client.post(
        f"/v1/jobs/ANALYSIS_TASK_BATCH_RUN/{run_id}/failed-documents/retry",
        headers=auth(token),
        json={"document_ids": [nonretryable_document_id]},
    )
    assert rejected.status_code == 409, rejected.text
    assert "retryable failure" in rejected.text

    enqueued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.routers.job_executions.enqueue_analysis_task_batch",
        lambda _db, workflow_id, execution_id: enqueued.append((workflow_id, execution_id)),
    )
    retried = client.post(
        f"/v1/jobs/ANALYSIS_TASK_BATCH_RUN/{run_id}/failed-documents/retry",
        headers=auth(token),
        json={"document_ids": [retryable_document_id]},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "QUEUED"
    assert retried.json()["attempt_number"] == 2
    assert retried.json()["progress_total"] == 1
    assert enqueued == [(retried.json()["dbos_workflow_id"], retried.json()["id"])]

    with TestingSessionLocal() as retry_db:
        retry_population = prepare_analysis_task_batch(retry_db, uuid.UUID(retried.json()["id"]))
        assert [str(document_id) for document_id in retry_population] == [retryable_document_id]

    remaining = client.get(
        f"/v1/jobs/ANALYSIS_TASK_BATCH_RUN/{run_id}/failed-documents",
        headers=auth(token),
    )
    assert remaining.status_code == 200, remaining.text
    assert remaining.json()["total"] == 1
    assert remaining.json()["retryable_count"] == 0
    assert remaining.json()["items"][0]["matter_document_id"] == nonretryable_document_id

    history = client.get(
        f"/v1/jobs/ANALYSIS_TASK_BATCH_RUN/{run_id}/execution",
        headers=auth(token),
    )
    assert history.status_code == 200, history.text
    assert len(history.json()["attempts"]) == 2
    assert history.json()["attempts"][0]["retry_of_workflow_run_id"] == history.json()["attempts"][1]["id"]


def test_openai_decisions_batch_run_requires_api_key_before_fan_out(
    client: TestClient,
    root_token: str,
    root_admin,
    db: Session,
    monkeypatch,
) -> None:
    _bootstrap_skills(db, root_admin)
    _, token, matter_id = create_tenant_context(client, root_token)
    add_documents(matter_id, root_admin.id, count=1)
    batch_response = client.post(
        f"/v1/matters/{matter_id}/review-batches",
        headers=auth(token),
        json={"name": "OpenAI analysis", "selection_type": "ALL_MATTER"},
    )
    assert batch_response.status_code == 202, batch_response.text
    task = _create_published_task(
        client,
        token,
        matter_id,
        decision_provider="OPENAI_DECISIONS",
    )
    monkeypatch.setattr(
        "app.analysis_task_batch.get_settings",
        lambda: SimpleNamespace(openai_api_key=None),
    )

    response = client.post(
        f"/v1/matters/{matter_id}/review-batches/{batch_response.json()['id']}/analysis-runs",
        headers=auth(token),
        json={"matter_analysis_task_id": task["id"]},
    )

    assert response.status_code == 409, response.text
    assert "OpenAI Decisions is not configured" in response.text
    assert "OPENAI_API_KEY" in response.text


def test_whole_matter_question_answering_uses_native_input_manifest(
    client: TestClient,
    root_token: str,
    root_admin,
    db: Session,
    monkeypatch,
) -> None:
    _bootstrap_skills(db, root_admin)
    _, token, matter_id = create_tenant_context(client, root_token)
    document_ids = add_documents(matter_id, root_admin.id, count=2)
    task = _create_published_task(client, token, matter_id)
    version = db.get(MatterAnalysisTaskVersion, uuid.UUID(task["version"]["id"]))
    assert version is not None
    version.scope_policy = {
        "allowed_modes": ["AD_HOC_BATCH", "WHOLE_MATTER"],
        "default_mode": "WHOLE_MATTER",
    }
    db.commit()

    content_by_id: dict[uuid.UUID, bytes] = {}
    references: dict[uuid.UUID, WorkflowArtifactReference] = {}

    def snapshots(*, item_ids, **_):
        return {
            item_id: AnalysisInputItemSnapshot(
                item_id=item_id,
                family_id=None,
                record_type="TEXT",
                original_filename="document.txt",
                file_date=None,
                email_sender=None,
                email_subject=None,
                email_sent_at=None,
                email_recipients={"TO": [], "CC": [], "BCC": []},
                text_source=TextSourceReference(
                    artifact_id=uuid.uuid5(item_id, "text"),
                    role="EXTRACTED_TEXT",
                    media_type="text/plain",
                    original_filename="document.txt",
                    content_hash="a" * 64,
                    byte_length=20,
                ),
            )
            for item_id in item_ids
        }

    def store(**kwargs):
        stream = kwargs["content"]
        stream.seek(0)
        content = stream.read()
        artifact_id = uuid.uuid4()
        reference = WorkflowArtifactReference(
            artifact_id=artifact_id,
            tenant_id=kwargs["tenant_id"],
            client_id=kwargs["client_id"],
            core_scope_kind=kwargs["core_scope_kind"],
            core_scope_id=kwargs["core_scope_id"],
            artifact_type=kwargs["artifact_type"],
            schema_version=kwargs["schema_version"],
            derivation_key=kwargs["derivation_key"],
            content_hash=hashlib.sha256(content).hexdigest(),
            media_type=kwargs["media_type"],
            byte_length=len(content),
            producer_kind=kwargs["producer_kind"],
            producer_run_id=kwargs["producer_run_id"],
            metadata=kwargs["artifact_metadata"],
            created_at=datetime.now(UTC),
        )
        content_by_id[artifact_id] = content
        references[artifact_id] = reference
        return reference, True

    def copy(**kwargs):
        content = content_by_id[kwargs["artifact_id"]]
        kwargs["destination"].write(content)
        return len(content)

    monkeypatch.setattr("app.analysis_task_batch.get_collection_item_analysis_input_snapshots", snapshots)
    monkeypatch.setattr("app.analysis_input_manifests.store_workflow_artifact", store)
    monkeypatch.setattr(
        "app.analysis_input_manifests.get_workflow_artifact_reference",
        lambda **kwargs: references[kwargs["artifact_id"]],
    )
    monkeypatch.setattr("app.analysis_input_manifests.copy_workflow_artifact_content", copy)

    path = f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/question-answering-runs"
    response = client.post(path, headers=auth(token), json={"idempotency_key": "whole-question-run"})
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["review_batch_id"] is None
    assert (run["scope_type"], run["scope_id"]) == ("MATTER", matter_id)
    assert run["analysis_input_manifest_id"] is not None
    duplicate = client.post(path, headers=auth(token), json={"idempotency_key": "whole-question-run"})
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == run["id"]
    execution = client.get(
        f"/v1/jobs/ANALYSIS_TASK_BATCH_RUN/{run['id']}/execution",
        headers=auth(token),
    )
    assert execution.status_code == 200, execution.text
    assert execution.json()["current_execution"]["status"] == "QUEUED"

    manifest = db.get(AnalysisInputManifest, uuid.UUID(run["analysis_input_manifest_id"]))
    assert manifest is not None
    assert (manifest.scope_type, manifest.scope_id, manifest.item_count) == (
        "MATTER",
        uuid.UUID(matter_id),
        2,
    )
    documents = list(
        db.scalars(
            select(ReviewBatchRunDocument)
            .where(ReviewBatchRunDocument.review_batch_run_id == uuid.UUID(run["id"]))
            .order_by(ReviewBatchRunDocument.input_manifest_ordinal)
        )
    )
    assert [str(document.matter_document_id) for document in documents] == document_ids
    assert [document.input_manifest_ordinal for document in documents] == [1, 2]

    def read_pinned_text(reference, **_):
        assert reference.content_hash == "a" * 64
        return "Counsel provided legal advice to the client in a confidential communication."

    monkeypatch.setattr("app.analysis_task_playground.read_text_source", read_pinned_text)
    registry = DecisionEngineRegistry()
    registry.register("jev", _FakeDecisionEngine())
    with TestingSessionLocal() as execution_db:
        review_run = execution_db.get(ReviewBatchRun, uuid.UUID(run["id"]))
        assert review_run is not None and review_run.workflow_run_record_id is not None
        prepared = prepare_analysis_task_batch(execution_db, review_run.workflow_run_record_id)
        assert [str(document_id) for document_id in prepared] == document_ids
        result = execute_analysis_task_document(
            execution_db,
            review_run.workflow_run_record_id,
            prepared[0],
            complete_run=False,
            registry=registry,
        )
        assert result.status == "COMPLETED"

    jobs = client.get(
        f"/v1/matters/{matter_id}/analysis-tasks/question-answering-jobs",
        headers=auth(token),
    )
    assert jobs.status_code == 200, jobs.text
    assert jobs.json()[0]["review_batch_name"] == "Entire matter"
    assert jobs.json()[0]["scope_type"] == "MATTER"


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
    execution_response = client.get(
        f"/v1/jobs/ANALYSIS_TASK_BATCH_RUN/{queued_data['id']}/execution",
        headers=auth(token),
    )
    assert execution_response.status_code == 200, execution_response.text
    assert execution_response.json()["current_execution"]["status"] == "QUEUED"
    assert execution_response.json()["current_execution"]["progress_total"] == 2
    jobs_response = client.get(
        f"/v1/matters/{matter_id}/analysis-tasks/question-answering-jobs",
        headers=auth(token),
    )
    assert jobs_response.status_code == 200, jobs_response.text
    jobs = jobs_response.json()
    assert len(jobs) == 1
    assert jobs[0]["id"] == queued_data["id"]
    assert jobs[0]["review_batch_id"] == batch_id
    assert jobs[0]["review_batch_name"] == "Jev batch"
    assert jobs[0]["matter_analysis_task_id"] == task["id"]
    assert jobs[0]["task_name"] == "Privilege review"
    assert jobs[0]["task_version"] == 1
    assert jobs[0]["decision_provider"] == "JEV"
    assert jobs[0]["model_key"] == queued_data["configuration_snapshot"]["model_key"]
    assert jobs[0]["status"] == "QUEUED"
    assert jobs[0]["processed_document_count"] == 0
    assert jobs[0]["successful_document_count"] == 0
    assert jobs[0]["document_count"] == 2
    assert jobs[0]["execution"] == execution_response.json()["current_execution"]

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
        assert workflow.job_type == "ANALYSIS_TASK_BATCH_RUN"
        assert workflow.job_id == review_run.id
        assert workflow.progress_unit == "documents"
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
        assert completed_workflow.status == "COMPLETED"
        assert completed_workflow.current_stage == "COMPLETED"
        assert completed_workflow.progress_completed == 2
        assert completed_workflow.request_count == 4
        assert completed_workflow.input_tokens == 168
        assert completed_workflow.output_tokens == 20

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
    completed_jobs_response = client.get(
        f"/v1/matters/{matter_id}/analysis-tasks/question-answering-jobs",
        headers=auth(token),
    )
    assert completed_jobs_response.status_code == 200, completed_jobs_response.text
    assert completed_jobs_response.json()[0]["successful_document_count"] == 2
    assert completed_jobs_response.json()[0]["document_count"] == 2
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

        destination = MetadataDefinition(
            matter_id=uuid.UUID(matter_id),
            key="jev_privilege_published",
            display_name="Jev privilege published",
            type="BOOLEAN",
            cardinality="SINGLE",
            value_source="ASSERTED",
            assertion_policy="IMMEDIATE",
            resolution_policy="LATEST_VALID",
            searchable=True,
            facetable=True,
            normalize_to_lowercase=False,
            reviewable=True,
            ai_assignable=True,
            status="ACTIVE",
        )
        check_db.add(destination)
        check_db.flush()
        task_version = check_db.get(
            MatterAnalysisTaskVersion,
            uuid.UUID(run.configuration_snapshot["task_version_id"]),
        )
        assert task_version is not None
        task_version.destination_metadata_definition_ids = [str(destination.id)]
        first_result = check_db.scalar(
            select(ReviewDecisionResult).where(
                ReviewDecisionResult.review_batch_run_id == run.id,
                ReviewDecisionResult.matter_document_id == uuid.UUID(document_ids[0]),
            )
        )
        assert first_result is not None
        check_db.add(
            ReviewBatchRunValue(
                review_batch_run_id=run.id,
                matter_document_id=uuid.UUID(document_ids[0]),
                metadata_definition_id=destination.id,
                value_ordinal=0,
                value_boolean=True,
                confidence=0.93,
                confidence_kind="DERIVED_PROBABILITY",
                review_decision_result_id=first_result.id,
                question_key="privilege.legal_advice",
            )
        )
        check_db.commit()
        destination_id = destination.id

    queued_promotion = client.post(
        f"/v1/matters/{matter_id}/review-batches/{batch_id}/runs/{queued_data['id']}/promotion",
        headers=auth(token),
    )
    assert queued_promotion.status_code == 202, queued_promotion.text
    promotion_id = uuid.UUID(queued_promotion.json()["id"])
    assert queued_promotion.json()["status"] == "QUEUED"
    assert queued_promotion.json()["destination_snapshot"][0]["key"] == "jev_privilege_published"
    assert queued_promotion.json()["destination_snapshot"][0]["display_name"] == "Jev privilege published"

    with TestingSessionLocal() as promotion_db:
        eligible = prepare_analysis_task_promotion(promotion_db, promotion_id)
        assert set(eligible) == set(document_ids)
        apply_analysis_task_promotion_chunk(
            promotion_db,
            promotion_id,
            [uuid.UUID(value) for value in eligible],
        )
        event_count = promotion_db.scalar(
            select(func.count()).select_from(MetadataEvent).where(
                MetadataEvent.source_id == f"analysis-task-promotion:{promotion_id}"
            )
        )
        apply_analysis_task_promotion_chunk(
            promotion_db,
            promotion_id,
            [uuid.UUID(value) for value in eligible],
        )
        assert promotion_db.scalar(
            select(func.count()).select_from(MetadataEvent).where(
                MetadataEvent.source_id == f"analysis-task-promotion:{promotion_id}"
            )
        ) == event_count
        complete_analysis_task_promotion(promotion_db, promotion_id)

        promotion = promotion_db.get(ReviewBatchRunPromotion, promotion_id)
        assert promotion is not None
        assert promotion.status == "COMPLETED"
        assert promotion.processed_document_count == 2
        assert promotion.event_count == 3
        promoted_run = promotion_db.get(ReviewBatchRun, uuid.UUID(queued_data["id"]))
        assert promoted_run is not None
        assert promoted_run.result_policy == "PUBLISH_TO_MATTER"
        first_current = promotion_db.scalar(
            select(DocumentMetadataCurrent).where(
                DocumentMetadataCurrent.matter_document_id == uuid.UUID(document_ids[0]),
                DocumentMetadataCurrent.metadata_definition_id == destination_id,
            )
        )
        second_current = promotion_db.scalar(
            select(DocumentMetadataCurrent).where(
                DocumentMetadataCurrent.matter_document_id == uuid.UUID(document_ids[1]),
                DocumentMetadataCurrent.metadata_definition_id == destination_id,
            )
        )
        assert first_current is not None and first_current.value_boolean is True
        assert second_current is not None and second_current.resolution_state == "EMPTY"

    fetched_promotion = client.get(
        f"/v1/matters/{matter_id}/review-batches/{batch_id}/runs/{queued_data['id']}/promotion",
        headers=auth(token),
    )
    assert fetched_promotion.status_code == 200, fetched_promotion.text
    assert fetched_promotion.json()["status"] == "COMPLETED"
