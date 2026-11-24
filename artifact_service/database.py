from collections.abc import Generator

from sqlalchemy.orm import DeclarativeBase, Session

from app.database_engine import DatabaseEngineProfile, create_service_engine, create_service_sessionmaker
from artifact_service.config import get_artifact_settings


class ArtifactBase(DeclarativeBase):
    pass


settings = get_artifact_settings()
profile = DatabaseEngineProfile(
    name="artifact-api",
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_timeout_seconds=settings.database_pool_timeout_seconds,
    pool_recycle_seconds=settings.database_pool_recycle_seconds,
    application_name="priv-view-artifact-api",
    statement_timeout_seconds=settings.database_statement_timeout_seconds,
    idle_transaction_timeout_seconds=settings.database_idle_transaction_timeout_seconds,
    checkout_warning_seconds=settings.database_checkout_warning_seconds,
    transaction_warning_seconds=settings.database_transaction_warning_seconds,
)
artifact_engine = create_service_engine(
    settings.database_url,
    profile,
)
ArtifactSessionLocal = create_service_sessionmaker(artifact_engine, profile)


def get_artifact_db() -> Generator[Session, None, None]:
    with ArtifactSessionLocal() as session:
        yield session
