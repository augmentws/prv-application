import json
import uuid

import pytest
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.document_metadata import add_metadata_event, apply_metadata_values
from app.fact_development_skills import ensure_standard_fact_development_skills
from app.fact_issue_membership import evaluate_issue_membership
from app.fact_specification_compilation import (
    compile_fact_specification_version,
    fact_compiler_output_validator,
)
from app.fact_specifications import (
    FactDevelopmentSpecification,
    fact_source_catalog,
    mappable_metadata_definitions,
)
from app.models import (
    Matter,
    MatterAnalysisTaskVersion,
    MetadataDefinition,
    ReviewBatch,
    ReviewBatchDocument,
    ReviewBatchReviewerCoding,
    SkillDefinition,
    SkillRun,
    Tenant,
    WorkflowRun,
    WorkflowSkillBinding,
)
from app.reviewer_coding import ReviewerFieldReplacement, append_reviewer_coding_revision
from tests.test_agents_and_matter_definitions import auth, create_tenant_context
from tests.test_review_batches import add_documents

GUIDANCE_MARKDOWN = (
    "# Pricing decisions\n\n"
    "Documents about setting or changing customer prices are coded to the pricing issue.\n\n"
    "# Safety reports\n\n"
    "Documents reporting product safety incidents are coded as safety."
)
TASK_DEFINITION = "# Facts\n\nRecord one dated event, decision, or statement per fact."


def _setup(client: TestClient, root_token: str) -> tuple[str, str, str]:
    _, token, matter_id = create_tenant_context(client, root_token)
    user_id = client.get("/v1/auth/me", headers=auth(token)).json()["id"]
    return token, matter_id, user_id


def _create_issue_field(client: TestClient, token: str, matter_id: str) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/metadata-definitions",
        headers=auth(token),
        json={
            "key": "issue_coding",
            "display_name": "Issue coding",
            "type": "ENUM",
            "cardinality": "MULTIPLE",
            "allowed_values": [
                {"key": "pricing", "label": "Pricing"},
                {"key": "pricing_discounts", "label": "Discounts", "parent_key": "pricing"},
                {"key": "safety", "label": "Safety"},
                {"key": "retired_topic", "label": "Retired", "active": False},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_published_guidance(client: TestClient, token: str, matter_id: str, content: str = GUIDANCE_MARKDOWN) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/guidance",
        headers=auth(token),
        json={"key": "second_pass", "name": "Second Pass Review", "content_markdown": content},
    )
    assert response.status_code == 201, response.text
    guidance = response.json()
    published = client.post(
        f"/v1/matters/{matter_id}/guidance/{guidance['id']}/revisions/1/publish",
        headers=auth(token),
    )
    assert published.status_code == 200, published.text
    return published.json()


def _create_fact_task(client: TestClient, token: str, matter_id: str, guidance_id: str, **extra) -> dict:
    return client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "fact_development",
            "name": "Fact development",
            "task_type": "FACT_DEVELOPMENT",
            "definition_markdown": TASK_DEFINITION,
            "review_guidance_id": guidance_id,
            **extra,
        },
    )


def _reference(catalog: list[dict], source: str, heading: str | None) -> dict:
    entry = next(item for item in catalog if item["source"] == source and item["heading"] == heading)
    return {"source": source, "heading": heading, "excerpt_hash": entry["excerpt_hash"]}


def _specification(guidance_markdown: str = GUIDANCE_MARKDOWN, *, safety_value: str = "safety") -> dict:
    catalog = fact_source_catalog(TASK_DEFINITION, guidance_markdown)
    return {
        "summary": "Build dated facts about pricing decisions and safety reports.",
        "date_rules": "Use the date the event happened, not the date the email was sent.",
        "owner_rules": "The owner is the person or organization in the record that made the decision.",
        "issues": [
            {
                "key": "pricing",
                "name": "Pricing decisions",
                "fact_guidance": "One price change or pricing decision per fact.",
                "source_refs": [_reference(catalog, "REVIEW_GUIDANCE", "Pricing decisions")],
                "coding_mappings": [
                    {"metadata_definition_key": "issue_coding", "match_type": "ENUM_VALUE", "value": "pricing"}
                ],
            },
            {
                "key": "safety",
                "name": "Safety reports",
                "fact_guidance": "One reported safety incident per fact.",
                "source_refs": [
                    _reference(catalog, "REVIEW_GUIDANCE", "Safety reports"),
                    _reference(catalog, "TASK_DEFINITION", "Facts"),
                ],
                "coding_mappings": [
                    {"metadata_definition_key": "issue_coding", "match_type": "ENUM_VALUE", "value": safety_value}
                ],
            },
        ],
    }


