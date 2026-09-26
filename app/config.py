from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Priv-View Core"
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/pvr-core"
    artifact_mode: str = "embedded"
    artifact_base_url: str = "http://localhost:8001"
    dbos_enabled: bool = True
    dbos_system_database_url: str | None = None
    dbos_system_schema: str = "dbos"
    dbos_application_name: str = "priv-view-workflows"
    dbos_application_version: str = "0.1.0"
    matter_import_batch_size: int = Field(default=1000, ge=1, le=10000)
    search_enabled: bool = True
    opensearch_url: str = "http://localhost:9200"
    opensearch_username: str | None = None
    opensearch_password: str | None = None
    opensearch_verify_tls: bool = True
    opensearch_index_prefix: str = "pvr"
    search_request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    search_bulk_batch_size: int = Field(default=500, ge=1, le=5000)
    search_projection_document_concurrency: int = Field(default=8, ge=1, le=32)
    search_body_text_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    agent_default_model: str | None = None
    agent_turn_queue_concurrency: int = Field(default=4, ge=1, le=100)
    agent_approval_timeout_seconds: int = Field(default=7 * 24 * 60 * 60, ge=60, le=30 * 24 * 60 * 60)
    agent_streaming_enabled: bool = True
    agent_stream_heartbeat_seconds: float = Field(default=15.0, ge=1, le=60)
    agent_stream_max_lifetime_seconds: int = Field(default=600, ge=30, le=3600)
    agent_stream_subscriber_queue_size: int = Field(default=32, ge=1, le=1000)
    agent_stream_replay_page_size: int = Field(default=250, ge=1, le=1000)
    agent_stream_replay_limit: int = Field(default=5000, ge=1, le=100_000)
    agent_stream_event_payload_max_bytes: int = Field(default=16_384, ge=1024, le=1_048_576)
    agent_stream_event_retention_days: int = Field(default=7, ge=1, le=365)
    agent_stream_cleanup_interval_seconds: int = Field(default=3600, ge=60, le=86_400)
    model_trace_enabled: bool = False
    model_trace_directory: str = ".model-traces"
    model_rate_limit_requests_per_minute: int = Field(default=12, ge=1, le=100_000)
    model_rate_limit_input_tokens_per_minute: int = Field(default=200_000, ge=1, le=1_000_000_000)
    model_rate_limit_max_retries: int = Field(default=5, ge=0, le=20)
    model_rate_limit_retry_base_seconds: float = Field(default=1.0, gt=0, le=300)
    model_rate_limit_retry_max_seconds: float = Field(default=120.0, gt=0, le=3600)
    model_rate_limit_characters_per_token: float = Field(default=3.0, ge=1, le=10)
    matter_embedding_batch_size: int = Field(default=250, ge=1, le=500)
    matter_embedding_document_concurrency: int = Field(default=8, ge=1, le=32)
    matter_topic_batch_size: int = Field(default=100, ge=1, le=1000)
    matter_topic_assignment_concurrency: int = Field(default=8, ge=1, le=32)
    matter_bulk_tag_batch_size: int = Field(default=250, ge=1, le=2000)
    embedding_text_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    chunk_target_characters: int = Field(default=1800, ge=200, le=20000)
    chunk_max_characters: int = Field(default=2600, ge=300, le=30000)
    chunk_overlap_characters: int = Field(default=200, ge=0, le=5000)
    definition_assessment_warning_document_count: int = Field(default=1000, ge=1)
    definition_assessment_document_concurrency: int = Field(default=8, ge=1, le=32)
    definition_assessment_map_max_characters: int = Field(default=60_000, ge=10_000, le=500_000)
    definition_assessment_provider_batch_size: int = Field(default=50, ge=1, le=100)
    definition_assessment_provider_batch_poll_seconds: float = Field(default=15.0, ge=1, le=300)

    jwt_secret: str = Field(default="development-only-change-me-please", min_length=32)
    jwt_issuer: str = "priv-view-core"
    jwt_audience: str = "priv-view-api"
    access_token_minutes: int = Field(default=15, ge=1, le=1440)
    refresh_token_days: int = Field(default=14, ge=1, le=90)
    login_max_failed_attempts: int = Field(default=5, ge=1, le=20)
    login_lockout_minutes: int = Field(default=15, ge=1, le=1440)

    bootstrap_root_tenant_name: str = "Priv-View Root"
    bootstrap_root_tenant_slug: str = "root"
    bootstrap_superuser_email: str | None = None
    bootstrap_superuser_password: str | None = None
    bootstrap_superuser_display_name: str = "Super Admin"

    @model_validator(mode="after")
    def validate_chunking(self) -> "Settings":
        if self.chunk_target_characters > self.chunk_max_characters:
            raise ValueError("CHUNK_TARGET_CHARACTERS must not exceed CHUNK_MAX_CHARACTERS")
        if self.chunk_overlap_characters >= self.chunk_target_characters:
            raise ValueError("CHUNK_OVERLAP_CHARACTERS must be smaller than CHUNK_TARGET_CHARACTERS")
        if self.model_rate_limit_retry_base_seconds > self.model_rate_limit_retry_max_seconds:
            raise ValueError("MODEL_RATE_LIMIT_RETRY_BASE_SECONDS must not exceed MODEL_RATE_LIMIT_RETRY_MAX_SECONDS")
        if self.agent_stream_replay_page_size > self.agent_stream_replay_limit:
            raise ValueError("AGENT_STREAM_REPLAY_PAGE_SIZE must not exceed AGENT_STREAM_REPLAY_LIMIT")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
