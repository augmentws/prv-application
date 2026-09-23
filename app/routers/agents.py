import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent_invocation import invoke_registered_agent, package_for
from app.agent_models import validate_agent_model_key
from app.agent_tools import (
    AGENT_TOOL_SPECS,
    EXECUTABLE_AGENT_TOOL_KEYS,
    validate_executable_agent_tool_keys,
)
from app.agent_workflows import (
    MATTER_DEFINITION_SETUP_WORKFLOW,
    WORKFLOW_AGENT_KEYS,
)
from app.artifact_gateway import get_collection_snapshot
from app.audit import record_audit
from app.config import get_settings
from app.database import get_db
from app.dependencies import (
    Principal,
    can_admin_client,
    can_admin_matter,
    can_admin_tenant,
    get_principal,
    require_root_admin,
)
from app.model_execution import ModelExecutionError, StructuredOutputValidationError
from app.models import AgentDefinition, AgentDefinitionVersion, AgentVersionTool, Client, Matter, Tenant, utcnow
from app.schemas import (
    AgentConversationWorkflow,
    AgentDefinitionCreate,
    AgentDefinitionCreated,
    AgentDefinitionRead,
    AgentDefinitionUpdate,
    AgentDefinitionVersionRead,
    AgentInvokeRequest,
    AgentInvokeResponse,
    AgentModelRead,
    AgentPackageRead,
    AgentToolAssignment,
    AgentToolRead,
    AgentVersionCreate,
)
from artifact_service.auth import ArtifactPrincipal, get_embedded_artifact_principal
from artifact_service.database import get_artifact_db

router = APIRouter(prefix="/v1", tags=["agents"])


def _version_read(db: Session, version: AgentDefinitionVersion) -> AgentDefinitionVersionRead:
    assignments = list(
        db.scalars(
            select(AgentVersionTool)
            .where(AgentVersionTool.agent_definition_version_id == version.id)
            .order_by(AgentVersionTool.tool_key)
        )
    )
    return AgentDefinitionVersionRead(
        id=version.id,
        agent_definition_id=version.agent_definition_id,
        version=version.version,
        system_prompt=version.system_prompt,
        model_key=version.model_key,
        model_policy=version.model_policy,
        invocation_mode=version.invocation_mode,
        usage_instructions=version.usage_instructions,
        scope_types=version.scope_types,
        input_schema=version.input_schema,
        output_schema=version.output_schema,
        limits=version.limits,
        status=version.status,
        created_by_user_id=version.created_by_user_id,
        created_at=version.created_at,
        published_at=version.published_at,
        tools=[AgentToolAssignment(key=item.tool_key, configuration=item.configuration) for item in assignments],
    )