def _put_specification(client: TestClient, token: str, matter_id: str, task_id: str, specification: dict):
    return client.put(
        f"/v1/matters/{matter_id}/analysis-tasks/{task_id}/versions/1/fact-specification",
        headers=auth(token),
        json={"fact_development_specification": specification},
    )


def _bootstrap_fact_compiler(db: Session, root_admin) -> None:
    root = db.get(Tenant, root_admin.tenant_id)
    assert root is not None
    assert ensure_standard_fact_development_skills(db, root, root_admin) is True
    db.commit()


def test_fact_task_requires_published_guidance_pin(client: TestClient, root_token: str) -> None:
    token, matter_id, _ = _setup(client, root_token)
    draft = client.post(
        f"/v1/matters/{matter_id}/guidance",
        headers=auth(token),
        json={"key": "unpublished", "name": "Unpublished", "content_markdown": GUIDANCE_MARKDOWN},
    ).json()

    missing_pin = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "fact_development",
            "name": "Fact development",
            "task_type": "FACT_DEVELOPMENT",
            "definition_markdown": TASK_DEFINITION,
        },
    )
    assert missing_pin.status_code == 409
    assert "pin a Review Guidance" in missing_pin.json()["error"]["message"]

    unpublished = _create_fact_task(client, token, matter_id, draft["id"])
    assert unpublished.status_code == 409
    assert "must be published" in unpublished.json()["error"]["message"]

    guidance = _create_published_guidance(client, token, matter_id)
    wrong_revision = _create_fact_task(client, token, matter_id, guidance["id"], review_guidance_revision=2)
    assert wrong_revision.status_code == 409

    with_destination = _create_fact_task(
        client,
        token,
        matter_id,
        guidance["id"],
        destination_metadata_definition_ids=[_create_issue_field(client, token, matter_id)["id"]],
    )
    assert with_destination.status_code == 409
    assert "do not write coding fields" in with_destination.json()["error"]["message"]

    created = _create_fact_task(client, token, matter_id, guidance["id"])
    assert created.status_code == 201, created.text
    version = created.json()["version"]
    assert created.json()["workflow_key"] == "fact_development_v1"
    assert version["review_guidance_id"] == guidance["id"]
    assert version["review_guidance_revision"] == 1
    assert version["review_guidance_pin_stale"] is False
    assert version["decision_model"] is None
    assert version["compilation_status"] == "NOT_GENERATED"

    other_type_pin = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks",
        headers=auth(token),
        json={
            "key": "qa_task",
            "name": "QA",
            "task_type": "QUESTION_ANSWERING",
            "definition_markdown": "# Scope\n\nAnything.",
            "review_guidance_id": guidance["id"],
        },
    )
    assert other_type_pin.status_code == 409
    assert "Only fact-development tasks pin Review Guidance" in other_type_pin.json()["error"]["message"]


