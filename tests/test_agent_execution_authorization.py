import uuid

from conftest import TestingSessionLocal
from fastapi.testclient import TestClient

from app.agent_runtime import _actor_can_execute_matter_changes
from app.models import Matter, User
from tests.test_authorization import auth, create_matter, create_tenant, login


def _create_user(client: TestClient, admin_token: str, tenant_id: str, email: str) -> dict:
    response = client.post(
        f"/v1/tenants/{tenant_id}/users",
        headers=auth(admin_token),
        json={"email": email, "display_name": email, "password": "agent-authz-user-password"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _authorization_decision(user_id: str, matter_id: str) -> bool:
    with TestingSessionLocal() as db:
        user = db.get(User, uuid.UUID(user_id))
        matter = db.get(Matter, uuid.UUID(matter_id))
        assert user is not None
        assert matter is not None
        return _actor_can_execute_matter_changes(db, user, matter)


def test_actor_without_a_matter_grant_cannot_execute_matter_changes(client: TestClient, root_token: str) -> None:
    created = create_tenant(client, root_token, slug="agent-authz-none")
    admin_token = login(client, created["initial_admin"]["email"], "rbac-administrator-password")
    _, matter = create_matter(client, admin_token, created["tenant"]["id"])
    user = _create_user(client, admin_token, created["tenant"]["id"], "no-grant@example.com")

    assert _authorization_decision(user["id"], matter["id"]) is False


def test_rbac_matter_administrator_can_execute_matter_changes(client: TestClient, root_token: str) -> None:
    created = create_tenant(client, root_token, slug="agent-authz-rbac")
    tenant = created["tenant"]
    admin_token = login(client, created["initial_admin"]["email"], "rbac-administrator-password")
    _, matter = create_matter(client, admin_token, tenant["id"])
    user = _create_user(client, admin_token, tenant["id"], "matter-admin@example.com")
    roles = client.get(f"/v1/tenants/{tenant['id']}/roles", headers=auth(admin_token)).json()
    matter_admin_role = next(role for role in roles if role["key"] == "MATTER_ADMINISTRATOR")
    grant = client.post(
        f"/v1/tenants/{tenant['id']}/authorization-grants",
        headers=auth(admin_token),
        json={
            "subject": {"type": "USER", "id": user["id"]},
            "role_id": matter_admin_role["id"],
            "scope": {"type": "MATTER", "id": matter["id"]},
        },
    )
    assert grant.status_code == 201, grant.text

    assert _authorization_decision(user["id"], matter["id"]) is True


def test_tenant_administrator_can_execute_matter_changes(client: TestClient, root_token: str) -> None:
    created = create_tenant(client, root_token, slug="agent-authz-admin")
    admin_token = login(client, created["initial_admin"]["email"], "rbac-administrator-password")
    _, matter = create_matter(client, admin_token, created["tenant"]["id"])

    assert _authorization_decision(created["initial_admin"]["id"], matter["id"]) is True


def test_root_administrator_can_execute_matter_changes_in_a_subtenant(client: TestClient, root_token: str, root_admin) -> None:
    created = create_tenant(client, root_token, slug="agent-authz-root")
    admin_token = login(client, created["initial_admin"]["email"], "rbac-administrator-password")
    _, matter = create_matter(client, admin_token, created["tenant"]["id"])

    assert _authorization_decision(str(root_admin.id), matter["id"]) is True
