from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.analysis_tasks import content_hash, json_content_hash
from app.decision_engine import DecisionEnvelope
from app.models import (
    Client,
    Matter,
    MatterAnalysisTask,
    MatterAnalysisTaskVersion,
    MatterDocument,
    MatterDocumentImportJob,
    ModelInvocation,
    ReviewBatch,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    SkillDefinition,
    SkillDefinitionVersion,
    SkillRun,
    WorkflowRun,
    WorkflowStepRun,
)
from app.review_decision_results import (
    ReviewDecisionResultConflict,
    ReviewDecisionResultError,
    record_review_decision_result,
)


def _specification(task_version_id: uuid.UUID) -> dict:
    return {
        "schema_version": "review-decision-specification-v1",
        "questions": {
            "privilege.legal_advice": {
                "type": "noul",
                "instructions": "Does the document request or provide legal advice?",
                "criteria": {"true": "Legal advice is present", "false": "It is absent"},
                "source_refs": [
                    {
                        "task_version_id": str(task_version_id),
                        "heading": "Legal advice",
                        "excerpt_hash": "a" * 64,
                    }
                ],
                "aggregation": {"operator": "ANY_WINDOW"},
                "evidence": {"required": True, "minimum_exists_probability": 0.7},
                "field_mapping": None,
            }
        },
        "decision_policy": {
            "version": "decision-policy-v1",
            "recommendations": {},
            "routes": {},
        },
        "state_contract": {
            "builder_version": "document-review-state-v1",
            "required_paths": ["document.paragraphs"],
        },
    }


