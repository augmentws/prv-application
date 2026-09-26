import asyncio
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel
from sqlalchemy import select

from app.assessment_execution import (
    _coverage_envelope,
    _execute_document_analysis,
    _load_document_analyses,
    _synthesis_context,
    _validate_retrieval_plan,
    _validate_synthesis_refinement,
    fail_assessment,
)
from app.assessment_guidance_refinement import create_guidance_revision
from app.document_evidence import build_document_map_plan, segment_paragraphs
from app.matter_definition_assessments import (
    AssessmentError,
    RetrievalHit,
    SelectedCandidate,
    materialize_assessment_batch,
    merge_retrieval_candidates,
)
from app.models import (
    Matter,
    MatterDefinition,
    MatterDefinitionAssessmentQuestion,
    MatterDefinitionAssessmentRun,
    MatterDefinitionRevision,
    MatterDocument,
    MatterDocumentImportJob,
    ModelInvocation,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    SearchIndexGeneration,
    SkillDefinitionVersion,
    SkillRun,
    Tenant,
    WorkflowRun,
    WorkflowStepRun,
)
from app.routers.matter_definition_assessments import _assessment_reads
from app.standard_skills import ensure_standard_assessment_skills


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_assessment_matter(client: TestClient, root_token: str, root_admin) -> str:
    created_client = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "Assessment Client"},
    )
    assert created_client.status_code == 201, created_client.text
    matter = client.post(
        f"/v1/clients/{created_client.json()['id']}/matters",
        headers=auth(root_token),
        json={"name": "Assessment Matter"},
    )
    assert matter.status_code == 201, matter.text
    matter_id = matter.json()["id"]
    definition = client.post(
        f"/v1/matters/{matter_id}/definition/revisions",
        headers=auth(root_token),
        json={"content_markdown": "# Review instructions\n\nIssue 1: hurricane insurance disruption."},
    )
    assert definition.status_code == 201, definition.text
    with TestingSessionLocal() as db:
        root = db.scalar(select(Tenant).where(Tenant.id == root_admin.tenant_id))
        assert root is not None
        ensure_standard_assessment_skills(db, root, db.merge(root_admin))
        db.add(
            SearchIndexGeneration(
                matter_id=uuid.UUID(matter_id),
                generation=1,
                index_name=f"test-{matter_id}-v1",
                alias_name=f"test-{matter_id}",
                schema_hash="a" * 64,
                status="ACTIVE",
                schema_snapshot={},
            )
        )
        db.commit()
    return matter_id


