import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.authorization import legacy_or_can_manage_tenant
from app.database import get_db
from app.dependencies import (
    Principal,
    get_principal,
    require_root_admin,
)
from app.models import AuthorizationGrant, PasswordCredential, Tenant, User
from app.rbac_catalog import ensure_rbac_catalog
from app.rbac_manifest import SYSTEM_ROLE_BY_KEY
from app.schemas import (
    TenantCreate,
    TenantCreated,
    TenantRead,
    UserCreate,
    UserRead,
    normalize_email,
)
from app.security import hash_password

router = APIRouter(prefix="/v1/tenants", tags=["tenants"])


def conflict(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)


@router.get("", response_model=list[TenantRead])
def list_tenants(
    principal: Principal = Depends(require_root_admin),
    db: Session = Depends(get_db),
) -> list[Tenant]:
    return list(db.scalars(select(Tenant).order_by(Tenant.is_root.desc(), Tenant.name)))


@router.post("", response_model=TenantCreated, status_code=status.HTTP_201_CREATED)
def create_tenant(
    payload: TenantCreate,
    principal: Principal = Depends(require_root_admin),
    db: Session = Depends(get_db),
) -> TenantCreated:
    parent = db.scalar(select(Tenant).where(Tenant.id == payload.parent_tenant_id))
    if parent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parent tenant not found")
    if parent.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Parent tenant is not active")

    tenant = Tenant(parent_tenant_id=parent.id, slug=payload.slug, name=payload.name, status="ACTIVE", is_root=False)
    db.add(tenant)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise conflict("Tenant slug already exists") from exc
    admin = User(
        tenant_id=tenant.id,
        email=str(payload.initial_admin.email),
        normalized_email=normalize_email(str(payload.initial_admin.email)),
        display_name=payload.initial_admin.display_name,
        status="ACTIVE",
        tenant_role="ADMIN",
    )
    db.add(admin)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise conflict("A user with that email already exists") from exc
    db.add(PasswordCredential(user_id=admin.id, password_hash=hash_password(payload.initial_admin.password)))
    ensure_rbac_catalog(db)
    tenant_admin_role = SYSTEM_ROLE_BY_KEY["TENANT_ADMINISTRATOR"]
    initial_grant = AuthorizationGrant(
        tenant_id=tenant.id,
        user_id=admin.id,
        role_id=tenant_admin_role.id,
        scope_type="TENANT",
        tenant_scope_id=tenant.id,
        created_by_user_id=principal.user.id,
        updated_by_user_id=principal.user.id,
    )
    db.add(initial_grant)
    db.flush()
    tenant.authorization_revision += 1
    record_audit(
        db,
        tenant_id=tenant.id,
        actor_user_id=principal.user.id,
        action="tenant.created",
        target_type="tenant",
        target_id=tenant.id,
        details={"parent_tenant_id": str(parent.id), "initial_admin_user_id": str(admin.id)},
    )
    record_audit(
        db,
        tenant_id=tenant.id,
        actor_user_id=principal.user.id,
        action="authorization.grant.created",
        target_type="authorization_grant",
        target_id=initial_grant.id,
        details={
            "subject_type": "USER",
            "subject_id": str(admin.id),
            "role_id": str(tenant_admin_role.id),
            "scope_type": "TENANT",
            "scope_id": str(tenant.id),
            "authorization_revision_before": 1,
            "authorization_revision_after": tenant.authorization_revision,
        },
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise conflict("Tenant slug or initial administrator email already exists") from exc
    return TenantCreated(tenant=TenantRead.model_validate(tenant), initial_admin=UserRead.model_validate(admin))


@router.get("/{tenant_id}", response_model=TenantRead)
def get_tenant(
    tenant_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Tenant:
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if not principal.is_root_admin and principal.user.tenant_id != tenant.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return tenant


@router.get("/{tenant_id}/users", response_model=list[UserRead])
def list_tenant_users(
    tenant_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[User]:
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if not legacy_or_can_manage_tenant(db, principal, tenant.id, "tenant.manage_users"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission required: tenant.manage_users")
    return list(db.scalars(select(User).where(User.tenant_id == tenant.id).order_by(User.display_name, User.email)))


@router.post("/{tenant_id}/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_tenant_user(
    tenant_id: uuid.UUID,
    payload: UserCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> User:
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if tenant.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant is not active")
    if not legacy_or_can_manage_tenant(db, principal, tenant.id, "tenant.manage_users"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission required: tenant.manage_users")

    user = User(
        tenant_id=tenant.id,
        email=str(payload.email),
        normalized_email=normalize_email(str(payload.email)),
        display_name=payload.display_name,
        status="ACTIVE",
        tenant_role="USER",
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise conflict("A user with that email already exists") from exc
    db.add(PasswordCredential(user_id=user.id, password_hash=hash_password(payload.password)))
    record_audit(
        db,
        tenant_id=tenant.id,
        actor_user_id=principal.user.id,
        action="tenant.user.created",
        target_type="user",
        target_id=user.id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise conflict("A user with that email already exists") from exc
    return user
