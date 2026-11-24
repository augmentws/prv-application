from fastapi.testclient import TestClient

from tests.test_agents_and_matter_definitions import create_tenant_context


def _create_entity(
    client: TestClient,
    *,
    matter_id: str,
    token: str,
    name: str,
    entity_type: str = "ORGANIZATION",
    organization_category: str | None = None,
    status: str = "CONFIRMED",
) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/entities",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "entity_type": entity_type,
            "organization_category": organization_category,
            "display_name": name,
            "status": status,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_outside_counsel_relationship_is_generic_audited_and_optional(
    client: TestClient,
    root_token: str,
) -> None:
    _tenant_id, token, matter_id = create_tenant_context(client, root_token)
    law_firm = _create_entity(
        client,
        matter_id=matter_id,
        token=token,
        name="Kirkland & Ellis",
        organization_category="LAW_FIRM",
    )
    client_organization = _create_entity(
        client,
        matter_id=matter_id,
        token=token,
        name="Enron",
        organization_category="CORPORATION",
    )

    created = client.post(
        f"/v1/matters/{matter_id}/entity-relationships",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "source_entity_id": law_firm["id"],
            "target_entity_id": client_organization["id"],
            "relationship_type": "OUTSIDE_COUNSEL_FOR",
        },
    )
    assert created.status_code == 201, created.text
    relationship = created.json()
    assert relationship["status"] == "CONFIRMED"
    assert relationship["forward_label"] == "Outside counsel for"
    assert relationship["inverse_label"] == "Represented by outside counsel"
    assert relationship["source"]["display_name"] == "Kirkland & Ellis"
    assert relationship["target"]["display_name"] == "Enron"

    listed = client.get(
        f"/v1/matters/{matter_id}/entity-relationships",
        headers={"Authorization": f"Bearer {token}"},
        params={"entity_id": client_organization["id"]},
    )
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [relationship["id"]]

    duplicate = client.post(
        f"/v1/matters/{matter_id}/entity-relationships",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "source_entity_id": law_firm["id"],
            "target_entity_id": client_organization["id"],
            "relationship_type": "OUTSIDE_COUNSEL_FOR",
        },
    )
    assert duplicate.status_code == 409

    surviving_law_firm = _create_entity(
        client,
        matter_id=matter_id,
        token=token,
        name="Kirkland and Ellis LLP",
        organization_category="LAW_FIRM",
    )
    merged = client.post(
        f"/v1/matters/{matter_id}/entities/{law_firm['id']}/merge",
        headers={"Authorization": f"Bearer {token}"},
        json={"lock_version": law_firm["lock_version"], "target_entity_id": surviving_law_firm["id"]},
    )
    assert merged.status_code == 200, merged.text
    moved = client.get(
        f"/v1/matters/{matter_id}/entity-relationships",
        headers={"Authorization": f"Bearer {token}"},
        params={"entity_id": surviving_law_firm["id"]},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()[0]["source"]["id"] == surviving_law_firm["id"]

    rejected_target = client.post(
        f"/v1/matters/{matter_id}/entities/{client_organization['id']}/reject",
        headers={"Authorization": f"Bearer {token}"},
        json={"lock_version": client_organization["lock_version"]},
    )
    assert rejected_target.status_code == 200, rejected_target.text
    rejected_relationships = client.get(
        f"/v1/matters/{matter_id}/entity-relationships",
        headers={"Authorization": f"Bearer {token}"},
        params={"entity_id": client_organization["id"], "status": "REJECTED"},
    )
    assert rejected_relationships.status_code == 200, rejected_relationships.text
    assert rejected_relationships.json()[0]["status"] == "REJECTED"

    definitions = client.get(
        f"/v1/matters/{matter_id}/entity-relationships/types",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert definitions.status_code == 200, definitions.text
    assert definitions.json() == [
        {
            "key": "OUTSIDE_COUNSEL_FOR",
            "forward_label": "Outside counsel for",
            "inverse_label": "Represented by outside counsel",
            "source_entity_types": ["ORGANIZATION"],
            "source_organization_categories": ["LAW_FIRM"],
            "target_entity_types": ["ORGANIZATION", "PERSON"],
        }
    ]


def test_outside_counsel_requires_confirmed_law_firm_source(
    client: TestClient,
    root_token: str,
) -> None:
    _tenant_id, token, matter_id = create_tenant_context(client, root_token)
    corporation = _create_entity(
        client,
        matter_id=matter_id,
        token=token,
        name="Ordinary Corporation",
        organization_category="CORPORATION",
    )
    person = _create_entity(
        client,
        matter_id=matter_id,
        token=token,
        name="Individual Client",
        entity_type="PERSON",
    )
    response = client.post(
        f"/v1/matters/{matter_id}/entity-relationships",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "source_entity_id": corporation["id"],
            "target_entity_id": person["id"],
            "relationship_type": "OUTSIDE_COUNSEL_FOR",
        },
    )
    assert response.status_code == 422
    assert "law-firm category" in response.json()["error"]["message"]

    proposed_law_firm = _create_entity(
        client,
        matter_id=matter_id,
        token=token,
        name="Unconfirmed Law Firm",
        organization_category="LAW_FIRM",
        status="PROPOSED",
    )
    response = client.post(
        f"/v1/matters/{matter_id}/entity-relationships",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "source_entity_id": proposed_law_firm["id"],
            "target_entity_id": person["id"],
            "relationship_type": "OUTSIDE_COUNSEL_FOR",
        },
    )
    assert response.status_code == 409
    assert "Confirm both entities" in response.json()["error"]["message"]
