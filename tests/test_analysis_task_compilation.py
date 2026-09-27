import json
import uuid

from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis_task_compilation import (
    build_source_reference_catalog,
    compile_analysis_task_version,
    parse_compiler_wire_output,
)
from app.analysis_task_skills import ensure_standard_analysis_task_skills
from app.decision_specifications import DecisionSpecificationCompilationOutput
from app.models import (
    ExternalProviderUsage,
    MatterAnalysisTaskVersion,
    SkillDefinition,
    SkillRun,
    Tenant,
    WorkflowRun,
    WorkflowSkillBinding,
)
from tests.test_agents_and_matter_definitions import auth, create_tenant_context


def _bootstrap_compiler(db: Session, root_admin) -> None:
    root = db.get(Tenant, root_admin.tenant_id)
    assert root is not None
    assert ensure_standard_analysis_task_skills(db, root, root_admin) is True
    db.commit()


def _create_task(client: TestClient, token: str, matter_id: str) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "privilege_review",
            "name": "Privilege review",
            "task_type": "PRIVILEGE_REVIEW",
            "definition_markdown": (
                "# Legal advice\n\n"
                "A document is potentially privileged when it requests or provides confidential legal advice.\n\n"
                "Business advice without a legal purpose is not privileged."
            ),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _compiler_output(task_version_id: str, excerpt_hash: str) -> dict:
    return {
        "decision_specification": {
            "schema_version": "review-decision-specification-v1",
            "questions": {
                "privilege.legal_advice": {
                    "type": "noul",
                    "instructions": {
                        "question": "Does the document request or provide confidential legal advice?",
                        "positive_evidence": "A request for or communication of legal advice.",
                        "negative_evidence": "Purely business advice with no legal purpose.",
                    },
                    "criteria": {
                        "true": "Confidential legal advice is requested or provided.",
                        "false": "No confidential legal advice is requested or provided.",
                    },
                    "source_refs": [
                        {
                            "task_version_id": task_version_id,
                            "heading": "Legal advice",
                            "excerpt_hash": excerpt_hash,
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
                "required_paths": ["document.id", "document.paragraphs"],
            },
        },
        "question_rationales": {
            "privilege.legal_advice": "Legal advice is an atomic prerequisite for the privilege determination."
        },
        "omissions": [],
        "warnings": [],
    }


def _compiler_wire_output(task_version_id: str, excerpt_hash: str) -> dict:
    return {"compiled_output_json": json.dumps(_compiler_output(task_version_id, excerpt_hash))}


def test_standard_analysis_task_compiler_skill_is_idempotent(db: Session, root_admin) -> None:
    root = db.get(Tenant, root_admin.tenant_id)
    assert root is not None
    assert ensure_standard_analysis_task_skills(db, root, root_admin) is True
    db.commit()
    assert ensure_standard_analysis_task_skills(db, root, root_admin) is False

    skill = db.scalar(
        select(SkillDefinition).where(SkillDefinition.key == "compile_analysis_task_decision_specification")
    )
    assert skill is not None
    assert skill.scope == "SYSTEM"
    binding = db.scalar(
        select(WorkflowSkillBinding).where(WorkflowSkillBinding.workflow_key == "matter_analysis_task_compilation_v1")
    )
    assert binding is not None
    assert binding.role_key == "decision_specification_compiler"
    assert binding.skill_definition_id == skill.id


def test_compile_endpoint_queues_pinned_managed_skill_workflow(
    client: TestClient,
    root_token: str,
    root_admin,
    db: Session,
) -> None:
    _bootstrap_compiler(db, root_admin)
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _create_task(client, token, matter_id)

    response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/compile",
        headers=auth(token),
    )
    assert response.status_code == 202, response.text
    queued = response.json()
    assert queued["version"]["compilation_status"] == "GENERATING"
    assert queued["version"]["compiler_workflow_run_id"] is not None
    workflow = db.get(WorkflowRun, uuid.UUID(queued["version"]["compiler_workflow_run_id"]))
    assert workflow is not None
    assert workflow.workflow_key == "matter_analysis_task_compilation_v1"
    assert workflow.binding_snapshot["decision_specification_compiler"]["skill_key"] == (
        "compile_analysis_task_decision_specification"
    )
    assert workflow.input_snapshot["definition_content_hash"] == queued["version"]["definition_content_hash"]

    duplicate = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/compile",
        headers=auth(token),
    )
    assert duplicate.status_code == 409


def test_compiler_persists_validated_specification_and_provenance(
    client: TestClient,
    root_token: str,
    root_admin,
    db: Session,
) -> None:
    _bootstrap_compiler(db, root_admin)
    _, token, matter_id = create_tenant_context(client, root_token)
    task = _create_task(client, token, matter_id)
    queued_response = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/compile",
        headers=auth(token),
    )
    assert queued_response.status_code == 202, queued_response.text
    queued = queued_response.json()
    version_id = uuid.UUID(queued["version"]["id"])
    references = build_source_reference_catalog(
        version_id,
        queued["version"]["definition_markdown"],
    )

    result = compile_analysis_task_version(
        db,
        version_id,
        model=TestModel(
            custom_output_args=_compiler_wire_output(
                str(version_id),
                references[0]["excerpt_hash"],
            )
        ),
    )
    assert set(result["questions"]) == {"privilege.legal_advice"}
    version = db.get(MatterAnalysisTaskVersion, version_id)
    assert version is not None
    assert version.compilation_status == "READY"
    assert version.specification_content_hash is not None
    assert version.compiler_skill_definition_version_id is not None
    assert version.compiler_skill_run_id is not None
    assert version.validation_report["status"] == "VALID"
    assert version.source_provenance["source_reference_catalog_hash"]

    workflow = db.get(WorkflowRun, version.compiler_workflow_run_id)
    skill_run = db.get(SkillRun, version.compiler_skill_run_id)
    assert workflow is not None and workflow.status == "COMPLETED"
    assert skill_run is not None and skill_run.status == "COMPLETED"
    usage = db.scalar(
        select(ExternalProviderUsage).where(
            ExternalProviderUsage.model_invocation_id.is_not(None),
            ExternalProviderUsage.job_id == workflow.id,
        )
    )
    assert usage is not None
    assert usage.job_type == "MATTER_ANALYSIS_TASK_COMPILATION"


