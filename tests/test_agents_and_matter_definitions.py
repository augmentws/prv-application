from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.bootstrap import ensure_standard_agents
from app.models import AgentDefinition, AgentDefinitionVersion, AgentVersionTool, User


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_tenant_context(client: TestClient, root_token: str) -> tuple[str, str, str]:
    root_tenant_id = client.get("/v1/auth/me", headers=auth(root_token)).json()["tenant_id"]
    tenant_response = client.post(
        "/v1/tenants",
        headers=auth(root_token),
        json={
            "parent_tenant_id": root_tenant_id,
            "slug": "agent-test-tenant",
            "name": "Agent Test Tenant",
            "initial_admin": {
                "email": "agent-admin@example.com",
                "display_name": "Agent Admin",
                "password": "another-correct-horse-password",
            },
        },
    )
    assert tenant_response.status_code == 201, tenant_response.text
    tenant_id = tenant_response.json()["tenant"]["id"]
    login_response = client.post(
        "/v1/auth/login",
        json={"email": "agent-admin@example.com", "password": "another-correct-horse-password"},
    )
    assert login_response.status_code == 200, login_response.text
    tenant_token = login_response.json()["access_token"]
    client_response = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(tenant_token),
        json={"name": "Agent Test Client"},
    )
    assert client_response.status_code == 201, client_response.text
    matter_response = client.post(
        f"/v1/clients/{client_response.json()['id']}/matters",
        headers=auth(tenant_token),
        json={"name": "Agent Test Matter"},
    )
    assert matter_response.status_code == 201, matter_response.text
    return tenant_id, tenant_token, matter_response.json()["id"]


def agent_payload(*, key: str = "matter_definition_setup") -> dict:
    return {
        "key": key,
        "name": "Matter Definition Setup",
        "description": "Reconciles reviewer guidance with coding fields.",
        "initial_version": {
            "system_prompt": "Guide the user through an approval-gated setup.",
            "model_key": "configured-default",
            "model_policy": {"temperature": 0},
            "output_schema": {"type": "object"},
            "limits": {"max_tool_calls": 20},
            "tools": [
                {"key": "matter_definition.read", "configuration": {}},
                {"key": "matter_definition.apply_draft_edit", "configuration": {}},
            ],
        },
    }


def test_standard_matter_definition_agent_bootstrap_is_idempotent(db: Session, root_admin: User) -> None:
    assert ensure_standard_agents(db, root_admin.tenant, root_admin) is True
    db.commit()
    assert ensure_standard_agents(db, root_admin.tenant, root_admin) is False

    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.key == "matter_definition_setup"))
    assert agent is not None
    assert agent.scope == "SYSTEM"
    assert agent.published_version == 1
    version = db.scalar(
        select(AgentDefinitionVersion).where(
            AgentDefinitionVersion.agent_definition_id == agent.id,
            AgentDefinitionVersion.version == 1,
        )
    )
    assert version is not None
    assert version.status == "PUBLISHED"
    assert db.scalar(
        select(func.count()).select_from(AgentVersionTool).where(
            AgentVersionTool.agent_definition_version_id == version.id
        )
    ) == 11


