from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.database_urls import build_postgres_url
from app.deployment_policy import AppEnvironment, reject_development_secrets


class ArtifactSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ARTIFACT_",
        extra="ignore",
    )

    app_env: AppEnvironment = Field(default="production", validation_alias="APP_ENV")
    # The Postgres connection is shared with Core; these aliases read the unprefixed POSTGRES_* variables.
    postgres_host: str = Field(default="localhost", validation_alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, ge=1, le=65535, validation_alias="POSTGRES_PORT")
    postgres_user: str = Field(default="postgres", min_length=1, validation_alias="POSTGRES_USER")
    postgres_password: str = Field(min_length=1, validation_alias="POSTGRES_PASSWORD")
    postgres_artifact_database: str = Field(
        default="pvr-artifact", min_length=1, validation_alias="POSTGRES_ARTIFACT_DATABASE"
    )
    # Optional full-URL override, used by the test suite; normally unset.
    database_url_override: str | None = Field(default=None, validation_alias="ARTIFACT_DATABASE_URL")
    storage_endpoint_url: str = "http://localhost:9000"
    storage_access_key: str = Field(min_length=1)
    storage_secret_key: str = Field(min_length=1)
    storage_region: str = "us-east-1"
    bucket_prefix: str = "pv-artifacts"
    bucket_suffix_length: int = Field(default=10, ge=8, le=32)
    upload_max_bytes: int = Field(default=2 * 1024**3, ge=1024 * 1024, le=20 * 1024**3)
    delegation_secret: str = Field(min_length=32)
    delegation_issuer: str = "priv-view-core"
    delegation_audience: str = "priv-view-artifact"

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return build_postgres_url(
            user=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_artifact_database,
        )

    @model_validator(mode="after")
    def validate_deployment_secrets(self) -> "ArtifactSettings":
        reject_development_secrets(self.app_env, {"ARTIFACT_DELEGATION_SECRET": self.delegation_secret})
        return self


@lru_cache
def get_artifact_settings() -> ArtifactSettings:
    return ArtifactSettings()
