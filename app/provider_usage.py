import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ExternalProviderUsage


def external_model_identity(model_id: Any) -> tuple[str, str] | None:
    """Return the provider and provider model for a Pydantic AI model id."""

    if not isinstance(model_id, str) or ":" not in model_id:
        return None
    provider, model = model_id.split(":", 1)
    provider = provider.strip().lower()
    model = model.strip()
    if not provider or not model:
        return None
    return provider, model


def record_external_provider_usage(
    db: Session,
    *,
    idempotency_key: str,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID | None,
    matter_id: uuid.UUID | None,
    started_by_user_id: uuid.UUID,
    job_type: str,
    job_id: uuid.UUID,
    job_created_at: datetime,
    provider: str,
    model: str,
    request_count: int,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    model_invocation_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> ExternalProviderUsage:
    """Stage one immutable usage record, returning an existing retry record when present."""

    for pending in db.new:
        if isinstance(pending, ExternalProviderUsage) and pending.idempotency_key == idempotency_key:
            return pending
    existing = db.scalar(
        select(ExternalProviderUsage).where(ExternalProviderUsage.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    if (
        request_count < 0
        or input_tokens < 0
        or cached_input_tokens < 0
        or cache_write_tokens < 0
        or output_tokens < 0
    ):
        raise ValueError("Provider usage counts cannot be negative")
    usage = ExternalProviderUsage(
        idempotency_key=idempotency_key,
        tenant_id=tenant_id,
        client_id=client_id,
        matter_id=matter_id,
        started_by_user_id=started_by_user_id,
        job_type=job_type,
        job_id=job_id,
        job_created_at=job_created_at,
        provider=provider,
        model=model,
        request_count=request_count,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        output_tokens=output_tokens,
        model_invocation_id=model_invocation_id,
        details=details or {},
    )
    db.add(usage)
    return usage
