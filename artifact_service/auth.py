import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import Principal, get_principal
from app.models import Client, ClientMembership, Custodian, Tenant
from artifact_service.config import ArtifactSettings, get_artifact_settings


@dataclass(frozen=True)
class ArtifactPrincipal:
    actor_user_id: uuid.UUID
    allowed_tenant_ids: frozenset[uuid.UUID]
    allowed_clients: frozenset[tuple[uuid.UUID, uuid.UUID]]
    allowed_custodians: frozenset[tuple[uuid.UUID, uuid.UUID]]

    def can_access_tenant(self, tenant_id: uuid.UUID) -> bool:
        return tenant_id in self.allowed_tenant_ids

    def can_access_client(self, tenant_id: uuid.UUID, client_id: uuid.UUID) -> bool:
        return (tenant_id, client_id) in self.allowed_clients

    def can_reference_custodian(self, client_id: uuid.UUID, custodian_id: uuid.UUID) -> bool:
        return (client_id, custodian_id) in self.allowed_custodians


def get_embedded_artifact_principal(
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> ArtifactPrincipal:
    if principal.is_root_admin:
        tenant_ids = frozenset(db.scalars(select(Tenant.id)))
        client_pairs = frozenset(db.execute(select(Client.tenant_id, Client.id)).all())
    elif principal.user.tenant_role == "ADMIN":
        tenant_ids = frozenset({principal.user.tenant_id})
        client_pairs = frozenset(
            db.execute(
                select(Client.tenant_id, Client.id).where(Client.tenant_id == principal.user.tenant_id)
            ).all()
        )
    else:
        tenant_ids = frozenset({principal.user.tenant_id})
        client_pairs = frozenset(
            db.execute(
                select(Client.tenant_id, Client.id)
                .join(ClientMembership, ClientMembership.client_id == Client.id)
                .where(ClientMembership.user_id == principal.user.id)
            ).all()
        )
    accessible_client_ids = [client_id for _, client_id in client_pairs]
    custodian_pairs = frozenset(
        db.execute(
            select(Custodian.client_id, Custodian.id).where(
                Custodian.client_id.in_(accessible_client_ids),
                Custodian.status == "ACTIVE",
            )
        ).all()
    )
    return ArtifactPrincipal(
        actor_user_id=principal.user.id,
        allowed_tenant_ids=tenant_ids,
        allowed_clients=client_pairs,
        allowed_custodians=custodian_pairs,
    )


delegation_bearer = HTTPBearer(auto_error=False)


def get_delegated_artifact_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(delegation_bearer),
    settings: ArtifactSettings = Depends(get_artifact_settings),
) -> ArtifactPrincipal:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Artifact delegation token required")
    try:
        claims = jwt.decode(
            credentials.credentials,
            settings.delegation_secret,
            algorithms=["HS256"],
            issuer=settings.delegation_issuer,
            audience=settings.delegation_audience,
            options={"require": ["exp", "iat", "sub", "tenants", "clients", "custodians"]},
        )
        if datetime.fromtimestamp(claims["exp"], tz=timezone.utc) <= datetime.now(timezone.utc):
            raise InvalidTokenError("Expired token")
        tenant_ids = frozenset(uuid.UUID(value) for value in claims["tenants"])
        client_pairs = frozenset((uuid.UUID(pair[0]), uuid.UUID(pair[1])) for pair in claims["clients"])
        custodian_pairs = frozenset(
            (uuid.UUID(pair[0]), uuid.UUID(pair[1])) for pair in claims["custodians"]
        )
        return ArtifactPrincipal(
            actor_user_id=uuid.UUID(claims["sub"]),
            allowed_tenant_ids=tenant_ids,
            allowed_clients=client_pairs,
            allowed_custodians=custodian_pairs,
        )
    except (InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid artifact delegation token") from exc


def mint_artifact_delegation(
    principal: ArtifactPrincipal,
    settings: ArtifactSettings,
    lifetime: timedelta = timedelta(minutes=5),
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "iss": settings.delegation_issuer,
        "aud": settings.delegation_audience,
        "sub": str(principal.actor_user_id),
        "tenants": [str(tenant_id) for tenant_id in sorted(principal.allowed_tenant_ids, key=str)],
        "clients": [
            [str(tenant_id), str(client_id)]
            for tenant_id, client_id in sorted(principal.allowed_clients, key=lambda pair: (str(pair[0]), str(pair[1])))
        ],
        "custodians": [
            [str(client_id), str(custodian_id)]
            for client_id, custodian_id in sorted(
                principal.allowed_custodians,
                key=lambda pair: (str(pair[0]), str(pair[1])),
            )
        ],
        "iat": now,
        "exp": now + lifetime,
    }
    return jwt.encode(payload, settings.delegation_secret, algorithm="HS256")


def require_tenant(principal: ArtifactPrincipal, tenant_id: uuid.UUID) -> None:
    if not principal.can_access_tenant(tenant_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant artifact access denied")


def require_client(principal: ArtifactPrincipal, tenant_id: uuid.UUID, client_id: uuid.UUID) -> None:
    if not principal.can_access_client(tenant_id, client_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client artifact access denied")
