from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuthSession

ROOT_LOGIN = {"email": "root@example.com", "password": "correct-horse-battery-staple"}


def _login_and_rotate(client: TestClient) -> tuple[str, str]:
    """Log in, rotate once, and return (original refresh token, rotated refresh token)."""

    original = client.post("/v1/auth/login", json=ROOT_LOGIN).json()["refresh_token"]
    rotated_response = client.post("/v1/auth/refresh", json={"refresh_token": original})
    assert rotated_response.status_code == 200, rotated_response.text
    return original, rotated_response.json()["refresh_token"]


def _backdate_rotation(db: Session, hours: int) -> None:
    for session in db.scalars(select(AuthSession)).all():
        session.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=hours)
    db.commit()


def test_stale_token_within_grace_is_rejected_without_revoking_session(client: TestClient, root_admin) -> None:
    original, rotated = _login_and_rotate(client)

    replay = client.post("/v1/auth/refresh", json={"refresh_token": original})
    assert replay.status_code == 401

    still_valid = client.post("/v1/auth/refresh", json={"refresh_token": rotated})
    assert still_valid.status_code == 200, still_valid.text


def test_stale_token_after_grace_revokes_the_session(client: TestClient, root_admin, db: Session) -> None:
    original, rotated = _login_and_rotate(client)
    _backdate_rotation(db, hours=1)

    replay = client.post("/v1/auth/refresh", json={"refresh_token": original})
    assert replay.status_code == 401

    # The legitimate holder of the rotated token is signed out too, because the session is revoked.
    after_revocation = client.post("/v1/auth/refresh", json={"refresh_token": rotated})
    assert after_revocation.status_code == 401
    db.expire_all()
    assert all(session.revoked_at is not None for session in db.scalars(select(AuthSession)).all())
