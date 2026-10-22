import io
import uuid

import pyarrow.parquet as pq
import pytest
from conftest import TestingSessionLocal, create_client_tenant
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.analysis_task_batch_set_runs as batch_set_runs
import app.analysis_task_batch_sets as batch_sets
from app.analysis_task_skills import ensure_standard_analysis_task_skills
from app.job_executions import ANALYSIS_TASK_BATCH_SET_RUN_JOB_TYPE, transition_job_execution
from app.models import (
    AnalysisInputManifest,
    AnalysisTaskBatchSetRun,
    AnalysisTaskBatchSetRunChild,
    AuditRecord,
    MatterAnalysisTaskVersion,
    MatterDefinitionAssessmentQuery,
    MatterDefinitionAssessmentQuestion,
    MatterDefinitionAssessmentRun,
    MetadataDefinition,
    Notification,
    ReviewBatchDocument,
    ReviewBatchRun,
    ReviewBatchRunDocument,
    ReviewBatchRunValue,
    ReviewProgram,
    ReviewProgramChild,
    ReviewProgramEvent,
    ReviewProgramRun,
    ReviewProgramStageAttempt,
    ReviewProgramVersion,
    SearchIndexGeneration,
    Tenant,
    WorkflowRun,
    utcnow,
)
from app.rbac_manifest import SYSTEM_ROLE_BY_KEY
from app.review_programs import (
    ReviewProgramError,
    link_review_program_child,
    project_review_program_assessment_terminal,
    project_review_program_qa_compilation_terminal,
)
from app.review_workflow_templates import FIRST_PASS_REVIEW, get_review_workflow_template
from app.standard_skills import ensure_standard_assessment_skills
from tests.test_review_batches import add_documents


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(client: TestClient, email: str, password: str) -> str:
    response = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def review_program_workspace(client: TestClient, root_token: str) -> tuple[dict, dict, dict, str]:
    parent_tenant_id = create_client_tenant(client, root_token)
    slug = f"review-program-{uuid.uuid4().hex[:10]}"
    password = "review-program-admin-password"
    tenant_response = client.post(
        "/v1/tenants",
        headers=auth(root_token),
        json={
            "parent_tenant_id": parent_tenant_id,
            "slug": slug,
            "name": "Review Program Tenant",
            "initial_admin": {
                "email": f"{slug}@example.com",
                "display_name": "Review Program Admin",
                "password": password,
            },
        },
    )
    assert tenant_response.status_code == 201, tenant_response.text
    created = tenant_response.json()
    token = login(client, created["initial_admin"]["email"], password)
    client_response = client.post(
        f"/v1/tenants/{created['tenant']['id']}/clients",
        headers=auth(token),
        json={"name": "Review Program Client"},
    )
    assert client_response.status_code == 201, client_response.text
    matter_response = client.post(
        f"/v1/clients/{client_response.json()['id']}/matters",
        headers=auth(token),
        json={"name": "Review Program Matter"},
    )
    assert matter_response.status_code == 201, matter_response.text
    return created, client_response.json(), matter_response.json(), token


def enable_definition_assessments(matter_id: str, root_admin) -> None:
    with TestingSessionLocal() as db:
        root = db.scalar(select(Tenant).where(Tenant.id == root_admin.tenant_id))
        assert root is not None
        ensure_standard_assessment_skills(db, root, db.merge(root_admin))
        ensure_standard_analysis_task_skills(db, root, db.merge(root_admin))
        db.add(
            SearchIndexGeneration(
                matter_id=uuid.UUID(matter_id),
                generation=1,
                index_name=f"test-review-program-{matter_id}-v1",
                alias_name=f"test-review-program-{matter_id}",
                schema_hash="a" * 64,
                status="ACTIVE",
                schema_snapshot={},
            )
        )
        db.commit()


def prepared_manifest_document_ids(prepared) -> list[uuid.UUID]:
    table = pq.read_table(io.BytesIO(prepared.content), columns=["matter_document_id"])
    return [uuid.UUID(value) for value in table.column("matter_document_id").to_pylist()]


