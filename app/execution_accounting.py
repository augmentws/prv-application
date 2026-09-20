import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.model_execution import InvocationTelemetry
from app.models import (
    AgentRun,
    ExternalProviderUsage,
    ModelInvocation,
    SkillRun,
    WorkflowRun,
    WorkflowStepRun,
)
from app.provider_usage import record_external_provider_usage


@dataclass(frozen=True)
class ProviderUsageContext:
    tenant_id: uuid.UUID
    client_id: uuid.UUID | None
    matter_id: uuid.UUID | None
    started_by_user_id: uuid.UUID
    job_type: str
    job_id: uuid.UUID
    job_created_at: datetime
    details: dict[str, str | int]


@dataclass(frozen=True)
class UsageAggregate:
    request_count: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0


def persist_model_invocations(
    db: Session,
    telemetry: tuple[InvocationTelemetry, ...],
    *,
    agent_run_id: uuid.UUID | None = None,
    skill_run_id: uuid.UUID | None = None,
    attempt: int = 1,
    usage_context: ProviderUsageContext | None = None,
) -> list[ModelInvocation]:
    if (agent_run_id is None) == (skill_run_id is None):
        raise ValueError("Exactly one invocation owner is required")
    invocations: list[ModelInvocation] = []
    for item in telemetry:
        owner_filter = (
            ModelInvocation.agent_run_id == agent_run_id
            if agent_run_id is not None
            else ModelInvocation.skill_run_id == skill_run_id
        )
        invocation = db.scalar(
            select(ModelInvocation).where(
                owner_filter,
                ModelInvocation.attempt == attempt,
                ModelInvocation.request_sequence == item.request_sequence,
            )
        )
        if invocation is None:
            invocation = ModelInvocation(
                agent_run_id=agent_run_id,
                skill_run_id=skill_run_id,
                provider_request_id=item.provider_request_id,
                provider=item.provider,
                model=item.model,
                model_configuration_hash=item.model_configuration_hash,
                attempt=attempt,
                request_sequence=item.request_sequence,
                request_count=item.request_count,
                input_tokens=item.input_tokens,
                cached_input_tokens=item.cached_input_tokens,
                cache_write_tokens=item.cache_write_tokens,
                output_tokens=item.output_tokens,
                latency_ms=item.latency_ms,
                status="COMPLETED",
                started_at=item.started_at,
                completed_at=item.completed_at,
            )
            db.add(invocation)
            db.flush()
        invocations.append(invocation)
        if usage_context is not None:
            details = dict(usage_context.details)
            details.update({"request_sequence": item.request_sequence, "attempt": attempt})
            record_external_provider_usage(
                db,
                idempotency_key=f"model-invocation:{invocation.id}:provider-usage",
                tenant_id=usage_context.tenant_id,
                client_id=usage_context.client_id,
                matter_id=usage_context.matter_id,
                started_by_user_id=usage_context.started_by_user_id,
                job_type=usage_context.job_type,
                job_id=usage_context.job_id,
                job_created_at=usage_context.job_created_at,
                provider=item.provider,
                model=item.model,
                request_count=item.request_count,
                input_tokens=item.input_tokens,
                cached_input_tokens=item.cached_input_tokens,
                cache_write_tokens=item.cache_write_tokens,
                output_tokens=item.output_tokens,
                model_invocation_id=invocation.id,
                details=details,
            )
    return invocations


def invocation_usage(
    db: Session,
    *,
    agent_run_id: uuid.UUID | None = None,
    skill_run_id: uuid.UUID | None = None,
) -> UsageAggregate:
    if (agent_run_id is None) == (skill_run_id is None):
        raise ValueError("Exactly one invocation owner is required")
    owner_filter = (
        ModelInvocation.agent_run_id == agent_run_id
        if agent_run_id is not None
        else ModelInvocation.skill_run_id == skill_run_id
    )
    row = db.execute(
        select(
            func.coalesce(func.sum(ModelInvocation.request_count), 0),
            func.coalesce(func.sum(ModelInvocation.input_tokens), 0),
            func.coalesce(func.sum(ModelInvocation.cached_input_tokens), 0),
            func.coalesce(func.sum(ModelInvocation.cache_write_tokens), 0),
            func.coalesce(func.sum(ModelInvocation.output_tokens), 0),
        ).where(owner_filter)
    ).one()
    return UsageAggregate(*(int(value) for value in row))


def apply_usage_aggregate(record, usage: UsageAggregate) -> None:
    record.request_count = usage.request_count
    record.input_tokens = usage.input_tokens
    record.cached_input_tokens = usage.cached_input_tokens
    record.cache_write_tokens = usage.cache_write_tokens
    record.output_tokens = usage.output_tokens


