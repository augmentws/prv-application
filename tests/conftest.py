import os

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("ARTIFACT_DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-jwt-signing")
os.environ.setdefault("DBOS_ENABLED", "false")
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import PasswordCredential, Tenant, User
from app.schemas import normalize_email
from app.security import hash_password
from artifact_service.database import ArtifactBase, get_artifact_db
from artifact_service.main import app as standalone_artifact_app
from artifact_service.storage import MemoryBlobStorage, get_storage

TEST_DATABASE_URL = "sqlite+pysqlite:///:memory:"
engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
artifact_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
ArtifactTestingSessionLocal = sessionmaker(bind=artifact_engine, autoflush=False, expire_on_commit=False)
memory_storage = MemoryBlobStorage()


@pytest.fixture(autouse=True)
def reset_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    ArtifactBase.metadata.drop_all(bind=artifact_engine)
    ArtifactBase.metadata.create_all(bind=artifact_engine)
    memory_storage.buckets.clear()
    yield
    ArtifactBase.metadata.drop_all(bind=artifact_engine)
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db() -> Session:
    with TestingSessionLocal() as session:
        yield session


@pytest.fixture
def root_admin(db: Session) -> User:
    root = Tenant(slug="root", name="Priv-View Root", status="ACTIVE", is_root=True)
    db.add(root)
    db.flush()
    user = User(
        tenant_id=root.id,
        email="root@example.com",
        normalized_email=normalize_email("root@example.com"),
        display_name="Root Admin",
        status="ACTIVE",
        tenant_role="ADMIN",
        is_superuser=True,
    )
    db.add(user)
    db.flush()
    db.add(PasswordCredential(user_id=user.id, password_hash=hash_password("correct-horse-battery-staple")))
    db.commit()
    return user


@pytest.fixture
def client() -> TestClient:
    def override_get_db():
        with TestingSessionLocal() as session:
            yield session

    def override_get_artifact_db():
        with ArtifactTestingSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_artifact_db] = override_get_artifact_db
    app.dependency_overrides[get_storage] = lambda: memory_storage
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def artifact_api_client() -> TestClient:
    def override_get_artifact_db():
        with ArtifactTestingSessionLocal() as session:
            yield session

    standalone_artifact_app.dependency_overrides[get_artifact_db] = override_get_artifact_db
    standalone_artifact_app.dependency_overrides[get_storage] = lambda: memory_storage
    with TestClient(standalone_artifact_app) as test_client:
        yield test_client
    standalone_artifact_app.dependency_overrides.clear()


@pytest.fixture
def root_token(client: TestClient, root_admin: User) -> str:
    response = client.post(
        "/v1/auth/login",
        json={
            "email": "root@example.com",
            "password": "correct-horse-battery-staple",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]
