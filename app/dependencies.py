import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import (
    AuthSession,
    Client,
    ClientMembership,
    Matter,
    MatterMembership,
    User,
)
from app.security import TokenError, decode_token

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user: User
    session: AuthSession

    @property
    def is_root_admin(self) -> bool:
        return self.user.tenant.is_root and self.user.tenant_role == "ADMIN"


def unauthorized(message: str = "Authentication required") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=message, headers={"WWW-Authenticate": "Bearer"})


def get_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Principal:
    if credentials is None:
        raise unauthorized()
    try:
        claims = decode_token(credentials.credentials, "access", settings)
    except TokenError as exc:
        raise unauthorized(str(exc)) from exc

    user = db.scalar(select(User).where(User.id == claims.user_id))
    session = db.scalar(select(AuthSession).where(AuthSession.id == claims.session_id))
    if (
        user is None
        or session is None
        or session.user_id != user.id
        or session.revoked_at is not None
        or user.status != "ACTIVE"
        or user.tenant.status != "ACTIVE"
        or user.tenant_id != claims.tenant_id
    ):
        raise unauthorized("Session is no longer active")
    return Principal(user=user, session=session)


def require_root_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_root_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Root tenant ADMIN required")
    return principal


def can_admin_tenant(principal: Principal, tenant_id: uuid.UUID) -> bool:
    return principal.is_root_admin or (
        principal.user.tenant_id == tenant_id and principal.user.tenant_role == "ADMIN"
    )


def can_admin_client(db: Session, principal: Principal, client: Client) -> bool:
    if can_admin_tenant(principal, client.tenant_id):
        return True
    membership = db.scalar(
        select(ClientMembership).where(
            ClientMembership.client_id == client.id,
            ClientMembership.user_id == principal.user.id,
            ClientMembership.role == "ADMIN",
        )
    )
    return membership is not None


def can_admin_matter(db: Session, principal: Principal, matter: Matter) -> bool:
    if can_admin_client(db, principal, matter.client):
        return True
    membership = db.scalar(
        select(MatterMembership).where(
            MatterMembership.matter_id == matter.id,
            MatterMembership.user_id == principal.user.id,
            MatterMembership.role == "ADMIN",
        )
    )
    return membership is not None
