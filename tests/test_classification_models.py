import uuid

from conftest import create_client_tenant
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditRecord, ClassificationModelConfiguration


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_matter(client: TestClient, token: str, tenant_id: str, suffix: str) -> dict:
    client_record = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(token),
        json={"name": f"Classification Client {suffix}"},
    )
    assert client_record.status_code == 201, client_record.text
    matter = client.post(
        f"/v1/clients/{client_record.json()['id']}/matters",
        headers=auth(token),
        json={"name": f"Classification Matter {suffix}"},
    )
    assert matter.status_code == 201, matter.text
    return matter.json()


def create_enum_field(
    client: TestClient,
    token: str,
    matter_id: str,
    *,
    key: str = "classification_target",
    reviewable: bool = True,
) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/metadata-definitions",
        headers=auth(token),
        json={
            "key": key,
            "display_name": "Classification Target",
            "type": "ENUM",
            "cardinality": "SINGLE",
            "reviewable": reviewable,
            "allowed_values": [
                {"key": "responsive", "label": "Responsive"},
                {"key": "not_responsive", "label": "Not responsive"},
                {"key": "needs_review", "label": "Needs review"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_model(client: TestClient, token: str, matter_id: str, definition_id: str) -> dict:
    response = client.post(
        f"/v1/matters/{matter_id}/classification-models",
        headers=auth(token),
        json={
            "target_metadata_definition_id": definition_id,
            "name": "Responsiveness classifier",
            "description": "Initial statistical responsiveness model",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def binary_configuration(*, threshold: float = 0.5) -> dict:
    return {
        "classification_strategy": "BINARY",
        "ordered_class_keys": ["negative", "positive"],
        "binary_mapping": {
            "negative_class_key": "negative",
            "positive_class_key": "positive",
            "negative_value_keys": ["not_responsive"],
            "positive_value_keys": ["responsive", "needs_review"],
        },
        "label_policy": {
            "allowed_sources": [
                {
                    "kind": "ADJUDICATED_REVIEW",
                    "authority": "GOLD",
                    "source_weight": 1.0,
                }
            ],
            "missing_label_action": "EXCLUDE",
            "source_conflict_action": "REQUIRE_ADJUDICATION",
            "target_value_conflict_action": "EXCLUDE",
        },
        "feature_contract": {
            "dimensions": 1024,
            "embedding_model": "voyageai/voyage-4-nano",
            "embedding_model_revision": "revision-1",
            "text_source_policy": "active_text",
        },
        "prediction_resolution_policy": {"binary_positive_threshold": threshold},
        "trainer_specification": {
            "kind": "sklearn_logistic_regression",
            "parameters": {
                "regularization": "l2",
                "c_values": [0.1, 1.0, 10.0],
                "maximum_iterations": 1000,
            },
        },
    }


def test_create_revise_list_and_read_classification_model(
    client: TestClient,
    root_token: str,
    root_admin,
    db: Session,
) -> None:
    tenant_id = create_client_tenant(client, root_token)
    matter = create_matter(client, root_token, tenant_id, "Registry")
    definition = create_enum_field(client, root_token, matter["id"])
    model = create_model(client, root_token, matter["id"], definition["id"])

    assert model["tenant_id"] == tenant_id
    assert model["matter_id"] == matter["id"]
    assert model["target_metadata_definition_id"] == definition["id"]
    assert model["target_field"]["stable_key"] == "classification_target"
    assert model["current_configuration"] is None

    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}"
    first = client.post(
        f"{base}/configurations",
        headers=auth(root_token),
        json=binary_configuration(threshold=0.5),
    )
    assert first.status_code == 201, first.text
    assert first.json()["revision"] == 1
    assert first.json()["target_field_snapshot"]["display_name"] == "Classification Target"
    first_hash = first.json()["configuration_hash"]
    first_fingerprint = first.json()["target_field_snapshot"]["schema_fingerprint"]

    renamed = client.patch(
        f"/v1/matters/{matter['id']}/metadata-definitions/{definition['id']}",
        headers=auth(root_token),
        json={"display_name": "Responsive Status"},
    )
    assert renamed.status_code == 200, renamed.text

    second = client.post(
        f"{base}/configurations",
        headers=auth(root_token),
        json=binary_configuration(threshold=0.65),
    )
    assert second.status_code == 201, second.text
    assert second.json()["revision"] == 2
    assert second.json()["configuration_hash"] != first_hash
    assert second.json()["target_field_snapshot"]["display_name"] == "Responsive Status"
    assert second.json()["target_field_snapshot"]["schema_fingerprint"] == first_fingerprint

    duplicate = client.post(
        f"{base}/configurations",
        headers=auth(root_token),
        json=binary_configuration(threshold=0.65),
    )
    assert duplicate.status_code == 409

    configurations = client.get(f"{base}/configurations", headers=auth(root_token))
    assert configurations.status_code == 200
    assert [item["revision"] for item in configurations.json()] == [1, 2]
    assert configurations.json()[0]["target_field_snapshot"]["display_name"] == "Classification Target"

    retrieved = client.get(base, headers=auth(root_token))
    assert retrieved.status_code == 200
    assert retrieved.json()["target_field"]["display_name"] == "Responsive Status"
    assert retrieved.json()["current_configuration_id"] == second.json()["id"]
    assert retrieved.json()["current_configuration"]["revision"] == 2
    assert retrieved.json()["row_version"] == 3

    listed = client.get(
        f"/v1/matters/{matter['id']}/classification-models",
        headers=auth(root_token),
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [model["id"]]

    db.expire_all()
    stored_configurations = db.scalars(
        select(ClassificationModelConfiguration)
        .where(ClassificationModelConfiguration.classification_model_id == uuid.UUID(model["id"]))
        .order_by(ClassificationModelConfiguration.revision)
    ).all()
    assert stored_configurations[0].target_field_snapshot["display_name"] == "Classification Target"
    actions = set(
        db.scalars(
            select(AuditRecord.action).where(
                AuditRecord.target_id.in_(
                    [uuid.UUID(model["id"]), *(configuration.id for configuration in stored_configurations)]
                )
            )
        )
    )
    assert actions == {
        "classification_model.created",
        "classification_model.configuration_created",
    }


def test_hierarchy_level_requires_a_delimited_text_target(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter = create_matter(client, root_token, create_client_tenant(client, root_token), "Hierarchy validation")
    definition = create_enum_field(client, root_token, matter["id"])
    model = create_model(client, root_token, matter["id"], definition["id"])
    payload = binary_configuration()
    payload["hierarchy_level"] = 1

    response = client.post(
        f"/v1/matters/{matter['id']}/classification-models/{model['id']}/configurations",
        headers=auth(root_token),
        json=payload,
    )

    assert response.status_code == 409, response.text
    assert "Hierarchy level requires a target field hierarchy separator" in response.text


def test_registry_accepts_asserted_text_and_non_reviewable_fields_but_rejects_wrong_scope_source_and_type(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    tenant_id = create_client_tenant(client, root_token)
    first_matter = create_matter(client, root_token, tenant_id, "First Scope")
    second_matter = create_matter(client, root_token, tenant_id, "Second Scope")
    first_field = create_enum_field(client, root_token, first_matter["id"])

    wrong_matter = client.post(
        f"/v1/matters/{second_matter['id']}/classification-models",
        headers=auth(root_token),
        json={
            "target_metadata_definition_id": first_field["id"],
            "name": "Wrong matter model",
        },
    )
    assert wrong_matter.status_code == 409

    definitions = client.get(
        f"/v1/matters/{second_matter['id']}/metadata-definitions",
        headers=auth(root_token),
    )
    assert definitions.status_code == 200, definitions.text
    imported_subject = next(item for item in definitions.json() if item["key"] == "email_subject")
    imported_model = client.post(
        f"/v1/matters/{second_matter['id']}/classification-models",
        headers=auth(root_token),
        json={
            "target_metadata_definition_id": imported_subject["id"],
            "name": "Imported subject model",
        },
    )
    assert imported_model.status_code == 409

    long_text_field = client.post(
        f"/v1/matters/{second_matter['id']}/metadata-definitions",
        headers=auth(root_token),
        json={
            "key": "notes_target",
            "display_name": "Notes",
            "type": "LONG_TEXT",
            "reviewable": True,
        },
    )
    assert long_text_field.status_code == 201, long_text_field.text
    text_model = client.post(
        f"/v1/matters/{second_matter['id']}/classification-models",
        headers=auth(root_token),
        json={"target_metadata_definition_id": long_text_field.json()["id"], "name": "Text model"},
    )
    assert text_model.status_code == 201, text_model.text

    non_reviewable = create_enum_field(
        client,
        root_token,
        second_matter["id"],
        key="non_reviewable_target",
        reviewable=False,
    )
    hidden_model = client.post(
        f"/v1/matters/{second_matter['id']}/classification-models",
        headers=auth(root_token),
        json={"target_metadata_definition_id": non_reviewable["id"], "name": "Hidden model"},
    )
    assert hidden_model.status_code == 201, hidden_model.text

    integer_field = client.post(
        f"/v1/matters/{second_matter['id']}/metadata-definitions",
        headers=auth(root_token),
        json={
            "key": "integer_target",
            "display_name": "Integer target",
            "type": "INTEGER",
            "reviewable": True,
        },
    )
    assert integer_field.status_code == 201, integer_field.text
    unsupported = client.post(
        f"/v1/matters/{second_matter['id']}/classification-models",
        headers=auth(root_token),
        json={
            "target_metadata_definition_id": integer_field.json()["id"],
            "name": "Integer model",
        },
    )
    assert unsupported.status_code == 409


def test_class_catalog_changes_remain_compatible_and_archive_blocks_revision(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter = create_matter(client, root_token, create_client_tenant(client, root_token), "Compatibility")
    definition = create_enum_field(client, root_token, matter["id"])
    model = create_model(client, root_token, matter["id"], definition["id"])
    base = f"/v1/matters/{matter['id']}/classification-models/{model['id']}"
    created = client.post(
        f"{base}/configurations",
        headers=auth(root_token),
        json=binary_configuration(),
    )
    assert created.status_code == 201, created.text

    compatible = client.get(f"{base}/compatibility", headers=auth(root_token))
    assert compatible.status_code == 200
    assert compatible.json()["compatible"] is True
    assert compatible.json()["schema_fingerprint_matches"] is True

    deactivated = client.post(
        f"/v1/matters/{matter['id']}/metadata-definitions/{definition['id']}"
        "/enum-values/needs_review/deactivate",
        headers=auth(root_token),
    )
    assert deactivated.status_code == 200, deactivated.text
    still_compatible = client.get(f"{base}/compatibility", headers=auth(root_token))
    assert still_compatible.status_code == 200
    assert still_compatible.json()["compatible"] is True
    assert still_compatible.json()["schema_fingerprint_matches"] is True
    assert still_compatible.json()["reasons"] == []

    archived = client.post(f"{base}/archive", headers=auth(root_token))
    assert archived.status_code == 200
    assert archived.json()["status"] == "ARCHIVED"
    revision = client.post(
        f"{base}/configurations",
        headers=auth(root_token),
        json=binary_configuration(threshold=0.7),
    )
    assert revision.status_code == 409


def test_classification_registry_requires_matter_admin(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter = create_matter(client, root_token, create_client_tenant(client, root_token), "Authorization")
    definition = create_enum_field(client, root_token, matter["id"])

    tenant = client.post(
        "/v1/tenants",
        headers=auth(root_token),
        json={
            "parent_tenant_id": str(root_admin.tenant_id),
            "slug": "classification-outsider",
            "name": "Classification Outsider",
            "initial_admin": {
                "email": "classification-outsider@example.com",
                "display_name": "Classification Outsider",
                "password": "classification-outsider-password",
            },
        },
    )
    assert tenant.status_code == 201, tenant.text
    login = client.post(
        "/v1/auth/login",
        json={
            "email": "classification-outsider@example.com",
            "password": "classification-outsider-password",
        },
    )
    assert login.status_code == 200, login.text

    response = client.post(
        f"/v1/matters/{matter['id']}/classification-models",
        headers=auth(login.json()["access_token"]),
        json={
            "target_metadata_definition_id": definition["id"],
            "name": "Unauthorized model",
        },
    )
    assert response.status_code == 403
