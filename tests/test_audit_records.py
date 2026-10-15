import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import AuditRecord, PasswordCredential, Tenant, User
from app.schemas import normalize_email
from app.security import hash_password


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_audit_records_default_to_latest_fifty_with_stable_cursor(
    client: TestClient,
    db: Session,
    root_admin: User,
    root_token: str,
) -> None:
    created_at = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    records = [
        AuditRecord(
            id=uuid.UUID(int=index),
            tenant_id=root_admin.tenant_id,
            actor_user_id=root_admin.id,
            action="test.cursor",
            target_type="test",
            target_id=uuid.uuid4(),
            details={"ordinal": index},
            created_at=created_at,
        )
        for index in range(1, 56)
    ]
    db.add_all(records)
    db.commit()

    first = client.get(
        f"/v1/tenants/{root_admin.tenant_id}/audit-records",
        params={"action": "test.cursor"},
        headers=auth(root_token),
    )
    assert first.status_code == 200, first.text
    first_page = first.json()
    assert len(first_page["items"]) == 50
    assert [item["details"]["ordinal"] for item in first_page["items"]] == list(range(55, 5, -1))
    assert first_page["next_cursor"] is not None

    second = client.get(
        f"/v1/tenants/{root_admin.tenant_id}/audit-records",
        params={"action": "test.cursor", "cursor": first_page["next_cursor"]},
        headers=auth(root_token),
    )
    assert second.status_code == 200, second.text
    second_page = second.json()
    assert [item["details"]["ordinal"] for item in second_page["items"]] == [5, 4, 3, 2, 1]
    assert second_page["next_cursor"] is None


def test_audit_record_filters_and_actor_display(
    client: TestClient,
    db: Session,
    root_admin: User,
    root_token: str,
) -> None:
    actor = User(
        tenant_id=root_admin.tenant_id,
        email="auditor@example.com",
        normalized_email=normalize_email("auditor@example.com"),
        display_name="Audit Actor",
        status="ACTIVE",
        tenant_role="ADMIN",
    )
    db.add(actor)
    db.flush()
    base = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    db.add_all(
        [
            AuditRecord(
                tenant_id=root_admin.tenant_id,
                actor_user_id=actor.id,
                action="matter.created",
                target_type="matter",
                target_id=uuid.uuid4(),
                created_at=base,
            ),
            AuditRecord(
                tenant_id=root_admin.tenant_id,
                actor_user_id=actor.id,
                action="matter.updated",
                target_type="matter",
                target_id=uuid.uuid4(),
                created_at=base + timedelta(days=1),
            ),
            AuditRecord(
                tenant_id=root_admin.tenant_id,
                actor_user_id=None,
                action="workflow.completed",
                target_type="workflow",
                target_id=uuid.uuid4(),
                created_at=base + timedelta(days=1, hours=1),
            ),
        ]
    )
    db.commit()

    by_actor_and_prefix = client.get(
        f"/v1/tenants/{root_admin.tenant_id}/audit-records",
        params={"actor_user_id": str(actor.id), "action_prefix": "matter."},
        headers=auth(root_token),
    )
    assert by_actor_and_prefix.status_code == 200, by_actor_and_prefix.text
    items = by_actor_and_prefix.json()["items"]
    assert [item["action"] for item in items] == ["matter.updated", "matter.created"]
    assert {item["actor_display_name"] for item in items} == {"Audit Actor"}
    assert {item["actor_email"] for item in items} == {"auditor@example.com"}

    by_date = client.get(
        f"/v1/tenants/{root_admin.tenant_id}/audit-records",
        params={
            "created_from": (base + timedelta(hours=12)).isoformat(),
            "created_to": (base + timedelta(days=2)).isoformat(),
            "action_prefix": "matter.",
        },
        headers=auth(root_token),
    )
    assert by_date.status_code == 200, by_date.text
    assert [item["action"] for item in by_date.json()["items"]] == ["matter.updated"]

    system = client.get(
        f"/v1/tenants/{root_admin.tenant_id}/audit-records",
        params={"system_actor": True},
        headers=auth(root_token),
    )
    assert system.status_code == 200, system.text
    system_items = system.json()["items"]
    assert [item["action"] for item in system_items] == ["workflow.completed"]
    assert system_items[0]["actor_user_id"] is None
    assert system_items[0]["actor_display_name"] is None


def test_audit_records_are_tenant_isolated_and_admin_only(
    client: TestClient,
    db: Session,
    root_admin: User,
    root_token: str,
) -> None:
    child = Tenant(
        parent_tenant_id=root_admin.tenant_id,
        slug="audit-child",
        name="Audit Child",
        status="ACTIVE",
        is_root=False,
    )
    db.add(child)
    db.flush()
    child_admin = User(
        tenant_id=child.id,
        email="audit-child@example.com",
        normalized_email=normalize_email("audit-child@example.com"),
        display_name="Audit Child Admin",
        status="ACTIVE",
        tenant_role="ADMIN",
    )
    db.add(child_admin)
    db.flush()
    db.add(PasswordCredential(user_id=child_admin.id, password_hash=hash_password("audit-child-password")))
    child_record = AuditRecord(
        tenant_id=child.id,
        actor_user_id=child_admin.id,
        action="child.action",
        target_type="tenant",
        target_id=child.id,
    )
    root_record = AuditRecord(
        tenant_id=root_admin.tenant_id,
        actor_user_id=root_admin.id,
        action="root.action",
        target_type="tenant",
        target_id=root_admin.tenant_id,
    )
    db.add_all([child_record, root_record])
    db.commit()

    login = client.post(
        "/v1/auth/login",
        json={"email": child_admin.email, "password": "audit-child-password"},
    )
    assert login.status_code == 200, login.text
    child_token = login.json()["access_token"]

    own = client.get(
        f"/v1/tenants/{child.id}/audit-records",
        params={"action_prefix": "child."},
        headers=auth(child_token),
    )
    assert own.status_code == 200, own.text
    assert [item["action"] for item in own.json()["items"]] == ["child.action"]

    denied = client.get(
        f"/v1/tenants/{root_admin.tenant_id}/audit-records",
        headers=auth(child_token),
    )
    assert denied.status_code == 403

    root_can_select_child = client.get(
        f"/v1/tenants/{child.id}/audit-records",
        params={"action_prefix": "child."},
        headers=auth(root_token),
    )
    assert root_can_select_child.status_code == 200, root_can_select_child.text
    assert [item["action"] for item in root_can_select_child.json()["items"]] == ["child.action"]


def test_audit_record_query_rejects_conflicting_filters_and_invalid_cursor(
    client: TestClient,
    root_admin: User,
    root_token: str,
) -> None:
    path = f"/v1/tenants/{root_admin.tenant_id}/audit-records"
    conflicting_action = client.get(
        path,
        params={"action": "matter.created", "action_prefix": "matter."},
        headers=auth(root_token),
    )
    assert conflicting_action.status_code == 400

    conflicting_actor = client.get(
        path,
        params={"actor_user_id": str(root_admin.id), "system_actor": True},
        headers=auth(root_token),
    )
    assert conflicting_actor.status_code == 400

    invalid_cursor = client.get(path, params={"cursor": "not-a-cursor"}, headers=auth(root_token))
    assert invalid_cursor.status_code == 400
