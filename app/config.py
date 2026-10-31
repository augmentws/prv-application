from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.database_urls import build_postgres_url
from app.deployment_policy import AppEnvironment, reject_development_secrets


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Priv-View Core"
    app_env: AppEnvironment = "production"
    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_user: str = Field(default="postgres", min_length=1)
    postgres_password: str = Field(min_length=1)
    postgres_core_database: str = Field(default="pvr-core", min_length=1)
    # Optional full-URL override, used by the test suite; normally unset.
    database_url_override: str | None = Field(default=None, validation_alias="DATABASE_URL")
    artifact_mode: str = "embedded"
    artifact_base_url: str = "http://localhost:8001"
    classification_mode: Literal["embedded", "remote"] = "embedded"
    classification_base_url: str = "http://localhost:8003"
    classification_service_token: str = Field(min_length=16)
    classification_request_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    classification_max_concurrency: int = Field(default=2, ge=1, le=64)
    classification_feature_load_concurrency: int = Field(default=16, ge=1, le=64)
    classification_runtime_image_digest: str = Field(default="development-runtime", min_length=1, max_length=500)
    classification_source_revision: str = Field(default="development", min_length=1, max_length=255)
    classification_package_max_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1024,
        le=2 * 1024 * 1024 * 1024,
    )
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
    search_export_page_size: int = Field(default=1000, ge=100, le=5000)
    search_export_retention_days: int = Field(default=7, ge=1, le=365)
    search_export_retry_hours: int = Field(default=24, ge=1, le=168)
    search_export_max_columns: int = Field(default=250, ge=1, le=1000)
    search_export_max_bytes: int = Field(default=20 * 1024**3, ge=1024, le=20 * 1024**3)
    fact_export_retention_days: int = Field(default=7, ge=1, le=365)
    # Exports are written within the request, so the cap keeps one export to a few minutes at most.
    fact_export_max_bytes: int = Field(default=500 * 1024**2, ge=1024, le=5 * 1024**3)
    fact_development_chunk_size: int = Field(default=50, ge=1, le=1_000)
    fact_development_document_concurrency: int = Field(default=4, ge=1, le=64)
    fact_development_max_document_characters: int = Field(default=200_000, ge=1_000, le=2_000_000)
    fact_development_recall_window_days: int = Field(default=365, ge=1, le=36_500)
    fact_development_chunk_window: int = Field(default=4, ge=1, le=256)
    fact_development_provider_batch_poll_seconds: float = Field(default=60.0, ge=1, le=3600)
    # Batch API runs that may wait on provider batches at once; each has one chunk in flight.
    fact_development_batch_run_concurrency: int = Field(default=8, ge=1, le=256)
    fact_duplicate_threshold: float = Field(default=0.85, gt=0, le=1)
    fact_merge_suggestion_threshold: float = Field(default=0.9, gt=0, le=1)
    agent_default_model: str | None = None
    agent_turn_queue_concurrency: int = Field(default=4, ge=1, le=100)
    agent_approval_timeout_seconds: int = Field(default=7 * 24 * 60 * 60, ge=60, le=30 * 24 * 60 * 60)
    agent_batch_chat_document_max_characters: int = Field(default=20_000, ge=1_000, le=2_000_000)
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
    # DBOS-global limits for question-answering orchestration. Provider-specific
    # concurrency and rate limits are applied separately inside each decision engine.
    qa_batch_concurrency: int = Field(default=2, ge=1, le=64)
    qa_document_concurrency: int = Field(default=12, ge=1, le=256)
    typesafe_api_key: str | None = None
    typesafe_base_url: str | None = None
    typesafe_default_model: str = "jev-latest"
    typesafe_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    typesafe_concurrency: int = Field(default=4, ge=1, le=100)
    typesafe_requests_per_minute: int = Field(default=15, ge=1, le=100_000)
    typesafe_input_tokens_per_minute: int = Field(default=200_000, ge=1, le=1_000_000_000)
    typesafe_max_retries: int = Field(default=5, ge=0, le=20)
    typesafe_retry_base_seconds: float = Field(default=1.0, gt=0, le=300)
    typesafe_retry_max_seconds: float = Field(default=120.0, gt=0, le=3600)
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_decisions_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    openai_decisions_concurrency: int = Field(default=4, ge=1, le=100)
    openai_decisions_requests_per_minute: int = Field(default=60, ge=1, le=100_000)
    openai_decisions_input_tokens_per_minute: int = Field(default=500_000, ge=1, le=1_000_000_000)
    openai_decisions_max_retries: int = Field(default=5, ge=0, le=20)
    openai_decisions_retry_base_seconds: float = Field(default=1.0, gt=0, le=300)
    openai_decisions_retry_max_seconds: float = Field(default=120.0, gt=0, le=3600)
    analysis_task_decision_max_characters: int = Field(default=100_000, ge=10_000, le=2_000_000)
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
    definition_assessment_provider_batch_concurrency: int = Field(default=8, ge=1, le=64)
    definition_assessment_provider_batch_poll_seconds: float = Field(default=15.0, ge=1, le=300)

    jwt_secret: str = Field(min_length=32)
    jwt_issuer: str = "priv-view-core"
    jwt_audience: str = "priv-view-api"
    access_token_minutes: int = Field(default=15, ge=1, le=1440)
    refresh_token_days: int = Field(default=14, ge=1, le=90)
    # A rotated refresh token presented within this window is a benign race, not theft.
    refresh_reuse_grace_seconds: int = Field(default=30, ge=0, le=300)
    login_max_failed_attempts: int = Field(default=5, ge=1, le=20)
    login_lockout_minutes: int = Field(default=15, ge=1, le=1440)

    bootstrap_root_tenant_name: str = "Priv-View Root"
    bootstrap_root_tenant_slug: str = "root"
    bootstrap_superuser_email: str | None = None
    bootstrap_superuser_password: str | None = None
    bootstrap_superuser_display_name: str = "Super Admin"

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return build_postgres_url(
            user=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_core_database,
        )

    @model_validator(mode="after")
    def validate_deployment_secrets(self) -> "Settings":
        reject_development_secrets(
            self.app_env,
            {"JWT_SECRET": self.jwt_secret, "CLASSIFICATION_SERVICE_TOKEN": self.classification_service_token},
        )
        return self

    @model_validator(mode="after")
    def validate_chunking(self) -> "Settings":
        if self.chunk_target_characters > self.chunk_max_characters:
            raise ValueError("CHUNK_TARGET_CHARACTERS must not exceed CHUNK_MAX_CHARACTERS")
        if self.chunk_overlap_characters >= self.chunk_target_characters:
            raise ValueError("CHUNK_OVERLAP_CHARACTERS must be smaller than CHUNK_TARGET_CHARACTERS")
        if self.model_rate_limit_retry_base_seconds > self.model_rate_limit_retry_max_seconds:
            raise ValueError("MODEL_RATE_LIMIT_RETRY_BASE_SECONDS must not exceed MODEL_RATE_LIMIT_RETRY_MAX_SECONDS")
        if self.typesafe_retry_base_seconds > self.typesafe_retry_max_seconds:
            raise ValueError("TYPESAFE_RETRY_BASE_SECONDS must not exceed TYPESAFE_RETRY_MAX_SECONDS")
        if self.openai_decisions_retry_base_seconds > self.openai_decisions_retry_max_seconds:
            raise ValueError(
                "OPENAI_DECISIONS_RETRY_BASE_SECONDS must not exceed OPENAI_DECISIONS_RETRY_MAX_SECONDS"
            )
        if self.agent_stream_replay_page_size > self.agent_stream_replay_limit:
            raise ValueError("AGENT_STREAM_REPLAY_PAGE_SIZE must not exceed AGENT_STREAM_REPLAY_LIMIT")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
