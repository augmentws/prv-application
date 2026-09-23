import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SkillDefinition, SkillDefinitionVersion, WorkflowSkillBinding

SUPPORTED_SKILL_CAPABILITIES = frozenset({"structured_output", "prompt_caching", "long_context"})


@dataclass(frozen=True)
class WorkflowRoleSpec:
    key: str
    input_schema_key: str
    output_schema_key: str
    allowed_capabilities: frozenset[str]
    allowed_tool_keys: frozenset[str]


@dataclass(frozen=True)
class WorkflowSpec:
    key: str
    code_version: str
    name: str
    description: str
    roles: tuple[WorkflowRoleSpec, ...]

    def role(self, role_key: str) -> WorkflowRoleSpec:
        for role in self.roles:
            if role.key == role_key:
                return role
        raise ValueError(f"Unknown role {role_key!r} for workflow {self.key!r}")


MATTER_DEFINITION_ASSESSMENT_SPEC = WorkflowSpec(
    key="matter_definition_assessment_v1",
    code_version="7",
    name="Matter Definition assessment",
    description="Builds a diagnostic batch, analyzes its documents, and synthesizes clarification questions.",
    roles=(
        WorkflowRoleSpec(
            key="retrieval_planner",
            input_schema_key="matter_definition_retrieval_plan_input_v1",
            output_schema_key="matter_definition_retrieval_plan_output_v3",
            allowed_capabilities=frozenset({"structured_output", "long_context"}),
            allowed_tool_keys=frozenset(),
        ),
        WorkflowRoleSpec(
            key="document_analysis",
            input_schema_key="matter_definition_document_analysis_input_v1",
            output_schema_key="document_analysis_v1",
            allowed_capabilities=frozenset({"structured_output", "prompt_caching", "long_context"}),
            allowed_tool_keys=frozenset(),
        ),
        WorkflowRoleSpec(
            key="assessment_synthesis",
            input_schema_key="matter_definition_assessment_synthesis_input_v2",
            output_schema_key="matter_definition_assessment_synthesis_output_v3",
            allowed_capabilities=frozenset({"structured_output", "long_context"}),
            allowed_tool_keys=frozenset(),
        ),
    ),
)

WORKFLOW_SPECS = {MATTER_DEFINITION_ASSESSMENT_SPEC.key: MATTER_DEFINITION_ASSESSMENT_SPEC}


def get_workflow_spec(workflow_key: str) -> WorkflowSpec:
    spec = WORKFLOW_SPECS.get(workflow_key)
    if spec is None:
        raise ValueError(f"Unknown workflow: {workflow_key}")
    return spec


def validate_skill_version_for_role(
    definition: SkillDefinition,
    version: SkillDefinitionVersion,
    *,
    workflow_key: str,
    role_key: str,
) -> WorkflowRoleSpec:
    role = get_workflow_spec(workflow_key).role(role_key)
    if definition.status != "ACTIVE":
        raise ValueError("Workflow bindings require an active skill")
    if version.skill_definition_id != definition.id:
        raise ValueError("Skill version does not belong to the selected skill")
    if version.status != "PUBLISHED":
        raise ValueError("Workflow bindings require a published skill version")
    if version.input_schema_key != role.input_schema_key:
        raise ValueError(
            f"Role {role.key} requires input schema {role.input_schema_key}, not {version.input_schema_key}"
        )
    if version.output_schema_key != role.output_schema_key:
        raise ValueError(
            f"Role {role.key} requires output schema {role.output_schema_key}, not {version.output_schema_key}"
        )
    unknown_capabilities = set(version.required_capabilities) - SUPPORTED_SKILL_CAPABILITIES
    if unknown_capabilities:
        raise ValueError(f"Unknown skill capabilities: {', '.join(sorted(unknown_capabilities))}")
    excess_capabilities = set(version.required_capabilities) - role.allowed_capabilities
    if excess_capabilities:
        raise ValueError(f"Role {role.key} does not allow capabilities: {', '.join(sorted(excess_capabilities))}")
    excess_tools = set(version.required_tools) - role.allowed_tool_keys
    if excess_tools:
        raise ValueError(f"Role {role.key} does not allow tools: {', '.join(sorted(excess_tools))}")
    return role


def resolve_workflow_skill_bindings(
    db: Session,
    *,
    workflow_key: str,
    tenant_id: uuid.UUID,
) -> dict[str, tuple[WorkflowSkillBinding, SkillDefinition, SkillDefinitionVersion]]:
    spec = get_workflow_spec(workflow_key)
    resolved: dict[str, tuple[WorkflowSkillBinding, SkillDefinition, SkillDefinitionVersion]] = {}
    for role in spec.roles:
        binding = db.scalar(
            select(WorkflowSkillBinding).where(
                WorkflowSkillBinding.workflow_key == workflow_key,
                WorkflowSkillBinding.role_key == role.key,
                WorkflowSkillBinding.scope == "TENANT",
                WorkflowSkillBinding.owner_tenant_id == tenant_id,
                WorkflowSkillBinding.status == "ACTIVE",
            )
        )
        if binding is None:
            binding = db.scalar(
                select(WorkflowSkillBinding).where(
                    WorkflowSkillBinding.workflow_key == workflow_key,
                    WorkflowSkillBinding.role_key == role.key,
                    WorkflowSkillBinding.scope == "SYSTEM",
                    WorkflowSkillBinding.status == "ACTIVE",
                )
            )
        if binding is None:
            raise ValueError(f"No active skill binding for workflow role {role.key}")
        definition = db.get(SkillDefinition, binding.skill_definition_id)
        version = db.get(SkillDefinitionVersion, binding.skill_definition_version_id)
        if definition is None or version is None:
            raise ValueError(f"Skill binding for role {role.key} references a missing skill version")
        validate_skill_version_for_role(
            definition,
            version,
            workflow_key=workflow_key,
            role_key=role.key,
        )
        resolved[role.key] = (binding, definition, version)
    return resolved


def binding_snapshot(
    resolved: dict[str, tuple[WorkflowSkillBinding, SkillDefinition, SkillDefinitionVersion]],
) -> dict[str, dict]:
    return {
        role_key: {
            "binding_id": str(binding.id),
            "binding_scope": binding.scope,
            "skill_definition_id": str(definition.id),
            "skill_key": definition.key,
            "skill_definition_version_id": str(version.id),
            "skill_version": version.version,
            "instructions": version.instructions,
            "input_schema_key": version.input_schema_key,
            "input_schema": version.input_schema,
            "output_schema_key": version.output_schema_key,
            "output_schema": version.output_schema,
            "model_key": version.model_key,
            "model_policy": version.model_policy,
            "limits": version.limits,
            "required_capabilities": version.required_capabilities,
            "required_tools": version.required_tools,
            "cache_policy": version.cache_policy,
            "configuration": binding.configuration,
        }
        for role_key, (binding, definition, version) in resolved.items()
    }