def test_assessment_launch_pins_inputs_and_requires_large_run_acknowledgment(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    base = f"/v1/matters/{matter_id}/definition-assessments"
    created = client.post(base, headers=auth(root_token), json={"name": "Initial hurricane coverage review"})
    assert created.status_code == 202, created.text
    payload = created.json()
    assert payload["name"] == "Initial hurricane coverage review"
    assert payload["requested_document_count"] == 500
    assert payload["use_batching"] is True
    assert payload["status"] == "QUEUED"
    assert payload["review_batch_id"] is None
    assert payload["definition_content_hash"]

    renamed = client.patch(
        f"{base}/{payload['id']}",
        headers=auth(root_token),
        json={"name": "  Renamed   hurricane assessment  "},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Renamed hurricane assessment"
    blank_name = client.patch(
        f"{base}/{payload['id']}",
        headers=auth(root_token),
        json={"name": "   "},
    )
    assert blank_name.status_code == 422

    warned = client.post(
        base,
        headers=auth(root_token),
        json={"maximum_document_count": 1001},
    )
    assert warned.status_code == 409
    assert "acknowledgment" in warned.json()["error"]["message"]

    acknowledged = client.post(
        base,
        headers=auth(root_token),
        json={"maximum_document_count": 1001, "acknowledge_large_run_warning": True},
    )
    assert acknowledged.status_code == 202, acknowledged.text
    assert acknowledged.json()["large_run_warning_acknowledged"] is True
    assert acknowledged.json()["warning_acknowledged_by_user_id"] == str(root_admin.id)

    realtime = client.post(
        base,
        headers=auth(root_token),
        json={"name": "Real-time assessment", "use_batching": False},
    )
    assert realtime.status_code == 202, realtime.text
    assert realtime.json()["use_batching"] is False

    listed = client.get(base, headers=auth(root_token))
    assert listed.status_code == 200
    assert len(listed.json()) == 3
    assert any(item["name"] == "Renamed hurricane assessment" for item in listed.json())
    with TestingSessionLocal() as db:
        records = list(db.scalars(select(MatterDefinitionAssessmentRun)))
        assert all(record.workflow_run_id for record in records)
        assert all(record.search_index_generation_id for record in records)
        assert all(
            set(record.binding_snapshot)
            == {"retrieval_planner", "document_analysis", "assessment_synthesis", "guidance_refinement"}
            for record in records
        )


def test_answering_all_refinement_questions_queues_and_creates_guidance_draft(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"maximum_document_count": 1},
    )
    assert launched.status_code == 202, launched.text
    assessment_id = uuid.UUID(launched.json()["id"])
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        assessment.status = "COMPLETED"
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        workflow.status = "COMPLETED"
        questions = [
            MatterDefinitionAssessmentQuestion(
                assessment_run_id=assessment.id,
                question="Should storm-adjacent claims be included?",
                rationale="The current boundary is ambiguous.",
                priority="HIGH",
                evidence=[],
                suggested_answers=["Include them.", "Exclude them."],
            ),
            MatterDefinitionAssessmentQuestion(
                assessment_run_id=assessment.id,
                question="Should the guidance require a direct causal link?",
                rationale="Reviewers need a consistent nexus rule.",
                priority="MEDIUM",
                evidence=[],
                suggested_answers=["Require a direct link.", "Allow an indirect link."],
            ),
        ]
        db.add_all(questions)
        db.commit()
        question_ids = [question.id for question in questions]

    first = client.put(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/questions/{question_ids[0]}",
        headers=auth(root_token),
        json={"status": "ANSWERED", "answer": "Include them."},
    )
    assert first.status_code == 200, first.text
    assert first.json()["suggested_answers"] == ["Include them.", "Exclude them."]
    assert first.json()["answer"] == "Include them."
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        assert assessment.guidance_refinement_status == "NOT_READY"

    second = client.put(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/questions/{question_ids[1]}",
        headers=auth(root_token),
        json={"status": "ANSWERED", "answer": "Require a direct link."},
    )
    assert second.status_code == 200, second.text
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        assert assessment.guidance_refinement_status == "QUEUED"
        assert assessment.guidance_refinement_workflow_run_id is not None
        refinement_workflow = db.get(WorkflowRun, assessment.guidance_refinement_workflow_run_id)
        assert refinement_workflow is not None
        assert set(refinement_workflow.binding_snapshot) == {"guidance_refinement"}

        revision_id = create_guidance_revision(
            db,
            assessment.id,
            model=TestModel(
                custom_output_args={
                    "content_markdown": (
                        "# Review instructions\n\nInclude storm-adjacent claims and require a direct causal link."
                    ),
                    "change_summary": ["Clarified the covered claims and causal-link requirement."],
                }
            ),
        )
        db.refresh(assessment)
        revision = db.get(MatterDefinitionRevision, revision_id)
        definition = db.scalar(select(MatterDefinition).where(MatterDefinition.matter_id == uuid.UUID(matter_id)))
        assert revision is not None and definition is not None
        assert revision.source_kind == "ASSESSMENT_REFINEMENT"
        assert revision.source_skill_run_id is not None
        assert revision.based_on_revision == 1
        assert definition.current_revision == 2
        assert definition.published_revision is None
        assert assessment.guidance_refinement_status == "COMPLETED"
        assert assessment.refined_matter_definition_revision_id == revision.id


def test_assessment_reads_use_live_document_counts(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    created = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"maximum_document_count": 10},
    )
    assert created.status_code == 202, created.text
    assessment = SimpleNamespace(**created.json())
    assessment.review_batch_run_id = uuid.uuid4()

    class StubSession:
        def execute(self, statement):
            del statement
            return [
                (assessment.review_batch_run_id, "COMPLETED", 7),
                (assessment.review_batch_run_id, "SKIPPED", 1),
                (assessment.review_batch_run_id, "FAILED", 2),
            ]

    result = _assessment_reads(StubSession(), [assessment])[0]  # type: ignore[arg-type]

    assert result.summarized_count == 7
    assert result.skipped_count == 1
    assert result.failed_count == 2


