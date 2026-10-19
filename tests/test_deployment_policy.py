import pytest
from pydantic import ValidationError

from app.config import Settings
from app.deployment_policy import DeploymentSecretError, reject_development_secrets
from artifact_service.config import ArtifactSettings
from classification_service.config import ClassificationSettings
from embedding_service.config import EmbeddingSettings

REAL_SECRET = "x9Lq2ZRandomSecretValueThatIsLongEnough0123456789"

# Every required setting the four services read, with values that satisfy their minimum lengths.
VALID_ENV = {
    "APP_ENV": "production",
    "POSTGRES_PASSWORD": "real-password",
    "JWT_SECRET": REAL_SECRET,
    "CLASSIFICATION_SERVICE_TOKEN": REAL_SECRET[:24],
    "EMBEDDING_SERVICE_TOKEN": REAL_SECRET,
    "ARTIFACT_DELEGATION_SECRET": REAL_SECRET + "-delegation",
    "ARTIFACT_STORAGE_ACCESS_KEY": "real-minio-user",
    "ARTIFACT_STORAGE_SECRET_KEY": "real-minio-password",
}


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    """Apply VALID_ENV, then the overrides; a None override removes the variable."""

    values = {**VALID_ENV, **{name.upper(): value for name, value in overrides.items()}}
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


@pytest.mark.parametrize(
    ("settings_class", "variable"),
    [
        (Settings, "JWT_SECRET"),
        (Settings, "CLASSIFICATION_SERVICE_TOKEN"),
        (Settings, "POSTGRES_PASSWORD"),
        (ArtifactSettings, "POSTGRES_PASSWORD"),
        (ArtifactSettings, "ARTIFACT_STORAGE_ACCESS_KEY"),
        (ArtifactSettings, "ARTIFACT_STORAGE_SECRET_KEY"),
        (ArtifactSettings, "ARTIFACT_DELEGATION_SECRET"),
        (ClassificationSettings, "CLASSIFICATION_SERVICE_TOKEN"),
        (EmbeddingSettings, "EMBEDDING_SERVICE_TOKEN"),
    ],
)
def test_missing_required_setting_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, settings_class: type, variable: str
) -> None:
    _set_env(monkeypatch, **{variable: None})

    with pytest.raises(ValidationError):
        settings_class(_env_file=None)


@pytest.mark.parametrize(
    "settings_class",
    [Settings, ArtifactSettings, ClassificationSettings, EmbeddingSettings],
)
def test_real_values_are_accepted_in_production(monkeypatch: pytest.MonkeyPatch, settings_class: type) -> None:
    _set_env(monkeypatch)

    assert settings_class(_env_file=None).app_env == "production"


def test_database_urls_are_built_from_postgres_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch,
        DATABASE_URL=None,
        ARTIFACT_DATABASE_URL=None,
        POSTGRES_HOST="db.internal",
        POSTGRES_PORT="6543",
        POSTGRES_USER="svc user",
        POSTGRES_PASSWORD="p@ss/word:1",
    )
    encoded = "postgresql+psycopg://svc%20user:p%40ss%2Fword%3A1@db.internal:6543"

    assert Settings(_env_file=None).database_url == f"{encoded}/pvr-core"
    assert ArtifactSettings(_env_file=None).database_url == f"{encoded}/pvr-artifact"


def test_database_url_override_is_used_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, DATABASE_URL="sqlite+pysqlite:///:memory:")

    assert Settings(_env_file=None).database_url == "sqlite+pysqlite:///:memory:"


def test_unset_app_env_defaults_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, APP_ENV=None, JWT_SECRET="development-only-change-me-please")

    with pytest.raises(ValidationError, match="JWT_SECRET is a development value"):
        Settings(_env_file=None)


def test_core_rejects_development_secret_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, JWT_SECRET="development-only-change-me-please")

    with pytest.raises(ValidationError, match="JWT_SECRET is a development value"):
        Settings(_env_file=None)


def test_development_secrets_are_accepted_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch,
        APP_ENV="development",
        JWT_SECRET="development-only-change-me-please",
        ARTIFACT_DELEGATION_SECRET="development-artifact-delegation-secret",
        CLASSIFICATION_SERVICE_TOKEN="development-classification-service-token",
        EMBEDDING_SERVICE_TOKEN="development-embedding-service-token",
    )

    assert Settings(_env_file=None).app_env == "development"
    assert ArtifactSettings(_env_file=None).app_env == "development"
    assert ClassificationSettings(_env_file=None).app_env == "development"
    assert EmbeddingSettings(_env_file=None).app_env == "development"


def test_placeholder_is_rejected_even_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, APP_ENV="development", JWT_SECRET="replace-with-at-least-32-random-characters")

    with pytest.raises(ValidationError, match="JWT_SECRET is still a placeholder"):
        Settings(_env_file=None)


def test_artifact_rejects_development_delegation_secret_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, ARTIFACT_DELEGATION_SECRET="development-artifact-delegation-secret")

    with pytest.raises(ValidationError, match="ARTIFACT_DELEGATION_SECRET is a development value"):
        ArtifactSettings(_env_file=None)


def test_classification_rejects_development_service_token_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, CLASSIFICATION_SERVICE_TOKEN="development-classification-service-token")

    with pytest.raises(ValidationError, match="CLASSIFICATION_SERVICE_TOKEN is a development value"):
        ClassificationSettings(_env_file=None)


def test_embedding_rejects_development_service_token_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, EMBEDDING_SERVICE_TOKEN="development-embedding-service-token")

    with pytest.raises(ValidationError, match="EMBEDDING_SERVICE_TOKEN is a development value"):
        EmbeddingSettings(_env_file=None)


def test_reject_development_secrets_ignores_development_values_in_development() -> None:
    reject_development_secrets("development", {"JWT_SECRET": "development-only-change-me-please"})


def test_reject_development_secrets_names_the_offending_variable() -> None:
    with pytest.raises(DeploymentSecretError, match="ARTIFACT_DELEGATION_SECRET"):
        reject_development_secrets("production", {"ARTIFACT_DELEGATION_SECRET": "development-artifact-secret"})
