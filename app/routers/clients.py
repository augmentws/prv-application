import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.authorization import ResourceRef, can, increment_authorization_revision, user_can, visible_client_ids
from app.database import get_db
from app.dependencies import Principal, can_admin_tenant, get_principal
from app.models import (
    AuthorizationGrant,
    Client,
    ClientMembership,
    Custodian,
    Matter,
    MatterDocument,
    MatterDocumentCustodian,
    Tenant,
    User,
)
from app.rbac_catalog import ensure_rbac_catalog
from app.rbac_manifest import SYSTEM_ROLE_BY_KEY
from app.schemas import ClientCreate, ClientRead, CustodianCreate, CustodianRead, UserRead

router = APIRouter(prefix="/v1", tags=["clients"])


@router.post("/tenants/{tenant_id}/clients", response_model=ClientRead, status_code=status.HTTP_201_CREATED)
def create_client(
    tenant_id: uuid.UUID,
    payload: ClientCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Client:
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if tenant.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant is not active")
    if not can_admin_tenant(principal, tenant.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant ADMIN required")

    client = Client(tenant_id=tenant.id, name=payload.name, status="ACTIVE")
    db.add(client)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Client name already exists in tenant") from exc
    if principal.user.tenant_id == tenant.id:
        db.add(ClientMembership(client_id=client.id, user_id=principal.user.id, role="ADMIN"))
        ensure_rbac_catalog(db)
        client_admin_role = SYSTEM_ROLE_BY_KEY["CLIENT_ADMINISTRATOR"]
        grant = AuthorizationGrant(
            tenant_id=tenant.id,
            user_id=principal.user.id,
            role_id=client_admin_role.id,
            scope_type="CLIENT",
            client_id=client.id,
            created_by_user_id=principal.user.id,
            updated_by_user_id=principal.user.id,
        )
        db.add(grant)
        db.flush()
        revision_before, revision_after = increment_authorization_revision(db, tenant.id)
        record_audit(
            db,
            tenant_id=tenant.id,
            actor_user_id=principal.user.id,
            action="authorization.grant.created",
            target_type="authorization_grant",
            target_id=grant.id,
            details={
                "subject_type": "USER",
                "subject_id": str(principal.user.id),
                "role_id": str(client_admin_role.id),
                "scope_type": "CLIENT",
                "scope_id": str(client.id),
                "authorization_revision_before": revision_before,
                "authorization_revision_after": revision_after,
            },
        )
    record_audit(
        db,
        tenant_id=tenant.id,
        actor_user_id=principal.user.id,
        action="client.created",
        target_type="client",
        target_id=client.id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Client name already exists in tenant") from exc
    return client


@router.get("/tenants/{tenant_id}/clients", response_model=list[ClientRead])
def list_clients(
    tenant_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[Client]:
    if not principal.is_root_admin and principal.user.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant access required")
    client_ids = visible_client_ids(db, principal, tenant_id)
    if not client_ids:
        return []
    return list(
        db.scalars(
            select(Client).where(Client.id.in_(client_ids)).order_by(Client.name)
        )
    )


@router.get("/clients/{client_id}", response_model=ClientRead)
def get_client(
    client_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Client:
    client = db.scalar(select(Client).where(Client.id == client_id))
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    if client.id not in visible_client_ids(db, principal, client.tenant_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    return client


@router.post("/clients/{client_id}/custodians", response_model=CustodianRead, status_code=status.HTTP_201_CREATED)
def create_custodian(
    client_id: uuid.UUID,
    payload: CustodianCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Custodian:
    client = db.scalar(select(Client).where(Client.id == client_id))
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    if client.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Client is not active")
    if not can(db, principal, "client.manage_custodians", ResourceRef.client(client.id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission required: client.manage_custodians")

    custodian = Custodian(
        client_id=client.id,
        display_name=payload.display_name.strip(),
        normalized_name=" ".join(payload.display_name.casefold().split()),
        email_addresses=[str(address).casefold() for address in payload.email_addresses],
        external_reference=payload.external_reference,
        status="ACTIVE",
    )
    db.add(custodian)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Custodian name already exists in client",
        ) from exc
    record_audit(
        db,
        tenant_id=client.tenant_id,
        actor_user_id=principal.user.id,
        action="custodian.created",
        target_type="custodian",
        target_id=custodian.id,
        details={"client_id": str(client.id)},
    )
    db.commit()
    return custodian


@router.get("/clients/{client_id}/custodians", response_model=list[CustodianRead])
def list_custodians(
    client_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[Custodian]:
    client = db.scalar(select(Client).where(Client.id == client_id))
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    if not can(db, principal, "client.view", ResourceRef.client(client.id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission required: client.view")
    return list(
        db.scalars(
            select(Custodian).where(Custodian.client_id == client.id).order_by(Custodian.display_name)
        )
    )


@router.get("/matters/{matter_id}/custodians", response_model=list[CustodianRead])
def list_matter_custodians(
    matter_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[Custodian]:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can(db, principal, "document.view", ResourceRef.matter(matter.id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission required: document.view")
    return list(
        db.scalars(
            select(Custodian)
            .where(
                exists()
                .where(MatterDocumentCustodian.custodian_id == Custodian.id)
                .where(MatterDocument.id == MatterDocumentCustodian.matter_document_id)
                .where(MatterDocument.matter_id == matter.id)
            )
            .order_by(Custodian.display_name)
        )
    )


@router.get("/matters/{matter_id}/access-users", response_model=list[UserRead])
def list_matter_access_users(
    matter_id: uuid.UUID,
    permission_key: Literal["document.view", "batch.review"] = Query(default="document.view"),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[User]:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can(db, principal, "matter.view", ResourceRef.matter(matter.id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission required: matter.view")
    users = list(
        db.scalars(
            select(User)
            .where(User.tenant_id == matter.client.tenant_id, User.status == "ACTIVE")
            .order_by(User.display_name, User.email)
        )
    )
    return [
        user
        for user in users
        if user_can(db, user, permission_key, ResourceRef.matter(matter.id))
    ]