def test_execution_can_omit_document_skill_runs(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"maximum_document_count": 1},
    )
    assert launched.status_code == 202, launched.text
    assessment_id = uuid.UUID(launched.json()["id"])
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        step = WorkflowStepRun(
            workflow_run_id=assessment.workflow_run_id,
            role_key="document_analysis",
            ordinal=3,
            status="COMPLETED",
            total_count=1,
            completed_count=1,
        )
        db.add(step)
        db.flush()
        db.add(
            SkillRun(
                workflow_run_id=assessment.workflow_run_id,
                workflow_step_run_id=step.id,
                skill_definition_version_id=uuid.UUID(
                    assessment.binding_snapshot["document_analysis"]["skill_definition_version_id"]
                ),
                scope_type="MATTER_DOCUMENT",
                scope_id=uuid.uuid4(),
                input_hash="a" * 64,
                configuration_hash="b" * 64,
                output_artifact_id=uuid.uuid4(),
                status="COMPLETED",
            )
        )
        db.commit()

    base = f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/execution"
    detailed = client.get(base, headers=auth(root_token))
    assert detailed.status_code == 200, detailed.text
    assert len(detailed.json()["skill_runs"]) == 1

    compact = client.get(f"{base}?include_skill_runs=false", headers=auth(root_token))
    assert compact.status_code == 200, compact.text
    assert compact.json()["skill_runs"] == []


def test_candidate_merge_is_deterministic_deduplicated_and_preserves_provenance() -> None:
    first, second, third, control = (uuid.uuid4() for _ in range(4))
    hits = [
        RetrievalHit(first, 1, "issue_1", 1, 9.0),
        RetrievalHit(second, 1, "issue_1", 2, 8.0),
        RetrievalHit(first, 2, "issue_2", 2, 0.8),
        RetrievalHit(third, 2, "issue_2", 1, 0.9),
    ]
    selected, candidates = merge_retrieval_candidates(
        hits,
        maximum_document_count=4,
        query_quotas={1: 1, 2: 1},
        control_document_ids=[first, second, third, control],
        control_sample_size=1,
        seed="stable-seed",
    )
    repeated, _ = merge_retrieval_candidates(
        list(reversed(hits)),
        maximum_document_count=4,
        query_quotas={1: 1, 2: 1},
        control_document_ids=[control, third, second, first],
        control_sample_size=1,
        seed="stable-seed",
    )
    assert [item.document_id for item in selected] == [item.document_id for item in repeated]
    assert len({item.document_id for item in selected}) == len(selected)
    first_candidate = next(item for item in candidates if item.document_id == first)
    assert {item["criterion_key"] for item in first_candidate.provenance} == {"issue_1", "issue_2"}
    assert selected[-1].document_id == control
    assert selected[-1].reason == "CONTROL_SAMPLE"


def test_retrieval_plan_validator_requires_controlled_nested_search() -> None:
    _validate_retrieval_plan(
        {
            "queries": [
                {
                    "criterion_key": "issue_1",
                    "criterion_label": "Issue 1",
                    "rationale": "Find direct evidence.",
                    "quota": 50,
                    "search": {"query": "insurance non-renewal", "search_mode": "HYBRID"},
                }
            ]
        }
    )

    with pytest.raises(AssessmentError, match="controlled search request"):
        _validate_retrieval_plan(
            {
                "queries": [
                    {
                        "criterion_key": "issue_1",
                        "criterion_label": "Issue 1",
                        "rationale": "Uses the wrong query contract.",
                        "quota": 50,
                        "query_string": "insurance non-renewal",
                        "search_type": "keyword",
                    }
                ]
            }
        )


def test_failed_assessment_marks_running_step_failed_with_skill_error(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"maximum_document_count": 1},
    )
    assert launched.status_code == 202, launched.text

    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(launched.json()["id"]))
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        step = WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key="retrieval_planner",
            ordinal=1,
            status="RUNNING",
            total_count=1,
        )
        db.add(step)
        db.flush()
        version = db.get(
            SkillDefinitionVersion,
            uuid.UUID(assessment.binding_snapshot["retrieval_planner"]["skill_definition_version_id"]),
        )
        assert version is not None
        skill_run = SkillRun(
            workflow_run_id=workflow.id,
            workflow_step_run_id=step.id,
            skill_definition_version_id=version.id,
            scope_type="MATTER_DEFINITION_REVISION",
            scope_id=assessment.matter_definition_revision_id,
            input_hash="a" * 64,
            configuration_hash="b" * 64,
            status="FAILED",
            error_code="INVALID_OUTPUT",
            error_message="Planner returned an incompatible query.",
        )
        db.add(skill_run)
        db.commit()

        fail_assessment(db, assessment.id, "DBOS retries exhausted")
        db.refresh(step)
        assert step.status == "FAILED"
        assert step.failed_count == 1
        assert step.error_message == "Planner returned an incompatible query."
        assert step.completed_at is not None

    retried = client.post(
        f"/v1/matters/{matter_id}/definition-assessments/{launched.json()['id']}/retry",
        headers=auth(root_token),
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["status"] == "QUEUED"
    assert retried.json()["error_message"] is None
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, uuid.UUID(launched.json()["id"]))
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        step = db.scalar(
            select(WorkflowStepRun).where(WorkflowStepRun.workflow_run_id == assessment.workflow_run_id)
        )
        assert workflow is not None and step is not None
        assert workflow.status == "QUEUED"
        assert ":retry:" in workflow.dbos_workflow_id
        assert step.status == "QUEUED"
        assert assessment.configuration_snapshot["retry_history"]

    repeated_retry = client.post(
        f"/v1/matters/{matter_id}/definition-assessments/{launched.json()['id']}/retry",
        headers=auth(root_token),
    )
    assert repeated_retry.status_code == 409


