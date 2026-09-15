from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from artifact_service.config import get_artifact_settings


class ArtifactBase(DeclarativeBase):
    pass


settings = get_artifact_settings()
artifact_engine = create_engine(settings.database_url, pool_pre_ping=True)
ArtifactSessionLocal = sessionmaker(bind=artifact_engine, autoflush=False, expire_on_commit=False)


def get_artifact_db() -> Generator[Session, None, None]:
    with ArtifactSessionLocal() as session:
        yield session