def test_source_catalog_uses_reviewable_heading_and_exact_excerpt_hash() -> None:
    version_id = uuid.uuid4()
    catalog = build_source_reference_catalog(
        version_id,
        "# Scope\n\nFirst line\ncontinues here.\n\n## Exclusions\n\nExclude public material.",
    )
    assert [(entry["heading"], entry["excerpt"]) for entry in catalog] == [
        ("Scope", "First line continues here."),
        ("Exclusions", "Exclude public material."),
    ]
    assert all(entry["task_version_id"] == str(version_id) for entry in catalog)
    assert all(len(entry["excerpt_hash"]) == 64 for entry in catalog)


def test_compiler_wire_output_parses_full_compilation_result() -> None:
    version_id = uuid.uuid4()
    compiled = parse_compiler_wire_output(_compiler_wire_output(str(version_id), "a" * 64))
    assert set(compiled.decision_specification.questions) == {"privilege.legal_advice"}


def test_compiler_wire_output_rejects_invalid_embedded_json() -> None:
    try:
        parse_compiler_wire_output({"compiled_output_json": "not-json"})
    except ValueError as exc:
        assert "not valid JSON" in str(exc)
    else:
        raise AssertionError("invalid embedded JSON should fail validation")


def test_compiler_schema_exposes_dotted_question_and_state_path_patterns() -> None:
    schema = DecisionSpecificationCompilationOutput.model_json_schema(mode="validation")
    definitions = schema["$defs"]
    questions = definitions["DecisionSpecification"]["properties"]["questions"]
    state_paths = definitions["DecisionStateContract"]["properties"]["required_paths"]
    pattern = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"
    assert set(questions["patternProperties"]) == {pattern}
    assert state_paths["items"]["pattern"] == pattern
