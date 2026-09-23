import uuid

from conftest import TestingSessionLocal
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import SkillDefinition, SkillDefinitionVersion, Tenant, WorkflowSkillBinding
from app.standard_skills import ensure_standard_assessment_skills
from app.workflow_specs import binding_snapshot, get_workflow_spec, resolve_workflow_skill_bindings


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def skill_payload(key: str, role_key: str, *, schema_override: str | None = None) -> dict:
    role = get_workflow_spec("matter_definition_assessment_v1").role(role_key)
    return {
        "key": key,
        "name": key.replace("_", " ").title(),
        "description": "A managed assessment skill.",
        "initial_version": {
            "instructions": "Return only the requested structured result.",
            "input_schema_key": schema_override or role.input_schema_key,
            "input_schema": {"type": "object"},
            "output_schema_key": role.output_schema_key,
            "output_schema": {"type": "object"},
            "model_key": "configured-default",
            "model_policy": {"temperature": 0},
            "limits": {"max_requests": 2},
            "required_capabilities": sorted(role.allowed_capabilities),
            "required_tools": [],
            "cache_policy": {"stable_prefix": ["instructions"]},
            "evaluation_fixtures": [],
        },
    }


def create_child_tenant(client: TestClient, root_token: str) -> str:
    root_tenant_id = client.get("/v1/auth/me", headers=auth(root_token)).json()["tenant_id"]
    suffix = uuid.uuid4().hex[:8]
    response = client.post(
        "/v1/tenants",
        headers=auth(root_token),
        json={
            "parent_tenant_id": root_tenant_id,
            "slug": f"skill-tenant-{suffix}",
            "name": "Skill Tenant",
            "initial_admin": {
                "email": f"admin-{suffix}@skill.example",
                "display_name": "Skill Admin",
                "password": "another-correct-horse-password",
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["tenant"]["id"]


def test_managed_skill_lifecycle_and_workflow_binding(
    client: TestClient,
    root_token: str,
) -> None:
    specs = client.get("/v1/workflow-specs", headers=auth(root_token))
    assert specs.status_code == 200, specs.text
    assessment = next(item for item in specs.json() if item["key"] == "matter_definition_assessment_v1")
    assert [role["key"] for role in assessment["roles"]] == [
        "retrieval_planner",
        "document_analysis",
        "assessment_synthesis",
    ]

    created = client.post(
        "/v1/admin/skills",
        headers=auth(root_token),
        json=skill_payload("test_retrieval_planner", "retrieval_planner"),
    )
    assert created.status_code == 201, created.text
    skill = created.json()
    skill_id = skill["skill"]["id"]
    version_id = skill["version"]["id"]
    assert skill["version"]["status"] == "DRAFT"

    draft_binding = client.put(
        "/v1/admin/workflow-skill-bindings/matter_definition_assessment_v1/retrieval_planner",
        headers=auth(root_token),
        json={"skill_definition_version_id": version_id},
    )
    assert draft_binding.status_code == 422
    assert "published" in draft_binding.json()["error"]["message"]

    published = client.post(
        f"/v1/skills/{skill_id}/versions/1/publish",
        headers=auth(root_token),
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "PUBLISHED"

    bound = client.put(
        "/v1/admin/workflow-skill-bindings/matter_definition_assessment_v1/retrieval_planner",
        headers=auth(root_token),
        json={"skill_definition_version_id": version_id, "configuration": {"temperature": 0}},
    )
    assert bound.status_code == 200, bound.text
    binding_id = bound.json()["id"]
    assert bound.json()["scope"] == "SYSTEM"

    rebound = client.put(
        "/v1/admin/workflow-skill-bindings/matter_definition_assessment_v1/retrieval_planner",
        headers=auth(root_token),
        json={"skill_definition_version_id": version_id, "status": "INACTIVE"},
    )
    assert rebound.status_code == 200, rebound.text
    assert rebound.json()["id"] == binding_id
    assert rebound.json()["status"] == "INACTIVE"

    second_version_payload = skill_payload("unused", "retrieval_planner")["initial_version"]
    second_version_payload["instructions"] = "Use the revised retrieval planning instructions."
    second_version = client.post(
        f"/v1/skills/{skill_id}/versions",
        headers=auth(root_token),
        json=second_version_payload,
    )
    assert second_version.status_code == 201, second_version.text
    publish_second = client.post(
        f"/v1/skills/{skill_id}/versions/2/publish",
        headers=auth(root_token),
    )
    assert publish_second.status_code == 200, publish_second.text
    versions = client.get(f"/v1/skills/{skill_id}/versions", headers=auth(root_token))
    assert versions.status_code == 200, versions.text
    assert [version["status"] for version in versions.json()] == ["PUBLISHED", "PUBLISHED"]
    bindings = client.get("/v1/admin/workflow-skill-bindings", headers=auth(root_token))
    pinned = next(binding for binding in bindings.json() if binding["id"] == binding_id)
    assert pinned["skill_definition_version_id"] == version_id

    incompatible = client.post(
        "/v1/admin/skills",
        headers=auth(root_token),
        json=skill_payload(
            "wrong_document_contract",
            "document_analysis",
            schema_override="wrong_input_schema",
        ),
    )
    assert incompatible.status_code == 201, incompatible.text
    incompatible_skill = incompatible.json()
    publish_incompatible = client.post(
        f"/v1/skills/{incompatible_skill['skill']['id']}/versions/1/publish",
        headers=auth(root_token),
    )
    assert publish_incompatible.status_code == 200
    rejected = client.put(
        "/v1/admin/workflow-skill-bindings/matter_definition_assessment_v1/document_analysis",
        headers=auth(root_token),
        json={"skill_definition_version_id": incompatible_skill["version"]["id"]},
    )
    assert rejected.status_code == 422
    assert "requires input schema" in rejected.json()["error"]["message"]


def test_tenant_binding_overrides_system_binding(client: TestClient, root_token: str, root_admin) -> None:
    tenant_id = create_child_tenant(client, root_token)
    system = client.post(
        "/v1/admin/skills",
        headers=auth(root_token),
        json=skill_payload("system_synthesis", "assessment_synthesis"),
    ).json()
    client.post(
        f"/v1/skills/{system['skill']['id']}/versions/1/publish",
        headers=auth(root_token),
    )
    system_binding = client.put(
        "/v1/admin/workflow-skill-bindings/matter_definition_assessment_v1/assessment_synthesis",
        headers=auth(root_token),
        json={"skill_definition_version_id": system["version"]["id"]},
    )
    assert system_binding.status_code == 200, system_binding.text

    tenant = client.post(
        f"/v1/tenants/{tenant_id}/skills",
        headers=auth(root_token),
        json=skill_payload("tenant_synthesis", "assessment_synthesis"),
    ).json()
    client.post(
        f"/v1/skills/{tenant['skill']['id']}/versions/1/publish",
        headers=auth(root_token),
    )
    tenant_binding = client.put(
        f"/v1/tenants/{tenant_id}/workflow-skill-bindings/matter_definition_assessment_v1/assessment_synthesis",
        headers=auth(root_token),
        json={"skill_definition_version_id": tenant["version"]["id"]},
    )
    assert tenant_binding.status_code == 200, tenant_binding.text
    assert tenant_binding.json()["scope"] == "TENANT"

    with TestingSessionLocal() as db:
        with_system_roles = ensure_standard_assessment_skills(
            db,
            db.get(Tenant, root_admin.tenant_id),
            db.get(type(root_admin), root_admin.id),
        )
        assert with_system_roles is True
        db.commit()
        resolved = resolve_workflow_skill_bindings(
            db,
            workflow_key="matter_definition_assessment_v1",
            tenant_id=uuid.UUID(tenant_id),
        )
        assert resolved["assessment_synthesis"][1].key == "tenant_synthesis"
        assert set(resolved) == {"retrieval_planner", "document_analysis", "assessment_synthesis"}
        snapshot = binding_snapshot(resolved)
        assert snapshot["assessment_synthesis"]["binding_scope"] == "TENANT"
        assert snapshot["assessment_synthesis"]["instructions"] == "Return only the requested structured result."
        assert snapshot["assessment_synthesis"]["output_schema"] == {"type": "object"}

    listed = client.get(
        f"/v1/tenants/{tenant_id}/workflow-skill-bindings",
        headers=auth(root_token),
    )
    assert listed.status_code == 200, listed.text
    assert {binding["scope"] for binding in listed.json()} == {"SYSTEM", "TENANT"}


def test_standard_assessment_skills_are_idempotent(db, root_admin) -> None:
    root = db.get(Tenant, root_admin.tenant_id)
    assert root is not None

    assert ensure_standard_assessment_skills(db, root, root_admin) is True
    db.commit()
    assert ensure_standard_assessment_skills(db, root, root_admin) is False

    skills = list(db.scalars(select(SkillDefinition).where(SkillDefinition.scope == "SYSTEM")))
    bindings = list(db.scalars(select(WorkflowSkillBinding).where(WorkflowSkillBinding.scope == "SYSTEM")))
    assert {skill.key for skill in skills} == {
        "matter_definition_retrieval_plan",
        "matter_definition_document_analysis",
        "matter_definition_assessment_synthesis",
    }
    assert {binding.role_key for binding in bindings} == {
        "retrieval_planner",
        "document_analysis",
        "assessment_synthesis",
    }
    planner = next(
        version
        for version in db.scalars(select(SkillDefinitionVersion))
        if version.output_schema_key.startswith("matter_definition_retrieval_plan_output")
    )
    assert planner.output_schema_key == "matter_definition_retrieval_plan_output_v3"
    query_schema = planner.output_schema["properties"]["queries"]["items"]
    assert set(query_schema["required"]) == {
        "criterion_key",
        "criterion_label",
        "rationale",
        "quota",
        "search",
    }
    search_schema = query_schema["properties"]["search"]
    assert search_schema["required"] == ["query", "search_mode"]
    assert set(search_schema["properties"]) == {"query", "search_mode"}


def test_standard_assessment_skills_publish_new_version_when_limits_change(db, root_admin) -> None:
    root = db.get(Tenant, root_admin.tenant_id)
    assert root is not None
    assert ensure_standard_assessment_skills(db, root, root_admin) is True
    db.commit()

    skill = db.scalar(select(SkillDefinition).where(SkillDefinition.key == "matter_definition_retrieval_plan"))
    assert skill is not None
    version = db.scalar(
        select(SkillDefinitionVersion).where(
            SkillDefinitionVersion.skill_definition_id == skill.id,
            SkillDefinitionVersion.version == skill.published_version,
        )
    )
    assert version is not None
    version.limits = {"max_requests": 2, "max_output_tokens": 12_000}
    db.commit()

    assert ensure_standard_assessment_skills(db, root, root_admin) is True
    db.commit()
    db.refresh(skill)
    assert skill.current_version == 2
    assert skill.published_version == 2

    upgraded = db.scalar(
        select(SkillDefinitionVersion).where(
            SkillDefinitionVersion.skill_definition_id == skill.id,
            SkillDefinitionVersion.version == 2,
        )
    )
    assert upgraded is not None
    assert upgraded.limits["max_requests"] == 3
    binding = db.scalar(
        select(WorkflowSkillBinding).where(
            WorkflowSkillBinding.role_key == "retrieval_planner",
            WorkflowSkillBinding.scope == "SYSTEM",
        )
    )
    assert binding is not None
    assert binding.skill_definition_version_id == upgraded.id
    assert ensure_standard_assessment_skills(db, root, root_admin) is False
