from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EmbeddingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="EMBEDDING_",
        env_ignore_empty=True,
        extra="ignore",
    )

    mode: Literal["embedded", "remote"] = "embedded"
    provider: Literal["sentence_transformers"] = "sentence_transformers"
    model: str = "voyageai/voyage-4-nano"
    model_revision: str | None = None
    dimensions: int = Field(default=1024, ge=32, le=4096)
    device: str = "auto"
    normalize: bool = True
    trust_remote_code: bool = True
    batch_size: int = Field(default=32, ge=1, le=1024)
    max_inputs: int = Field(default=256, ge=1, le=1000)
    max_input_characters: int = Field(default=200_000, ge=1_000, le=10_000_000)
    base_url: str = "http://localhost:8002"
    service_token: str = Field(default="development-embedding-service-token", min_length=32)
    request_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)


@lru_cache
def get_embedding_settings() -> EmbeddingSettings:
    return EmbeddingSettings()