def _require_agent_admin(db: Session, principal: Principal, agent: AgentDefinition) -> None:
    if agent.scope == "SYSTEM":
        if not principal.is_root_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Root tenant ADMIN required")
        return
    if not can_admin_tenant(principal, agent.owner_tenant_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant ADMIN required")


def _create_agent(
    db: Session,
    principal: Principal,
    payload: AgentDefinitionCreate,
    *,
    owner_tenant_id: uuid.UUID,
    scope: str,
) -> AgentDefinitionCreated:
    try:
        validate_executable_agent_tool_keys([tool.key for tool in payload.initial_version.tools])
        validate_agent_model_key(payload.initial_version.model_key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    agent = AgentDefinition(
        owner_tenant_id=owner_tenant_id,
        scope=scope,
        key=payload.key,
        name=payload.name.strip(),
        description=payload.description,
        current_version=1,
        status="ACTIVE",
        created_by_user_id=principal.user.id,
    )
    db.add(agent)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent key already exists for tenant") from exc
    version = AgentDefinitionVersion(
        agent_definition_id=agent.id,
        version=1,
        system_prompt=payload.initial_version.system_prompt,
        model_key=payload.initial_version.model_key,
        model_policy=payload.initial_version.model_policy,
        invocation_mode=payload.initial_version.invocation_mode,
        usage_instructions=payload.initial_version.usage_instructions,
        scope_types=payload.initial_version.scope_types,
        input_schema=payload.initial_version.input_schema,
        output_schema=payload.initial_version.output_schema,
        limits=payload.initial_version.limits,
        status="DRAFT",
        created_by_user_id=principal.user.id,
    )
    db.add(version)
    db.flush()
    db.add_all(
        AgentVersionTool(
            agent_definition_version_id=version.id,
            tool_key=tool.key,
            configuration=tool.configuration,
        )
        for tool in payload.initial_version.tools
    )
    record_audit(
        db,
        tenant_id=owner_tenant_id,
        actor_user_id=principal.user.id,
        action="agent.created",
        target_type="agent_definition",
        target_id=agent.id,
        details={"scope": scope, "version": 1},
    )
    db.commit()
    return AgentDefinitionCreated(
        agent=AgentDefinitionRead.model_validate(agent),
        version=_version_read(db, version),
    )


@router.get("/agent-tools", response_model=list[AgentToolRead])
def list_agent_tools(_: Principal = Depends(get_principal)) -> list[AgentToolRead]:
    return [
        AgentToolRead(
            **tool.__dict__,
            runtime_available=tool.key in EXECUTABLE_AGENT_TOOL_KEYS,
        )
        for tool in AGENT_TOOL_SPECS
    ]


@router.get("/agent-models", response_model=list[AgentModelRead])
def list_agent_models(_: Principal = Depends(get_principal)) -> list[AgentModelRead]:
    settings = get_settings()
    return [
        AgentModelRead(
            key="configured-default",
            name="Platform default",
            description="The model selected by the platform AGENT_DEFAULT_MODEL setting.",
            configured_model=settings.agent_default_model,
            available=settings.agent_default_model is not None,
        )
    ]


@router.get("/matters/{matter_id}/agents", response_model=list[AgentDefinitionRead])
def list_available_matter_agents(
    matter_id: uuid.UUID,
    workflow_type: AgentConversationWorkflow = Query(default=MATTER_DEFINITION_SETUP_WORKFLOW),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[AgentDefinition]:
    """List active, published agents that the caller may run for a matter."""
    matter = db.scalar(select(Matter).where(Matter.id == matter_id))
    if matter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matter not found")
    if not can_admin_matter(db, principal, matter):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Matter ADMIN required")
    return list(
        db.scalars(
            select(AgentDefinition)
            .where(
                AgentDefinition.status == "ACTIVE",
                AgentDefinition.published_version.is_not(None),
                AgentDefinition.key.in_(WORKFLOW_AGENT_KEYS[workflow_type]),
                or_(
                    AgentDefinition.owner_tenant_id == matter.client.tenant_id,
                    AgentDefinition.scope == "SYSTEM",
                ),
            )
            .order_by(AgentDefinition.scope, AgentDefinition.name)
        )
    )


@router.get("/admin/agents", response_model=list[AgentDefinitionRead])
def list_system_agents(
    principal: Principal = Depends(require_root_admin),
    db: Session = Depends(get_db),
) -> list[AgentDefinition]:
    return list(
        db.scalars(
            select(AgentDefinition)
            .where(AgentDefinition.scope == "SYSTEM", AgentDefinition.owner_tenant_id == principal.user.tenant_id)
            .order_by(AgentDefinition.name)
        )
    )


@router.post("/admin/agents", response_model=AgentDefinitionCreated, status_code=status.HTTP_201_CREATED)
def create_system_agent(
    payload: AgentDefinitionCreate,
    principal: Principal = Depends(require_root_admin),
    db: Session = Depends(get_db),
) -> AgentDefinitionCreated:
    return _create_agent(db, principal, payload, owner_tenant_id=principal.user.tenant_id, scope="SYSTEM")


@router.get("/tenants/{tenant_id}/agents", response_model=list[AgentDefinitionRead])
def list_tenant_agents(
    tenant_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[AgentDefinition]:
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if not can_admin_tenant(principal, tenant.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant ADMIN required")
    return list(
        db.scalars(
            select(AgentDefinition)
            .where(
                AgentDefinition.status != "ARCHIVED",
                or_(
                    AgentDefinition.owner_tenant_id == tenant.id,
                    AgentDefinition.scope == "SYSTEM",
                ),
            )
            .order_by(AgentDefinition.scope, AgentDefinition.name)
        )
    )


@router.post("/tenants/{tenant_id}/agents", response_model=AgentDefinitionCreated, status_code=status.HTTP_201_CREATED)
def create_tenant_agent(
    tenant_id: uuid.UUID,
    payload: AgentDefinitionCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AgentDefinitionCreated:
    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if tenant.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant is not active")
    if not can_admin_tenant(principal, tenant.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant ADMIN required")
    return _create_agent(db, principal, payload, owner_tenant_id=tenant.id, scope="TENANT")


@router.get("/agent-packages", response_model=list[AgentPackageRead])
def list_agent_packages(
    scope_type: str = Query(min_length=1, max_length=50),
    scope_id: uuid.UUID = Query(),
    principal: Principal = Depends(get_principal),
    artifact_principal: ArtifactPrincipal = Depends(get_embedded_artifact_principal),
    db: Session = Depends(get_db),
    artifact_db: Session = Depends(get_artifact_db),
) -> list[dict]:
    if scope_type != "COLLECTION":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unsupported agent scope")
    try:
        collection = get_collection_snapshot(scope_id, artifact_principal, artifact_db)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    client = db.get(Client, collection.client_id)
    if client is None or client.tenant_id != collection.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    if not can_admin_client(db, principal, client):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client ADMIN required")
    rows = db.execute(
        select(AgentDefinition, AgentDefinitionVersion)
        .join(
            AgentDefinitionVersion,
            (AgentDefinitionVersion.agent_definition_id == AgentDefinition.id)
            & (AgentDefinitionVersion.version == AgentDefinition.published_version),
        )
        .where(
            AgentDefinition.status == "ACTIVE",
            AgentDefinition.published_version.is_not(None),
            AgentDefinitionVersion.status == "PUBLISHED",
            or_(AgentDefinition.scope == "SYSTEM", AgentDefinition.owner_tenant_id == collection.tenant_id),
        )
        .order_by(AgentDefinition.scope, AgentDefinition.name)
    ).all()
    return [package_for(agent, version) for agent, version in rows if scope_type in version.scope_types]


@router.get("/agents/{agent_id}", response_model=AgentDefinitionCreated)
def get_agent(
    agent_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AgentDefinitionCreated:
    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.id == agent_id))
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    _require_agent_admin(db, principal, agent)
    version = db.scalar(
        select(AgentDefinitionVersion).where(
            AgentDefinitionVersion.agent_definition_id == agent.id,
            AgentDefinitionVersion.version == agent.current_version,
        )
    )
    if version is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent has no current version")
    return AgentDefinitionCreated(agent=AgentDefinitionRead.model_validate(agent), version=_version_read(db, version))


@router.get("/agents/{agent_id}/package", response_model=AgentPackageRead)
def get_agent_package(
    agent_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict:
    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.id == agent_id))
    if agent is None or agent.status != "ACTIVE" or agent.published_version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Published agent not found")
    if agent.scope != "SYSTEM" and agent.owner_tenant_id != principal.user.tenant_id and not principal.is_root_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Agent access denied")
    version = db.scalar(
        select(AgentDefinitionVersion).where(
            AgentDefinitionVersion.agent_definition_id == agent.id,
            AgentDefinitionVersion.version == agent.published_version,
            AgentDefinitionVersion.status == "PUBLISHED",
        )
    )
    if version is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent has no published version")
    return package_for(agent, version)


@router.post("/agents/{agent_id}:invoke", response_model=AgentInvokeResponse)
async def invoke_agent(
    agent_id: uuid.UUID,
    payload: AgentInvokeRequest,
    principal: Principal = Depends(get_principal),
    artifact_principal: ArtifactPrincipal = Depends(get_embedded_artifact_principal),
    db: Session = Depends(get_db),
    artifact_db: Session = Depends(get_artifact_db),
) -> AgentInvokeResponse:
    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.id == agent_id))
    if agent is None or agent.status != "ACTIVE" or agent.published_version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Published agent not found")
    if agent.scope != "SYSTEM" and agent.owner_tenant_id != principal.user.tenant_id and not principal.is_root_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Agent access denied")
    version = db.scalar(
        select(AgentDefinitionVersion).where(
            AgentDefinitionVersion.agent_definition_id == agent.id,
            AgentDefinitionVersion.version == agent.published_version,
            AgentDefinitionVersion.status == "PUBLISHED",
        )
    )
    if version is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent has no published version")
    try:
        output = await invoke_registered_agent(
            agent=agent,
            version=version,
            scope=payload.scope,
            payload=payload.input,
            principal=principal,
            artifact_principal=artifact_principal,
            db=db,
            artifact_db=artifact_db,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except StructuredOutputValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ModelExecutionError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return AgentInvokeResponse(
        agent_id=agent.id,
        agent_version_id=version.id,
        version=version.version,
        output=output,
    )


@router.patch("/agents/{agent_id}", response_model=AgentDefinitionRead)
def update_agent(
    agent_id: uuid.UUID,
    payload: AgentDefinitionUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AgentDefinition:
    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.id == agent_id).with_for_update())
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    _require_agent_admin(db, principal, agent)
    changes: dict[str, dict[str, str | None]] = {}
    for field_name in ("name", "description", "status"):
        if field_name not in payload.model_fields_set:
            continue
        next_value = getattr(payload, field_name)
        prior_value = getattr(agent, field_name)
        if next_value != prior_value:
            changes[field_name] = {"from": prior_value, "to": next_value}
            setattr(agent, field_name, next_value)
    if changes:
        record_audit(
            db,
            tenant_id=agent.owner_tenant_id,
            actor_user_id=principal.user.id,
            action="agent.updated",
            target_type="agent_definition",
            target_id=agent.id,
            details={"changes": changes},
        )
        db.commit()
        db.refresh(agent)
    return agent


@router.get("/agents/{agent_id}/versions", response_model=list[AgentDefinitionVersionRead])
def list_agent_versions(
    agent_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[AgentDefinitionVersionRead]:
    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.id == agent_id))
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    _require_agent_admin(db, principal, agent)
    versions = list(
        db.scalars(
            select(AgentDefinitionVersion)
            .where(AgentDefinitionVersion.agent_definition_id == agent.id)
            .order_by(AgentDefinitionVersion.version.desc())
        )
    )
    return [_version_read(db, version) for version in versions]


@router.post("/agents/{agent_id}/versions", response_model=AgentDefinitionVersionRead, status_code=status.HTTP_201_CREATED)
def create_agent_version(
    agent_id: uuid.UUID,
    payload: AgentVersionCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AgentDefinitionVersionRead:
    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.id == agent_id).with_for_update())
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    _require_agent_admin(db, principal, agent)
    if agent.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent is not active")
    try:
        validate_executable_agent_tool_keys([tool.key for tool in payload.tools])
        validate_agent_model_key(payload.model_key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    agent.current_version += 1
    version = AgentDefinitionVersion(
        agent_definition_id=agent.id,
        version=agent.current_version,
        system_prompt=payload.system_prompt,
        model_key=payload.model_key,
        model_policy=payload.model_policy,
        invocation_mode=payload.invocation_mode,
        usage_instructions=payload.usage_instructions,
        scope_types=payload.scope_types,
        input_schema=payload.input_schema,
        output_schema=payload.output_schema,
        limits=payload.limits,
        status="DRAFT",
        created_by_user_id=principal.user.id,
    )
    db.add(version)
    db.flush()
    db.add_all(
        AgentVersionTool(
            agent_definition_version_id=version.id,
            tool_key=tool.key,
            configuration=tool.configuration,
        )
        for tool in payload.tools
    )
    record_audit(
        db,
        tenant_id=agent.owner_tenant_id,
        actor_user_id=principal.user.id,
        action="agent.version.created",
        target_type="agent_definition",
        target_id=agent.id,
        details={"version": version.version},
    )
    db.commit()
    return _version_read(db, version)


@router.post("/agents/{agent_id}/versions/{version_number}/publish", response_model=AgentDefinitionVersionRead)
def publish_agent_version(
    agent_id: uuid.UUID,
    version_number: int,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> AgentDefinitionVersionRead:
    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.id == agent_id).with_for_update())
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    _require_agent_admin(db, principal, agent)
    if agent.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent is not active")
    version = db.scalar(
        select(AgentDefinitionVersion).where(
            AgentDefinitionVersion.agent_definition_id == agent.id,
            AgentDefinitionVersion.version == version_number,
        )
    )
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent version not found")
    if agent.published_version is not None and agent.published_version != version_number:
        prior = db.scalar(
            select(AgentDefinitionVersion).where(
                AgentDefinitionVersion.agent_definition_id == agent.id,
                AgentDefinitionVersion.version == agent.published_version,
            )
        )
        if prior is not None:
            prior.status = "RETIRED"
    version.status = "PUBLISHED"
    version.published_at = utcnow()
    agent.published_version = version.version
    record_audit(
        db,
        tenant_id=agent.owner_tenant_id,
        actor_user_id=principal.user.id,
        action="agent.version.published",
        target_type="agent_definition",
        target_id=agent.id,
        details={"version": version.version},
    )
    db.commit()
    return _version_read(db, version)
