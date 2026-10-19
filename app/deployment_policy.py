"""Refuse development or placeholder secrets outside the development environment.

The Core, Artifact, Classification, and Embedding services each read their own
settings. Each settings class calls ``reject_development_secrets`` from its
validator, and each process loads its settings at startup so a bad value stops
the process before it serves traffic.
"""

from typing import Literal

AppEnvironment = Literal["development", "staging", "production"]

# .env.example ships "replace-with..." placeholders, which are never valid secrets.
PLACEHOLDER_SECRET_PREFIXES = ("replace-with",)
# Local development may keep "development-..." values in .env; other environments may not.
DEVELOPMENT_SECRET_PREFIXES = ("development-",)


class DeploymentSecretError(ValueError):
    pass


def reject_development_secrets(app_env: str, secrets: dict[str, str]) -> None:
    for name, value in secrets.items():
        if value.startswith(PLACEHOLDER_SECRET_PREFIXES):
            raise DeploymentSecretError(f"{name} is still a placeholder; replace it with a generated secret")
        if app_env != "development" and value.startswith(DEVELOPMENT_SECRET_PREFIXES):
            raise DeploymentSecretError(
                f"{name} is a development value; set a real secret when APP_ENV={app_env}"
            )


def load_core_runtime_settings() -> None:
    """Instantiate every settings object the Core process reads so their checks run at startup."""

    from app.config import get_settings
    from artifact_service.config import get_artifact_settings
    from classification_service.config import get_classification_settings
    from embedding_service.config import get_embedding_settings

    get_settings()
    get_artifact_settings()
    get_classification_settings()
    get_embedding_settings()