def test_completed_with_errors_can_retry_failed_documents(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"maximum_document_count": 5},
    )
    assert launched.status_code == 202, launched.text

    assessment_id = uuid.UUID(launched.json()["id"])
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        assessment.status = "COMPLETED_WITH_ERRORS"
        assessment.selected_count = 5
        assessment.summarized_count = 3
        assessment.failed_count = 2
        assessment.partial_coverage_count = 1
        assessment.invalid_result_count = 1
        assessment.coverage_snapshot = {"status": "SUFFICIENT"}
        assessment.synthesis_result = {"status": "COMPLETED", "findings": ["stale"]}
        assessment.completed_at = assessment.created_at
        workflow.status = "COMPLETED_WITH_ERRORS"
        db.add_all(
            [
                WorkflowStepRun(
                    workflow_run_id=workflow.id,
                    role_key="document_analysis",
                    ordinal=3,
                    status="COMPLETED_WITH_ERRORS",
                    total_count=5,
                    completed_count=3,
                    failed_count=2,
                ),
                WorkflowStepRun(
                    workflow_run_id=workflow.id,
                    role_key="assessment_synthesis",
                    ordinal=4,
                    status="COMPLETED",
                    total_count=1,
                    completed_count=1,
                ),
            ]
        )
        db.commit()

    retried = client.post(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/retry",
        headers=auth(root_token),
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["status"] == "QUEUED"
    assert retried.json()["summarized_count"] == 3
    assert retried.json()["failed_count"] == 0
    assert retried.json()["coverage_snapshot"] is None
    assert retried.json()["synthesis_result"] is None

    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        steps = list(
            db.scalars(
                select(WorkflowStepRun)
                .where(WorkflowStepRun.workflow_run_id == assessment.workflow_run_id)
                .order_by(WorkflowStepRun.ordinal)
            )
        )
        assert [step.status for step in steps] == ["QUEUED", "QUEUED"]
        assert steps[0].completed_count == 3
        assert steps[0].failed_count == 0
        assert steps[1].completed_count == 0
        assert assessment.configuration_snapshot["retry_history"][-1]["previous_failed_document_count"] == 2


def test_completed_assessment_can_regenerate_only_synthesis(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"maximum_document_count": 5},
    )
    assert launched.status_code == 202, launched.text
    assessment_id = uuid.UUID(launched.json()["id"])
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        assessment.status = "COMPLETED"
        assessment.selected_count = 5
        assessment.summarized_count = 5
        assessment.synthesis_result = {
            "status": "COMPLETED",
            "clarification_questions": [],
        }
        workflow.status = "COMPLETED"
        db.commit()

    regenerated = client.post(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/regenerate-synthesis",
        headers=auth(root_token),
    )
    assert regenerated.status_code == 202, regenerated.text
    assert regenerated.json()["status"] == "QUEUED"
    assert regenerated.json()["summarized_count"] == 5
    assert regenerated.json()["synthesis_result"] is None
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        assert ":synthesis:" in workflow.dbos_workflow_id
        assert workflow.code_version == "8"
        assert (
            assessment.binding_snapshot["assessment_synthesis"]["output_schema_key"]
            == "matter_definition_assessment_synthesis_output_v4"
        )
        step = db.scalar(
            select(WorkflowStepRun).where(
                WorkflowStepRun.workflow_run_id == workflow.id,
                WorkflowStepRun.ordinal == 4,
            )
        )
        assert step is not None
        assert step.status == "QUEUED"


