import uuid

import pytest
from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classification_lifecycle import authorization_context
from app.models import (
    AuditRecord,
    Matter,
    MatterDocument,
    MatterDocumentImportJob,
    Permission,
    ReviewBatch,
    Tenant,
    User,
)
from app.rbac_catalog import ensure_rbac_catalog
from app.rbac_manifest import SYSTEM_ROLE_BY_KEY


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_tenant(client: TestClient, root_token: str, *, slug: str = "rbac-test") -> dict:
    root_tenant_id = client.get("/v1/auth/me", headers=auth(root_token)).json()["tenant_id"]
    response = client.post(
        "/v1/tenants",
        headers=auth(root_token),
        json={
            "parent_tenant_id": root_tenant_id,
            "slug": slug,
            "name": "RBAC Test",
            "initial_admin": {
                "email": f"admin-{slug}@example.com",
                "display_name": "RBAC Administrator",
                "password": "rbac-administrator-password",
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def login(client: TestClient, email: str, password: str) -> str:
    response = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def create_matter(client: TestClient, token: str, tenant_id: str) -> tuple[dict, dict]:
    client_response = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(token),
        json={"name": "RBAC Client"},
    )
    assert client_response.status_code == 201, client_response.text
    matter_response = client.post(
        f"/v1/clients/{client_response.json()['id']}/matters",
        headers=auth(token),
        json={"name": "RBAC Matter"},
    )
    assert matter_response.status_code == 201, matter_response.text
    return client_response.json(), matter_response.json()


def test_code_owned_permission_catalog_detects_database_drift(db: Session) -> None:
    ensure_rbac_catalog(db)
    permission = db.scalar(select(Permission).where(Permission.key == "matter.view"))
    assert permission is not None
    permission.display_name = "Unexpected value"
    db.flush()

    with pytest.raises(RuntimeError, match="matter.view"):
        ensure_rbac_catalog(db)


def test_human_batch_promotion_is_reserved_for_matter_administrators_and_above() -> None:
    permission_key = "batch.publish_coding"
    assert permission_key in SYSTEM_ROLE_BY_KEY["MATTER_ADMINISTRATOR"].permission_keys
    assert permission_key in SYSTEM_ROLE_BY_KEY["CLIENT_ADMINISTRATOR"].permission_keys
    assert permission_key in SYSTEM_ROLE_BY_KEY["TENANT_ADMINISTRATOR"].permission_keys
    assert permission_key in SYSTEM_ROLE_BY_KEY["ROOT_ADMINISTRATOR"].permission_keys
    assert permission_key not in SYSTEM_ROLE_BY_KEY["REVIEWER"].permission_keys
    assert permission_key not in SYSTEM_ROLE_BY_KEY["ANALYSIS_OPERATOR"].permission_keys


def test_rbac_matter_administrator_is_authorized_for_classifier_workflows(
    client: TestClient,
    root_token: str,
) -> None:
    created = create_tenant(client, root_token, slug="classifier-rbac")
    tenant = created["tenant"]
    admin_token = login(client, created["initial_admin"]["email"], "rbac-administrator-password")
    _, matter = create_matter(client, admin_token, tenant["id"])
    user_response = client.post(
        f"/v1/tenants/{tenant['id']}/users",
        headers=auth(admin_token),
        json={
            "email": "classifier-manager@example.com",
            "display_name": "Classifier Manager",
            "password": "classifier-manager-password",
        },
    )
    assert user_response.status_code == 201, user_response.text
    user = user_response.json()
    roles_response = client.get(
        f"/v1/tenants/{tenant['id']}/roles",
        headers=auth(admin_token),
    )
    matter_admin_role = next(role for role in roles_response.json() if role["key"] == "MATTER_ADMINISTRATOR")
    grant_response = client.post(
        f"/v1/tenants/{tenant['id']}/authorization-grants",
        headers=auth(admin_token),
        json={
            "subject": {"type": "USER", "id": user["id"]},
            "role_id": matter_admin_role["id"],
            "scope": {"type": "MATTER", "id": matter["id"]},
        },
    )
    assert grant_response.status_code == 201, grant_response.text

    with TestingSessionLocal() as db:
        matter_record = db.get(Matter, uuid.UUID(matter["id"]))
        user_record = db.get(User, uuid.UUID(user["id"]))
        assert matter_record is not None
        assert user_record is not None
        context = authorization_context(db, matter=matter_record, actor_user_id=user_record.id)

    assert context["authorization_source"] == "RBAC_MATTER_ADMIN"
    assert context["required_role"] == "MATTER_ADMIN"


@pytest.mark.parametrize("permission_key", ["batch.delete", "job.delete"])
def test_soft_delete_is_reserved_for_matter_administrators_and_above(permission_key: str) -> None:
    assert permission_key in SYSTEM_ROLE_BY_KEY["MATTER_ADMINISTRATOR"].permission_keys
    assert permission_key in SYSTEM_ROLE_BY_KEY["CLIENT_ADMINISTRATOR"].permission_keys
    assert permission_key in SYSTEM_ROLE_BY_KEY["TENANT_ADMINISTRATOR"].permission_keys
    assert permission_key in SYSTEM_ROLE_BY_KEY["ROOT_ADMINISTRATOR"].permission_keys
    assert permission_key not in SYSTEM_ROLE_BY_KEY["REVIEWER"].permission_keys
    assert permission_key not in SYSTEM_ROLE_BY_KEY["ANALYSIS_OPERATOR"].permission_keys


def test_group_grant_inherits_permissions_and_explains_winning_grant(
    client: TestClient,
    root_token: str,
) -> None:
    created = create_tenant(client, root_token)
    tenant = created["tenant"]
    admin_token = login(client, created["initial_admin"]["email"], "rbac-administrator-password")
    _, matter = create_matter(client, admin_token, tenant["id"])

    user_response = client.post(
        f"/v1/tenants/{tenant['id']}/users",
        headers=auth(admin_token),
        json={
            "email": "reviewer@example.com",
            "display_name": "Reviewer",
            "password": "reviewer-password-long",
        },
    )
    assert user_response.status_code == 201, user_response.text
    user = user_response.json()
    assert user["tenant_role"] == "USER"
    user_token = login(client, user["email"], "reviewer-password-long")

    before = client.get(
        "/v1/authorization/effective",
        headers=auth(user_token),
        params={"resource_type": "MATTER", "resource_id": matter["id"]},
    )
    assert before.status_code == 200, before.text
    assert before.json()["permission_keys"] == []

    roles_response = client.get(
        f"/v1/tenants/{tenant['id']}/roles",
        headers=auth(admin_token),
    )
    assert roles_response.status_code == 200, roles_response.text
    reviewer_role = next(role for role in roles_response.json() if role["key"] == "REVIEWER")

    group_response = client.post(
        f"/v1/tenants/{tenant['id']}/access-groups",
        headers=auth(admin_token),
        json={"key": "review-team", "display_name": "Review team"},
    )
    assert group_response.status_code == 201, group_response.text
    group = group_response.json()
    membership_response = client.put(
        f"/v1/tenants/{tenant['id']}/access-groups/{group['id']}/members",
        headers=auth(admin_token),
        json={"user_ids": [user["id"]], "row_version": group["row_version"]},
    )
    assert membership_response.status_code == 200, membership_response.text

    grant_response = client.post(
        f"/v1/tenants/{tenant['id']}/authorization-grants",
        headers=auth(admin_token),
        json={
            "subject": {"type": "ACCESS_GROUP", "id": group["id"]},
            "role_id": reviewer_role["id"],
            "scope": {"type": "MATTER", "id": matter["id"]},
        },
    )
    assert grant_response.status_code == 201, grant_response.text
    grant = grant_response.json()

    effective = client.get(
        "/v1/authorization/effective",
        headers=auth(user_token),
        params={"resource_type": "MATTER", "resource_id": matter["id"]},
    )
    assert effective.status_code == 200, effective.text
    permissions = set(effective.json()["permission_keys"])
    assert {"matter.view", "document.view", "document.review", "batch.review"} <= permissions
    assert "metadata.manage" not in permissions

    explanation = client.get(
        "/v1/authorization/explain",
        headers=auth(user_token),
        params={
            "permission_key": "document.review",
            "resource_type": "MATTER",
            "resource_id": matter["id"],
        },
    )
    assert explanation.status_code == 200, explanation.text
    assert explanation.json() == {
        "allowed": True,
        "permission_key": "document.review",
        "resource_type": "MATTER",
        "resource_id": matter["id"],
        "grant_id": grant["id"],
        "role_id": reviewer_role["id"],
        "role_key": "REVIEWER",
        "subject_type": "ACCESS_GROUP",
        "subject_id": group["id"],
        "scope_type": "MATTER",
        "scope_id": matter["id"],
    }


def test_custom_roles_grants_conflicts_and_audit_are_transactional(
    client: TestClient,
    db: Session,
    root_token: str,
) -> None:
    created = create_tenant(client, root_token, slug="custom-role")
    tenant = created["tenant"]
    admin_token = login(client, created["initial_admin"]["email"], "rbac-administrator-password")
    _, matter = create_matter(client, admin_token, tenant["id"])
    user = client.post(
        f"/v1/tenants/{tenant['id']}/users",
        headers=auth(admin_token),
        json={
            "email": "analyst@example.com",
            "display_name": "Analyst",
            "password": "analyst-password-long",
        },
    ).json()

    created_role_response = client.post(
        f"/v1/tenants/{tenant['id']}/roles",
        headers=auth(admin_token),
        json={
            "key": "CASE_ANALYST",
            "display_name": "Case analyst",
            "permission_keys": ["matter.view", "document.view", "analysis_task.run"],
        },
    )
    assert created_role_response.status_code == 201, created_role_response.text
    role = created_role_response.json()

    grant_payload = {
        "subject": {"type": "USER", "id": user["id"]},
        "role_id": role["id"],
        "scope": {"type": "MATTER", "id": matter["id"]},
    }
    grant_response = client.post(
        f"/v1/tenants/{tenant['id']}/authorization-grants",
        headers=auth(admin_token),
        json=grant_payload,
    )
    assert grant_response.status_code == 201, grant_response.text
    duplicate = client.post(
        f"/v1/tenants/{tenant['id']}/authorization-grants",
        headers=auth(admin_token),
        json=grant_payload,
    )
    assert duplicate.status_code == 409

    stale_update = client.patch(
        f"/v1/tenants/{tenant['id']}/roles/{role['id']}",
        headers=auth(admin_token),
        json={"display_name": "Stale", "row_version": role["row_version"] + 1},
    )
    assert stale_update.status_code == 409

    system_roles = client.get(
        f"/v1/tenants/{tenant['id']}/roles",
        headers=auth(admin_token),
    ).json()
    reviewer_role = next(item for item in system_roles if item["key"] == "REVIEWER")
    immutable_system_role = client.put(
        f"/v1/tenants/{tenant['id']}/roles/{reviewer_role['id']}/permissions",
        headers=auth(admin_token),
        json={"permission_keys": [], "row_version": reviewer_role["row_version"]},
    )
    assert immutable_system_role.status_code == 409

    actions = list(
        db.scalars(
            select(AuditRecord.action).where(
                AuditRecord.tenant_id == uuid.UUID(tenant["id"]),
                AuditRecord.action.like("authorization.%"),
            )
        )
    )
    assert "authorization.role.created" in actions
    assert "authorization.grant.created" in actions
    tenant_row = db.get(Tenant, uuid.UUID(tenant["id"]))
    assert tenant_row is not None
    assert tenant_row.authorization_revision >= 5


def test_cross_tenant_grants_are_rejected_and_last_tenant_admin_is_preserved(
    client: TestClient,
    root_token: str,
) -> None:
    first = create_tenant(client, root_token, slug="first-tenant")
    second = create_tenant(client, root_token, slug="second-tenant")
    first_tenant = first["tenant"]
    first_admin_token = login(
        client,
        first["initial_admin"]["email"],
        "rbac-administrator-password",
    )
    _, second_matter = create_matter(client, root_token, second["tenant"]["id"])
    roles = client.get(
        f"/v1/tenants/{first_tenant['id']}/roles",
        headers=auth(first_admin_token),
    ).json()
    reviewer_role = next(role for role in roles if role["key"] == "REVIEWER")
    tenant_admin_role = next(role for role in roles if role["key"] == "TENANT_ADMINISTRATOR")

    cross_tenant = client.post(
        f"/v1/tenants/{first_tenant['id']}/authorization-grants",
        headers=auth(first_admin_token),
        json={
            "subject": {"type": "USER", "id": first["initial_admin"]["id"]},
            "role_id": reviewer_role["id"],
            "scope": {"type": "MATTER", "id": second_matter["id"]},
        },
    )
    assert cross_tenant.status_code == 422

    empty_group = client.post(
        f"/v1/tenants/{first_tenant['id']}/access-groups",
        headers=auth(first_admin_token),
        json={"key": "empty-admins", "display_name": "Empty administrators"},
    ).json()
    empty_group_grant = client.post(
        f"/v1/tenants/{first_tenant['id']}/authorization-grants",
        headers=auth(first_admin_token),
        json={
            "subject": {"type": "ACCESS_GROUP", "id": empty_group["id"]},
            "role_id": tenant_admin_role["id"],
            "scope": {"type": "TENANT", "id": first_tenant["id"]},
        },
    )
    assert empty_group_grant.status_code == 201, empty_group_grant.text

    grants_response = client.get(
        f"/v1/tenants/{first_tenant['id']}/authorization-grants",
        headers=auth(first_admin_token),
    )
    assert grants_response.status_code == 200, grants_response.text
    tenant_admin_grant = next(
        grant
        for grant in grants_response.json()
        if grant["role_key"] == "TENANT_ADMINISTRATOR"
        and grant["scope_type"] == "TENANT"
        and grant["subject_type"] == "USER"
    )
    deletion = client.delete(
        f"/v1/tenants/{first_tenant['id']}/authorization-grants/{tenant_admin_grant['id']}",
        headers=auth(first_admin_token),
    )
    assert deletion.status_code == 409
    assert "at least one active Tenant Administrator" in deletion.text

    membership = client.put(
        f"/v1/tenants/{first_tenant['id']}/access-groups/{empty_group['id']}/members",
        headers=auth(first_admin_token),
        json={"user_ids": [first["initial_admin"]["id"]], "row_version": empty_group["row_version"]},
    )
    assert membership.status_code == 200, membership.text
    deletion = client.delete(
        f"/v1/tenants/{first_tenant['id']}/authorization-grants/{tenant_admin_grant['id']}",
        headers=auth(first_admin_token),
    )
    assert deletion.status_code == 204, deletion.text
    remove_last_member = client.put(
        f"/v1/tenants/{first_tenant['id']}/access-groups/{empty_group['id']}/members",
        headers=auth(first_admin_token),
        json={"user_ids": [], "row_version": membership.json()["row_version"]},
    )
    assert remove_last_member.status_code == 409


def test_matter_grants_filter_discovery_and_separate_review_from_read_only(
    client: TestClient,
    root_token: str,
) -> None:
    created = create_tenant(client, root_token, slug="enforced-access")
    tenant = created["tenant"]
    admin_token = login(client, created["initial_admin"]["email"], "rbac-administrator-password")
    client_record, first_matter = create_matter(client, admin_token, tenant["id"])
    second_response = client.post(
        f"/v1/clients/{client_record['id']}/matters",
        headers=auth(admin_token),
        json={"name": "Hidden Matter"},
    )
    assert second_response.status_code == 201, second_response.text
    second_matter = second_response.json()

    roles = client.get(
        f"/v1/tenants/{tenant['id']}/roles",
        headers=auth(admin_token),
    ).json()
    roles_by_key = {role["key"]: role for role in roles}

    def create_user_with_role(email: str, role_key: str) -> tuple[dict, str]:
        user_response = client.post(
            f"/v1/tenants/{tenant['id']}/users",
            headers=auth(admin_token),
            json={
                "email": email,
                "display_name": email.split("@", 1)[0],
                "password": "long-enough-test-password",
            },
        )
        assert user_response.status_code == 201, user_response.text
        user = user_response.json()
        grant_response = client.post(
            f"/v1/tenants/{tenant['id']}/authorization-grants",
            headers=auth(admin_token),
            json={
                "subject": {"type": "USER", "id": user["id"]},
                "role_id": roles_by_key[role_key]["id"],
                "scope": {"type": "MATTER", "id": first_matter["id"]},
            },
        )
        assert grant_response.status_code == 201, grant_response.text
        return user, login(client, email, "long-enough-test-password")

    reviewer, reviewer_token = create_user_with_role("matter-reviewer@example.com", "REVIEWER")
    reader, read_only_token = create_user_with_role("matter-reader@example.com", "READ_ONLY")

    visible_clients = client.get(
        f"/v1/tenants/{tenant['id']}/clients",
        headers=auth(reviewer_token),
    )
    assert visible_clients.status_code == 200, visible_clients.text
    assert [row["id"] for row in visible_clients.json()] == [client_record["id"]]

    visible_matters = client.get(
        f"/v1/clients/{client_record['id']}/matters",
        headers=auth(reviewer_token),
    )
    assert visible_matters.status_code == 200, visible_matters.text
    assert [row["id"] for row in visible_matters.json()] == [first_matter["id"]]
    assert client.get(
        f"/v1/matters/{second_matter['id']}",
        headers=auth(reviewer_token),
    ).status_code == 404
    assert client.get(
        f"/v1/clients/{client_record['id']}/custodians",
        headers=auth(reviewer_token),
    ).status_code == 403
    assert client.post(
        f"/v1/clients/{client_record['id']}/custodians",
        headers=auth(reviewer_token),
        json={"display_name": "Unauthorized custodian", "email_addresses": []},
    ).status_code == 403

    definitions = client.get(
        f"/v1/matters/{first_matter['id']}/metadata-definitions",
        headers=auth(reviewer_token),
    )
    assert definitions.status_code == 200, definitions.text
    responsiveness = next(row for row in definitions.json() if row["key"] == "responsiveness")
    forbidden_definition = client.post(
        f"/v1/matters/{first_matter['id']}/metadata-definitions",
        headers=auth(reviewer_token),
        json={"key": "reviewer_field", "display_name": "Reviewer field", "type": "TEXT"},
    )
    assert forbidden_definition.status_code == 403

    with TestingSessionLocal() as db:
        job = MatterDocumentImportJob(
            matter_id=uuid.UUID(first_matter["id"]),
            source_collection_id=uuid.uuid4(),
            selection_type="EXPLICIT",
            selection={},
            selection_summary="RBAC test document",
            status="COMPLETED",
            workflow_id=f"rbac-test:{uuid.uuid4()}",
            created_by_user_id=uuid.UUID(reviewer["id"]),
        )
        db.add(job)
        db.flush()
        document = MatterDocument(
            matter_id=uuid.UUID(first_matter["id"]),
            source_collection_id=job.source_collection_id,
            collection_item_id=uuid.uuid4(),
            added_by_import_job_id=job.id,
        )
        db.add(document)
        own_batch = ReviewBatch(
            matter_id=uuid.UUID(first_matter["id"]),
            name="Assigned to reviewer",
            description=None,
            selection_type="ALL_MATTER",
            selection_definition={"type": "ALL_MATTER"},
            sample_size=None,
            random_seed=None,
            assigned_user_id=uuid.UUID(reviewer["id"]),
            reviewer_value_visibility="OWN_VALUES",
            status="READY",
            search_status="NOT_CONFIGURED",
            workflow_id=f"rbac-batch:{uuid.uuid4()}",
            document_count=0,
            created_by_user_id=uuid.UUID(reviewer["id"]),
        )
        other_batch = ReviewBatch(
            matter_id=uuid.UUID(first_matter["id"]),
            name="Assigned to another reviewer",
            description=None,
            selection_type="ALL_MATTER",
            selection_definition={"type": "ALL_MATTER"},
            sample_size=None,
            random_seed=None,
            assigned_user_id=uuid.UUID(reader["id"]),
            reviewer_value_visibility="OWN_VALUES",
            status="READY",
            search_status="NOT_CONFIGURED",
            workflow_id=f"rbac-batch:{uuid.uuid4()}",
            document_count=0,
            created_by_user_id=uuid.UUID(reviewer["id"]),
        )
        db.add_all([own_batch, other_batch])
        db.commit()
        document_id = str(document.id)
        other_batch_id = str(other_batch.id)

    reviewer_batches = client.get(
        f"/v1/matters/{first_matter['id']}/review-batches",
        headers=auth(reviewer_token),
    )
    assert reviewer_batches.status_code == 200, reviewer_batches.text
    assert [batch["name"] for batch in reviewer_batches.json()] == ["Assigned to reviewer"]
    assert client.get(
        f"/v1/matters/{first_matter['id']}/review-batches/{other_batch_id}",
        headers=auth(reviewer_token),
    ).status_code == 403

    event_path = (
        f"/v1/matters/{first_matter['id']}/documents/{document_id}"
        f"/metadata-values/{responsiveness['id']}/events"
    )
    reviewer_write = client.post(
        event_path,
        headers=auth(reviewer_token),
        json={"operation": "SET", "value": "responsive"},
    )
    assert reviewer_write.status_code == 201, reviewer_write.text
    assert client.get(event_path, headers=auth(read_only_token)).status_code == 200
    assert client.post(
        event_path,
        headers=auth(read_only_token),
        json={"operation": "SET", "value": "not_responsive"},
    ).status_code == 403
