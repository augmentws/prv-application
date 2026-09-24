from fastapi.testclient import TestClient


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_core_phase_one_flow(client: TestClient, root_token: str) -> None:
    tenant_response = client.post(
        "/v1/tenants",
        headers=auth(root_token),
        json={
            "parent_tenant_id": str(client.get("/v1/auth/me", headers=auth(root_token)).json()["tenant_id"]),
            "slug": "acme-legal",
            "name": "Acme Legal",
            "initial_admin": {
                "email": "admin@acme.example",
                "display_name": "Acme Admin",
                "password": "another-correct-horse-password",
            },
        },
    )
    assert tenant_response.status_code == 201, tenant_response.text
    tenant_id = tenant_response.json()["tenant"]["id"]

    tenants_response = client.get("/v1/tenants", headers=auth(root_token))
    assert tenants_response.status_code == 200
    assert [tenant["slug"] for tenant in tenants_response.json()] == ["root", "acme-legal"]

    login_response = client.post(
        "/v1/auth/login",
        json={
            "email": "admin@acme.example",
            "password": "another-correct-horse-password",
        },
    )
    assert login_response.status_code == 200, login_response.text
    tenant_token = login_response.json()["access_token"]
    assert client.get("/v1/auth/me", headers=auth(tenant_token)).json()["tenant_id"] == tenant_id

    users_response = client.get(f"/v1/tenants/{tenant_id}/users", headers=auth(tenant_token))
    assert users_response.status_code == 200
    assert users_response.json()[0]["email"] == "admin@acme.example"

    client_response = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(tenant_token),
        json={"name": "Acme Corporation"},
    )
    assert client_response.status_code == 201, client_response.text
    client_id = client_response.json()["id"]

    matter_response = client.post(
        f"/v1/clients/{client_id}/matters",
        headers=auth(tenant_token),
        json={"name": "Regulatory Inquiry"},
    )
    assert matter_response.status_code == 201, matter_response.text
    matter_id = matter_response.json()["id"]

    default_definitions = client.get(
        f"/v1/matters/{matter_id}/metadata-definitions",
        headers=auth(tenant_token),
    )
    assert default_definitions.status_code == 200, default_definitions.text
    definitions_by_key = {definition["key"]: definition for definition in default_definitions.json()}
    assert len(definitions_by_key) == 24
    assert {
        key: definitions_by_key["custodian"][key]
        for key in ("value_source", "reference_target", "cardinality", "facetable")
    } == {
        "value_source": "SYSTEM",
        "reference_target": "CUSTODIAN",
        "cardinality": "MULTIPLE",
        "facetable": True,
    }
    assert definitions_by_key["family_id"]["value_source"] == "SYSTEM"
    assert definitions_by_key["family_id"]["facetable"] is True
    assert definitions_by_key["file_size"]["type"] == "INTEGER"
    assert definitions_by_key["email_from"]["normalize_to_lowercase"] is True
    assert definitions_by_key["email_to"]["normalize_to_lowercase"] is True
    assert definitions_by_key["email_cc"]["normalize_to_lowercase"] is True
    assert definitions_by_key["email_bcc"]["normalize_to_lowercase"] is True
    assert definitions_by_key["email_subject"]["normalize_to_lowercase"] is False
    assert definitions_by_key["responsiveness"]["value_source"] == "ASSERTED"
    assert definitions_by_key["responsiveness"]["template_key"] == "edrm-core"
    assert definitions_by_key["responsiveness"]["template_version"] == 2

    definition_response = client.post(
        f"/v1/matters/{matter_id}/metadata-definitions",
        headers=auth(tenant_token),
        json={
            "key": "issue_tag",
            "display_name": "Issue tag",
            "type": "ENUM",
            "cardinality": "SINGLE",
            "allowed_values": [
                {"key": "contract", "label": "Contract"},
                {"key": "conduct", "label": "Conduct"},
            ],
            "facetable": True,
            "normalize_to_lowercase": False,
            "ai_assignable": True,
        },
    )
    assert definition_response.status_code == 201, definition_response.text
    assert definition_response.json()["key"] == "issue_tag"
    assert definition_response.json()["value_source"] == "ASSERTED"
    assert definition_response.json()["template_key"] is None

    duplicate_definition = client.post(
        f"/v1/matters/{matter_id}/metadata-definitions",
        headers=auth(tenant_token),
        json={
            "key": "issue_tag",
            "display_name": "Duplicate",
            "type": "BOOLEAN",
        },
    )
    assert duplicate_definition.status_code == 409

    invalid_enum = client.post(
        f"/v1/matters/{matter_id}/metadata-definitions",
        headers=auth(tenant_token),
        json={
            "key": "invalid_enum",
            "display_name": "Invalid Enum",
            "type": "ENUM",
        },
    )
    assert invalid_enum.status_code == 422

    definition_id = definition_response.json()["id"]
    update_definition = client.patch(
        f"/v1/matters/{matter_id}/metadata-definitions/{definition_id}",
        headers=auth(tenant_token),
        json={"display_name": "Issue classification", "resolution_policy": "HUMAN_PRECEDENCE"},
    )
    assert update_definition.status_code == 200, update_definition.text
    assert update_definition.json()["display_name"] == "Issue classification"
    assert update_definition.json()["resolution_policy"] == "HUMAN_PRECEDENCE"

    add_enum = client.post(
        f"/v1/matters/{matter_id}/metadata-definitions/{definition_id}/enum-values",
        headers=auth(tenant_token),
        json={"key": "finance", "label": "Finance", "description": "Financial issues"},
    )
    assert add_enum.status_code == 201, add_enum.text
    assert add_enum.json()["allowed_values"][-1] == {
        "key": "finance",
        "label": "Finance",
        "description": "Financial issues",
        "active": True,
    }

    update_enum = client.patch(
        f"/v1/matters/{matter_id}/metadata-definitions/{definition_id}/enum-values/finance",
        headers=auth(tenant_token),
        json={"label": "Financial conduct"},
    )
    assert update_enum.status_code == 200, update_enum.text
    finance_value = next(
        value for value in update_enum.json()["allowed_values"] if value["key"] == "finance"
    )
    assert finance_value["label"] == "Financial conduct"

    deactivate_enum = client.post(
        f"/v1/matters/{matter_id}/metadata-definitions/{definition_id}/enum-values/finance/deactivate",
        headers=auth(tenant_token),
    )
    assert deactivate_enum.status_code == 200, deactivate_enum.text
    finance_value = next(
        value for value in deactivate_enum.json()["allowed_values"] if value["key"] == "finance"
    )
    assert finance_value["active"] is False

    protected_system_definition = client.patch(
        f"/v1/matters/{matter_id}/metadata-definitions/{definitions_by_key['custodian']['id']}",
        headers=auth(tenant_token),
        json={"display_name": "Evidence holder"},
    )
    assert protected_system_definition.status_code == 409