def complete_discovery_profile_child(
    db,
    *,
    child: AnalysisTaskBatchSetRunChild,
    destination_id: uuid.UUID,
    value: bool,
    fail: bool = False,
) -> None:
    launched = batch_set_runs.launch_child(db, child.id)
    review_run = db.get(ReviewBatchRun, launched.child_run_id)
    workflow = db.get(WorkflowRun, launched.workflow_record_id)
    assert review_run is not None and workflow is not None
    if fail:
        review_run.status = "FAILED"
        review_run.error_message = "Synthetic provider failure"
        review_run.completed_at = utcnow()
        workflow.status = "FAILED"
        workflow.error_message = review_run.error_message
        workflow.completed_at = utcnow()
        child.status = "FAILED"
        child.error_message = review_run.error_message
        child.completed_at = utcnow()
        return
    document_ids = list(
        db.scalars(
            select(ReviewBatchDocument.matter_document_id)
            .where(ReviewBatchDocument.review_batch_id == child.review_batch_id)
            .order_by(ReviewBatchDocument.sequence_number)
        )
    )
    for document_id in document_ids:
        run_document = db.get(
            ReviewBatchRunDocument,
            (review_run.id, document_id),
        )
        assert run_document is not None
        run_document.status = "COMPLETED"
        run_document.completed_at = utcnow()
        db.add(
            ReviewBatchRunValue(
                review_batch_run_id=review_run.id,
                matter_document_id=document_id,
                metadata_definition_id=destination_id,
                value_ordinal=0,
                value_boolean=value,
            )
        )
    review_run.status = "COMPLETED"
    review_run.processed_document_count = len(document_ids)
    review_run.completed_at = utcnow()
    workflow.status = "COMPLETED"
    workflow.request_count = len(document_ids)
    workflow.input_tokens = len(document_ids) * 10
    workflow.output_tokens = len(document_ids) * 2
    workflow.completed_at = utcnow()
    child.status = "COMPLETED"
    child.completed_at = utcnow()


def complete_discovery_profile_run(
    db,
    *,
    profile_run: AnalysisTaskBatchSetRun,
    destination_id: uuid.UUID,
    value: bool,
    failed_child_ordinal: int | None = None,
) -> None:
    transition_job_execution(
        db,
        job_type=ANALYSIS_TASK_BATCH_SET_RUN_JOB_TYPE,
        job_id=profile_run.id,
        status="RUNNING",
    )
    children = list(
        db.scalars(
            select(AnalysisTaskBatchSetRunChild)
            .where(AnalysisTaskBatchSetRunChild.batch_set_run_id == profile_run.id)
            .order_by(AnalysisTaskBatchSetRunChild.ordinal)
        )
    )
    for child in children:
        complete_discovery_profile_child(
            db,
            child=child,
            destination_id=destination_id,
            value=value,
            fail=child.ordinal == failed_child_ordinal,
        )
    batch_set_runs.complete_batch_set_run(db, profile_run.id)


def test_first_pass_template_contract_locks_batching_policy() -> None:
    template = get_review_workflow_template("FIRST_PASS_REVIEW")
    assert template is FIRST_PASS_REVIEW
    assert [phase.ordinal for phase in template.phases] == [1, 2, 3, 4, 5]
    assert len(template.content_hash) == 64
    assert {
        stage.key for stage in template.stages if stage.batching_policy == "AD_HOC_BATCH"
    } == {"DEFINITION_ASSESSMENT", "DISAGREEMENT_REVIEW"}
    assert {
        stage.key for stage in template.stages if stage.batching_policy == "WHOLE_MATTER"
    } == {"WHOLE_COLLECTION_SCORING"}
    discovery = next(stage for stage in template.stages if stage.key == "DISCOVERY_QA")
    assert discovery.batching_policy == "PARTITIONED_SET"
    assert discovery.requires_human_gate is True


def test_review_program_permissions_are_assigned_to_expected_roles() -> None:
    assert "review_program.manage" in SYSTEM_ROLE_BY_KEY["MATTER_ADMINISTRATOR"].permission_keys
    assert "review_program.manage" not in SYSTEM_ROLE_BY_KEY["REVIEWER"].permission_keys
    assert "review_program.view" in SYSTEM_ROLE_BY_KEY["REVIEWER"].permission_keys
    assert "review_program.view" in SYSTEM_ROLE_BY_KEY["READ_ONLY"].permission_keys


def test_create_multiple_independent_programs_from_one_template(
    client: TestClient,
    root_token: str,
) -> None:
    created, _, matter, token = review_program_workspace(client, root_token)
    templates = client.get("/v1/review-workflow-templates", headers=auth(token))
    assert templates.status_code == 200, templates.text
    assert [template["key"] for template in templates.json()] == ["FIRST_PASS_REVIEW"]
    template = templates.json()[0]
    assert template["content_hash"] == FIRST_PASS_REVIEW.content_hash
    assert {
        stage["key"] for stage in template["stages"] if stage["batching_policy"] == "AD_HOC_BATCH"
    } == {"DEFINITION_ASSESSMENT", "DISAGREEMENT_REVIEW"}

    created_programs = []
    for key, name in (("standard-first-pass", "Standard first pass"), ("privilege-first-pass", "Privilege pass")):
        response = client.post(
            f"/v1/matters/{matter['id']}/review-programs",
            headers=auth(token),
            json={
                "template_key": "FIRST_PASS_REVIEW",
                "key": key,
                "name": name,
                "description": f"Independent guidance for {name}",
            },
        )
        assert response.status_code == 201, response.text
        program = response.json()
        assert program["status"] == "DRAFT"
        assert program["owner_user_id"] == created["initial_admin"]["id"]
        assert program["current_run_id"] is None
        assert program["current_version"]["version"] == 1
        assert program["current_version"]["configuration_stage"] == "MATTER_DEFINITION"
        assert program["current_version"]["template_content_hash"] == FIRST_PASS_REVIEW.content_hash
        created_programs.append(program)

    assert created_programs[0]["id"] != created_programs[1]["id"]
    listing = client.get(f"/v1/matters/{matter['id']}/review-programs", headers=auth(token))
    assert listing.status_code == 200, listing.text
    assert [program["key"] for program in listing.json()] == ["standard-first-pass", "privilege-first-pass"]

    duplicate = client.post(
        f"/v1/matters/{matter['id']}/review-programs",
        headers=auth(token),
        json={"key": "standard-first-pass", "name": "Duplicate"},
    )
    assert duplicate.status_code == 409

    with TestingSessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ReviewProgram)) == 2
        assert db.scalar(select(func.count()).select_from(ReviewProgramVersion)) == 2
        audit = db.scalar(
            select(AuditRecord)
            .where(AuditRecord.action == "review_program.created")
            .order_by(AuditRecord.created_at.desc())
        )
        assert audit is not None
        assert audit.details["matter_id"] == matter["id"]


