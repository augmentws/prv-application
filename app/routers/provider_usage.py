import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import Principal, can_admin_matter, can_admin_tenant, get_principal
from app.models import ExternalProviderUsage, Matter, Tenant
from app.schemas import (
    ExternalProviderUsageRead,
    MatterProviderUsageReportRead,
    ProviderUsageJobBreakdownRead,
    ProviderUsageModelBreakdownRead,
    ProviderUsageTotalsRead,
)

router = APIRouter(prefix="/v1/tenants/{tenant_id}/provider-usage", tags=["provider usage"])
matter_router = APIRouter(prefix="/v1/matters/{matter_id}/provider-usage", tags=["provider usage"])


def _totals(values) -> ProviderUsageTotalsRead:
    record_count, request_count, input_tokens, cached_input_tokens, cache_write_tokens, output_tokens = (
        int(value or 0) for value in values
    )
    return ProviderUsageTotalsRead(
        record_count=record_count,
        request_count=request_count,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def _aggregate_columns():
    return (
        func.count(ExternalProviderUsage.id),
        func.coalesce(func.sum(ExternalProviderUsage.request_count), 0),
        func.coalesce(func.sum(ExternalProviderUsage.input_tokens), 0),
        func.coalesce(func.sum(ExternalProviderUsage.cached_input_tokens), 0),
        func.coalesce(func.sum(ExternalProviderUsage.cache_write_tokens), 0),
        func.coalesce(func.sum(ExternalProviderUsage.output_tokens), 0),
    )


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


@matter_router.get("", response_model=MatterProviderUsageReportRead)
def get_matter_provider_usage(
    matter_id: uuid.UUID,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> MatterProviderUsageReportRead:
    matter = db.get(Matter, matter_id)
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")

    scope = ExternalProviderUsage.matter_id == matter.id
    totals = _totals(db.execute(select(*_aggregate_columns()).where(scope)).one())
    by_model = [
        ProviderUsageModelBreakdownRead(
            provider=provider,
            model=model,
            **_totals(values).model_dump(),
        )
        for provider, model, *values in db.execute(
            select(
                ExternalProviderUsage.provider,
                ExternalProviderUsage.model,
                *_aggregate_columns(),
            )
            .where(scope)
            .group_by(ExternalProviderUsage.provider, ExternalProviderUsage.model)
            .order_by(
                (
                    func.sum(ExternalProviderUsage.input_tokens)
                    + func.sum(ExternalProviderUsage.output_tokens)
                ).desc(),
                ExternalProviderUsage.provider,
                ExternalProviderUsage.model,
            )
        )
    ]
    by_job_type = [
        ProviderUsageJobBreakdownRead(
            job_type=job_type,
            **_totals(values).model_dump(),
        )
        for job_type, *values in db.execute(
            select(ExternalProviderUsage.job_type, *_aggregate_columns())
            .where(scope)
            .group_by(ExternalProviderUsage.job_type)
            .order_by(
                (
                    func.sum(ExternalProviderUsage.input_tokens)
                    + func.sum(ExternalProviderUsage.output_tokens)
                ).desc(),
                ExternalProviderUsage.job_type,
            )
        )
    ]
    entries = list(
        db.scalars(
            select(ExternalProviderUsage)
            .where(scope)
            .order_by(
                ExternalProviderUsage.job_created_at.desc(),
                ExternalProviderUsage.created_at.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
    )
    return MatterProviderUsageReportRead(
        matter_id=matter.id,
        totals=totals,
        by_model=by_model,
        by_job_type=by_job_type,
        entries=entries,
        entries_offset=offset,
        entries_limit=limit,
    )