def test_completed_assessment_can_reanalyze_same_frozen_batch(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"maximum_document_count": 2},
    )
    assert launched.status_code == 202, launched.text
    assessment_id = uuid.UUID(launched.json()["id"])
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        job = MatterDocumentImportJob(
            matter_id=uuid.UUID(matter_id),
            source_collection_id=uuid.uuid4(),
            selection_type="EXPLICIT",
            selection={},
            selection_summary="Reanalysis fixture",
            status="COMPLETED",
            workflow_id=f"fixture:{uuid.uuid4()}",
            created_by_user_id=root_admin.id,
        )
        db.add(job)
        db.flush()
        documents = [
            MatterDocument(
                matter_id=uuid.UUID(matter_id),
                source_collection_id=job.source_collection_id,
                collection_item_id=uuid.uuid4(),
                added_by_import_job_id=job.id,
            )
            for _ in range(2)
        ]
        db.add_all(documents)
        db.flush()
        selected = [
            SelectedCandidate(document.id, 1.0, ({"query_ordinal": 1},), "CRITERION_QUOTA")
            for document in documents
        ]
        materialize_assessment_batch(
            db,
            assessment,
            selected=selected,
            all_candidates=selected,
            query_plan={"criteria": [], "queries": []},
        )
        db.flush()
        previous_review_run_id = assessment.review_batch_run_id
        assert previous_review_run_id is not None
        previous_review_run = db.get(ReviewBatchRun, previous_review_run_id)
        assert previous_review_run is not None
        previous_review_run.status = "COMPLETED"
        for run_document in db.scalars(
            select(ReviewBatchRunDocument).where(
                ReviewBatchRunDocument.review_batch_run_id == previous_review_run_id
            )
        ):
            run_document.status = "COMPLETED"
        assessment.status = "COMPLETED"
        assessment.summarized_count = 2
        assessment.synthesis_result = {"status": "COMPLETED"}
        workflow.status = "COMPLETED"
        db.commit()

    regenerated = client.post(
        f"/v1/matters/{matter_id}/definition-assessments/{assessment_id}/regenerate-document-analyses",
        headers=auth(root_token),
    )
    assert regenerated.status_code == 202, regenerated.text
    assert regenerated.json()["status"] == "QUEUED"
    assert regenerated.json()["summarized_count"] == 0
    assert regenerated.json()["synthesis_result"] is None

    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        assert assessment.review_batch_run_id != previous_review_run_id
        current_review_run = db.get(ReviewBatchRun, assessment.review_batch_run_id)
        assert current_review_run is not None
        assert current_review_run.parent_run_id == previous_review_run_id
        assert current_review_run.status == "QUEUED"
        assert set(
            db.scalars(
                select(ReviewBatchRunDocument.status).where(
                    ReviewBatchRunDocument.review_batch_run_id == current_review_run.id
                )
            )
        ) == {"QUEUED"}
        assert set(
            db.scalars(
                select(ReviewBatchRunDocument.status).where(
                    ReviewBatchRunDocument.review_batch_run_id == previous_review_run_id
                )
            )
        ) == {"COMPLETED"}
        assert ":analysis:" in workflow.dbos_workflow_id
        assert workflow.code_version == "8"
        history = assessment.configuration_snapshot["document_analysis_regeneration_history"]
        assert history[-1]["previous_review_batch_run_id"] == str(previous_review_run_id)
        steps = list(
            db.scalars(
                select(WorkflowStepRun)
                .where(
                    WorkflowStepRun.workflow_run_id == workflow.id,
                    WorkflowStepRun.ordinal.in_((3, 4)),
                )
                .order_by(WorkflowStepRun.ordinal)
            )
        )
        assert [step.status for step in steps] == ["QUEUED", "QUEUED"]
        assert [step.total_count for step in steps] == [2, 1]


