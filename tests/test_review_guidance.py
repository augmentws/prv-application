from fastapi.testclient import TestClient

from tests.test_agents_and_matter_definitions import auth, create_tenant_context
from tests.test_matter_definition_assessments import create_assessment_matter


def create_guidance(
    client: TestClient,
    token: str,
    matter_id: str,
    *,
    key: str,
    name: str,
    content: str,
):
    response = client.post(
        f"/v1/matters/{matter_id}/guidance",
        headers=auth(token),
        json={
            "key": key,
            "name": name,
            "description": f"Guidance for {name}",
            "content_markdown": content,
            "source_kind": "PASTE",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_guidance_profiles_have_independent_revisions_and_publication(
    client: TestClient,
    root_token: str,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    first = create_guidance(
        client,
        token,
        matter_id,
        key="first_pass",
        name="First Pass Review",
        content="# First pass\n\nReview responsiveness.",
    )
    privilege = create_guidance(
        client,
        token,
        matter_id,
        key="privilege_review",
        name="Privilege Review",
        content="# Privilege\n\nReview legal advice.",
    )

    revised = client.post(
        f"/v1/matters/{matter_id}/guidance/{first['id']}/revisions",
        headers=auth(token),
        json={
            "content_markdown": "# First pass\n\nReview responsiveness and issues.",
            "source_kind": "USER_EDIT",
            "based_on_revision": 1,
        },
    )
    assert revised.status_code == 201, revised.text
    assert revised.json()["current_revision"] == 2

    published = client.post(
        f"/v1/matters/{matter_id}/guidance/{first['id']}/revisions/2/publish",
        headers=auth(token),
    )
    assert published.status_code == 200, published.text
    assert published.json()["published_revision"] == 2

    profiles = client.get(
        f"/v1/matters/{matter_id}/guidance", headers=auth(token)
    )
    assert profiles.status_code == 200, profiles.text
    by_key = {item["key"]: item for item in profiles.json()}
    assert by_key["first_pass"]["current_revision"] == 2
    assert by_key["first_pass"]["published_revision"] == 2
    assert by_key["privilege_review"]["current_revision"] == 1
    assert by_key["privilege_review"]["published_revision"] is None
    assert privilege["id"] == by_key["privilege_review"]["id"]


def test_guidance_clone_provenance_and_archive_rules(
    client: TestClient,
    root_token: str,
) -> None:
    _, token, matter_id = create_tenant_context(client, root_token)
    source = create_guidance(
        client,
        token,
        matter_id,
        key="privilege_review",
        name="Privilege Review",
        content="# Privilege\n\nProtect legal advice.",
    )
    cloned = client.post(
        f"/v1/matters/{matter_id}/guidance/{source['id']}/clone",
        headers=auth(token),
        json={
            "key": "gc_review",
            "name": "General Counsel Review",
            "description": "Escalated legal review.",
            "source_revision": 1,
        },
    )
    assert cloned.status_code == 201, cloned.text
    clone = cloned.json()
    assert clone["revision"]["source_kind"] == "CLONE"
    assert clone["revision"]["source_guidance_id"] == source["id"]
    assert clone["revision"]["source_revision_id"] == source["revision"]["id"]
    assert len(clone["revision"]["source_content_hash"]) == 64

    archived = client.post(
        f"/v1/matters/{matter_id}/guidance/{clone['id']}/archive",
        headers=auth(token),
    )
    assert archived.status_code == 200
    assert archived.json()["status"] == "ARCHIVED"
    blocked = client.post(
        f"/v1/matters/{matter_id}/guidance/{clone['id']}/revisions",
        headers=auth(token),
        json={
            "content_markdown": "blocked",
            "source_kind": "USER_EDIT",
            "based_on_revision": 1,
        },
    )
    assert blocked.status_code == 409

    restored = client.post(
        f"/v1/matters/{matter_id}/guidance/{clone['id']}/restore",
        headers=auth(token),
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "ACTIVE"


def test_legacy_routes_resolve_general_review_and_nested_assessments_are_scoped(
    client: TestClient,
    root_token: str,
    root_admin,
) -> None:
    matter_id = create_assessment_matter(client, root_token, root_admin)
    profiles = client.get(
        f"/v1/matters/{matter_id}/guidance", headers=auth(root_token)
    ).json()
    general = profiles[0]
    assert general["key"] == "general_review"
    create_guidance(
        client,
        root_token,
        matter_id,
        key="privilege_review",
        name="Privilege Review",
        content="# Privilege review",
    )

    legacy = client.get(
        f"/v1/matters/{matter_id}/definition", headers=auth(root_token)
    )
    assert legacy.status_code == 200
    assert legacy.json()["id"] == general["id"]

    started = client.post(
        f"/v1/matters/{matter_id}/guidance/{general['id']}/assessments",
        headers=auth(root_token),
        json={"name": "General guidance assessment", "maximum_document_count": 10},
    )
    assert started.status_code == 202, started.text
    assert started.json()["guidance_id"] == general["id"]
    assert started.json()["guidance_key"] == "general_review"

    scoped = client.get(
        f"/v1/matters/{matter_id}/guidance/{general['id']}/assessments",
        headers=auth(root_token),
    )
    assert scoped.status_code == 200, scoped.text
    assert [item["id"] for item in scoped.json()] == [started.json()["id"]]