def refresh_agent_run_usage(db: Session, run: AgentRun) -> UsageAggregate:
    usage = invocation_usage(db, agent_run_id=run.id)
    apply_usage_aggregate(run, usage)
    return usage


def refresh_skill_run_usage(db: Session, run: SkillRun) -> UsageAggregate:
    usage = invocation_usage(db, skill_run_id=run.id)
    apply_usage_aggregate(run, usage)
    db.flush()
    refresh_workflow_execution_usage(db, run.workflow_run_id)
    return usage


def refresh_workflow_execution_usage(db: Session, workflow_run_id: uuid.UUID) -> UsageAggregate:
    steps = list(
        db.scalars(select(WorkflowStepRun).where(WorkflowStepRun.workflow_run_id == workflow_run_id))
    )
    for step in steps:
        values = db.execute(
            select(
                func.coalesce(func.sum(SkillRun.request_count), 0),
                func.coalesce(func.sum(SkillRun.tool_call_count), 0),
                func.coalesce(func.sum(SkillRun.input_tokens), 0),
                func.coalesce(func.sum(SkillRun.cached_input_tokens), 0),
                func.coalesce(func.sum(SkillRun.cache_write_tokens), 0),
                func.coalesce(func.sum(SkillRun.output_tokens), 0),
            ).where(SkillRun.workflow_step_run_id == step.id)
        ).one()
        step.request_count = int(values[0])
        step.tool_call_count = int(values[1])
        step.input_tokens = int(values[2])
        step.cached_input_tokens = int(values[3])
        step.cache_write_tokens = int(values[4])
        step.output_tokens = int(values[5])
    db.flush()
    workflow = db.get(WorkflowRun, workflow_run_id)
    if workflow is None:
        raise ValueError("Workflow run not found")
    values = db.execute(
        select(
            func.coalesce(func.sum(WorkflowStepRun.request_count), 0),
            func.coalesce(func.sum(WorkflowStepRun.tool_call_count), 0),
            func.coalesce(func.sum(WorkflowStepRun.input_tokens), 0),
            func.coalesce(func.sum(WorkflowStepRun.cached_input_tokens), 0),
            func.coalesce(func.sum(WorkflowStepRun.cache_write_tokens), 0),
            func.coalesce(func.sum(WorkflowStepRun.output_tokens), 0),
        ).where(WorkflowStepRun.workflow_run_id == workflow_run_id)
    ).one()
    workflow.request_count = int(values[0])
    workflow.tool_call_count = int(values[1])
    workflow.input_tokens = int(values[2])
    workflow.cached_input_tokens = int(values[3])
    workflow.cache_write_tokens = int(values[4])
    workflow.output_tokens = int(values[5])
    return UsageAggregate(
        request_count=workflow.request_count,
        input_tokens=workflow.input_tokens,
        cached_input_tokens=workflow.cached_input_tokens,
        cache_write_tokens=workflow.cache_write_tokens,
        output_tokens=workflow.output_tokens,
    )


def reconcile_invocation_usage(db: Session, invocation_ids: list[uuid.UUID]) -> UsageAggregate:
    if not invocation_ids:
        return UsageAggregate()
    invocation_values = db.execute(
        select(
            func.coalesce(func.sum(ModelInvocation.request_count), 0),
            func.coalesce(func.sum(ModelInvocation.input_tokens), 0),
            func.coalesce(func.sum(ModelInvocation.cached_input_tokens), 0),
            func.coalesce(func.sum(ModelInvocation.cache_write_tokens), 0),
            func.coalesce(func.sum(ModelInvocation.output_tokens), 0),
        ).where(ModelInvocation.id.in_(invocation_ids))
    ).one()
    ledger_values = db.execute(
        select(
            func.coalesce(func.sum(ExternalProviderUsage.request_count), 0),
            func.coalesce(func.sum(ExternalProviderUsage.input_tokens), 0),
            func.coalesce(func.sum(ExternalProviderUsage.cached_input_tokens), 0),
            func.coalesce(func.sum(ExternalProviderUsage.cache_write_tokens), 0),
            func.coalesce(func.sum(ExternalProviderUsage.output_tokens), 0),
        ).where(ExternalProviderUsage.model_invocation_id.in_(invocation_ids))
    ).one()
    invocation_usage = UsageAggregate(*(int(value) for value in invocation_values))
    ledger_usage = UsageAggregate(*(int(value) for value in ledger_values))
    if invocation_usage != ledger_usage:
        raise ValueError("Model invocation usage does not reconcile with ExternalProviderUsage")
    return invocation_usage
