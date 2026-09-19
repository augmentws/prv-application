import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import Principal, can_admin_tenant, get_principal
from app.models import ExternalProviderUsage, Tenant
from app.schemas import ExternalProviderUsageRead

router = APIRouter(prefix="/v1/tenants/{tenant_id}/provider-usage", tags=["provider usage"])


@router.get("", response_model=list[ExternalProviderUsageRead])
def list_external_provider_usage(
    tenant_id: uuid.UUID,
    provider: str | None = Query(default=None, max_length=100),
    job_type: str | None = Query(default=None, max_length=80),
    started_by_user_id: uuid.UUID | None = None,
    job_created_after: datetime | None = None,
    job_created_before: datetime | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[ExternalProviderUsage]:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if not can_admin_tenant(principal, tenant.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant ADMIN required")

    statement = select(ExternalProviderUsage).where(ExternalProviderUsage.tenant_id == tenant.id)
    if provider is not None:
        statement = statement.where(ExternalProviderUsage.provider == provider)
    if job_type is not None:
        statement = statement.where(ExternalProviderUsage.job_type == job_type)
    if started_by_user_id is not None:
        statement = statement.where(ExternalProviderUsage.started_by_user_id == started_by_user_id)
    if job_created_after is not None:
        statement = statement.where(ExternalProviderUsage.job_created_at >= job_created_after)
    if job_created_before is not None:
        statement = statement.where(ExternalProviderUsage.job_created_at < job_created_before)
    statement = statement.order_by(
        ExternalProviderUsage.job_created_at.desc(),
        ExternalProviderUsage.created_at.desc(),
    ).offset(offset).limit(limit)
    return list(db.scalars(statement))
