import asyncio
import uuid

from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from sqlalchemy import select

from app import agent_runtime
from app.agent_runtime import (
    build_agent,
    execute_prepared_agent_run,
    persist_agent_run_outcome,
    prepare_agent_run,
)
from app.models import AuditRecord
from tests.test_agents_and_matter_definitions import agent_payload, auth, create_tenant_context


def create_published_agent(
    client: TestClient,
    root_token: str,
    *,
    tool_keys: list[str] | None = None,
) -> str:
    payload = agent_payload()
    payload["initial_version"]["tools"] = [
        {"key": key, "configuration": {}}
        for key in tool_keys
        or ["matter_definition.read", "matter_definition.apply_draft_edit"]
    ]
    response = client.post("/v1/admin/agents", headers=auth(root_token), json=payload)
    assert response.status_code == 201, response.text
    agent_id = response.json()["agent"]["id"]
    publish = client.post(
        f"/v1/agents/{agent_id}/versions/1/publish",
        headers=auth(root_token),
    )
    assert publish.status_code == 200, publish.text
    return agent_id


def start_conversation_and_turn(
    client: TestClient,
    tenant_token: str,
    matter_id: str,
    agent_id: str,
) -> tuple[str, str]:
    conversation_response = client.post(
        f"/v1/matters/{matter_id}/agent-conversations",
        headers=auth(tenant_token),
        json={"agent_definition_id": agent_id, "workflow_type": "MATTER_DEFINITION_SETUP"},
    )
    assert conversation_response.status_code == 201, conversation_response.text
    conversation_id = conversation_response.json()["id"]
    turn_response = client.post(
        f"/v1/agent-conversations/{conversation_id}/turns",
        headers=auth(tenant_token),
        json={"message": "Review these coding instructions."},
    )
    assert turn_response.status_code == 202, turn_response.text
    return conversation_id, turn_response.json()["run"]["id"]


def test_agent_runtime_completes_with_injected_pydantic_model(
    client: TestClient,
    root_token: str,
    monkeypatch,
) -> None:
    _, tenant_token, matter_id = create_tenant_context(client, root_token)
    agent_id = create_published_agent(client, root_token)
    conversation_id, run_id = start_conversation_and_turn(
        client, tenant_token, matter_id, agent_id
    )
    monkeypatch.setattr(agent_runtime, "SessionLocal", TestingSessionLocal)

    prepared = prepare_agent_run(uuid.UUID(run_id))
    outcome = asyncio.run(
        execute_prepared_agent_run(
            prepared,
            build_agent(),
            model=TestModel(call_tools=[], custom_output_text="The guidance is ready for field reconciliation."),
        )
    )
    assert outcome.status == "COMPLETED"
    persist_agent_run_outcome(uuid.UUID(run_id), outcome)

    runs = client.get(
        f"/v1/agent-conversations/{conversation_id}/runs", headers=auth(tenant_token)
    )
    assert runs.status_code == 200
    assert runs.json()[0]["status"] == "COMPLETED"
    messages = client.get(
        f"/v1/agent-conversations/{conversation_id}/messages", headers=auth(tenant_token)
    )
    assert [message["role"] for message in messages.json()] == ["USER", "ASSISTANT"]
    assert messages.json()[1]["content"] == "The guidance is ready for field reconciliation."