def test_refresh_token_is_rotated(client: TestClient, root_admin) -> None:
    login_response = client.post(
        "/v1/auth/login",
        json={
            "email": "root@example.com",
            "password": "correct-horse-battery-staple",
        },
    )
    old_refresh = login_response.json()["refresh_token"]
    refresh_response = client.post("/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert refresh_response.status_code == 200, refresh_response.text
    assert refresh_response.json()["refresh_token"] != old_refresh

    replay_response = client.post("/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert replay_response.status_code == 401


def test_login_rejects_client_supplied_tenant_and_uses_server_assignment(
    client: TestClient,
    root_admin,
) -> None:
    stale_request = client.post(
        "/v1/auth/login",
        json={
            "tenant_slug": "some-other-tenant",
            "email": "root@example.com",
            "password": "correct-horse-battery-staple",
        },
    )
    assert stale_request.status_code == 422

    login_response = client.post(
        "/v1/auth/login",
        json={
            "email": "ROOT@example.com",
            "password": "correct-horse-battery-staple",
        },
    )
    assert login_response.status_code == 200, login_response.text
    me_response = client.get(
        "/v1/auth/me",
        headers=auth(login_response.json()["access_token"]),
    )
    assert me_response.status_code == 200
    assert me_response.json()["tenant_id"] == str(root_admin.tenant_id)


def test_user_email_is_unique_across_tenants(client: TestClient, root_token: str) -> None:
    root_tenant_id = client.get("/v1/auth/me", headers=auth(root_token)).json()["tenant_id"]
    response = client.post(
        "/v1/tenants",
        headers=auth(root_token),
        json={
            "parent_tenant_id": root_tenant_id,
            "slug": "duplicate-email-tenant",
            "name": "Duplicate Email Tenant",
            "initial_admin": {
                "email": "ROOT@example.com",
                "display_name": "Duplicate Root",
                "password": "another-correct-horse-password",
            },
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "A user with that email already exists"


def test_subtenant_admin_cannot_create_tenant(client: TestClient, root_token: str) -> None:
    root_tenant_id = client.get("/v1/auth/me", headers=auth(root_token)).json()["tenant_id"]
    tenant_response = client.post(
        "/v1/tenants",
        headers=auth(root_token),
        json={
            "parent_tenant_id": root_tenant_id,
            "slug": "child-one",
            "name": "Child One",
            "initial_admin": {
                "email": "child@example.com",
                "display_name": "Child Admin",
                "password": "long-enough-child-password",
            },
        },
    )
    assert tenant_response.status_code == 201
    child_token = client.post(
        "/v1/auth/login",
        json={"email": "child@example.com", "password": "long-enough-child-password"},
    ).json()["access_token"]

    list_denied = client.get("/v1/tenants", headers=auth(child_token))
    assert list_denied.status_code == 403

    denied = client.post(
        "/v1/tenants",
        headers=auth(child_token),
        json={
            "parent_tenant_id": tenant_response.json()["tenant"]["id"],
            "slug": "grandchild",
            "name": "Grandchild",
            "initial_admin": {
                "email": "grandchild@example.com",
                "display_name": "Grandchild Admin",
                "password": "long-enough-grandchild-password",
            },
        },
    )
    assert denied.status_code == 403


def test_metadata_groups_preferences_templates_and_clone(client: TestClient, root_token: str) -> None:
    me = client.get("/v1/auth/me", headers=auth(root_token)).json()
    client_response = client.post(
        f"/v1/tenants/{me['tenant_id']}/clients",
        headers=auth(root_token),
        json={"name": "Template Test Client"},
    )
    assert client_response.status_code == 201, client_response.text
    client_id = client_response.json()["id"]

    matter_response = client.post(
        f"/v1/clients/{client_id}/matters",
        headers=auth(root_token),
        json={"name": "Source Matter"},
    )
    assert matter_response.status_code == 201, matter_response.text
    matter_id = matter_response.json()["id"]

    definitions = client.get(
        f"/v1/matters/{matter_id}/metadata-definitions",
        headers=auth(root_token),
    ).json()
    definitions_by_key = {definition["key"]: definition for definition in definitions}
    custom_definition = client.post(
        f"/v1/matters/{matter_id}/metadata-definitions",
        headers=auth(root_token),
        json={"key": "hot_document", "display_name": "Hot document", "type": "BOOLEAN", "facetable": True},
    )
    assert custom_definition.status_code == 201, custom_definition.text

    groups_response = client.get(f"/v1/matters/{matter_id}/metadata-groups", headers=auth(root_token))
    assert groups_response.status_code == 200, groups_response.text
    groups = groups_response.json()
    assert len(groups) == 7
    groups_by_key = {group["key"]: group for group in groups}
    assert groups_by_key["email"]["default_table_visible"] is False
    assert groups_by_key["email"]["table_visible"] is False
    assert groups_by_key["email"]["document_visible"] is True

    visibility_response = client.put(
        f"/v1/matters/{matter_id}/metadata-groups/{groups_by_key['email']['id']}/visibility",
        headers=auth(root_token),
        json={"surface": "TABLE", "visible": True},
    )
    assert visibility_response.status_code == 200, visibility_response.text
    assert visibility_response.json()["table_visible"] is True
    assert visibility_response.json()["default_table_visible"] is False

    personal_group = client.post(
        f"/v1/matters/{matter_id}/metadata-groups",
        headers=auth(root_token),
        json={
            "display_name": "My coding fields",
            "scope": "PERSONAL",
            "definition_ids": [definitions_by_key["responsiveness"]["id"], custom_definition.json()["id"]],
            "default_table_visible": True,
            "default_document_visible": False,
        },
    )
    assert personal_group.status_code == 201, personal_group.text
    assert personal_group.json()["owner_user_id"] == me["id"]

    shared_group = client.post(
        f"/v1/matters/{matter_id}/metadata-groups",
        headers=auth(root_token),
        json={
            "display_name": "Priority coding",
            "scope": "MATTER",
            "definition_ids": [custom_definition.json()["id"], definitions_by_key["responsiveness"]["id"]],
            "default_table_visible": True,
            "default_document_visible": True,
        },
    )
    assert shared_group.status_code == 201, shared_group.text

    second_user_response = client.post(
        f"/v1/tenants/{me['tenant_id']}/users",
        headers=auth(root_token),
        json={
            "email": "second-reviewer@example.com",
            "display_name": "Second Reviewer",
            "password": "second-reviewer-password",
        },
    )
    assert second_user_response.status_code == 201, second_user_response.text
    second_login = client.post(
        "/v1/auth/login",
        json={
            "email": "second-reviewer@example.com",
            "password": "second-reviewer-password",
        },
    )
    assert second_login.status_code == 200, second_login.text
    second_token = second_login.json()["access_token"]
    second_user_groups = client.get(
        f"/v1/matters/{matter_id}/metadata-groups",
        headers=auth(second_token),
    ).json()
    assert "my_coding_fields" not in {group["key"] for group in second_user_groups}
    assert "priority_coding" in {group["key"] for group in second_user_groups}
    assert next(group for group in second_user_groups if group["key"] == "email")["table_visible"] is False

    template_response = client.post(
        f"/v1/matters/{matter_id}/templates",
        headers=auth(root_token),
        json={"name": "Standard Investigation", "description": "Shared coding setup", "scope": "TENANT"},
    )
    assert template_response.status_code == 201, template_response.text
    template = template_response.json()
    assert template["definition_count"] == 25
    assert template["group_count"] == 8

    available_templates = client.get(
        f"/v1/clients/{client_id}/matter-templates",
        headers=auth(root_token),
    )
    assert available_templates.status_code == 200
    assert [item["name"] for item in available_templates.json()] == ["Standard Investigation"]

    from_template = client.post(
        f"/v1/clients/{client_id}/matters",
        headers=auth(root_token),
        json={"name": "From Template", "template_id": template["id"]},
    )
    assert from_template.status_code == 201, from_template.text
    template_matter_id = from_template.json()["id"]
    template_definitions = client.get(
        f"/v1/matters/{template_matter_id}/metadata-definitions",
        headers=auth(root_token),
    ).json()
    assert "hot_document" in {definition["key"] for definition in template_definitions}
    template_groups = client.get(
        f"/v1/matters/{template_matter_id}/metadata-groups",
        headers=auth(root_token),
    ).json()
    assert {group["key"] for group in template_groups} == {
        "identification",
        "family",
        "custodian_source",
        "email",
        "file_details",
        "source_dates",
        "review_coding",
        "priority_coding",
    }
    assert next(group for group in template_groups if group["key"] == "email")["table_visible"] is False

    clone_response = client.post(
        f"/v1/clients/{client_id}/matters",
        headers=auth(root_token),
        json={"name": "Cloned Configuration", "clone_from_matter_id": matter_id},
    )
    assert clone_response.status_code == 201, clone_response.text
    clone_groups = client.get(
        f"/v1/matters/{clone_response.json()['id']}/metadata-groups",
        headers=auth(root_token),
    ).json()
    assert {group["key"] for group in clone_groups} == {group["key"] for group in template_groups}
    assert next(group for group in clone_groups if group["key"] == "email")["table_visible"] is False

    invalid_source = client.post(
        f"/v1/clients/{client_id}/matters",
        headers=auth(root_token),
        json={
            "name": "Invalid Configuration Source",
            "template_id": template["id"],
            "clone_from_matter_id": matter_id,
        },
    )
    assert invalid_source.status_code == 422