def test_synthesis_loads_only_latest_completed_analysis_per_document(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"maximum_document_count": 1},
    )
    assert launched.status_code == 202, launched.text
    assessment_id = uuid.UUID(launched.json()["id"])
    document_id = uuid.uuid4()
    old_artifact_id = uuid.uuid4()
    current_artifact_id = uuid.uuid4()

    def read_fixture(*, artifact_id, **_kwargs) -> bytes:
        return json.dumps(
            {
                "result": {
                    "summary": [{"text": str(artifact_id), "citation_ids": ["¶1"]}],
                    "coverage": {"status": "COMPLETE"},
                }
            }
        ).encode()

    monkeypatch.setattr("app.assessment_execution.read_artifact_bytes", read_fixture)
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        matter = db.get(Matter, uuid.UUID(matter_id))
        assert assessment is not None and matter is not None
        step = WorkflowStepRun(
            workflow_run_id=assessment.workflow_run_id,
            role_key="document_analysis",
            ordinal=3,
            status="COMPLETED",
            total_count=1,
            completed_count=1,
        )
        db.add(step)
        db.flush()
        version_id = uuid.UUID(
            assessment.binding_snapshot["document_analysis"]["skill_definition_version_id"]
        )
        db.add_all(
            [
                SkillRun(
                    workflow_run_id=assessment.workflow_run_id,
                    workflow_step_run_id=step.id,
                    skill_definition_version_id=version_id,
                    scope_type="MATTER_DOCUMENT",
                    scope_id=document_id,
                    input_hash="a" * 64,
                    configuration_hash="b" * 64,
                    output_artifact_id=old_artifact_id,
                    status="COMPLETED",
                    created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                ),
                SkillRun(
                    workflow_run_id=assessment.workflow_run_id,
                    workflow_step_run_id=step.id,
                    skill_definition_version_id=version_id,
                    scope_type="MATTER_DOCUMENT",
                    scope_id=document_id,
                    input_hash="c" * 64,
                    configuration_hash="d" * 64,
                    output_artifact_id=current_artifact_id,
                    status="COMPLETED",
                    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ),
            ]
        )
        db.commit()
        analyses, partial, invalid = _load_document_analyses(db, assessment, matter)

    assert partial == 0
    assert invalid == 0
    assert len(analyses) == 1
    assert analyses[0]["output_artifact_id"] == str(current_artifact_id)


def test_synthesis_coverage_policy_blocks_substantive_results_for_small_or_incomplete_samples() -> None:
    assessment = SimpleNamespace(selected_count=10, skipped_count=2, failed_count=3)
    insufficient = _coverage_envelope(assessment, successful=2, partial=1, invalid=1)
    assert insufficient["status"] == "INSUFFICIENT"
    assert insufficient["successful_document_ratio"] == 0.2
    assert insufficient["policy"]["version"] == "assessment_synthesis_coverage_v1"

    sufficient = _coverage_envelope(assessment, successful=5, partial=1, invalid=0)
    assert sufficient["status"] == "SUFFICIENT"


def test_synthesis_context_exposes_computed_counts_and_refinement_signals() -> None:
    coverage = {
        "selected_document_count": 3,
        "skipped_document_count": 1,
        "failed_document_count": 0,
        "partial_coverage_document_count": 0,
        "invalid_result_count": 0,
    }
    analyses = [
        {
            "matter_document_id": str(uuid.uuid4()),
            "analysis": {
                "determination": "RESPONSIVE",
                "criterion_matches": [
                    {
                        "criterion_key": "issue_7",
                        "label": "Related insurance lines",
                        "match_type": "NEAR_MISS",
                        "reasoning": "The boundary is unclear.",
                        "citation_ids": ["¶2"],
                    }
                ],
                "clarification_requests": [],
                "limitations": ["An attachment was unavailable."],
            },
        },
        {
            "matter_document_id": str(uuid.uuid4()),
            "analysis": {
                "determination": "NON_RESPONSIVE",
                "criterion_matches": [],
                "clarification_requests": [],
                "limitations": [],
            },
        },
    ]

    statistics, signals = _synthesis_context(analyses, coverage)

    assert statistics["analyzed_document_count"] == 2
    assert statistics["determination_counts"] == {"NON_RESPONSIVE": 1, "RESPONSIVE": 1}
    assert signals["near_miss_count"] == 1
    assert signals["near_misses"][0]["paragraph_ids"] == ["¶2"]
    assert signals["recurring_near_miss_pattern_count"] == 0
    assert signals["document_limitation_count"] == 1


