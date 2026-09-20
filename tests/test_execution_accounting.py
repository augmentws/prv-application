import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.execution_accounting import (
    ProviderUsageContext,
    persist_model_invocations,
    reconcile_invocation_usage,
    refresh_skill_run_usage,
)
from app.model_execution import InvocationTelemetry
from app.models import (
    ExternalProviderUsage,
    ModelInvocation,
    SkillDefinition,
    SkillDefinitionVersion,
    SkillRun,
    Tenant,
    WorkflowRun,
    WorkflowStepRun,
    utcnow,
)


def test_skill_invocations_aggregate_and_reconcile_with_provider_usage(db, root_admin) -> None:
    root = db.get(Tenant, root_admin.tenant_id)
    skill = SkillDefinition(
        owner_tenant_id=root.id,
        scope="SYSTEM",
        key="accounting_test_skill",
        name="Accounting test skill",
        current_version=1,
        published_version=1,
        status="ACTIVE",
        created_by_user_id=root_admin.id,
    )
    db.add(skill)
    db.flush()
    version = SkillDefinitionVersion(
        skill_definition_id=skill.id,
        version=1,
        instructions="Return structured output.",
        input_schema_key="input_v1",
        input_schema={"type": "object"},
        output_schema_key="output_v1",
        output_schema={"type": "object"},
        model_key="configured-default",
        model_policy={},
        limits={},
        required_capabilities=["structured_output"],
        required_tools=[],
        cache_policy={},
        evaluation_fixtures=[],
        status="PUBLISHED",
        created_by_user_id=root_admin.id,
        published_at=utcnow(),
    )
    db.add(version)
    db.flush()
    workflow = WorkflowRun(
        tenant_id=root.id,
        workflow_key="test_workflow",
        code_version="1",
        dbos_workflow_id=f"test-workflow:{uuid.uuid4()}",
        initiated_by_user_id=root_admin.id,
    )
    db.add(workflow)
    db.flush()
    step = WorkflowStepRun(
        workflow_run_id=workflow.id,
        role_key="analysis",
        ordinal=1,
        status="RUNNING",
    )
    db.add(step)
    db.flush()
    run = SkillRun(
        workflow_run_id=workflow.id,
        workflow_step_run_id=step.id,
        skill_definition_version_id=version.id,
        scope_type="MATTER_DOCUMENT",
        scope_id=uuid.uuid4(),
        input_hash="a" * 64,
        configuration_hash="b" * 64,
        cache_fingerprint="c" * 64,
        status="RUNNING",
    )
    db.add(run)
    db.flush()

    started = utcnow()
    telemetry = (
        InvocationTelemetry(
            request_sequence=1,
            provider_request_id="request-1",
            provider="openai",
            model="gpt-5.6",
            model_configuration_hash="d" * 64,
            request_count=1,
            input_tokens=1000,
            cached_input_tokens=800,
            cache_write_tokens=100,
            output_tokens=125,
            latency_ms=250,
            started_at=started,
            completed_at=started + timedelta(milliseconds=250),
        ),
        InvocationTelemetry(
            request_sequence=2,
            provider_request_id="request-2",
            provider="openai",
            model="gpt-5.6",
            model_configuration_hash="d" * 64,
            request_count=1,
            input_tokens=400,
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=75,
            latency_ms=100,
            started_at=started,
            completed_at=started + timedelta(milliseconds=100),
        ),
    )
    usage_context = ProviderUsageContext(
        tenant_id=root.id,
        client_id=None,
        matter_id=None,
        started_by_user_id=root_admin.id,
        job_type="SKILL_RUN",
        job_id=run.id,
        job_created_at=run.created_at,
        details={"workflow_run_id": str(workflow.id)},
    )
    invocations = persist_model_invocations(
        db,
        telemetry,
        skill_run_id=run.id,
        usage_context=usage_context,
    )
    persist_model_invocations(
        db,
        telemetry,
        skill_run_id=run.id,
        usage_context=usage_context,
    )
    aggregate = refresh_skill_run_usage(db, run)
    db.commit()

    assert aggregate.request_count == 2
    assert aggregate.input_tokens == 1400
    assert aggregate.cached_input_tokens == 800
    assert aggregate.cache_write_tokens == 100
    assert aggregate.output_tokens == 200
    assert step.input_tokens == 1400
    assert workflow.input_tokens == 1400
    assert db.query(ModelInvocation).count() == 2
    assert db.query(ExternalProviderUsage).count() == 2
    reconciled = reconcile_invocation_usage(db, [invocation.id for invocation in invocations])
    assert reconciled == aggregate
    assert db.scalar(select(func.sum(ExternalProviderUsage.cached_input_tokens))) == 800
