import uuid

import pytest
from conftest import TestingSessionLocal, create_client_tenant
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import (
    AuditRecord,
    MatterDefinitionAssessmentQuery,
    MatterDefinitionAssessmentQuestion,
    MatterDefinitionAssessmentRun,
    Notification,
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
)
from app.review_workflow_templates import FIRST_PASS_REVIEW, get_review_workflow_template
from app.standard_skills import ensure_standard_assessment_skills


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
    resumed = client.post(
        f"{runs_url}/{run_id}/actions/{qa_action['id']}/resolve",
        headers=auth(token),
        json={"preview_hash": qa_action["preview_hash"], "choice_key": "CONTINUE"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "RUNNING"
    assert resumed.json()["current_stage"] == "DISCOVERY_QA"

    with TestingSessionLocal() as db:
        attempt = db.scalar(
            select(ReviewProgramStageAttempt)
            .where(
                ReviewProgramStageAttempt.run_id == uuid.UUID(run_id),
                ReviewProgramStageAttempt.stage_key == "DISCOVERY_QA",
            )
            .order_by(ReviewProgramStageAttempt.attempt.desc())
        )
        assert attempt is not None
        attempt.status = "COMPLETED"
        db.commit()

    reconciled = client.post(f"{runs_url}/{run_id}/reconcile", headers=auth(token))
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["current_stage"] == "DISCOVERY_ANALYSIS"
    assert reconciled.json()["status"] == "RUNNING"
    reconciled_again = client.post(f"{runs_url}/{run_id}/reconcile", headers=auth(token))
    assert reconciled_again.status_code == 200, reconciled_again.text
    assert reconciled_again.json()["current_stage"] == "DISCOVERY_ANALYSIS"

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