def test_recurring_near_miss_patterns_require_evidence_backed_questions() -> None:
    document_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    analyses = [
        {
            "matter_document_id": document_id,
            "analysis": {
                "summary": [{"text": "Recurring boundary evidence", "citation_ids": ["¶1"]}],
                "determination": "NON_RESPONSIVE",
                "criterion_matches": [
                    {
                        "criterion_key": criterion_key,
                        "label": "Related insurance lines",
                        "match_type": "NEAR_MISS",
                        "reasoning": "The document concerns another insurance line without the required nexus.",
                        "citation_ids": ["¶1"],
                    }
                ],
                "clarification_requests": [],
                "limitations": [],
            },
        }
        for document_id, criterion_key in zip(document_ids, ["Issue 7", "ISSUE_7_RELATED_LINES"], strict=True)
    ]
    coverage = {
        "status": "SUFFICIENT",
        "selected_document_count": 2,
        "skipped_document_count": 0,
        "failed_document_count": 0,
        "partial_coverage_document_count": 0,
        "invalid_result_count": 0,
    }
    _, signals = _synthesis_context(analyses, coverage)
    assert signals["recurring_near_miss_pattern_count"] == 1
    assert signals["recurring_near_miss_patterns"][0]["signal_id"] == "NEAR_MISS_ISSUE_7"
    assert signals["recurring_near_miss_patterns"][0]["document_count"] == 2

    dimensions = [
        {
            "dimension": dimension,
            "conclusion": "NO_REFINEMENT_NEEDED",
            "rationale": "No change proposed.",
            "evidence": [],
        }
        for dimension in sorted(
            {
                "INCLUSION_EXCLUSION_BOUNDARIES",
                "UNCOVERED_SUBJECTS",
                "CONFLICTING_TREATMENT",
                "TEMPORAL_SCOPE",
                "GEOGRAPHIC_SCOPE",
                "ACTOR_ENTITY_SCOPE",
            }
        )
    ]
    no_refinement = {
        "refinement_assessment": {
            "outcome": "NO_REFINEMENT_WARRANTED",
            "rationale": "The existing boundary was applied consistently.",
            "evaluated_dimensions": dimensions,
        },
        "clarification_questions": [],
    }
    with pytest.raises(ValueError, match="require clarification questions"):
        _validate_synthesis_refinement(no_refinement, analyses, coverage, signals)

    evidence = [
        {
            "matter_document_id": document_ids[0],
            "paragraph_ids": ["¶1"],
            "reason": "Representative recurring boundary evidence.",
        }
    ]
    proposed = {
        "refinement_assessment": {
            "outcome": "QUESTIONS_PROPOSED",
            "rationale": "The recurring boundary requires confirmation.",
            "evaluated_dimensions": [
                {
                    **item,
                    "conclusion": "QUESTION_NEEDED",
                    "evidence": evidence,
                }
                if item["dimension"] == "INCLUSION_EXCLUSION_BOUNDARIES"
                else item
                for item in dimensions
            ],
        },
        "clarification_questions": [
            {
                "question": "Should unrelated insurance lines remain excluded without a crisis nexus?",
                "evidence": evidence,
                "suggested_answers": ["Yes, keep them excluded.", "No, include them."],
            }
        ],
    }
    _validate_synthesis_refinement(proposed, analyses, coverage, signals)


def test_synthesis_refinement_requires_questions_or_structured_no_refinement_explanation() -> None:
    document_id = str(uuid.uuid4())
    analyses = [
        {
            "matter_document_id": document_id,
            "analysis": {
                "summary": [{"text": "Evidence", "citation_ids": ["¶1"]}],
                "criterion_matches": [],
            },
        }
    ]
    dimensions = [
        {
            "dimension": dimension,
            "conclusion": "NO_REFINEMENT_NEEDED",
            "rationale": "The current instruction is consistently applied.",
            "evidence": [],
        }
        for dimension in sorted(
            {
                "INCLUSION_EXCLUSION_BOUNDARIES",
                "UNCOVERED_SUBJECTS",
                "CONFLICTING_TREATMENT",
                "TEMPORAL_SCOPE",
                "GEOGRAPHIC_SCOPE",
                "ACTOR_ENTITY_SCOPE",
            }
        )
    ]
    valid = {
        "refinement_assessment": {
            "outcome": "NO_REFINEMENT_WARRANTED",
            "rationale": "No recurring ambiguity was found after evaluating every dimension.",
            "evaluated_dimensions": dimensions,
        },
        "clarification_questions": [],
    }
    _validate_synthesis_refinement(valid, analyses, {"status": "SUFFICIENT"})

    invalid = {
        **valid,
        "refinement_assessment": {**valid["refinement_assessment"], "outcome": "QUESTIONS_PROPOSED"},
    }
    with pytest.raises(ValueError, match="requires at least one"):
        _validate_synthesis_refinement(invalid, analyses, {"status": "SUFFICIENT"})

    proposed = {
        "refinement_assessment": {
            "outcome": "QUESTIONS_PROPOSED",
            "rationale": "A recurring boundary needs a policy choice.",
            "evaluated_dimensions": [
                {
                    **item,
                    "conclusion": "QUESTION_NEEDED",
                    "evidence": [
                        {
                            "matter_document_id": document_id,
                            "paragraph_ids": ["¶1"],
                            "reason": "Representative boundary evidence.",
                        }
                    ],
                }
                if item["dimension"] == "INCLUSION_EXCLUSION_BOUNDARIES"
                else item
                for item in dimensions
            ],
        },
        "clarification_questions": [
            {
                "question": "Should this boundary be included?",
                "suggested_answers": ["Include it.", "Exclude it."],
                "evidence": [
                    {
                        "matter_document_id": document_id,
                        "paragraph_ids": ["¶1"],
                        "reason": "Representative boundary evidence.",
                    }
                ],
            }
        ],
    }
    _validate_synthesis_refinement(proposed, analyses, {"status": "SUFFICIENT"})


