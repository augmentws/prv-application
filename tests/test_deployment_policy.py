import pytest
from pydantic import ValidationError

from app.config import Settings
from app.deployment_policy import DeploymentSecretError, reject_development_secrets
from artifact_service.config import ArtifactSettings
from classification_service.config import ClassificationSettings
from embedding_service.config import EmbeddingSettings

REAL_SECRET = "x9Lq2ZRandomSecretValueThatIsLongEnough0123456789"


def _clear_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "JWT_SECRET",
        "CLASSIFICATION_SERVICE_TOKEN",
        "ARTIFACT_DELEGATION_SECRET",
        "EMBEDDING_SERVICE_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_unset_app_env_defaults_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_secret_env(monkeypatch)
    monkeypatch.delenv("APP_ENV", raising=False)

    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(_env_file=None)


def test_core_rejects_default_secrets_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CLASSIFICATION_SERVICE_TOKEN", REAL_SECRET)

    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(_env_file=None)


def test_core_rejects_placeholder_classification_token_in_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("JWT_SECRET", REAL_SECRET)
    monkeypatch.setenv("CLASSIFICATION_SERVICE_TOKEN", "replace-with-a-long-random-secret-value")

    with pytest.raises(ValidationError, match="CLASSIFICATION_SERVICE_TOKEN"):
        Settings(_env_file=None)


def test_core_accepts_development_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "development")

    settings = Settings(_env_file=None)

    assert settings.app_env == "development"


def test_core_accepts_real_secrets_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", REAL_SECRET)
    monkeypatch.setenv("CLASSIFICATION_SERVICE_TOKEN", REAL_SECRET + "-classification")

    settings = Settings(_env_file=None)

    assert settings.app_env == "production"


def test_artifact_rejects_default_delegation_secret_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(ValidationError, match="ARTIFACT_DELEGATION_SECRET"):
        ArtifactSettings(_env_file=None)


def test_artifact_accepts_default_delegation_secret_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "development")

    assert ArtifactSettings(_env_file=None).app_env == "development"


def test_classification_rejects_default_service_token_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(ValidationError, match="CLASSIFICATION_SERVICE_TOKEN"):
        ClassificationSettings(_env_file=None)


def test_embedding_rejects_default_service_token_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(ValidationError, match="EMBEDDING_SERVICE_TOKEN"):
        EmbeddingSettings(_env_file=None)


def test_embedding_accepts_real_service_token_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("EMBEDDING_SERVICE_TOKEN", REAL_SECRET)

    assert EmbeddingSettings(_env_file=None).app_env == "production"


def test_reject_development_secrets_ignores_development_environment() -> None:
    reject_development_secrets("development", {"JWT_SECRET": "development-only-change-me-please"})


def test_reject_development_secrets_names_the_offending_variable() -> None:
    with pytest.raises(DeploymentSecretError, match="ARTIFACT_DELEGATION_SECRET"):
        reject_development_secrets("production", {"ARTIFACT_DELEGATION_SECRET": "development-artifact-secret"})
