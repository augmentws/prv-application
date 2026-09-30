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
        "cached_input_tokens": 700,
        "cache_write_tokens": 80,
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
    assert item["cached_input_tokens"] == 700
    assert item["cache_write_tokens"] == 80
    assert item["output_tokens"] == 345
    assert item["model_invocation_id"] is None
    assert item["total_tokens"] == 1545
    assert db.query(ExternalProviderUsage).count() == 1


def test_matter_provider_usage_reports_complete_totals_and_breakdowns(
    client: TestClient,
    db: Session,
    root_admin: User,
    root_token: str,
) -> None:
    headers = {"Authorization": f"Bearer {root_token}"}
    created_client = client.post(
        f"/v1/tenants/{root_admin.tenant_id}/clients",
        headers=headers,
        json={"name": "Usage client"},
    )
    assert created_client.status_code == 201, created_client.text
    client_id = uuid.UUID(created_client.json()["id"])
    created_matter = client.post(
        f"/v1/clients/{client_id}/matters",
        headers=headers,
        json={"name": "Usage matter"},
    )
    assert created_matter.status_code == 201, created_matter.text
    matter_id = uuid.UUID(created_matter.json()["id"])

    for index, values in enumerate(
        [
            ("google", "gemini-flash", "MATTER_EMBEDDING", 2, 1_000, 600, 50, 200),
            ("openai", "gpt-test", "MATTER_ANALYSIS_TASK_BATCH", 3, 2_000, 250, 80, 500),
        ]
    ):
        provider, model, job_type, requests, input_tokens, cached, cache_write, output = values
        record_external_provider_usage(
            db,
            idempotency_key=f"matter-usage:{matter_id}:{index}",
            tenant_id=root_admin.tenant_id,
            client_id=client_id,
            matter_id=matter_id,
            started_by_user_id=root_admin.id,
            job_type=job_type,
            job_id=uuid.uuid4(),
            job_created_at=root_admin.created_at,
            provider=provider,
            model=model,
            request_count=requests,
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            cache_write_tokens=cache_write,
            output_tokens=output,
        )
    db.commit()

    response = client.get(
        f"/v1/matters/{matter_id}/provider-usage",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    report = response.json()
    assert report["matter_id"] == str(matter_id)
    assert report["totals"] == {
        "record_count": 2,
        "request_count": 5,
        "input_tokens": 3_000,
        "cached_input_tokens": 850,
        "cache_write_tokens": 130,
        "output_tokens": 700,
        "total_tokens": 3_700,
    }
    assert [item["model"] for item in report["by_model"]] == ["gpt-test", "gemini-flash"]
    assert [item["job_type"] for item in report["by_job_type"]] == [
        "MATTER_ANALYSIS_TASK_BATCH",
        "MATTER_EMBEDDING",
    ]
    assert len(report["entries"]) == 2
    assert {item["matter_id"] for item in report["entries"]} == {str(matter_id)}