def test_synthetic_run_advances_waits_resumes_retries_reconciles_and_cancels(
    client: TestClient,
    root_token: str,
    root_admin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, matter, token = review_program_workspace(client, root_token)
    enable_definition_assessments(matter["id"], root_admin)
    create_response = client.post(
        f"/v1/matters/{matter['id']}/review-programs",
        headers=auth(token),
        json={"key": "coordinator-proof", "name": "Coordinator proof"},
    )
    assert create_response.status_code == 201, create_response.text
    program_id = create_response.json()["id"]
    runs_url = f"/v1/matters/{matter['id']}/review-programs/{program_id}/runs"

    first_start = client.post(
        runs_url,
        headers=auth(token),
        json={"idempotency_key": "coordinator-proof-run"},
    )
    assert first_start.status_code == 201, first_start.text
    run_id = first_start.json()["id"]
    assert first_start.json()["status"] == "WAITING_FOR_USER"
    assert first_start.json()["current_stage"] == "MATTER_DEFINITION"
    assert len(first_start.json()["stage_attempts"]) == 1

    repeated_start = client.post(
        runs_url,
        headers=auth(token),
        json={"idempotency_key": "coordinator-proof-run"},
    )
    assert repeated_start.status_code == 201, repeated_start.text
    assert repeated_start.json()["id"] == run_id
    competing_start = client.post(
        runs_url,
        headers=auth(token),
        json={"idempotency_key": "different-run"},
    )
    assert competing_start.status_code == 409

    with TestingSessionLocal() as db:
        program = db.get(ReviewProgram, uuid.UUID(program_id))
        run = db.get(ReviewProgramRun, uuid.UUID(run_id))
        assert program is not None and run is not None
        first_attempt = db.scalar(
            select(ReviewProgramStageAttempt).where(ReviewProgramStageAttempt.run_id == run.id)
        )
        assert first_attempt is not None
        with pytest.raises(ReviewProgramError, match="requires NONE batching"):
            link_review_program_child(
                db,
                program=program,
                run=run,
                stage_attempt=first_attempt,
                purpose="synthetic",
                child_type="synthetic_job",
                child_id=uuid.uuid4(),
                derivation_key="invalid-batching",
                batching_policy="AD_HOC_BATCH",
            )

    waiting = client.get(f"{runs_url}/{run_id}", headers=auth(token))
    assert waiting.status_code == 200, waiting.text
    setup_action = next(action for action in waiting.json()["actions"] if action["status"] == "OPEN")
    assert setup_action["action_type"] == "MATTER_DEFINITION_SETUP"
    assert {choice["key"] for choice in setup_action["allowed_choices"]} == {
        "CREATE",
        "CLONE",
        "ADOPT",
        "STOP",
    }
    counts = client.get("/v1/notifications/counts", headers=auth(token))
    assert counts.status_code == 200
    assert counts.json()["action_required"] == 1

    setup = client.post(
        f"{runs_url}/{run_id}/actions/{setup_action['id']}/definition-setup",
        headers=auth(token),
        json={
            "preview_hash": setup_action["preview_hash"],
            "mode": "CREATE",
            "definition_key": "coordinator-proof-definition",
            "name": "Coordinator proof definition",
            "content_markdown": "# Review instructions\n\nIssue 1: hurricane insurance disruption.",
            "assessment_target_document_count": 10,
        },
    )
    assert setup.status_code == 200, setup.text
    assert setup.json()["current_stage"] == "DEFINITION_ASSESSMENT"
    assert setup.json()["status"] == "RUNNING"

    with TestingSessionLocal() as db:
        setup_notification = db.scalar(
            select(Notification).where(
                Notification.source_type == "review_program_action",
                Notification.source_id == uuid.UUID(setup_action["id"]),
            )
        )
        assert setup_notification is not None
        assert setup_notification.resolution_code == "CREATE"
        assert setup_notification.resolved_at is not None
        child = db.scalar(
            select(ReviewProgramChild).where(
                ReviewProgramChild.run_id == uuid.UUID(run_id),
                ReviewProgramChild.child_type == "MATTER_DEFINITION_ASSESSMENT",
            )
        )
        assert child is not None
        assert child.context["batching_policy"] == "AD_HOC_BATCH"
        first_assessment = db.get(MatterDefinitionAssessmentRun, child.child_id)
        assert first_assessment is not None
        workflow_run = db.get(WorkflowRun, first_assessment.workflow_run_id)
        assert workflow_run is not None
        assert workflow_run.notification_policy == "PARENT_MANAGED"
        assert workflow_run.notification_parent_type == "REVIEW_PROGRAM_RUN"
        assert workflow_run.notification_parent_id == uuid.UUID(run_id)
        first_assessment.status = "FAILED"
        first_assessment.error_message = "Synthetic assessment failure"
        first_assessment.completed_at = utcnow()
        project_review_program_assessment_terminal(db, assessment=first_assessment)
        db.commit()

    failed = client.get(f"{runs_url}/{run_id}", headers=auth(token))
    assert failed.status_code == 200
    assert failed.json()["status"] == "FAILED"
    assert failed.json()["failure_retriable"] is True
    retried = client.post(
        f"{runs_url}/{run_id}/retry",
        headers=auth(token),
        json={"idempotency_key": "retry-definition-assessment"},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "RUNNING"
    assert retried.json()["current_stage"] == "DEFINITION_ASSESSMENT"
    assert retried.json()["stage_attempts"][-1]["attempt"] == 2

    with TestingSessionLocal() as db:
        children = list(
            db.scalars(
                select(ReviewProgramChild)
                .where(
                    ReviewProgramChild.run_id == uuid.UUID(run_id),
                    ReviewProgramChild.child_type == "MATTER_DEFINITION_ASSESSMENT",
                )
                .order_by(ReviewProgramChild.created_at, ReviewProgramChild.id)
            )
        )
        assert len(children) == 2
        assessment = db.get(MatterDefinitionAssessmentRun, children[-1].child_id)
        assert assessment is not None
        assessment.status = "COMPLETED"
        assessment.completed_at = utcnow()
        assessment.synthesis_result = {"status": "SUFFICIENT"}
        db.add(
            MatterDefinitionAssessmentQuery(
                assessment_run_id=assessment.id,
                ordinal=1,
                criterion_key="hurricane-disruption",
                criterion_label="Hurricane disruption",
                rationale="Identify responsive insurance disruption documents.",
                search_request={"query": "hurricane disruption"},
                quota=10,
                result_count=5,
            )
        )
        db.add(
            MatterDefinitionAssessmentQuestion(
                assessment_run_id=assessment.id,
                question="Should hurricane preparation communications be responsive?",
                rationale="The definition does not distinguish preparation from response.",
                priority="HIGH",
                blocking=True,
            )
        )
        db.commit()

    reconciled_assessment = client.post(f"{runs_url}/{run_id}/reconcile", headers=auth(token))
    assert reconciled_assessment.status_code == 200, reconciled_assessment.text
    assert reconciled_assessment.json()["current_stage"] == "DEFINITION_REFINEMENT"
    assert reconciled_assessment.json()["status"] == "WAITING_FOR_USER"
    refinement_action = next(
        action for action in reconciled_assessment.json()["actions"] if action["status"] == "OPEN"
    )
    blocked = client.post(
        f"{runs_url}/{run_id}/actions/{refinement_action['id']}/resolve",
        headers=auth(token),
        json={
            "preview_hash": refinement_action["preview_hash"],
            "choice_key": "CONTINUE",
        },
    )
    assert blocked.status_code == 409
    assert "blocking assessment question" in blocked.text
    with TestingSessionLocal() as db:
        question = db.scalar(
            select(MatterDefinitionAssessmentQuestion).where(
                MatterDefinitionAssessmentQuestion.assessment_run_id == uuid.UUID(
                    reconciled_assessment.json()["progress_summary"]["current_assessment_id"]
                )
            )
        )
        assert question is not None
        question.status = "DISMISSED"
        db.commit()
    refined = client.post(
        f"{runs_url}/{run_id}/actions/{refinement_action['id']}/resolve",
        headers=auth(token),
        json={
            "preview_hash": refinement_action["preview_hash"],
            "choice_key": "CONTINUE",
        },
    )
    assert refined.status_code == 200, refined.text
    assert refined.json()["current_stage"] == "DEFINITION_ACCEPTANCE"
    assert refined.json()["status"] == "WAITING_FOR_USER"
    acceptance_action = next(action for action in refined.json()["actions"] if action["status"] == "OPEN")
    assert {choice["key"] for choice in acceptance_action["allowed_choices"]} == {
        "ACCEPT",
        "REASSESS",
        "STOP",
    }
    reassessed = client.post(
        f"{runs_url}/{run_id}/actions/{acceptance_action['id']}/resolve",
        headers=auth(token),
        json={
            "preview_hash": acceptance_action["preview_hash"],
            "choice_key": "REASSESS",
        },
    )
    assert reassessed.status_code == 200, reassessed.text
    assert reassessed.json()["current_stage"] == "DEFINITION_ASSESSMENT"
    assert reassessed.json()["progress_summary"]["definition_assessment_round"] == 3
    with TestingSessionLocal() as db:
        assessment = db.get(
            MatterDefinitionAssessmentRun,
            uuid.UUID(reassessed.json()["progress_summary"]["current_assessment_id"]),
        )
        assert assessment is not None
        assessment.status = "COMPLETED"
        assessment.completed_at = utcnow()
        assessment.synthesis_result = {"status": "SUFFICIENT"}
        db.add(
            MatterDefinitionAssessmentQuery(
                assessment_run_id=assessment.id,
                ordinal=1,
                criterion_key="hurricane-disruption",
                criterion_label="Hurricane disruption",
                rationale="Identify responsive insurance disruption documents.",
                search_request={"query": "hurricane disruption"},
                quota=10,
                result_count=6,
            )
        )
        db.commit()
    third_result = client.post(f"{runs_url}/{run_id}/reconcile", headers=auth(token))
    assert third_result.status_code == 200, third_result.text
    third_refinement = next(action for action in third_result.json()["actions"] if action["status"] == "OPEN")
    third_ready = client.post(
        f"{runs_url}/{run_id}/actions/{third_refinement['id']}/resolve",
        headers=auth(token),
        json={"preview_hash": third_refinement["preview_hash"], "choice_key": "CONTINUE"},
    )
    assert third_ready.status_code == 200, third_ready.text
    final_acceptance = next(action for action in third_ready.json()["actions"] if action["status"] == "OPEN")
    accepted = client.post(
        f"{runs_url}/{run_id}/actions/{final_acceptance['id']}/resolve",
        headers=auth(token),
        json={"preview_hash": final_acceptance["preview_hash"], "choice_key": "ACCEPT"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["current_stage"] == "QA_TASK_SETUP"
    assert accepted.json()["status"] == "WAITING_FOR_USER"
    frozen = accepted.json()["progress_summary"]["accepted_matter_definition"]
    assert frozen["issue_keys"] == ["hurricane-disruption"]
    assert len(frozen["assessment_ids"]) == 3

    qa_action = next(action for action in accepted.json()["actions"] if action["status"] == "OPEN")
    with TestingSessionLocal() as db:
        destination = MetadataDefinition(
            matter_id=uuid.UUID(matter["id"]),
            key="responsive",
            display_name="Responsive",
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
        db.add(destination)
        db.commit()
        destination_id = destination.id
    generated = client.post(
        f"{runs_url}/{run_id}/actions/{qa_action['id']}/qa-setup",
        headers=auth(token),
        json={
            "preview_hash": qa_action["preview_hash"],
            "task_key": "coordinator_qa",
            "name": "Coordinator Q&A",
            "destination_metadata_definition_ids": [str(destination_id)],
        },
    )
    assert generated.status_code == 202, generated.text
    assert generated.json()["progress_summary"]["qa_task_setup_state"] == "COMPILING"
    setup = generated.json()["progress_summary"]["qa_task_setup"]
    with TestingSessionLocal() as db:
        task_version = db.get(MatterAnalysisTaskVersion, uuid.UUID(setup["task_version_id"]))
        workflow = db.get(WorkflowRun, uuid.UUID(setup["compiler_workflow_run_id"]))
        assert task_version is not None and workflow is not None
        task_version.compilation_status = "READY"
        task_version.decision_specification = {}
        task_version.specification_content_hash = "b" * 64
        task_version.validation_report = {"status": "VALID", "errors": [], "warnings": []}
        workflow.status = "COMPLETED"
        workflow.completed_at = utcnow()
        project_review_program_qa_compilation_terminal(db, workflow=workflow)
        db.commit()
    publication_state = client.get(f"{runs_url}/{run_id}", headers=auth(token))
    assert publication_state.status_code == 200, publication_state.text
    publication_action = next(
        action for action in publication_state.json()["actions"] if action["status"] == "OPEN"
    )
    assert publication_action["action_type"] == "QA_TASK_PUBLICATION"
    resumed = client.post(
        f"{runs_url}/{run_id}/actions/{publication_action['id']}/resolve",
        headers=auth(token),
        json={"preview_hash": publication_action["preview_hash"], "choice_key": "PUBLISH"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "WAITING_FOR_USER"
    assert resumed.json()["current_stage"] == "DISCOVERY_QA"
    discovery_action = next(
        action for action in resumed.json()["actions"] if action["status"] == "OPEN"
    )
    assert discovery_action["action_type"] == "DISCOVERY_QA_SETUP"
    user_id = uuid.UUID(client.get("/v1/auth/me", headers=auth(token)).json()["id"])
    document_ids = [uuid.UUID(value) for value in add_documents(matter["id"], user_id, count=3)]
    search_hits = list(document_ids)
    monkeypatch.setattr(
        "app.review_programs.search_document_ids",
        lambda *args, **kwargs: list(search_hits),
    )

    published_populations: list[list[uuid.UUID]] = []

    def publish_manifest(db, *, prepared, **kwargs):
        published_populations.append(prepared_manifest_document_ids(prepared))
        manifest = AnalysisInputManifest(
            id=uuid.uuid4(),
            tenant_id=kwargs["tenant_id"],
            client_id=kwargs["client_id"],
            matter_id=kwargs["matter_id"],
            owner_type=kwargs["owner_type"],
            owner_id=kwargs["owner_id"],
            scope_type=prepared.scope_type,
            scope_id=prepared.scope_id,
            manifest_schema="analysis-input-manifest",
            manifest_schema_version=1,
            resolver_version=prepared.contract.resolver_version,
            input_contract=prepared.contract.model_dump(mode="json"),
            input_contract_hash=prepared.contract_hash,
            derivation_key=prepared.content_hash,
            artifact_id=uuid.uuid4(),
            artifact_content_hash=prepared.content_hash,
            artifact_byte_length=prepared.byte_length,
            item_count=prepared.item_count,
            manifest_status="FINALIZED",
            producer_kind=kwargs["producer_kind"],
            producer_run_id=kwargs["producer_run_id"],
            created_by_user_id=kwargs["actor_user_id"],
            finalized_at=utcnow(),
        )
        db.add(manifest)
        db.flush()
        return manifest, object()

    monkeypatch.setattr("app.review_programs.publish_analysis_input_manifest", publish_manifest)
    discovery_started = client.post(
        f"{runs_url}/{run_id}/actions/{discovery_action['id']}/discovery-setup",
        headers=auth(token),
        json={
            "preview_hash": discovery_action["preview_hash"],
            "target_document_count": 3,
            "control_sample_size": 0,
            "target_batch_size": 2,
            "keep_families_together": False,
        },
    )
    assert discovery_started.status_code == 202, discovery_started.text
    assert discovery_started.json()["status"] == "RUNNING"
    assert discovery_started.json()["progress_summary"]["discovery_qa_state"] == "MATERIALIZING_BATCH_SET"
    assert discovery_started.json()["progress_summary"]["discovery_qa"]["selected_document_count"] == 3

    class DiscoveryReader:
        def __init__(self, _manifest, *, actor_user_id):
            assert actor_user_id == user_id

        def rows(self, start, count):
            assert (start, count) == (0, len(document_ids))
            return [
                {"matter_document_id": str(document_id), "input_status": "READY"}
                for document_id in document_ids
            ]

    monkeypatch.setattr(batch_sets, "AnalysisInputManifestReader", DiscoveryReader)
    with TestingSessionLocal() as db:
        discovery_child = db.scalar(
            select(ReviewProgramChild).where(
                ReviewProgramChild.run_id == uuid.UUID(run_id),
                ReviewProgramChild.child_type == "ANALYSIS_TASK_BATCH_SET",
            )
        )
        assert discovery_child is not None
        assert discovery_child.context["batching_policy"] == "PARTITIONED_SET"
        batch_set_workflow = db.scalar(
            select(WorkflowRun).where(WorkflowRun.job_id == discovery_child.child_id)
        )
        assert batch_set_workflow is not None
        assert batch_set_workflow.notification_policy == "PARENT_MANAGED"
        assert batch_set_workflow.notification_parent_id == uuid.UUID(run_id)
        batch_sets.materialize_batch_set(
            db,
            discovery_child.child_id,
            batch_sets.get_settings().model_copy(update={"search_enabled": False}),
        )
        batch_sets.complete_batch_set_materialization(db, discovery_child.child_id)
        db.commit()

    running_profiles = client.get(f"{runs_url}/{run_id}", headers=auth(token))
    assert running_profiles.status_code == 200, running_profiles.text
    assert running_profiles.json()["current_stage"] == "DISCOVERY_QA"
    assert running_profiles.json()["progress_summary"]["discovery_qa_state"] == "RUNNING_PROFILES"
    assert len(running_profiles.json()["progress_summary"]["discovery_qa"]["batch_set_run_ids"]) == 2
    assert published_populations == [document_ids]

    with TestingSessionLocal() as db:
        profile_run_ids = list(
            db.scalars(
                select(ReviewProgramChild.child_id)
                .where(
                    ReviewProgramChild.run_id == uuid.UUID(run_id),
                    ReviewProgramChild.child_type == "ANALYSIS_TASK_BATCH_SET_RUN",
                )
                .order_by(ReviewProgramChild.created_at, ReviewProgramChild.id)
            )
        )
        profile_runs = [db.get(AnalysisTaskBatchSetRun, run_id_) for run_id_ in profile_run_ids]
        assert all(item is not None for item in profile_runs)
        complete_discovery_profile_run(
            db,
            profile_run=profile_runs[0],
            destination_id=destination_id,
            value=True,
        )
        complete_discovery_profile_run(
            db,
            profile_run=profile_runs[1],
            destination_id=destination_id,
            value=True,
            failed_child_ordinal=1,
        )
        db.commit()

    failed_discovery = client.get(f"{runs_url}/{run_id}", headers=auth(token))
    assert failed_discovery.status_code == 200, failed_discovery.text
    assert failed_discovery.json()["status"] == "FAILED"
    assert failed_discovery.json()["failure_retriable"] is True
    assert failed_discovery.json()["failure_code"] == "DISCOVERY_QA_PROFILE_FAILED"

    with TestingSessionLocal() as db:
        failed_profile = db.get(AnalysisTaskBatchSetRun, profile_run_ids[1])
        assert failed_profile is not None
        retry_workflow = batch_set_runs.retry_batch_set_run(
            db,
            failed_profile,
            actor_user_id=user_id,
        )
        assert retry_workflow.notification_policy == "PARENT_MANAGED"
        assert retry_workflow.notification_parent_id == uuid.UUID(run_id)
        db.commit()

    with TestingSessionLocal() as db:
        failed_profile = db.get(AnalysisTaskBatchSetRun, profile_run_ids[1])
        assert failed_profile is not None
        transition_job_execution(
            db,
            job_type=ANALYSIS_TASK_BATCH_SET_RUN_JOB_TYPE,
            job_id=failed_profile.id,
            status="RUNNING",
        )
        retry_children = list(
            db.scalars(
                select(AnalysisTaskBatchSetRunChild)
                .where(AnalysisTaskBatchSetRunChild.batch_set_run_id == failed_profile.id)
                .order_by(AnalysisTaskBatchSetRunChild.ordinal)
            )
        )
        assert [child.status for child in retry_children] == ["PENDING", "COMPLETED"]
        complete_discovery_profile_child(
            db,
            child=retry_children[0],
            destination_id=destination_id,
            value=True,
        )
        batch_set_runs.complete_batch_set_run(db, failed_profile.id)
        db.commit()

    support_state = client.get(f"{runs_url}/{run_id}", headers=auth(token))
    assert support_state.status_code == 200, support_state.text
    assert support_state.json()["status"] == "WAITING_FOR_USER"
    assert support_state.json()["current_stage"] == "ISSUE_SUPPORT_DECISION"
    analysis = support_state.json()["progress_summary"]["discovery_analysis"]
    assert analysis["overall"] == {
        "exact_count": 3,
        "partial_count": 0,
        "disagreement_count": 0,
        "incomplete_count": 0,
    }
    assert analysis["support_status"] == "INSUFFICIENT"
    assert {item["value_key"] for item in analysis["low_support"]} == {"true", "false"}
    support_action = next(
        action for action in support_state.json()["actions"] if action["status"] == "OPEN"
    )
    assert {choice["key"] for choice in support_action["allowed_choices"]} == {
        "SUPPLEMENT",
        "IGNORE_LOW_SUPPORT",
        "REFINE_DEFINITION",
        "STOP",
    }

    supplemental_documents = [
        uuid.UUID(value) for value in add_documents(matter["id"], user_id, count=2)
    ]
    search_hits.extend(supplemental_documents)
    supplement = client.post(
        f"{runs_url}/{run_id}/actions/{support_action['id']}/resolve",
        headers=auth(token),
        json={
            "preview_hash": support_action["preview_hash"],
            "choice_key": "SUPPLEMENT",
        },
    )
    assert supplement.status_code == 200, supplement.text
    assert supplement.json()["current_stage"] == "DISCOVERY_QA"
    assert supplement.json()["current_stage_ordinal"] == 6
    second_discovery_action = next(
        action for action in supplement.json()["actions"] if action["status"] == "OPEN"
    )
    second_started = client.post(
        f"{runs_url}/{run_id}/actions/{second_discovery_action['id']}/discovery-setup",
        headers=auth(token),
        json={
            "preview_hash": second_discovery_action["preview_hash"],
            "target_document_count": 2,
            "control_sample_size": 0,
            "target_batch_size": 2,
            "keep_families_together": False,
        },
    )
    assert second_started.status_code == 202, second_started.text
    assert second_started.json()["progress_summary"]["discovery_qa"]["round"] == 2
    assert published_populations == [document_ids, supplemental_documents]

    with TestingSessionLocal() as db:
        discovery_children = list(
            db.scalars(
                select(ReviewProgramChild)
                .where(
                    ReviewProgramChild.run_id == uuid.UUID(run_id),
                    ReviewProgramChild.child_type == "ANALYSIS_TASK_BATCH_SET",
                )
                .order_by(ReviewProgramChild.created_at, ReviewProgramChild.id)
            )
        )
        assert len(discovery_children) == 2
        supplemental_set = db.get(batch_sets.AnalysisTaskBatchSet, discovery_children[-1].child_id)
        assert supplemental_set is not None
        provenance = supplemental_set.selection_definition["review_program"]
        assert provenance["excluded_prior_program_document_count"] == len(document_ids)
        assert provenance["prior_discovery_batch_set_ids"] == [str(discovery_children[0].child_id)]

    canceled = client.post(
        f"{runs_url}/{run_id}/cancel",
        headers=auth(token),
        json={"idempotency_key": "cancel-proof", "reason": "Synthetic proof complete"},
    )
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "CANCELED"
    repeated_cancel = client.post(
        f"{runs_url}/{run_id}/cancel",
        headers=auth(token),
        json={"idempotency_key": "cancel-proof", "reason": "Synthetic proof complete"},
    )
    assert repeated_cancel.status_code == 200, repeated_cancel.text

    with TestingSessionLocal() as db:
        discovery_children = list(
            db.scalars(
                select(ReviewProgramChild)
                .where(
                    ReviewProgramChild.run_id == uuid.UUID(run_id),
                    ReviewProgramChild.child_type == "ANALYSIS_TASK_BATCH_SET",
                )
                .order_by(ReviewProgramChild.created_at, ReviewProgramChild.id)
            )
        )
        assert len(discovery_children) == 2
        discovery_workflow = db.scalar(
            select(WorkflowRun)
            .where(WorkflowRun.job_id == discovery_children[-1].child_id)
            .order_by(WorkflowRun.attempt_number.desc())
        )
        assert discovery_workflow is not None
        assert discovery_workflow.status == "CANCELED"
        profile_run_ids = list(
            db.scalars(
                select(ReviewProgramChild.child_id).where(
                    ReviewProgramChild.run_id == uuid.UUID(run_id),
                    ReviewProgramChild.child_type == "ANALYSIS_TASK_BATCH_SET_RUN",
                )
            )
        )
        profile_runs = list(
            db.scalars(select(AnalysisTaskBatchSetRun).where(AnalysisTaskBatchSetRun.id.in_(profile_run_ids)))
        )
        assert len(profile_runs) == 2
        assert {item.status for item in profile_runs} == {"COMPLETED"}
        assert (
            db.scalar(
                select(func.count())
                .select_from(ReviewProgramEvent)
                .where(
                    ReviewProgramEvent.program_id == uuid.UUID(program_id),
                    ReviewProgramEvent.idempotency_key.like("assessment-terminal:%:COMPLETED"),
                )
            )
            == 2
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(Notification)
                .where(
                    Notification.source_type == "review_program_run",
                    Notification.source_id == uuid.UUID(run_id),
                    Notification.type_key == "review_program.stopped",
                )
            )
            == 1
        )


def test_cancel_program_stops_live_definition_assessment(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    _, _, matter, token = review_program_workspace(client, root_token)
    enable_definition_assessments(matter["id"], root_admin)
    program = client.post(
        f"/v1/matters/{matter['id']}/review-programs",
        headers=auth(token),
        json={"key": "cancel-live-assessment", "name": "Cancel live assessment"},
    ).json()
    runs_url = f"/v1/matters/{matter['id']}/review-programs/{program['id']}/runs"
    started = client.post(
        runs_url,
        headers=auth(token),
        json={"idempotency_key": "cancel-live-assessment"},
    ).json()
    setup_action = next(action for action in started["actions"] if action["status"] == "OPEN")
    assessing = client.post(
        f"{runs_url}/{started['id']}/actions/{setup_action['id']}/definition-setup",
        headers=auth(token),
        json={
            "preview_hash": setup_action["preview_hash"],
            "mode": "CREATE",
            "definition_key": "cancel-live-definition",
            "name": "Cancel live definition",
            "content_markdown": "# Review instructions\n\nIssue: hurricane response.",
            "assessment_target_document_count": 10,
        },
    )
    assert assessing.status_code == 200, assessing.text
    assessment_id = uuid.UUID(assessing.json()["progress_summary"]["current_assessment_id"])

    canceled = client.post(
        f"{runs_url}/{started['id']}/cancel",
        headers=auth(token),
        json={"idempotency_key": "cancel-live", "reason": "Stop the program"},
    )
    assert canceled.status_code == 200, canceled.text
    assert canceled.json()["status"] == "CANCELED"
    with TestingSessionLocal() as db:
        assessment = db.get(MatterDefinitionAssessmentRun, assessment_id)
        assert assessment is not None
        assert assessment.status == "CANCELED"
        assert assessment.canceled_at is not None
        workflow = db.get(WorkflowRun, assessment.workflow_run_id)
        assert workflow is not None
        assert workflow.status == "CANCELED"
        assert workflow.cancel_requested_at is not None
        child = db.scalar(
            select(ReviewProgramChild).where(
                ReviewProgramChild.run_id == uuid.UUID(started["id"]),
                ReviewProgramChild.child_id == assessment_id,
            )
        )
        assert child is not None
        assert child.status == "CANCELED"
