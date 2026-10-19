import uuid

from conftest import create_client_tenant
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.email_domains import extract_email_domains
from app.models import MetadataDefinition, MetadataGroupField, SearchProjectionOperation


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_extract_email_domains_normalizes_and_deduplicates_participants() -> None:
    assert extract_email_domains(
        'Sender Name <Sender@Example.COM>',
        {
            "TO": ["first@example.com", "Second <second@Sub.Example.COM>"],
            "CC": ["one@EXAMPLE.com; two@xn--bcher-kva.example"],
            "BCC": ["missing-at-sign", "unicode@bücher.example"],
        },
    ) == ["example.com", "sub.example.com", "xn--bcher-kva.example"]


def test_extract_email_domains_returns_empty_for_non_email_documents() -> None:
    assert extract_email_domains(None, {"TO": [], "CC": [], "BCC": []}) == []


def test_update_installs_missing_email_domain_field_for_existing_matter(
    client: TestClient,
    root_token: str,
    db: Session,
) -> None:
    tenant_id = create_client_tenant(client, root_token)
    client_response = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(root_token),
        json={"name": "Email Domain Upgrade Client"},
    )
    client_id = client_response.json()["id"]
    matter_response = client.post(
        f"/v1/clients/{client_id}/matters",
        headers=auth(root_token),
        json={"name": "Legacy Matter"},
    )
    matter_id = uuid.UUID(matter_response.json()["id"])

    definition = db.scalar(
        select(MetadataDefinition).where(
            MetadataDefinition.matter_id == matter_id,
            MetadataDefinition.key == "email_domain",
        )
    )
    assert definition is not None
    db.execute(
        delete(MetadataGroupField).where(
            MetadataGroupField.metadata_definition_id == definition.id
        )
    )
    db.delete(definition)
    db.commit()

    response = client.post(
        f"/v1/matters/{matter_id}/metadata-definitions/email-domain/update",
        headers=auth(root_token),
    )
    assert response.status_code == 202, response.text
    assert response.json()["definition_created"] is True

    db.expire_all()
    installed = db.scalar(
        select(MetadataDefinition).where(
            MetadataDefinition.matter_id == matter_id,
            MetadataDefinition.key == "email_domain",
        )
    )
    operation = db.get(SearchProjectionOperation, uuid.UUID(response.json()["search_operation_id"]))
    assert installed is not None
    assert installed.value_source == "SYSTEM"
    assert installed.cardinality == "MULTIPLE"
    assert operation is not None
    assert operation.payload["metadata_backfill_fields"] == ["email_domain"]
