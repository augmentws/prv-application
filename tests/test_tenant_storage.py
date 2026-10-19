import uuid

from conftest import ArtifactTestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import select

from artifact_service.models import TenantStorage
from tests.test_authorization import auth, create_tenant, login


def _storage_record(tenant_id: str) -> TenantStorage | None:
    with ArtifactTestingSessionLocal() as artifact_db:
        return artifact_db.scalar(select(TenantStorage).where(TenantStorage.tenant_id == uuid.UUID(tenant_id)))


def test_tenant_creation_provisions_artifact_storage(client: TestClient, root_token: str) -> None:
    created = create_tenant(client, root_token, slug="storage-on-create")

    record = _storage_record(created["tenant"]["id"])
    assert record is not None
    assert record.status == "ACTIVE"


def test_failed_storage_provisioning_leaves_no_tenant(client: TestClient, root_token: str, monkeypatch) -> None:
    def fail(**_kwargs):
        raise RuntimeError("object store unavailable")

    monkeypatch.setattr("app.routers.tenants.ensure_tenant_artifact_storage", fail)
    response = client.post(
        "/v1/tenants",
        headers=auth(root_token),
        json={
            "parent_tenant_id": create_root_tenant_id(client, root_token),
            "slug": "storage-fails",
            "name": "Storage fails",
            "initial_admin": {
                "email": "storage-fails-admin@example.com",
                "display_name": "Storage Fails Admin",
                "password": "storage-fails-password",
            },
        },
    )

    assert response.status_code == 503, response.text
    slugs = [tenant["slug"] for tenant in client.get("/v1/tenants", headers=auth(root_token)).json()]
    assert "storage-fails" not in slugs


def test_storage_repair_endpoint_requires_tenant_administration(client: TestClient, root_token: str) -> None:
    created = create_tenant(client, root_token, slug="storage-repair")
    tenant_id = created["tenant"]["id"]
    admin_token = login(client, created["initial_admin"]["email"], "rbac-administrator-password")
    user = client.post(
        f"/v1/tenants/{tenant_id}/users",
        headers=auth(admin_token),
        json={"email": "storage-user@example.com", "display_name": "Storage User", "password": "storage-user-password"},
    )
    assert user.status_code == 201, user.text
    user_token = login(client, "storage-user@example.com", "storage-user-password")

    denied = client.post(f"/v1/tenants/{tenant_id}/artifact-storage/ensure", headers=auth(user_token))
    allowed = client.post(f"/v1/tenants/{tenant_id}/artifact-storage/ensure", headers=auth(admin_token))

    assert denied.status_code == 403, denied.text
    assert allowed.status_code == 201, allowed.text
    assert _storage_record(tenant_id) is not None


def create_root_tenant_id(client: TestClient, root_token: str) -> str:
    tenants = client.get("/v1/tenants", headers=auth(root_token)).json()
    return next(tenant["id"] for tenant in tenants if tenant["is_root"])


def test_root_tenant_cannot_contain_clients(client: TestClient, root_token: str) -> None:
    root_id = create_root_tenant_id(client, root_token)

    response = client.post(
        f"/v1/tenants/{root_id}/clients",
        headers=auth(root_token),
        json={"name": "Not Allowed"},
    )

    assert response.status_code == 403, response.text
    assert "root tenant" in response.json()["error"]["message"]