def test_long_document_map_reduce_uses_one_skill_run(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    launched = client.post(
        f"/v1/matters/{matter_id}/definition-assessments",
        headers=auth(root_token),
        json={"maximum_document_count": 1},
    )
    assert launched.status_code == 202, launched.text
    assessment_id = uuid.UUID(launched.json()["id"])
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        revision = db.get(MatterDefinitionRevision, assessment.matter_definition_revision_id)
        assert workflow is not None and revision is not None
        job = MatterDocumentImportJob(
            matter_id=uuid.UUID(matter_id),
            source_collection_id=uuid.uuid4(),
            selection_type="EXPLICIT",
            selection={},
            selection_summary="Map/reduce fixture",
            status="COMPLETED",
            workflow_id=f"fixture:{uuid.uuid4()}",
            created_by_user_id=root_admin.id,
        )
        db.add(job)
        db.flush()
        document = MatterDocument(
            matter_id=uuid.UUID(matter_id),
            source_collection_id=job.source_collection_id,
            collection_item_id=uuid.uuid4(),
            added_by_import_job_id=job.id,
        )
        db.add(document)
        step = WorkflowStepRun(
            workflow_run_id=workflow.id,
            role_key="document_analysis",
            ordinal=3,
            fan_out_group="documents",
            status="RUNNING",
            total_count=1,
        )
        db.add(step)
        db.flush()
        version = db.get(
            SkillDefinitionVersion,
            uuid.UUID(assessment.binding_snapshot["document_analysis"]["skill_definition_version_id"]),
        )
        assert version is not None
        paragraph_map = segment_paragraphs("evidence " * 120)
        plan = build_document_map_plan(paragraph_map, max_characters=120, overlap_segments=0)
        assert len(plan.windows) > 1
        output = {
            "summary": [{"text": "The document contains evidence.", "citation_ids": ["¶1"]}],
            "determination": "RESPONSIVE",
            "confidence": 0.9,
            "responsiveness_summary": [{"text": "It matches the instruction.", "citation_ids": ["¶1"]}],
            "criterion_matches": [],
            "scope_analysis": [],
            "countervailing_considerations": [],
            "clarification_requests": [],
            "limitations": [],
            "coverage": {
                "status": "COMPLETE",
                "map_plan_version": plan.version,
                "paragraph_map_version": paragraph_map.version,
                "window_count": 1,
                "successful_window_count": 1,
                "analyzed_paragraph_ids": ["¶1"],
                "partial_paragraph_ids": [],
                "omitted_ranges": [],
            },
        }
        result, skill_run = asyncio.run(
            _execute_document_analysis(
                db,
                assessment=assessment,
                workflow=workflow,
                step=step,
                version=version,
                revision=revision,
                document=document,
                source=SimpleNamespace(artifact_id=uuid.uuid4(), content_hash="b" * 64),
                paragraph_map=paragraph_map,
                plan=plan,
                model=TestModel(custom_output_args=output),
            )
        )
        db.commit()
        assert result["coverage"]["window_count"] == len(plan.windows)
        assert skill_run.status == "COMPLETED"
        assert len(list(db.scalars(select(SkillRun).where(SkillRun.scope_id == document.id)))) == 1
        invocations = list(db.scalars(select(ModelInvocation).where(ModelInvocation.skill_run_id == skill_run.id)))
        assert len(invocations) == len(plan.windows) + 1
        assert {item.attempt for item in invocations} == set(range(1, len(plan.windows) + 2))