def test_approval_request_rejection_resumes_as_a_new_run(
    client: TestClient,
    root_token: str,
    monkeypatch,
) -> None:
    _, tenant_token, matter_id = create_tenant_context(client, root_token)
    agent_id = create_published_agent(client, root_token)
    conversation_id, run_id = start_conversation_and_turn(
        client, tenant_token, matter_id, agent_id
    )
    monkeypatch.setattr(agent_runtime, "SessionLocal", TestingSessionLocal)

    prepared = prepare_agent_run(uuid.UUID(run_id))
    deferred = asyncio.run(
        execute_prepared_agent_run(
            prepared,
            build_agent(),
            model=TestModel(call_tools=["matter_definition_apply_draft_edit"]),
        )
    )
    assert deferred.status == "WAITING_APPROVAL"
    persist_agent_run_outcome(uuid.UUID(run_id), deferred)

    actions = client.get(
        f"/v1/agent-conversations/{conversation_id}/action-requests",
        headers=auth(tenant_token),
    )
    assert actions.status_code == 200
    assert len(actions.json()) == 1
    action = actions.json()[0]
    assert action["tool_key"] == "matter_definition.apply_draft_edit"
    assert action["status"] == "PENDING"

    decision = client.post(
        f"/v1/agent-action-requests/{action['id']}/decision",
        headers=auth(tenant_token),
        json={"decision": "REJECT", "reason": "Keep the original wording."},
    )
    assert decision.status_code == 200, decision.text
    resumed_run = decision.json()["resumed_run"]
    assert resumed_run["deferred_from_run_id"] == run_id
    assert decision.json()["action_request"]["status"] == "REJECTED"

    resumed_prepared = prepare_agent_run(uuid.UUID(resumed_run["id"]))
    completed = asyncio.run(
        execute_prepared_agent_run(
            resumed_prepared,
            build_agent(),
            model=TestModel(call_tools=[], custom_output_text="I kept the existing draft unchanged."),
        )
    )
    persist_agent_run_outcome(uuid.UUID(resumed_run["id"]), completed)

    runs = client.get(
        f"/v1/agent-conversations/{conversation_id}/runs", headers=auth(tenant_token)
    )
    assert [run["status"] for run in runs.json()] == ["WAITING_APPROVAL", "COMPLETED"]
    messages = client.get(
        f"/v1/agent-conversations/{conversation_id}/messages", headers=auth(tenant_token)
    )
    assert messages.json()[-1]["content"] == "I kept the existing draft unchanged."


def test_approved_agent_edit_uses_domain_command_and_records_revision(
    client: TestClient,
    root_token: str,
    monkeypatch,
) -> None:
    _, tenant_token, matter_id = create_tenant_context(client, root_token)
    definition = client.post(
        f"/v1/matters/{matter_id}/definition/revisions",
        headers=auth(tenant_token),
        json={"content_markdown": "# Original guidance", "source_kind": "PASTE"},
    )
    assert definition.status_code == 201, definition.text
    agent_id = create_published_agent(client, root_token)
    conversation_id, run_id = start_conversation_and_turn(
        client, tenant_token, matter_id, agent_id
    )
    monkeypatch.setattr(agent_runtime, "SessionLocal", TestingSessionLocal)

    model_call_count = 0

    def model_function(_messages, _info):
        nonlocal model_call_count
        model_call_count += 1
        if model_call_count == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="matter_definition_apply_draft_edit",
                        args={
                            "content_markdown": "# Improved guidance\n\nCode responsiveness consistently.",
                            "based_on_revision": 1,
                            "reason": "Clarify the coding standard.",
                        },
                        tool_call_id="approved-edit-1",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("The approved guidance edit is now revision 2.")])

    model = FunctionModel(model_function)
    prepared = prepare_agent_run(uuid.UUID(run_id))
    deferred = asyncio.run(
        execute_prepared_agent_run(prepared, build_agent(), model=model)
    )
    persist_agent_run_outcome(uuid.UUID(run_id), deferred)
    action = client.get(
        f"/v1/agent-conversations/{conversation_id}/action-requests",
        headers=auth(tenant_token),
    ).json()[0]
    decision = client.post(
        f"/v1/agent-action-requests/{action['id']}/decision",
        headers=auth(tenant_token),
        json={"decision": "APPROVE"},
    )
    assert decision.status_code == 200, decision.text
    resumed_run_id = decision.json()["resumed_run"]["id"]

    resumed = prepare_agent_run(uuid.UUID(resumed_run_id))
    completed = asyncio.run(
        execute_prepared_agent_run(resumed, build_agent(), model=model)
    )
    persist_agent_run_outcome(uuid.UUID(resumed_run_id), completed)

    current_definition = client.get(
        f"/v1/matters/{matter_id}/definition", headers=auth(tenant_token)
    )
    assert current_definition.status_code == 200
    assert current_definition.json()["current_revision"] == 2
    assert current_definition.json()["revision"]["source_kind"] == "AGENT_EDIT"
    assert current_definition.json()["revision"]["agent_run_id"] == resumed_run_id
    action_after = client.get(
        f"/v1/agent-conversations/{conversation_id}/action-requests",
        headers=auth(tenant_token),
    ).json()[0]
    assert action_after["status"] == "EXECUTED"


