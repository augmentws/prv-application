import uuid

from dbos import DBOS, Queue
from pydantic_ai import RunContext
from pydantic_ai.durable_exec.dbos import DBOSDurability
from pydantic_ai.models.test import TestModel

from app.agent_runtime import (
    AssessmentControlSampleSize,
    AssessmentMaximumDocumentCount,
    AgentEnumDescription,
    AgentEnumLabel,
    AgentMetadataChangeReason,
    AgentMetadataDescription,
    AgentMetadataDisplayName,
    AgentRunOutcome,
    AgentRuntimeDeps,
    MatterDefinitionContent,
    MatterDefinitionEditReason,
    MatterDefinitionRevisionNumber,
    PreparedAgentRun,
    add_matter_metadata_enum_value,
    apply_matter_definition_draft_edit,
    build_agent,
    compare_metadata,
    create_matter_metadata_definition,
    deactivate_matter_metadata_enum_value,
    execute_prepared_agent_run,
    fail_agent_run,
    list_editable_metadata,
    persist_agent_run_outcome,
    prepare_agent_run,
    read_matter_definition,
    start_matter_definition_assessment,
    update_matter_metadata_definition,
    update_matter_metadata_enum_value,
    validate_matter_definition,
)
from app.config import get_settings
from app.schemas import (
    AssertionPolicy,
    Cardinality,
    EnumValueKey,
    MetadataEnumValueCreate,
    MetadataKey,
    MetadataType,
    ResolutionPolicy,
    ResourceStatus,
)

AGENT_QUEUE = Queue("agent-turns", global_concurrency=get_settings().agent_turn_queue_concurrency)


@DBOS.step(name="agent_tool_matter_definition_read")
def durable_read_matter_definition(ctx: RunContext[AgentRuntimeDeps]) -> dict:
    return read_matter_definition(ctx)


@DBOS.step(name="agent_tool_matter_metadata_list_editable")
def durable_list_editable_metadata(ctx: RunContext[AgentRuntimeDeps]) -> dict:
    return list_editable_metadata(ctx)


@DBOS.step(name="agent_tool_matter_metadata_compare")
def durable_compare_metadata(
    ctx: RunContext[AgentRuntimeDeps], proposed_field_keys: list[str]
) -> dict:
    return compare_metadata(ctx, proposed_field_keys)


@DBOS.step(name="agent_tool_matter_definition_validate")
def durable_validate_matter_definition(
    ctx: RunContext[AgentRuntimeDeps], referenced_field_keys: list[str]
) -> dict:
    return validate_matter_definition(ctx, referenced_field_keys)


@DBOS.step(name="agent_tool_matter_metadata_create_definition")
def durable_create_matter_metadata_definition(
    ctx: RunContext[AgentRuntimeDeps],
    key: MetadataKey,
    display_name: AgentMetadataDisplayName,
    field_type: MetadataType,
    reason: AgentMetadataChangeReason,
    cardinality: Cardinality = "SINGLE",
    description: AgentMetadataDescription | None = None,
    allowed_values: list[MetadataEnumValueCreate] | None = None,
    assertion_policy: AssertionPolicy = "IMMEDIATE",
    resolution_policy: ResolutionPolicy = "EXPLICIT_ONLY",
    searchable: bool = True,
    facetable: bool = False,
    reviewable: bool = True,
    ai_assignable: bool = False,
) -> dict:
    return create_matter_metadata_definition(
        ctx,
        key,
        display_name,
        field_type,
        reason,
        cardinality,
        description,
        allowed_values,
        assertion_policy,
        resolution_policy,
        searchable,
        facetable,
        reviewable,
        ai_assignable,
    )


@DBOS.step(name="agent_tool_matter_metadata_update_definition")
def durable_update_matter_metadata_definition(
    ctx: RunContext[AgentRuntimeDeps],
    definition_key: MetadataKey,
    reason: AgentMetadataChangeReason,
    display_name: AgentMetadataDisplayName | None = None,
    description: AgentMetadataDescription | None = None,
    assertion_policy: AssertionPolicy | None = None,
    resolution_policy: ResolutionPolicy | None = None,
    searchable: bool | None = None,
    facetable: bool | None = None,
    reviewable: bool | None = None,
    ai_assignable: bool | None = None,
    status: ResourceStatus | None = None,
) -> dict:
    return update_matter_metadata_definition(
        ctx,
        definition_key,
        reason,
        display_name,
        description,
        assertion_policy,
        resolution_policy,
        searchable,
        facetable,
        reviewable,
        ai_assignable,
        status,
    )


