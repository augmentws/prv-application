import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.config import Settings, get_settings
from app.database import get_db
from app.dependencies import Principal, get_principal, unauthorized
from app.models import AuthSession, User
from app.schemas import (
    LoginRequest,
    RefreshRequest,
    TokenPair,
    UserRead,
    normalize_email,
)
from app.security import (
    TokenError,
    decode_token,
    hash_jti,
    mint_access_token,
    mint_refresh_token,
    password_hasher,
    verify_password,
)

router = APIRouter(prefix="/v1/auth", tags=["authentication"])
DUMMY_PASSWORD_HASH = password_hasher.hash("not-a-real-user-password")


def token_pair_for(user: User, session_id: uuid.UUID, settings: Settings) -> tuple[TokenPair, str, datetime]:
    access_token, _ = mint_access_token(
        user_id=user.id, tenant_id=user.tenant_id, session_id=session_id, settings=settings
    )
    refresh_token, refresh_jti, refresh_expires_at = mint_refresh_token(
        user_id=user.id, tenant_id=user.tenant_id, session_id=session_id, settings=settings
    )
    return (
        TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_in=settings.access_token_minutes * 60,
            refresh_expires_in=settings.refresh_token_days * 86400,
        ),
        refresh_jti,
        refresh_expires_at,
    )


@router.post("/login", response_model=TokenPair)
def login(payload: LoginRequest, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> TokenPair:
    matching_users = list(
        db.scalars(
            select(User)
            .where(User.normalized_email == normalize_email(str(payload.email)))
            .limit(2)
        )
    )
    # The database enforces global email uniqueness. Treat legacy ambiguous data as
    # invalid credentials rather than guessing a tenant before that constraint is applied.
    user = matching_users[0] if len(matching_users) == 1 else None
    tenant = user.tenant if user is not None else None

    credential = user.password_credential if user is not None else None
    candidate_hash = credential.password_hash if credential is not None else DUMMY_PASSWORD_HASH
    password_valid = verify_password(payload.password, candidate_hash)
    now = datetime.now(timezone.utc)

    if credential is not None and credential.locked_until is not None:
        locked_until = credential.locked_until
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if locked_until > now:
            password_valid = False

    if (
        user is None
        or tenant is None
        or credential is None
        or not password_valid
        or tenant.status != "ACTIVE"
        or user.status != "ACTIVE"
    ):
        if credential is not None:
            credential.failed_attempt_count += 1
            if credential.failed_attempt_count >= settings.login_max_failed_attempts:
                credential.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
                credential.failed_attempt_count = 0
            db.commit()
        raise unauthorized("Invalid credentials")

    credential.failed_attempt_count = 0
    credential.locked_until = None
    user.last_authenticated_at = now
    session_id = uuid.uuid4()
    pair, refresh_jti, refresh_expires_at = token_pair_for(user, session_id, settings)
    db.add(
        AuthSession(
            id=session_id,
            user_id=user.id,
            refresh_jti_hash=hash_jti(refresh_jti),
            expires_at=refresh_expires_at,
            last_seen_at=now,
        )
    )
    record_audit(
        db,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="auth.login",
        target_type="auth_session",
        target_id=session_id,
    )
    db.commit()
    return pair


@router.post("/refresh", response_model=TokenPair)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> TokenPair:
    try:
        claims = decode_token(payload.refresh_token, "refresh", settings)
    except TokenError as exc:
        raise unauthorized(str(exc)) from exc

    session = db.scalar(select(AuthSession).where(AuthSession.id == claims.session_id))
    user = db.scalar(select(User).where(User.id == claims.user_id))
    if (
        session is None
        or user is None
        or session.user_id != user.id
        or session.revoked_at is not None
        or user.status != "ACTIVE"
        or user.tenant.status != "ACTIVE"
        or session.refresh_jti_hash != hash_jti(claims.jti)
    ):
        raise unauthorized("Refresh session is no longer active")

    pair, refresh_jti, refresh_expires_at = token_pair_for(user, session.id, settings)
    session.refresh_jti_hash = hash_jti(refresh_jti)
    session.expires_at = refresh_expires_at
    session.last_seen_at = datetime.now(timezone.utc)
    db.commit()
    return pair


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> None:
    principal.session.revoked_at = datetime.now(timezone.utc)
    record_audit(
        db,
        tenant_id=principal.user.tenant_id,
        actor_user_id=principal.user.id,
        action="auth.logout",
        target_type="auth_session",
        target_id=principal.session.id,
    )
    db.commit()


@router.get("/me", response_model=UserRead)
def me(principal: Principal = Depends(get_principal)) -> User:
    return principal.user
