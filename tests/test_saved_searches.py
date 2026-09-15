from fastapi.testclient import TestClient

from app.schemas import MatterSearchResponse


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(client: TestClient, email: str, password: str) -> str:
    response = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def create_context(client: TestClient, root_token: str) -> tuple[str, str, dict[str, str]]:
    tenant_response = client.post(
        "/v1/tenants",
        headers=auth(root_token),
        json={
            "parent_tenant_id": client.get("/v1/auth/me", headers=auth(root_token)).json()["tenant_id"],
            "slug": "saved-search-tenant",
            "name": "Saved Search Tenant",
            "initial_admin": {
                "email": "owner@example.com",
                "display_name": "Search Owner",
                "password": "correct-horse-battery-staple",
            },
        },
    )
    assert tenant_response.status_code == 201, tenant_response.text
    tenant_id = tenant_response.json()["tenant"]["id"]
    owner_token = login(client, "owner@example.com", "correct-horse-battery-staple")

    users: dict[str, str] = {}
    for label in ("shared", "other"):
        response = client.post(
            f"/v1/tenants/{tenant_id}/users",
            headers=auth(owner_token),
            json={
                "email": f"{label}@example.com",
                "display_name": f"{label.title()} User",
                "password": "correct-horse-battery-staple",
            },
        )
        assert response.status_code == 201, response.text
        users[label] = response.json()["id"]
        users[f"{label}_token"] = login(
            client,
            f"{label}@example.com",
            "correct-horse-battery-staple",
        )

    client_record = client.post(
        f"/v1/tenants/{tenant_id}/clients",
        headers=auth(owner_token),
        json={"name": "Saved Search Client"},
    )
    assert client_record.status_code == 201, client_record.text
    matter = client.post(
        f"/v1/clients/{client_record.json()['id']}/matters",
        headers=auth(owner_token),
        json={"name": "Saved Search Matter"},
    )
    assert matter.status_code == 201, matter.text
    return matter.json()["id"], owner_token, users


def saved_search_payload(name: str, visibility: str, shared_user_ids: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": "Current pricing review",
        "visibility": visibility,
        "shared_user_ids": shared_user_ids or [],
        "search": {
            "query": "price coordination",
            "search_mode": "HYBRID",
            "filters": [{"field": "privilege", "operator": "IN", "values": ["privileged"]}],
            "sort": [{"field": "_score", "direction": "DESC"}],
            "offset": 150,
            "size": 50,
        },
    }


def test_saved_search_visibility_and_owner_management(
    client: TestClient,
    root_token: str,
) -> None:
    matter_id, owner_token, users = create_context(client, root_token)
    base = f"/v1/matters/{matter_id}/saved-searches"

    private = client.post(base, headers=auth(owner_token), json=saved_search_payload("Private search", "PRIVATE"))
    assert private.status_code == 201, private.text
    assert private.json()["search"]["offset"] == 0
    assert private.json()["is_owner"] is True

    public = client.post(base, headers=auth(owner_token), json=saved_search_payload("Public search", "PUBLIC"))
    assert public.status_code == 201, public.text
    shared = client.post(
        base,
        headers=auth(owner_token),
        json=saved_search_payload("Shared search", "SHARED", [users["shared"]]),
    )
    assert shared.status_code == 201, shared.text

    shared_list = client.get(base, headers=auth(users["shared_token"]))
    assert shared_list.status_code == 200, shared_list.text
    assert {item["name"] for item in shared_list.json()} == {"Public search", "Shared search"}
    assert all(item["is_owner"] is False for item in shared_list.json())

    other_list = client.get(base, headers=auth(users["other_token"]))
    assert other_list.status_code == 200, other_list.text
    assert [item["name"] for item in other_list.json()] == ["Public search"]

    hidden = client.get(f"{base}/{private.json()['id']}", headers=auth(users["shared_token"]))
    assert hidden.status_code == 404
    edit_denied = client.put(
        f"{base}/{shared.json()['id']}",
        headers=auth(users["shared_token"]),
        json=saved_search_payload("Renamed", "PRIVATE"),
    )
    assert edit_denied.status_code == 403

    renamed = client.put(
        f"{base}/{shared.json()['id']}",
        headers=auth(owner_token),
        json=saved_search_payload("Renamed shared search", "SHARED", [users["other"]]),
    )
    assert renamed.status_code == 200, renamed.text
    assert [user["id"] for user in renamed.json()["shared_users"]] == [users["other"]]

    deleted = client.delete(f"{base}/{public.json()['id']}", headers=auth(owner_token))
    assert deleted.status_code == 204


def test_saved_search_reexecutes_current_query(
    client: TestClient,
    root_token: str,
    monkeypatch,
) -> None:
    matter_id, owner_token, _ = create_context(client, root_token)
    base = f"/v1/matters/{matter_id}/saved-searches"
    created = client.post(base, headers=auth(owner_token), json=saved_search_payload("Executable", "PRIVATE"))
    assert created.status_code == 201, created.text

    captured = {}

    def fake_execute(matter, payload, *, db, settings):
        del matter, db, settings
        captured["payload"] = payload
        return MatterSearchResponse(total=0, took_ms=1, timed_out=False, hits=[], facets={})

    monkeypatch.setattr("app.routers.saved_searches.execute_matter_search", fake_execute)
    response = client.post(
        f"{base}/{created.json()['id']}/execute",
        headers=auth(owner_token),
        json={"offset": 100, "size": 25},
    )
    assert response.status_code == 200, response.text
    assert captured["payload"].query == "price coordination"
    assert captured["payload"].search_mode == "HYBRID"
    assert captured["payload"].offset == 100
    assert captured["payload"].size == 25
