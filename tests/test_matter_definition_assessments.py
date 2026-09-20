import asyncio
import uuid
from types import SimpleNamespace

from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel
from sqlalchemy import select

from app.assessment_execution import _execute_document_analysis
from app.document_evidence import build_document_map_plan, segment_paragraphs
from app.matter_definition_assessments import RetrievalHit, merge_retrieval_candidates
from app.models import (
    MatterDefinitionAssessmentRun,
    MatterDefinitionRevision,
    MatterDocument,
    MatterDocumentImportJob,
    ModelInvocation,
    SearchIndexGeneration,
    SkillDefinitionVersion,
    SkillRun,
    Tenant,
    WorkflowRun,
    WorkflowStepRun,
)
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
    created = client.post(base, headers=auth(root_token), json={})
    assert created.status_code == 202, created.text
    payload = created.json()
    assert payload["requested_document_count"] == 500
    assert payload["status"] == "QUEUED"
    assert payload["review_batch_id"] is None
    assert payload["definition_content_hash"]

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

    listed = client.get(base, headers=auth(root_token))
    assert listed.status_code == 200
    assert len(listed.json()) == 2
    with TestingSessionLocal() as db:
        records = list(db.scalars(select(MatterDefinitionAssessmentRun)))
        assert all(record.workflow_run_id for record in records)
        assert all(record.search_index_generation_id for record in records)
        assert all(set(record.binding_snapshot) == {
            "retrieval_planner", "document_analysis", "assessment_synthesis"
        } for record in records)


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
            "responsiveness_summary": [
                {"text": "It matches the instruction.", "citation_ids": ["¶1"]}
            ],
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
        invocations = list(
            db.scalars(select(ModelInvocation).where(ModelInvocation.skill_run_id == skill_run.id))
        )
        assert len(invocations) == len(plan.windows) + 1
        assert {item.attempt for item in invocations} == set(range(1, len(plan.windows) + 2))