def test_root_and_tenant_agent_control_plane(client: TestClient, root_token: str) -> None:
    tenant_id, tenant_token, matter_id = create_tenant_context(client, root_token)

    tools_response = client.get("/v1/agent-tools", headers=auth(tenant_token))
    assert tools_response.status_code == 200
    tools = {tool["key"]: tool for tool in tools_response.json()}
    assert tools["matter_definition.read"]["requires_approval"] is False
    assert tools["matter_definition.read"]["runtime_available"] is True
    assert tools["matter_definition.apply_draft_edit"]["requires_approval"] is True
    assert tools["matter_metadata.create_definition"]["runtime_available"] is True
    assert tools["matter_metadata.enum.deactivate"]["runtime_available"] is True

    models_response = client.get("/v1/agent-models", headers=auth(root_token))
    assert models_response.status_code == 200
    assert models_response.json()[0]["key"] == "configured-default"

    system_response = client.post(
        "/v1/admin/agents",
        headers=auth(root_token),
        json=agent_payload(),
    )
    assert system_response.status_code == 201, system_response.text
    system_agent = system_response.json()
    assert system_agent["agent"]["scope"] == "SYSTEM"
    assert system_agent["version"]["status"] == "DRAFT"
    assert [tool["key"] for tool in system_agent["version"]["tools"]] == [
        "matter_definition.apply_draft_edit",
        "matter_definition.read",
    ]

    publish_response = client.post(
        f"/v1/agents/{system_agent['agent']['id']}/versions/1/publish",
        headers=auth(root_token),
    )
    assert publish_response.status_code == 200, publish_response.text
    assert publish_response.json()["status"] == "PUBLISHED"

    versions_response = client.get(
        f"/v1/agents/{system_agent['agent']['id']}/versions",
        headers=auth(root_token),
    )
    assert versions_response.status_code == 200
    assert [version["version"] for version in versions_response.json()] == [1]

    suspend_response = client.patch(
        f"/v1/agents/{system_agent['agent']['id']}",
        headers=auth(root_token),
        json={"status": "SUSPENDED", "description": "Temporarily unavailable."},
    )
    assert suspend_response.status_code == 200, suspend_response.text
    assert suspend_response.json()["status"] == "SUSPENDED"

    publish_while_suspended = client.post(
        f"/v1/agents/{system_agent['agent']['id']}/versions/1/publish",
        headers=auth(root_token),
    )
    assert publish_while_suspended.status_code == 409

    reactivate_response = client.patch(
        f"/v1/agents/{system_agent['agent']['id']}",
        headers=auth(root_token),
        json={"status": "ACTIVE"},
    )
    assert reactivate_response.status_code == 200

    tenant_list = client.get(f"/v1/tenants/{tenant_id}/agents", headers=auth(tenant_token))
    assert tenant_list.status_code == 200
    assert [(agent["scope"], agent["key"]) for agent in tenant_list.json()] == [
        ("SYSTEM", "matter_definition_setup")
    ]

    matter_list = client.get(f"/v1/matters/{matter_id}/agents", headers=auth(tenant_token))
    assert matter_list.status_code == 200
    assert [(agent["scope"], agent["key"]) for agent in matter_list.json()] == [
        ("SYSTEM", "matter_definition_setup")
    ]

    forbidden_system_edit = client.post(
        f"/v1/agents/{system_agent['agent']['id']}/versions",
        headers=auth(tenant_token),
        json=agent_payload()["initial_version"],
    )
    assert forbidden_system_edit.status_code == 403

    tenant_response = client.post(
        f"/v1/tenants/{tenant_id}/agents",
        headers=auth(tenant_token),
        json=agent_payload(key="tenant_coding_assistant"),
    )
    assert tenant_response.status_code == 201, tenant_response.text
    assert tenant_response.json()["agent"]["scope"] == "TENANT"

    tenant_agent_id = tenant_response.json()["agent"]["id"]
    publish_tenant_agent = client.post(
        f"/v1/agents/{tenant_agent_id}/versions/1/publish",
        headers=auth(tenant_token),
    )
    assert publish_tenant_agent.status_code == 200
    incompatible_conversation = client.post(
        f"/v1/matters/{matter_id}/agent-conversations",
        headers=auth(tenant_token),
        json={"agent_definition_id": tenant_agent_id, "workflow_type": "MATTER_DEFINITION_SETUP"},
    )
    assert incompatible_conversation.status_code == 409
    assert "not compatible" in incompatible_conversation.json()["error"]["message"]

    bad_tool_payload = agent_payload(key="bad_tool")
    bad_tool_payload["initial_version"]["tools"] = [{"key": "arbitrary.http", "configuration": {}}]
    bad_tool_response = client.post(
        f"/v1/tenants/{tenant_id}/agents",
        headers=auth(tenant_token),
        json=bad_tool_payload,
    )
    assert bad_tool_response.status_code == 422
    assert "Unknown agent tools" in bad_tool_response.json()["error"]["message"]

    metadata_tool_payload = agent_payload(key="metadata_tool")
    metadata_tool_payload["initial_version"]["tools"] = [
        {"key": "matter_metadata.create_definition", "configuration": {}}
    ]
    metadata_tool_response = client.post(
        f"/v1/tenants/{tenant_id}/agents",
        headers=auth(tenant_token),
        json=metadata_tool_payload,
    )
    assert metadata_tool_response.status_code == 201, metadata_tool_response.text


def test_matter_definition_revisions_and_publish(client: TestClient, root_token: str) -> None:
    _, tenant_token, matter_id = create_tenant_context(client, root_token)

    empty_response = client.get(f"/v1/matters/{matter_id}/definition", headers=auth(tenant_token))
    assert empty_response.status_code == 200
    assert empty_response.json() is None

    first_response = client.post(
        f"/v1/matters/{matter_id}/definition/revisions",
        headers=auth(tenant_token),
        json={
            "content_markdown": "# Coding instructions\n\nReview for responsiveness.",
            "source_kind": "PASTE",
        },
    )
    assert first_response.status_code == 201, first_response.text
    first = first_response.json()
    assert first["current_revision"] == 1
    assert first["published_revision"] is None
    assert first["revision"]["source_kind"] == "PASTE"

    stale_response = client.post(
        f"/v1/matters/{matter_id}/definition/revisions",
        headers=auth(tenant_token),
        json={
            "content_markdown": "stale edit",
            "source_kind": "USER_EDIT",
            "based_on_revision": 2,
        },
    )
    assert stale_response.status_code == 409

    second_response = client.post(
        f"/v1/matters/{matter_id}/definition/revisions",
        headers=auth(tenant_token),
        json={
            "content_markdown": "# Coding instructions\n\nReview for responsiveness and privilege.",
            "source_kind": "USER_EDIT",
            "based_on_revision": 1,
        },
    )
    assert second_response.status_code == 201, second_response.text
    assert second_response.json()["current_revision"] == 2

    publish_response = client.post(
        f"/v1/matters/{matter_id}/definition/revisions/2/publish",
        headers=auth(tenant_token),
    )
    assert publish_response.status_code == 200, publish_response.text
    assert publish_response.json()["published_revision"] == 2
    assert publish_response.json()["revision"]["revision"] == 2

    revisions_response = client.get(
        f"/v1/matters/{matter_id}/definition/revisions",
        headers=auth(tenant_token),
    )
    assert revisions_response.status_code == 200
    assert [revision["revision"] for revision in revisions_response.json()] == [2, 1]
