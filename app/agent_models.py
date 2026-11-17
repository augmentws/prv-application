from app.config import Settings

DEFAULT_MODEL_KEY = "configured-default"
EVIDENCE_EXTRACTION_MODEL_KEY = "matter-definition-evidence-extraction"
CRITERION_ADJUDICATION_MODEL_KEY = "matter-definition-criterion-adjudication"
EVIDENCE_NARRATIVE_MODEL_KEY = "matter-definition-evidence-narrative"


def validate_agent_model_key(model_key: str) -> None:
    if model_key != DEFAULT_MODEL_KEY:
        raise ValueError(
            "Unknown agent model key. Tenant agent definitions must use a model exposed by the platform registry."
        )


def resolve_agent_model(model_key: str, settings: Settings) -> str:
    if model_key == EVIDENCE_EXTRACTION_MODEL_KEY:
        return settings.agent_evidence_extraction_model
    if model_key == CRITERION_ADJUDICATION_MODEL_KEY:
        return settings.agent_criterion_adjudication_model
    if model_key == EVIDENCE_NARRATIVE_MODEL_KEY:
        return settings.agent_evidence_narrative_model
    validate_agent_model_key(model_key)
    if settings.agent_default_model is None:
        raise RuntimeError("AGENT_DEFAULT_MODEL must be configured before agent runs can execute")
    return settings.agent_default_model
