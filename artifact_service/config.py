from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ArtifactSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ARTIFACT_",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/pvr-artifact"
    storage_endpoint_url: str = "http://localhost:9000"
    storage_access_key: str = "minioadmin"
    storage_secret_key: str = "minioadmin"
    storage_region: str = "us-east-1"
    bucket_prefix: str = "pv-artifacts"
    bucket_suffix_length: int = Field(default=10, ge=8, le=32)
    delegation_secret: str = Field(default="development-artifact-delegation-secret", min_length=32)
    delegation_issuer: str = "priv-view-core"
    delegation_audience: str = "priv-view-artifact"


@lru_cache
def get_artifact_settings() -> ArtifactSettings:
    return ArtifactSettings()