@DBOS.step(name="agent_tool_matter_metadata_enum_add")
def durable_add_matter_metadata_enum_value(
    ctx: RunContext[AgentRuntimeDeps],
    definition_key: MetadataKey,
    value_key: EnumValueKey,
    label: AgentEnumLabel,
    reason: AgentMetadataChangeReason,
    description: AgentEnumDescription | None = None,
) -> dict:
    return add_matter_metadata_enum_value(
        ctx,
        definition_key,
        value_key,
        label,
        reason,
        description,
    )


@DBOS.step(name="agent_tool_matter_metadata_enum_update")
def durable_update_matter_metadata_enum_value(
    ctx: RunContext[AgentRuntimeDeps],
    definition_key: MetadataKey,
    value_key: EnumValueKey,
    reason: AgentMetadataChangeReason,
    label: AgentEnumLabel | None = None,
    description: AgentEnumDescription | None = None,
) -> dict:
    return update_matter_metadata_enum_value(
        ctx,
        definition_key,
        value_key,
        reason,
        label,
        description,
    )


@DBOS.step(name="agent_tool_matter_metadata_enum_deactivate")
def durable_deactivate_matter_metadata_enum_value(
    ctx: RunContext[AgentRuntimeDeps],
    definition_key: MetadataKey,
    value_key: EnumValueKey,
    reason: AgentMetadataChangeReason,
) -> dict:
    return deactivate_matter_metadata_enum_value(ctx, definition_key, value_key, reason)


@DBOS.step(name="agent_tool_matter_definition_apply_draft_edit")
def durable_apply_matter_definition_draft_edit(
    ctx: RunContext[AgentRuntimeDeps],
    content_markdown: MatterDefinitionContent,
    based_on_revision: MatterDefinitionRevisionNumber,
    reason: MatterDefinitionEditReason,
) -> dict:
    return apply_matter_definition_draft_edit(ctx, content_markdown, based_on_revision, reason)


@DBOS.step(name="agent_tool_matter_definition_start_assessment")
def durable_start_matter_definition_assessment(
    ctx: RunContext[AgentRuntimeDeps],
    maximum_document_count: AssessmentMaximumDocumentCount = 500,
    control_sample_size: AssessmentControlSampleSize = 0,
    revision: MatterDefinitionRevisionNumber | None = None,
) -> dict:
    return start_matter_definition_assessment(
        ctx,
        maximum_document_count,
        control_sample_size,
        revision,
    )


_settings = get_settings()
# DBOS requires a construction-time model so it can register durable model
# operations. An unconfigured deployment receives a non-executing sentinel;
# prepare_agent_run fails before execution until AGENT_DEFAULT_MODEL is set.
_registration_model = _settings.agent_default_model or TestModel(
    custom_output_text="AGENT_DEFAULT_MODEL is not configured"
)

DURABLE_AGENT = build_agent(
    model=_registration_model,
    capabilities=[DBOSDurability(parallel_execution_mode="sequential")],
    tool_functions={
        "matter_definition.read": durable_read_matter_definition,
        "matter_metadata.list_editable": durable_list_editable_metadata,
        "matter_metadata.compare": durable_compare_metadata,
        "matter_definition.validate": durable_validate_matter_definition,
        "matter_metadata.create_definition": durable_create_matter_metadata_definition,
        "matter_metadata.update_definition": durable_update_matter_metadata_definition,
        "matter_metadata.enum.add": durable_add_matter_metadata_enum_value,
        "matter_metadata.enum.update": durable_update_matter_metadata_enum_value,
        "matter_metadata.enum.deactivate": durable_deactivate_matter_metadata_enum_value,
        "matter_definition.apply_draft_edit": durable_apply_matter_definition_draft_edit,
        "matter_definition.start_assessment": durable_start_matter_definition_assessment,
    },
)


@DBOS.step(name="prepare_agent_run")
def durable_prepare_agent_run(run_id: str) -> PreparedAgentRun:
    return prepare_agent_run(uuid.UUID(run_id))


@DBOS.step(name="persist_agent_run_outcome")
def durable_persist_agent_run_outcome(run_id: str, outcome: AgentRunOutcome) -> None:
    persist_agent_run_outcome(uuid.UUID(run_id), outcome)


@DBOS.step(name="fail_agent_run")
def durable_fail_agent_run(run_id: str, message: str) -> None:
    fail_agent_run(uuid.UUID(run_id), message)


@DBOS.workflow(name="agent_turn")
async def agent_turn(run_id: str) -> None:
    try:
        prepared = durable_prepare_agent_run(run_id)
        outcome = await execute_prepared_agent_run(prepared, DURABLE_AGENT)
        durable_persist_agent_run_outcome(run_id, outcome)
    except Exception as exc:
        durable_fail_agent_run(run_id, str(exc))
        raise
