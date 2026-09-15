from app.config import Settings

DEFAULT_MODEL_KEY = "configured-default"


def validate_agent_model_key(model_key: str) -> None:
    if model_key != DEFAULT_MODEL_KEY:
        raise ValueError(
            "Unknown agent model key. Tenant agent definitions must use a model exposed by the platform registry."
        )


def resolve_agent_model(model_key: str, settings: Settings) -> str:
    validate_agent_model_key(model_key)
    if settings.agent_default_model is None:
        raise RuntimeError("AGENT_DEFAULT_MODEL must be configured before agent runs can execute")
    return settings.agent_default_model
