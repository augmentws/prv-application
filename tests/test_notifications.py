import uuid

import pytest
from conftest import TestingSessionLocal, create_client_tenant
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.authorization import ResourceRef
from app.models import (
    Notification,
    NotificationRecipient,
    NotificationRecipientAudience,
    User,
)
from app.notifications import (
    NotificationError,
    NotificationSource,
    PermissionAudience,
    RoleAudience,
    UserAudience,
    publish_notification,
    resolve_source_notifications,
)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(client: TestClient, email: str, password: str) -> str:
    response = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def notification_workspace(client: TestClient, root_token: str) -> tuple[dict, dict, dict, str]:
    parent_tenant_id = create_client_tenant(client, root_token)
    slug = f"notifications-{uuid.uuid4().hex[:10]}"
    password = "notification-admin-password"
    tenant_response = client.post(
        "/v1/tenants",
        headers=auth(root_token),
        json={
            "parent_tenant_id": parent_tenant_id,
            "slug": slug,
            "name": "Notification Tenant",
            "initial_admin": {
                "email": f"{slug}@example.com",
                "display_name": "Notification Admin",
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
        json={"name": "Notification Client"},
    )
    assert client_response.status_code == 201, client_response.text
    matter_response = client.post(
        f"/v1/clients/{client_response.json()['id']}/matters",
        headers=auth(token),
        json={"name": "Notification Matter"},
    )
    assert matter_response.status_code == 201, matter_response.text
    return created, client_response.json(), matter_response.json(), token


def publish_job_notification(*, user_id: uuid.UUID, client_id: uuid.UUID, matter_id: uuid.UUID) -> uuid.UUID:
    with TestingSessionLocal() as db:
        notification = publish_notification(
            db,
            type_key="job.completed",
            scope=ResourceRef.matter(matter_id),
            source=NotificationSource("workflow_run", uuid.uuid4()),
            audiences=(UserAudience(user_id),),
            template_data={"job_label": "Fact development"},
            route_params={"client_id": client_id, "matter_id": matter_id},
            idempotency_key=f"test:{uuid.uuid4()}",
            created_by_kind="WORKFLOW",
        )
        db.commit()
        return notification.id


def test_notification_inbox_read_and_dismiss(
    client: TestClient,
    root_token: str,
) -> None:
    created, client_record, matter, token = notification_workspace(client, root_token)
    notification_id = publish_job_notification(
        user_id=uuid.UUID(created["initial_admin"]["id"]),
        client_id=uuid.UUID(client_record["id"]),
        matter_id=uuid.UUID(matter["id"]),
    )

    counts = client.get("/v1/notifications/counts", headers=auth(token))
    assert counts.status_code == 200, counts.text
    assert counts.json() == {"unread": 1, "action_required": 0}

    inbox = client.get("/v1/notifications", headers=auth(token))
    assert inbox.status_code == 200, inbox.text
    assert len(inbox.json()["items"]) == 1
    item = inbox.json()["items"][0]
    assert item["id"] == str(notification_id)
    assert item["title"] == "Fact development completed"
    assert item["client_id"] == client_record["id"]
    assert item["client_name"] == "Notification Client"
    assert item["matter_name"] == "Notification Matter"
    assert item["read_at"] is None

    filtered = client.get(
        "/v1/notifications",
        headers=auth(token),
        params={"client_id": client_record["id"]},
    )
    assert filtered.status_code == 200, filtered.text
    assert [entry["id"] for entry in filtered.json()["items"]] == [str(notification_id)]

    read = client.post(f"/v1/notifications/{notification_id}/read", headers=auth(token))
    assert read.status_code == 200, read.text
    assert read.json() == {"updated": 1}
    assert client.get("/v1/notifications/counts", headers=auth(token)).json()["unread"] == 0

    dismissed = client.post(f"/v1/notifications/{notification_id}/dismiss", headers=auth(token))
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json() == {"updated": 1}
    assert client.get("/v1/notifications", headers=auth(token)).json()["items"] == []


def test_role_and_permission_audiences_are_deduplicated_and_explained(
    client: TestClient,
    root_token: str,
) -> None:
    created, client_record, matter, token = notification_workspace(client, root_token)
    roles_response = client.get(
        f"/v1/tenants/{created['tenant']['id']}/roles",
        headers=auth(token),
    )
    assert roles_response.status_code == 200, roles_response.text
    matter_admin_role = next(role for role in roles_response.json() if role["key"] == "MATTER_ADMINISTRATOR")

    user_response = client.post(
        f"/v1/tenants/{created['tenant']['id']}/users",
        headers=auth(token),
        json={
            "email": "notification-manager@example.com",
            "display_name": "Notification Manager",
            "password": "notification-manager-password",
        },
    )
    assert user_response.status_code == 201, user_response.text
    manager = user_response.json()
    grant_response = client.post(
        f"/v1/tenants/{created['tenant']['id']}/authorization-grants",
        headers=auth(token),
        json={
            "subject": {"type": "USER", "id": manager["id"]},
            "role_id": matter_admin_role["id"],
            "scope": {"type": "MATTER", "id": matter["id"]},
        },
    )
    assert grant_response.status_code == 201, grant_response.text

    with TestingSessionLocal() as db:
        notification = publish_notification(
            db,
            type_key="agent_action.requested",
            scope=ResourceRef.matter(uuid.UUID(matter["id"])),
            source=NotificationSource("agent_action_request", uuid.uuid4()),
            audiences=(
                RoleAudience(uuid.UUID(matter_admin_role["id"])),
                PermissionAudience("matter.configure"),
            ),
            template_data={},
            route_params={
                "client_id": client_record["id"],
                "matter_id": matter["id"],
                "conversation_id": str(uuid.uuid4()),
                "workflow_type": "MATTER_DEFINITION_SETUP",
                "review_batch_id": None,
            },
            idempotency_key=f"test-action:{uuid.uuid4()}",
            created_by_kind="WORKFLOW",
        )
        db.commit()
        manager_user = db.scalar(select(User).where(User.id == uuid.UUID(manager["id"])))
        assert manager_user is not None
        recipient = db.scalar(
            select(NotificationRecipient).where(
                NotificationRecipient.notification_id == notification.id,
                NotificationRecipient.user_id == manager_user.id,
            )
        )
        assert recipient is not None
        assert (
            db.scalar(
                select(func.count())
                .select_from(NotificationRecipientAudience)
                .where(NotificationRecipientAudience.recipient_id == recipient.id)
            )
            == 2
        )


def test_notification_idempotency_and_source_resolution(
    client: TestClient,
    root_token: str,
) -> None:
    created, client_record, matter, _ = notification_workspace(client, root_token)
    user_id = uuid.UUID(created["initial_admin"]["id"])
    source_id = uuid.uuid4()
    idempotency_key = f"stable:{uuid.uuid4()}"
    with TestingSessionLocal() as db:
        arguments = {
            "type_key": "agent_action.requested",
            "scope": ResourceRef.matter(uuid.UUID(matter["id"])),
            "source": NotificationSource("agent_action_request", source_id),
            "audiences": (UserAudience(user_id),),
            "template_data": {},
            "route_params": {
                "client_id": client_record["id"],
                "matter_id": matter["id"],
                "conversation_id": str(uuid.uuid4()),
                "workflow_type": "MATTER_DEFINITION_SETUP",
                "review_batch_id": None,
            },
            "idempotency_key": idempotency_key,
        }
        first = publish_notification(db, **arguments)
        second = publish_notification(db, **arguments)
        assert first.id == second.id
        resolved = resolve_source_notifications(
            db,
            source=NotificationSource("agent_action_request", source_id),
            resolution_code="APPROVED",
        )
        assert [item.id for item in resolved] == [first.id]
        db.commit()

    with TestingSessionLocal() as db:
        stored = db.get(Notification, first.id)
        assert stored is not None
        assert stored.resolution_code == "APPROVED"
        assert stored.resolved_at is not None


def test_notification_type_rejects_wrong_scope_and_route(
    client: TestClient,
    root_token: str,
) -> None:
    created, client_record, matter, _ = notification_workspace(client, root_token)
    arguments = {
        "type_key": "job.completed",
        "source": NotificationSource("workflow_run", uuid.uuid4()),
        "audiences": (UserAudience(uuid.UUID(created["initial_admin"]["id"])),),
        "template_data": {"job_label": "Fact development"},
        "route_params": {"client_id": client_record["id"], "matter_id": matter["id"]},
        "idempotency_key": f"scope-validation:{uuid.uuid4()}",
    }
    with TestingSessionLocal() as db:
        with pytest.raises(NotificationError, match="scope type"):
            publish_notification(
                db,
                scope=ResourceRef.client(uuid.UUID(client_record["id"])),
                **arguments,
            )
        with pytest.raises(NotificationError, match="route does not match"):
            publish_notification(
                db,
                scope=ResourceRef.matter(uuid.UUID(matter["id"])),
                **{
                    **arguments,
                    "route_params": {
                        "client_id": client_record["id"],
                        "matter_id": str(uuid.uuid4()),
                    },
                },
            )
