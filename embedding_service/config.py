from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
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
    provider: Literal["sentence_transformers", "voyage_api"] = "sentence_transformers"
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
    query_mode: Literal["embedded", "remote"] = "embedded"
    query_model: str | None = None
    query_model_revision: str | None = None
    query_device: str = "auto"
    query_base_url: str | None = None
    voyage_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("VOYAGE_API_KEY", "EMBEDDING_VOYAGE_API_KEY"),
    )
    voyage_base_url: str = "https://api.voyageai.com"
    voyage_max_retries: int = Field(default=5, ge=0, le=10)
    voyage_retry_base_seconds: float = Field(default=1.0, gt=0, le=60)
    voyage_batch_enabled: bool = False
    voyage_batch_request_size: int = Field(default=100, ge=1, le=1000)
    voyage_batch_poll_seconds: float = Field(default=15.0, gt=0, le=3600)
    voyage_batch_concurrency: int = Field(default=20, ge=1, le=100)
    voyage_realtime_concurrency: int = Field(default=8, ge=1, le=100)
    voyage_tokens_per_minute: int = Field(default=3_000_000, ge=1_000, le=1_000_000_000)
    voyage_requests_per_minute: int = Field(default=2_000, ge=1, le=100_000)

    @model_validator(mode="after")
    def validate_provider(self) -> "EmbeddingSettings":
        if self.query_model == "voyageai/voyage-4-nano" and self.model not in {
            "voyageai/voyage-4-nano",
            "voyage-4-lite",
            "voyage-4",
            "voyage-4-large",
        }:
            raise ValueError("voyage-4-nano query embeddings require Voyage 4 document embeddings")
        if self.provider != "voyage_api":
            return self
        if self.mode != "remote":
            raise ValueError("EMBEDDING_PROVIDER=voyage_api requires EMBEDDING_MODE=remote")
        if self.voyage_api_key is None:
            raise ValueError("VOYAGE_API_KEY is required when EMBEDDING_PROVIDER=voyage_api")
        if self.model not in {"voyage-4-lite", "voyage-4", "voyage-4-large"}:
            raise ValueError("Voyage API model must be voyage-4-lite, voyage-4, or voyage-4-large")
        if self.model_revision is not None:
            raise ValueError("Hosted Voyage models do not accept EMBEDDING_MODEL_REVISION")
        if self.dimensions not in {256, 512, 1024, 2048}:
            raise ValueError("Hosted Voyage dimensions must be 256, 512, 1024, or 2048")
        if not self.normalize:
            raise ValueError("Hosted Voyage embeddings require EMBEDDING_NORMALIZE=true")
        return self


@lru_cache
def get_embedding_settings() -> EmbeddingSettings:
    return EmbeddingSettings()
