from collections.abc import Generator

from sqlalchemy.orm import DeclarativeBase, Session

from app.config import get_settings
from app.database_engine import DatabaseEngineProfile, create_service_engine, create_service_sessionmaker


class Base(DeclarativeBase):
    pass


settings = get_settings()
profile = DatabaseEngineProfile(
    name=f"core-{settings.database_pool_profile}",
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_timeout_seconds=settings.database_pool_timeout_seconds,
    pool_recycle_seconds=settings.database_pool_recycle_seconds,
    application_name=settings.database_application_name,
    statement_timeout_seconds=settings.database_statement_timeout_seconds,
    idle_transaction_timeout_seconds=settings.database_idle_transaction_timeout_seconds,
    checkout_warning_seconds=settings.database_checkout_warning_seconds,
    transaction_warning_seconds=settings.database_transaction_warning_seconds,
)
engine = create_service_engine(
    settings.database_url,
    profile,
)
SessionLocal = create_service_sessionmaker(engine, profile)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
