from app.models import AgentDefinition

MATTER_DEFINITION_SETUP_WORKFLOW = "MATTER_DEFINITION_SETUP"
STANDARD_MATTER_DEFINITION_AGENT_KEY = "matter_definition_setup"

WORKFLOW_AGENT_KEYS: dict[str, frozenset[str]] = {
    MATTER_DEFINITION_SETUP_WORKFLOW: frozenset({STANDARD_MATTER_DEFINITION_AGENT_KEY}),
}


def agent_supports_workflow(agent: AgentDefinition, workflow_type: str) -> bool:
    return agent.key in WORKFLOW_AGENT_KEYS.get(workflow_type, frozenset())
