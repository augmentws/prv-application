import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.database import get_db
from app.dependencies import Principal, can_admin_client, can_admin_tenant, get_principal
from app.models import Client, ClientMembership, Custodian, Tenant
from app.schemas import ClientCreate, ClientRead, CustodianCreate, CustodianRead

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
    if not can_admin_tenant(principal, tenant_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant ADMIN required")
    return list(db.scalars(select(Client).where(Client.tenant_id == tenant_id).order_by(Client.name)))


@router.get("/clients/{client_id}", response_model=ClientRead)
def get_client(
    client_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Client:
    client = db.scalar(select(Client).where(Client.id == client_id))
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    if not can_admin_client(db, principal, client):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client ADMIN required")
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
    if not can_admin_client(db, principal, client):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client ADMIN required")

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
    if not can_admin_client(db, principal, client):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client ADMIN required")
    return list(
        db.scalars(
            select(Custodian).where(Custodian.client_id == client.id).order_by(Custodian.display_name)
        )
    )
