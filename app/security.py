import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import jwt
from jwt import InvalidTokenError
from pwdlib import PasswordHash

from app.config import Settings

password_hasher = PasswordHash.recommended()


class TokenError(ValueError):
    pass


@dataclass(frozen=True)
class TokenClaims:
    token_type: Literal["access", "refresh"]
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    jti: str
    expires_at: datetime


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


def hash_jti(jti: str) -> str:
    return hashlib.sha256(jti.encode("utf-8")).hexdigest()


def _mint_token(
    *,
    token_type: Literal["access", "refresh"],
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    lifetime: timedelta,
    settings: Settings,
) -> tuple[str, str, datetime]:
    now = datetime.now(timezone.utc)
    expires_at = now + lifetime
    jti = secrets.token_urlsafe(32)
    payload: dict[str, Any] = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(user_id),
        "tid": str(tenant_id),
        "sid": str(session_id),
        "jti": jti,
        "type": token_type,
        "iat": now,
        "exp": expires_at,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return token, jti, expires_at


def mint_access_token(
    *, user_id: uuid.UUID, tenant_id: uuid.UUID, session_id: uuid.UUID, settings: Settings
) -> tuple[str, datetime]:
    token, _, expires_at = _mint_token(
        token_type="access",
        user_id=user_id,
        tenant_id=tenant_id,
        session_id=session_id,
        lifetime=timedelta(minutes=settings.access_token_minutes),
        settings=settings,
    )
    return token, expires_at


def mint_refresh_token(
    *, user_id: uuid.UUID, tenant_id: uuid.UUID, session_id: uuid.UUID, settings: Settings
) -> tuple[str, str, datetime]:
    return _mint_token(
        token_type="refresh",
        user_id=user_id,
        tenant_id=tenant_id,
        session_id=session_id,
        lifetime=timedelta(days=settings.refresh_token_days),
        settings=settings,
    )


def decode_token(token: str, expected_type: Literal["access", "refresh"], settings: Settings) -> TokenClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["exp", "iat", "sub", "tid", "sid", "jti", "type"]},
        )
        if payload["type"] != expected_type:
            raise TokenError("Incorrect token type")
        return TokenClaims(
            token_type=payload["type"],
            user_id=uuid.UUID(payload["sub"]),
            tenant_id=uuid.UUID(payload["tid"]),
            session_id=uuid.UUID(payload["sid"]),
            jti=payload["jti"],
            expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        )
    except (InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        raise TokenError("Invalid or expired token") from exc