def test_fact_specification_is_validated_before_ready_and_publish(client: TestClient, root_token: str) -> None:
    token, matter_id, _ = _setup(client, root_token)
    _create_issue_field(client, token, matter_id)
    guidance = _create_published_guidance(client, token, matter_id)
    task = _create_fact_task(client, token, matter_id, guidance["id"]).json()

    unpublishable = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/publish",
        headers=auth(token),
    )
    assert unpublishable.status_code == 409

    invented_hash = _specification()
    invented_hash["issues"][0]["source_refs"][0]["excerpt_hash"] = "0" * 64
    response = _put_specification(client, token, matter_id, task["id"], invented_hash)
    assert response.status_code == 409
    assert "not in the pinned guidance or task" in response.json()["error"]["message"]

    deactivated = _put_specification(
        client, token, matter_id, task["id"], _specification(safety_value="retired_topic")
    )
    assert deactivated.status_code == 409
    assert "deactivated value retired_topic" in deactivated.json()["error"]["message"]

    unknown_field = _specification()
    unknown_field["issues"][0]["coding_mappings"][0]["metadata_definition_key"] = "no_such_field"
    assert _put_specification(client, token, matter_id, task["id"], unknown_field).status_code == 409

    wrong_match_type = _specification()
    wrong_match_type["issues"][0]["coding_mappings"][0] = {
        "metadata_definition_key": "issue_coding",
        "match_type": "TEXT_EXACT",
        "value": "pricing",
    }
    wrong = _put_specification(client, token, matter_id, task["id"], wrong_match_type)
    assert wrong.status_code == 409
    assert "must use ENUM_VALUE" in wrong.json()["error"]["message"]

    duplicate_keys = _specification()
    duplicate_keys["issues"][1]["key"] = "pricing"
    assert _put_specification(client, token, matter_id, task["id"], duplicate_keys).status_code == 422

    identical = _specification()
    identical["issues"][1]["coding_mappings"] = identical["issues"][0]["coding_mappings"]
    saved_with_warning = _put_specification(client, token, matter_id, task["id"], identical)
    assert saved_with_warning.status_code == 200, saved_with_warning.text
    assert saved_with_warning.json()["version"]["validation_report"]["warnings"][0]["code"] == (
        "ISSUE_MAPPINGS_IDENTICAL"
    )

    saved = _put_specification(client, token, matter_id, task["id"], _specification())
    assert saved.status_code == 200, saved.text
    version = saved.json()["version"]
    assert version["compilation_status"] == "READY"
    assert version["specification_content_hash"]
    assert version["fact_development_specification"]["issues"][0]["key"] == "pricing"
    assert version["source_provenance"]["specification_origin"] == "USER_EDIT"

    published = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/publish",
        headers=auth(token),
    )
    assert published.status_code == 200, published.text
    assert published.json()["published_version"] == 1

    immutable = _put_specification(client, token, matter_id, task["id"], _specification())
    assert immutable.status_code == 409


def test_publish_revalidates_coding_fields_changed_after_specification(
    client: TestClient,
    root_token: str,
) -> None:
    token, matter_id, _ = _setup(client, root_token)
    field = _create_issue_field(client, token, matter_id)
    guidance = _create_published_guidance(client, token, matter_id)
    task = _create_fact_task(client, token, matter_id, guidance["id"]).json()
    assert _put_specification(client, token, matter_id, task["id"], _specification()).status_code == 200

    with TestingSessionLocal() as db:
        definition = db.get(MetadataDefinition, uuid.UUID(field["id"]))
        assert definition is not None
        definition.allowed_values = [
            {**value, "active": False} if value["key"] == "safety" else value for value in definition.allowed_values
        ]
        db.commit()

    published = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/publish",
        headers=auth(token),
    )
    assert published.status_code == 409
    assert "deactivated value safety" in published.json()["error"]["message"]


def test_guidance_pin_reports_stale_and_new_version_adopts_published_revision(
    client: TestClient,
    root_token: str,
) -> None:
    token, matter_id, _ = _setup(client, root_token)
    guidance = _create_published_guidance(client, token, matter_id)
    task = _create_fact_task(client, token, matter_id, guidance["id"]).json()

    revised = client.post(
        f"/v1/matters/{matter_id}/guidance/{guidance['id']}/revisions",
        headers=auth(token),
        json={"content_markdown": GUIDANCE_MARKDOWN + "\n\n# Recalls\n\nRecall notices.", "based_on_revision": 1},
    )
    assert revised.status_code == 201, revised.text
    assert client.post(
        f"/v1/matters/{matter_id}/guidance/{guidance['id']}/revisions/2/publish",
        headers=auth(token),
    ).status_code == 200

    stale = client.get(f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}", headers=auth(token)).json()
    assert stale["version"]["review_guidance_revision"] == 1
    assert stale["version"]["review_guidance_pin_stale"] is True

    adopted = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions",
        headers=auth(token),
        json={"definition_markdown": TASK_DEFINITION, "based_on_version": 1, "review_guidance_revision": 2},
    )
    assert adopted.status_code == 201, adopted.text
    version = adopted.json()["version"]
    assert version["version"] == 2
    assert version["review_guidance_revision"] == 2
    assert version["review_guidance_pin_stale"] is False
    assert version["review_guidance_content_hash"] != stale["version"]["review_guidance_content_hash"]

    carried = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions",
        headers=auth(token),
        json={"definition_markdown": TASK_DEFINITION + "\n\nMore detail.", "based_on_version": 2},
    )
    assert carried.status_code == 201, carried.text
    assert carried.json()["version"]["review_guidance_revision"] == 2