def _provenance(db: Session, root_admin) -> dict[str, uuid.UUID]:
    client = Client(tenant_id=root_admin.tenant_id, name="Decision result client", status="ACTIVE")
    db.add(client)
    db.flush()
    matter = Matter(client_id=client.id, name="Decision result matter", status="ACTIVE")
    db.add(matter)
    db.flush()
    import_job = MatterDocumentImportJob(
        matter_id=matter.id,
        source_collection_id=uuid.uuid4(),
        selection_type="EXPLICIT",
        selection={},
        selection_summary="test",
        status="COMPLETED",
        workflow_id=f"import-{uuid.uuid4()}",
        created_by_user_id=root_admin.id,
    )
    db.add(import_job)
    db.flush()
    document = MatterDocument(
        matter_id=matter.id,
        source_collection_id=import_job.source_collection_id,
        collection_item_id=uuid.uuid4(),
        added_by_import_job_id=import_job.id,
    )
    db.add(document)

    task = MatterAnalysisTask(
        matter_id=matter.id,
        key="privilege_review",
        name="Privilege review",
        task_type="PRIVILEGE_REVIEW",
        workflow_key="privilege_review_v1",
        current_version=1,
        published_version=1,
        status="ACTIVE",
        created_by_user_id=root_admin.id,
    )
    db.add(task)
    db.flush()
    task_version_id = uuid.uuid4()
    specification = _specification(task_version_id)
    task_version = MatterAnalysisTaskVersion(
        id=task_version_id,
        matter_analysis_task_id=task.id,
        version=1,
        status="PUBLISHED",
        compilation_status="READY",
        definition_markdown="# Legal advice\n\nReview legal advice.",
        definition_content_hash=content_hash("# Legal advice\n\nReview legal advice."),
        decision_specification=specification,
        specification_content_hash=json_content_hash(specification),
        created_by_user_id=root_admin.id,
        published_by_user_id=root_admin.id,
        published_at=datetime.now(UTC),
    )
    db.add(task_version)

    workflow = WorkflowRun(
        tenant_id=root_admin.tenant_id,
        client_id=client.id,
        matter_id=matter.id,
        workflow_key="first_pass_document_review_v1",
        code_version="v1",
        dbos_workflow_id=f"decision-{uuid.uuid4()}",
        status="RUNNING",
        initiated_by_user_id=root_admin.id,
    )
    db.add(workflow)
    batch = ReviewBatch(
        matter_id=matter.id,
        name="Decision result batch",
        selection_type="ALL_MATTER",
        status="READY",
        search_status="READY",
        workflow_id=f"batch-{uuid.uuid4()}",
        document_count=1,
        created_by_user_id=root_admin.id,
    )
    db.add(batch)
    db.flush()
    batch_run = ReviewBatchRun(
        review_batch_id=batch.id,
        run_type="WORKFLOW",
        purpose="REVIEW",
        status="RUNNING",
        result_policy="ISOLATED",
        workflow_run_record_id=workflow.id,
        initiated_by_user_id=root_admin.id,
    )
    db.add(batch_run)
    db.flush()
    db.add(
        ReviewBatchRunDocument(
            review_batch_run_id=batch_run.id,
            matter_document_id=document.id,
            status="IN_PROGRESS",
        )
    )

    skill = SkillDefinition(
        owner_tenant_id=root_admin.tenant_id,
        scope="SYSTEM",
        key="evaluate_decision_specification",
        name="Evaluate decision specification",
        current_version=1,
        published_version=1,
        status="ACTIVE",
        created_by_user_id=root_admin.id,
    )
    db.add(skill)
    db.flush()
    skill_version = SkillDefinitionVersion(
        skill_definition_id=skill.id,
        version=1,
        instructions="Evaluate the typed questions.",
        input_schema_key="decision-input-v1",
        input_schema={},
        output_schema_key="decision-output-v1",
        output_schema={},
        model_key="jev-latest",
        status="PUBLISHED",
        created_by_user_id=root_admin.id,
        published_at=datetime.now(UTC),
    )
    db.add(skill_version)
    step = WorkflowStepRun(
        workflow_run_id=workflow.id,
        role_key="decision_evaluation",
        ordinal=1,
        status="RUNNING",
        total_count=1,
    )
    db.add(step)
    db.flush()
    skill_run = SkillRun(
        workflow_run_id=workflow.id,
        workflow_step_run_id=step.id,
        skill_definition_version_id=skill_version.id,
        scope_type="MATTER_DOCUMENT",
        scope_id=document.id,
        input_hash="b" * 64,
        configuration_hash="c" * 64,
        status="COMPLETED",
        request_count=1,
        input_tokens=20,
        output_tokens=4,
    )
    db.add(skill_run)
    db.flush()
    invocation = ModelInvocation(
        skill_run_id=skill_run.id,
        provider_request_id="req_123",
        provider="typesafe",
        model="jev-2026-09-01",
        model_configuration_hash="d" * 64,
        attempt=1,
        request_sequence=1,
        request_count=1,
        input_tokens=20,
        output_tokens=4,
        latency_ms=125,
        status="COMPLETED",
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    db.add(invocation)
    db.flush()
    return {
        "workflow": workflow.id,
        "batch_run": batch_run.id,
        "document": document.id,
        "task_version": task_version.id,
        "skill_run": skill_run.id,
        "invocation": invocation.id,
    }


def _envelope() -> DecisionEnvelope:
    now = datetime.now(UTC)
    return DecisionEnvelope.model_validate(
        {
            "answers": {"privilege.legal_advice": {"type": "noul", "noul": 0.91}},
            "provider": "typesafe",
            "model": "jev-2026-09-01",
            "provider_request_id": "req_123",
            "usage": {"request_count": 1, "input_tokens": 20, "output_tokens": 4},
            "latency_ms": 125,
            "started_at": now,
            "completed_at": now,
            "attempts": 1,
        }
    )


def test_decision_result_is_complete_auditable_and_idempotent(
    db: Session,
    root_admin,
    client: TestClient,
    root_token: str,
) -> None:
    ids = _provenance(db, root_admin)
    kwargs = {
        "workflow_run_id": ids["workflow"],
        "review_batch_run_id": ids["batch_run"],
        "matter_document_id": ids["document"],
        "task_version_id": ids["task_version"],
        "evaluation_skill_run_id": ids["skill_run"],
        "model_invocation_id": ids["invocation"],
        "engine_key": "jev",
        "source_artifact_id": uuid.uuid4(),
        "source_content_hash": "e" * 64,
        "state": {"document": {"paragraphs": [{"number": 1, "text": "Counsel advised the client."}]}},
        "paragraph_map_version": "paragraph-map-v1",
        "envelope": _envelope(),
        "status": "COMPLETED",
        "recommendations": {"privileged": True},
        "routes": {"review": False},
        "evidence": {"privilege.legal_advice": [{"paragraph": 1}]},
    }
    result = record_review_decision_result(db, **kwargs)
    db.flush()

    assert result.coverage == {
        "question_count": 1,
        "answered_question_count": 1,
        "missing_question_keys": [],
        "complete": True,
    }
    assert result.answers["privilege.legal_advice"] == {"type": "noul", "noul": 0.91}
    assert result.definition_content_hash == content_hash("# Legal advice\n\nReview legal advice.")
    assert result.raw_answer_hash == json_content_hash(result.answers)
    assert result.question_set_hash
    assert result.decision_policy_hash
    assert result.model_invocation_id == ids["invocation"]
    assert record_review_decision_result(db, **kwargs).id == result.id

    db.commit()
    response = client.get(
        f"/v1/matters/{db.get(WorkflowRun, ids['workflow']).matter_id}/review-batches/"
        f"{db.get(ReviewBatchRun, ids['batch_run']).review_batch_id}/runs/{ids['batch_run']}/documents/"
        f"{ids['document']}/decision-result",
        headers={"Authorization": f"Bearer {root_token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(result.id)
    assert response.json()["answers"]["privilege.legal_advice"] == {"type": "noul", "noul": 0.91}

    with pytest.raises(ReviewDecisionResultConflict):
        record_review_decision_result(db, **{**kwargs, "routes": {"review": True}})


def test_completed_decision_result_cannot_hide_partial_coverage(db: Session, root_admin) -> None:
    ids = _provenance(db, root_admin)
    envelope = _envelope()
    envelope.answers = {}
    with pytest.raises(ReviewDecisionResultError, match="must answer every"):
        record_review_decision_result(
            db,
            workflow_run_id=ids["workflow"],
            review_batch_run_id=ids["batch_run"],
            matter_document_id=ids["document"],
            task_version_id=ids["task_version"],
            evaluation_skill_run_id=ids["skill_run"],
            model_invocation_id=ids["invocation"],
            engine_key="jev",
            source_artifact_id=uuid.uuid4(),
            source_content_hash="e" * 64,
            state={"document": {"paragraphs": []}},
            paragraph_map_version="paragraph-map-v1",
            envelope=envelope,
            status="COMPLETED",
            recommendations={},
            routes={},
            evidence={},
        )
