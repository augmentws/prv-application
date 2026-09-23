from dataclasses import dataclass


@dataclass(frozen=True)
class AgentToolSpec:
    key: str
    name: str
    description: str
    requires_approval: bool


AGENT_TOOL_SPECS = (
    AgentToolSpec(
        key="matter_definition.read",
        name="Read matter definition",
        description="Read the current Matter Definition draft and its source references.",
        requires_approval=False,
    ),
    AgentToolSpec(
        key="matter_metadata.list_editable",
        name="List editable metadata",
        description="List matter-owned ASSERTED metadata definitions and enum values.",
        requires_approval=False,
    ),
    AgentToolSpec(
        key="matter_metadata.compare",
        name="Compare coding fields",
        description="Compare proposed coding fields with the matter's editable metadata definitions.",
        requires_approval=False,
    ),
    AgentToolSpec(
        key="matter_definition.validate",
        name="Validate matter definition",
        description="Validate a complete draft and report unresolved field or enum references.",
        requires_approval=False,
    ),
    AgentToolSpec(
        key="matter_metadata.create_definition",
        name="Create metadata definition",
        description="Create an editable matter metadata definition after user approval.",
        requires_approval=True,
    ),
    AgentToolSpec(
        key="matter_metadata.update_definition",
        name="Update metadata definition",
        description="Update an editable matter metadata definition after user approval.",
        requires_approval=True,
    ),
    AgentToolSpec(
        key="matter_metadata.enum.add",
        name="Add enum value",
        description="Add a stable enum value after user approval.",
        requires_approval=True,
    ),
    AgentToolSpec(
        key="matter_metadata.enum.update",
        name="Update enum value",
        description="Update an enum label or description after user approval.",
        requires_approval=True,
    ),
    AgentToolSpec(
        key="matter_metadata.enum.deactivate",
        name="Deactivate enum value",
        description="Deactivate an enum value without deleting history after user approval.",
        requires_approval=True,
    ),
    AgentToolSpec(
        key="matter_definition.apply_draft_edit",
        name="Apply draft edit",
        description="Create a Matter Definition revision from an approved agent proposal.",
        requires_approval=True,
    ),
    AgentToolSpec(
        key="matter_definition.start_assessment",
        name="Start Matter Definition assessment",
        description="Start a diagnostic corpus assessment for a pinned Matter Definition revision after approval.",
        requires_approval=True,
    ),
    AgentToolSpec(
        key="batch.search_summaries",
        name="Search batch summaries",
        description=(
            "Run a semantic search limited to the conversation's review batch and return reusable "
            "structured document summaries for the strongest matches."
        ),
        requires_approval=False,
    ),
)

AGENT_TOOL_REGISTRY = {tool.key: tool for tool in AGENT_TOOL_SPECS}
EXECUTABLE_AGENT_TOOL_KEYS = frozenset(
    {
        "matter_definition.read",
        "matter_metadata.list_editable",
        "matter_metadata.compare",
        "matter_definition.validate",
        "matter_metadata.create_definition",
        "matter_metadata.update_definition",
        "matter_metadata.enum.add",
        "matter_metadata.enum.update",
        "matter_metadata.enum.deactivate",
        "matter_definition.apply_draft_edit",
        "matter_definition.start_assessment",
        "batch.search_summaries",
    }
)


def validate_agent_tool_keys(tool_keys: list[str]) -> None:
    unknown = sorted(set(tool_keys) - AGENT_TOOL_REGISTRY.keys())
    if unknown:
        raise ValueError(f"Unknown agent tools: {', '.join(unknown)}")


def validate_executable_agent_tool_keys(tool_keys: list[str]) -> None:
    validate_agent_tool_keys(tool_keys)
    unavailable = sorted(set(tool_keys) - EXECUTABLE_AGENT_TOOL_KEYS)
    if unavailable:
        raise ValueError(f"Agent tools are not executable yet: {', '.join(unavailable)}")