def test_fact_compiler_skill_bootstrap_is_idempotent(db: Session, root_admin) -> None:
    root = db.get(Tenant, root_admin.tenant_id)
    assert root is not None
    assert ensure_standard_fact_development_skills(db, root, root_admin) is True
    db.commit()
    assert ensure_standard_fact_development_skills(db, root, root_admin) is False
    skill = db.scalar(select(SkillDefinition).where(SkillDefinition.key == "compile_fact_development_specification"))
    assert skill is not None and skill.scope == "SYSTEM"
    binding = db.scalar(
        select(WorkflowSkillBinding).where(
            WorkflowSkillBinding.workflow_key == "fact_development_specification_compilation_v1"
        )
    )
    assert binding is not None
    assert binding.role_key == "fact_specification_compiler"
    assert binding.skill_definition_id == skill.id


def test_fact_compiler_queues_and_persists_validated_specification(
    client: TestClient,
    root_token: str,
    root_admin,
    db: Session,
) -> None:
    _bootstrap_fact_compiler(db, root_admin)
    token, matter_id, _ = _setup(client, root_token)
    _create_issue_field(client, token, matter_id)
    guidance = _create_published_guidance(client, token, matter_id)
    task = _create_fact_task(client, token, matter_id, guidance["id"]).json()

    queued = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/compile",
        headers=auth(token),
    )
    assert queued.status_code == 202, queued.text
    version = queued.json()["version"]
    assert version["compilation_status"] == "GENERATING"
    assert version["compilation_progress"]["stage"] == "QUEUED"
    assert version["compilation_progress"]["guidance_reference_count"] == 2
    workflow = db.get(WorkflowRun, uuid.UUID(version["compiler_workflow_run_id"]))
    assert workflow is not None
    assert workflow.workflow_key == "fact_development_specification_compilation_v1"
    assert workflow.input_snapshot["review_guidance_content_hash"] == version["review_guidance_content_hash"]
    duplicate = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/compile",
        headers=auth(token),
    )
    assert duplicate.status_code == 409

    compiled_output = {
        "fact_development_specification": _specification(),
        "issue_rationales": {"pricing": "Defined in guidance.", "safety": "Defined in guidance."},
        "omissions": [],
        "warnings": [],
    }
    result = compile_fact_specification_version(
        db,
        uuid.UUID(version["id"]),
        model=TestModel(custom_output_args={"compiled_output_json": json.dumps(compiled_output)}),
    )
    assert [issue["key"] for issue in result["issues"]] == ["pricing", "safety"]
    db.expire_all()
    stored = db.get(MatterAnalysisTaskVersion, uuid.UUID(version["id"]))
    assert stored is not None
    assert stored.compilation_status == "READY"
    assert stored.compiler_skill_run_id is not None
    assert stored.source_provenance["specification_origin"] == "COMPILER"
    assert stored.validation_report["status"] == "VALID"
    workflow = db.get(WorkflowRun, stored.compiler_workflow_run_id)
    skill_run = db.get(SkillRun, stored.compiler_skill_run_id)
    assert workflow is not None and workflow.status == "COMPLETED"
    assert workflow.progress["issue_count"] == 2
    assert skill_run is not None and skill_run.status == "COMPLETED"


def test_fact_compiler_validator_rejects_invented_hashes_and_missing_rationales(
    client: TestClient,
    root_token: str,
) -> None:
    token, matter_id, _ = _setup(client, root_token)
    _create_issue_field(client, token, matter_id)
    catalog = fact_source_catalog(TASK_DEFINITION, GUIDANCE_MARKDOWN)
    with TestingSessionLocal() as db:
        definitions = mappable_metadata_definitions(db, uuid.UUID(matter_id))
    validate = fact_compiler_output_validator(source_catalog=catalog, definitions=definitions)

    output = {
        "fact_development_specification": _specification(),
        "issue_rationales": {"pricing": "Defined.", "safety": "Defined."},
        "omissions": [],
        "warnings": [],
    }
    validate({"compiled_output_json": json.dumps(output)})

    output["issue_rationales"] = {"pricing": "Defined."}
    with pytest.raises(ValueError, match="issue_rationales is missing: safety"):
        validate({"compiled_output_json": json.dumps(output)})

    output["issue_rationales"] = {"pricing": "Defined.", "safety": "Defined."}
    output["fact_development_specification"]["issues"][0]["source_refs"][0]["excerpt_hash"] = "f" * 64
    with pytest.raises(ValueError, match="compiler repair rules"):
        validate({"compiled_output_json": json.dumps(output)})

    with pytest.raises(ValueError, match="not valid JSON"):
        validate({"compiled_output_json": "{not json"})


