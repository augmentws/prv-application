import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ExternalProviderUsage, User
from app.provider_usage import external_model_identity, record_external_provider_usage


def test_external_model_identity_parses_provider_model_ids() -> None:
    assert external_model_identity("google:gemini-3.7-flash") == ("google", "gemini-3.7-flash")
    assert external_model_identity("openai:gpt-5") == ("openai", "gpt-5")
    assert external_model_identity("configured-default") is None
    assert external_model_identity(object()) is None


def test_provider_usage_is_idempotent_and_visible_to_tenant_admin(
    client: TestClient,
    db: Session,
    root_admin: User,
    root_token: str,
) -> None:
    job_id = uuid.uuid4()
    kwargs = {
        "idempotency_key": f"agent-run:{job_id}:provider-usage",
        "tenant_id": root_admin.tenant_id,
        "client_id": None,
        "matter_id": None,
        "started_by_user_id": root_admin.id,
        "job_type": "AGENT_RUN",
        "job_id": job_id,
        "job_created_at": root_admin.created_at,
        "provider": "google",
        "model": "gemini-3.7-flash",
        "request_count": 2,
        "input_tokens": 1200,
        "output_tokens": 345,
        "details": {"workflow_id": "agent-run:test"},
    }
    first = record_external_provider_usage(db, **kwargs)
    second = record_external_provider_usage(db, **kwargs)
    assert first is second
    db.commit()

    response = client.get(
        f"/v1/tenants/{root_admin.tenant_id}/provider-usage",
        params={"provider": "google", "job_type": "AGENT_RUN"},
        headers={"Authorization": f"Bearer {root_token}"},
    )

    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
    item = response.json()[0]
    assert item["job_id"] == str(job_id)
    assert item["started_by_user_id"] == str(root_admin.id)
    assert item["started_by_email"] == root_admin.email
    assert item["provider"] == "google"
    assert item["input_tokens"] == 1200
    assert item["output_tokens"] == 345
    assert item["total_tokens"] == 1545
    assert db.query(ExternalProviderUsage).count() == 1