def test_approved_agent_metadata_create_uses_shared_command(
    client: TestClient,
    root_token: str,
    monkeypatch,
) -> None:
    _, tenant_token, matter_id = create_tenant_context(client, root_token)
    agent_id = create_published_agent(
        client,
        root_token,
        tool_keys=["matter_metadata.create_definition"],
    )
    conversation_id, run_id = start_conversation_and_turn(
        client, tenant_token, matter_id, agent_id
    )
    monkeypatch.setattr(agent_runtime, "SessionLocal", TestingSessionLocal)

    model_call_count = 0

    def model_function(_messages, _info):
        nonlocal model_call_count
        model_call_count += 1
        if model_call_count == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="matter_metadata_create_definition",
                        args={
                            "key": "investigation_topic",
                            "display_name": "Investigation topic",
                            "field_type": "ENUM",
                            "cardinality": "MULTIPLE",
                            "allowed_values": [
                                {
                                    "key": "accounting",
                                    "label": "Accounting",
                                    "description": "Accounting and reporting conduct",
                                }
                            ],
                            "facetable": True,
                            "ai_assignable": True,
                            "reason": "The reviewer guidance defines this coding field.",
                        },
                        tool_call_id="approved-metadata-create-1",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("The approved coding field is now available.")])

    model = FunctionModel(model_function)
    prepared = prepare_agent_run(uuid.UUID(run_id))
    deferred = asyncio.run(execute_prepared_agent_run(prepared, build_agent(), model=model))
    assert deferred.status == "WAITING_APPROVAL"
    persist_agent_run_outcome(uuid.UUID(run_id), deferred)
    action = client.get(
        f"/v1/agent-conversations/{conversation_id}/action-requests",
        headers=auth(tenant_token),
    ).json()[0]
    assert action["tool_key"] == "matter_metadata.create_definition"

    decision = client.post(
        f"/v1/agent-action-requests/{action['id']}/decision",
        headers=auth(tenant_token),
        json={"decision": "APPROVE"},
    )
    assert decision.status_code == 200, decision.text
    resumed_run_id = decision.json()["resumed_run"]["id"]
    resumed = prepare_agent_run(uuid.UUID(resumed_run_id))
    completed = asyncio.run(execute_prepared_agent_run(resumed, build_agent(), model=model))
    persist_agent_run_outcome(uuid.UUID(resumed_run_id), completed)

    definitions = client.get(
        f"/v1/matters/{matter_id}/metadata-definitions",
        headers=auth(tenant_token),
    )
    created = next(
        definition
        for definition in definitions.json()
        if definition["key"] == "investigation_topic"
    )
    assert created["cardinality"] == "MULTIPLE"
    assert created["allowed_values"][0]["key"] == "accounting"
    assert created["ai_assignable"] is True
    with TestingSessionLocal() as db:
        audit = db.scalar(
            select(AuditRecord).where(
                AuditRecord.action == "metadata_definition.created",
                AuditRecord.target_id == uuid.UUID(created["id"]),
            )
        )
        assert audit is not None
        assert audit.details["agent_run_id"] == resumed_run_id
    action_after = client.get(
        f"/v1/agent-conversations/{conversation_id}/action-requests",
        headers=auth(tenant_token),
    ).json()[0]
    assert action_after["status"] == "EXECUTED"