def _ready_batch(db: Session, matter_id: str, user_id: str, document_ids: list[str]) -> ReviewBatch:
    batch = ReviewBatch(
        matter_id=uuid.UUID(matter_id),
        name="Responsive",
        selection_type="ALL_MATTER",
        status="READY",
        search_status="NOT_CONFIGURED",
        workflow_id=f"test-batch:{uuid.uuid4()}",
        document_count=len(document_ids),
        created_by_user_id=uuid.UUID(user_id),
    )
    db.add(batch)
    db.flush()
    db.add_all(
        ReviewBatchDocument(
            review_batch_id=batch.id,
            matter_document_id=uuid.UUID(document_id),
            sequence_number=index + 1,
        )
        for index, document_id in enumerate(document_ids)
    )
    db.flush()
    return batch


def test_issue_membership_matches_hierarchy_boolean_and_text_from_matter_coding(
    client: TestClient,
    root_token: str,
) -> None:
    token, matter_id, user_id = _setup(client, root_token)
    field = _create_issue_field(client, token, matter_id)
    hot = client.post(
        f"/v1/matters/{matter_id}/metadata-definitions",
        headers=auth(token),
        json={"key": "hot_document", "display_name": "Hot", "type": "BOOLEAN"},
    ).json()
    request = client.post(
        f"/v1/matters/{matter_id}/metadata-definitions",
        headers=auth(token),
        json={"key": "request_number", "display_name": "Request", "type": "TEXT"},
    ).json()
    document_ids = add_documents(matter_id, uuid.UUID(user_id), count=5)
    coding = [
        (document_ids[0], field["id"], ["pricing_discounts"]),
        (document_ids[1], field["id"], ["safety", "pricing"]),
        (document_ids[2], hot["id"], [True]),
        (document_ids[3], request["id"], ["RFP 12"]),
    ]
    with TestingSessionLocal() as db:
        for document_id, definition_id, values in coding:
            if definition_id == field["id"]:
                apply_metadata_values(
                    db,
                    matter_id=uuid.UUID(matter_id),
                    document_id=uuid.UUID(document_id),
                    definition_id=uuid.UUID(definition_id),
                    values=values,
                    replace=True,
                    source_type="HUMAN",
                    actor_id=uuid.UUID(user_id),
                    source_id=f"test:{document_id}",
                )
            else:
                add_metadata_event(
                    db,
                    matter_id=uuid.UUID(matter_id),
                    document_id=uuid.UUID(document_id),
                    definition_id=uuid.UUID(definition_id),
                    operation="SET",
                    value=values[0],
                    source_type="HUMAN",
                    actor_id=uuid.UUID(user_id),
                    source_id=f"test:{document_id}",
                )
        batch = _ready_batch(db, matter_id, user_id, document_ids)
        db.commit()
        batch_id = batch.id

    specification = _specification()
    specification["issues"].append(
        {
            "key": "hot_docs",
            "name": "Hot documents",
            "fact_guidance": "Key admissions.",
            "source_refs": specification["issues"][0]["source_refs"],
            "coding_mappings": [
                {"metadata_definition_key": "hot_document", "match_type": "BOOLEAN", "value": True},
                {"metadata_definition_key": "request_number", "match_type": "TEXT_EXACT", "value": "RFP 12"},
            ],
        }
    )
    specification["issues"].append(
        {
            "key": "pricing_exact",
            "name": "Pricing only",
            "fact_guidance": "Pricing without discounts.",
            "source_refs": specification["issues"][0]["source_refs"],
            "coding_mappings": [
                {
                    "metadata_definition_key": "issue_coding",
                    "match_type": "ENUM_VALUE",
                    "value": "pricing",
                    "include_descendants": False,
                }
            ],
        }
    )
    with TestingSessionLocal() as db:
        membership = evaluate_issue_membership(
            db,
            matter_id=uuid.UUID(matter_id),
            specification=FactDevelopmentSpecification.model_validate(specification),
            document_ids=[uuid.UUID(value) for value in document_ids],
        )
    assert membership == {
        uuid.UUID(document_ids[0]): ["pricing"],
        uuid.UUID(document_ids[1]): ["pricing", "safety", "pricing_exact"],
        uuid.UUID(document_ids[2]): ["hot_docs"],
        uuid.UUID(document_ids[3]): ["hot_docs"],
    }

    guidance = _create_published_guidance(client, token, matter_id)
    task = _create_fact_task(client, token, matter_id, guidance["id"]).json()
    assert _put_specification(client, token, matter_id, task["id"], specification).status_code == 200
    preview = client.post(
        f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/issue-membership-preview",
        headers=auth(token),
        json={"review_batch_id": str(batch_id)},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["batch_document_count"] == 5
    assert body["matched_document_count"] == 4
    assert {item["issue_key"]: item["document_count"] for item in body["issues"]} == {
        "pricing": 2,
        "safety": 1,
        "hot_docs": 2,
        "pricing_exact": 1,
    }


def test_issue_membership_reads_reviewer_coding_at_the_pinned_revision(
    client: TestClient,
    root_token: str,
) -> None:
    token, matter_id, user_id = _setup(client, root_token)
    field = _create_issue_field(client, token, matter_id)
    document_ids = add_documents(matter_id, uuid.UUID(user_id), count=2)
    with TestingSessionLocal() as db:
        batch = _ready_batch(db, matter_id, user_id, document_ids)
        reviewer_coding = ReviewBatchReviewerCoding(review_batch_id=batch.id, reviewer_user_id=uuid.UUID(user_id))
        db.add(reviewer_coding)
        db.flush()
        definition = db.get(MetadataDefinition, uuid.UUID(field["id"]))
        assert definition is not None
        append_reviewer_coding_revision(
            db,
            coding=reviewer_coding,
            matter_document_id=uuid.UUID(document_ids[0]),
            status="COMPLETED",
            field_replacements=[ReviewerFieldReplacement(definition=definition, values=("safety",))],
            actor_user_id=uuid.UUID(user_id),
            tenant_id=_tenant_id(db, matter_id),
            matter_id=uuid.UUID(matter_id),
        )
        db.commit()
        batch_id, coding_id = batch.id, reviewer_coding.id

    guidance = _create_published_guidance(client, token, matter_id)
    task = _create_fact_task(client, token, matter_id, guidance["id"]).json()
    assert _put_specification(client, token, matter_id, task["id"], _specification()).status_code == 200
    preview_url = f"/v1/matters/{matter_id}/analysis-tasks/{task['id']}/versions/1/issue-membership-preview"
    first = client.post(
        preview_url,
        headers=auth(token),
        json={"review_batch_id": str(batch_id), "coding_source": "REVIEWER_CODING", "reviewer_coding_id": str(coding_id)},
    )
    assert first.status_code == 200, first.text
    assert first.json()["reviewer_coding_revision"] == 1
    assert {item["issue_key"]: item["document_count"] for item in first.json()["issues"]} == {
        "pricing": 0,
        "safety": 1,
    }

    with TestingSessionLocal() as db:
        reviewer_coding = db.get(ReviewBatchReviewerCoding, coding_id)
        definition = db.get(MetadataDefinition, uuid.UUID(field["id"]))
        assert reviewer_coding is not None and definition is not None
        append_reviewer_coding_revision(
            db,
            coding=reviewer_coding,
            matter_document_id=uuid.UUID(document_ids[1]),
            status="COMPLETED",
            field_replacements=[ReviewerFieldReplacement(definition=definition, values=("pricing",))],
            actor_user_id=uuid.UUID(user_id),
            tenant_id=_tenant_id(db, matter_id),
            matter_id=uuid.UUID(matter_id),
        )
        db.commit()
        pinned = evaluate_issue_membership(
            db,
            matter_id=uuid.UUID(matter_id),
            specification=FactDevelopmentSpecification.model_validate(_specification()),
            document_ids=[uuid.UUID(value) for value in document_ids],
            coding_source="REVIEWER_CODING",
            reviewer_coding_id=coding_id,
            reviewer_coding_revision=1,
        )
    assert pinned == {uuid.UUID(document_ids[0]): ["safety"]}

    wrong_source = client.post(
        preview_url,
        headers=auth(token),
        json={"review_batch_id": str(batch_id), "reviewer_coding_id": str(coding_id)},
    )
    assert wrong_source.status_code == 422


def _tenant_id(db: Session, matter_id: str) -> uuid.UUID:
    matter = db.get(Matter, uuid.UUID(matter_id))
    assert matter is not None
    return matter.client.tenant_id
