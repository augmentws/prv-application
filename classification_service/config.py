from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.deployment_policy import AppEnvironment, reject_development_secrets


class ClassificationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: AppEnvironment = Field(default="production", validation_alias="APP_ENV")
    service_token: str = Field(
        min_length=16,
        validation_alias="CLASSIFICATION_SERVICE_TOKEN",
    )
    artifact_base_url: str = Field(
        default="http://localhost:8001",
        validation_alias="ARTIFACT_BASE_URL",
    )
    request_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        le=3600,
        validation_alias="CLASSIFICATION_REQUEST_TIMEOUT_SECONDS",
    )
    max_concurrency: int = Field(
        default=2,
        ge=1,
        le=64,
        validation_alias="CLASSIFICATION_MAX_CONCURRENCY",
    )
    runtime_image_digest: str = Field(
        default="development-runtime",
        min_length=1,
        max_length=500,
        validation_alias="CLASSIFICATION_RUNTIME_IMAGE_DIGEST",
    )
    source_revision: str = Field(
        default="development",
        min_length=1,
        max_length=255,
        validation_alias="CLASSIFICATION_SOURCE_REVISION",
    )
    package_max_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1024,
        le=2 * 1024 * 1024 * 1024,
        validation_alias="CLASSIFICATION_PACKAGE_MAX_BYTES",
    )

    @model_validator(mode="after")
    def validate_deployment_secrets(self) -> "ClassificationSettings":
        reject_development_secrets(self.app_env, {"CLASSIFICATION_SERVICE_TOKEN": self.service_token})
        return self


@lru_cache
def get_classification_settings() -> ClassificationSettings:
    return ClassificationSettings()
