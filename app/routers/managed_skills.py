import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent_models import validate_agent_model_key
from app.agent_tools import validate_executable_agent_tool_keys
from app.audit import record_audit
from app.database import get_db
from app.dependencies import Principal, can_admin_tenant, get_principal, require_root_admin
from app.models import SkillDefinition, SkillDefinitionVersion, Tenant, WorkflowSkillBinding, utcnow
from app.schemas import (
    SkillDefinitionCreate,
    SkillDefinitionCreated,
    SkillDefinitionRead,
    SkillDefinitionUpdate,
    SkillDefinitionVersionRead,
    SkillVersionCreate,
    WorkflowRoleSpecRead,
    WorkflowSkillBindingRead,
    WorkflowSkillBindingUpsert,
    WorkflowSpecRead,
)
from app.workflow_specs import (
    SUPPORTED_SKILL_CAPABILITIES,
    WORKFLOW_SPECS,
    get_workflow_spec,
    validate_skill_version_for_role,
)

router = APIRouter(prefix="/v1", tags=["managed skills"])


def _version_read(version: SkillDefinitionVersion) -> SkillDefinitionVersionRead:
    return SkillDefinitionVersionRead.model_validate(version)


def _require_skill_admin(principal: Principal, skill: SkillDefinition) -> None:
    if skill.scope == "SYSTEM":
        if not principal.is_root_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Root tenant ADMIN required")
        return
    if not can_admin_tenant(principal, skill.owner_tenant_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant ADMIN required")


def _validate_version_payload(payload: SkillVersionCreate) -> None:
    try:
        validate_agent_model_key(payload.model_key)
        validate_executable_agent_tool_keys(payload.required_tools)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    unknown_capabilities = set(payload.required_capabilities) - SUPPORTED_SKILL_CAPABILITIES
    if unknown_capabilities:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown skill capabilities: {', '.join(sorted(unknown_capabilities))}",
        )


def _new_version(
    *,
    skill: SkillDefinition,
    version_number: int,
    payload: SkillVersionCreate,
    actor_user_id: uuid.UUID,
) -> SkillDefinitionVersion:
    return SkillDefinitionVersion(
        skill_definition_id=skill.id,
        version=version_number,
        instructions=payload.instructions,
        input_schema_key=payload.input_schema_key,
        input_schema=payload.input_schema,
        output_schema_key=payload.output_schema_key,
        output_schema=payload.output_schema,
        model_key=payload.model_key,
        model_policy=payload.model_policy,
        limits=payload.limits,
        required_capabilities=payload.required_capabilities,
        required_tools=payload.required_tools,
        cache_policy=payload.cache_policy,
        evaluation_fixtures=payload.evaluation_fixtures,
        status="DRAFT",
        created_by_user_id=actor_user_id,
    )


def _create_skill(
    db: Session,
    principal: Principal,
    payload: SkillDefinitionCreate,
    *,
    owner_tenant_id: uuid.UUID,
    scope: str,
) -> SkillDefinitionCreated:
    _validate_version_payload(payload.initial_version)
    skill = SkillDefinition(
        owner_tenant_id=owner_tenant_id,
        scope=scope,
        key=payload.key,
        name=payload.name.strip(),
        description=payload.description,
        current_version=1,
        status="ACTIVE",
        created_by_user_id=principal.user.id,
    )
    db.add(skill)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Skill key already exists for tenant") from exc
    version = _new_version(
        skill=skill,
        version_number=1,
        payload=payload.initial_version,
        actor_user_id=principal.user.id,
    )
    db.add(version)
    db.flush()
    record_audit(
        db,
        tenant_id=owner_tenant_id,
        actor_user_id=principal.user.id,
        action="skill.created",
        target_type="skill_definition",
        target_id=skill.id,
        details={"scope": scope, "version": 1},
    )
    db.commit()
    return SkillDefinitionCreated(
        skill=SkillDefinitionRead.model_validate(skill),
        version=_version_read(version),
    )


@router.get("/workflow-specs", response_model=list[WorkflowSpecRead])
def list_workflow_specs(_: Principal = Depends(get_principal)) -> list[WorkflowSpecRead]:
    return [
        WorkflowSpecRead(
            key=spec.key,
            code_version=spec.code_version,
            name=spec.name,
            description=spec.description,
            roles=[
                WorkflowRoleSpecRead(
                    key=role.key,
                    input_schema_key=role.input_schema_key,
                    output_schema_key=role.output_schema_key,
                    allowed_capabilities=sorted(role.allowed_capabilities),
                    allowed_tool_keys=sorted(role.allowed_tool_keys),
                )
                for role in spec.roles
            ],
        )
        for spec in WORKFLOW_SPECS.values()
    ]


@router.get("/admin/skills", response_model=list[SkillDefinitionRead])
def list_system_skills(
    principal: Principal = Depends(require_root_admin),
    db: Session = Depends(get_db),
) -> list[SkillDefinition]:
    return list(
        db.scalars(
            select(SkillDefinition)
            .where(
                SkillDefinition.scope == "SYSTEM",
                SkillDefinition.owner_tenant_id == principal.user.tenant_id,
            )
            .order_by(SkillDefinition.name)
        )
    )


@router.post("/admin/skills", response_model=SkillDefinitionCreated, status_code=status.HTTP_201_CREATED)
def create_system_skill(
    payload: SkillDefinitionCreate,
    principal: Principal = Depends(require_root_admin),
    db: Session = Depends(get_db),
) -> SkillDefinitionCreated:
    return _create_skill(
        db,
        principal,
        payload,
        owner_tenant_id=principal.user.tenant_id,
        scope="SYSTEM",
    )


@router.get("/tenants/{tenant_id}/skills", response_model=list[SkillDefinitionRead])
def list_tenant_skills(
    tenant_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[SkillDefinition]:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if not can_admin_tenant(principal, tenant.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant ADMIN required")
    return list(
        db.scalars(
            select(SkillDefinition)
            .where(
                SkillDefinition.status != "ARCHIVED",
                or_(
                    SkillDefinition.owner_tenant_id == tenant.id,
                    SkillDefinition.scope == "SYSTEM",
                ),
            )
            .order_by(SkillDefinition.scope, SkillDefinition.name)
        )
    )


@router.post("/tenants/{tenant_id}/skills", response_model=SkillDefinitionCreated, status_code=status.HTTP_201_CREATED)
def create_tenant_skill(
    tenant_id: uuid.UUID,
    payload: SkillDefinitionCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> SkillDefinitionCreated:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if tenant.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant is not active")
    if not can_admin_tenant(principal, tenant.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant ADMIN required")
    return _create_skill(db, principal, payload, owner_tenant_id=tenant.id, scope="TENANT")


@router.get("/skills/{skill_id}", response_model=SkillDefinitionCreated)
def get_skill(
    skill_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> SkillDefinitionCreated:
    skill = db.get(SkillDefinition, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    _require_skill_admin(principal, skill)
    version = db.scalar(
        select(SkillDefinitionVersion).where(
            SkillDefinitionVersion.skill_definition_id == skill.id,
            SkillDefinitionVersion.version == skill.current_version,
        )
    )
    if version is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Skill has no current version")
    return SkillDefinitionCreated(skill=SkillDefinitionRead.model_validate(skill), version=_version_read(version))


@router.patch("/skills/{skill_id}", response_model=SkillDefinitionRead)
def update_skill(
    skill_id: uuid.UUID,
    payload: SkillDefinitionUpdate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> SkillDefinition:
    skill = db.scalar(select(SkillDefinition).where(SkillDefinition.id == skill_id).with_for_update())
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    _require_skill_admin(principal, skill)
    changes: dict[str, dict[str, str | None]] = {}
    for field_name in ("name", "description", "status"):
        if field_name not in payload.model_fields_set:
            continue
        next_value = getattr(payload, field_name)
        prior_value = getattr(skill, field_name)
        if next_value != prior_value:
            changes[field_name] = {"from": prior_value, "to": next_value}
            setattr(skill, field_name, next_value)
    if changes:
        record_audit(
            db,
            tenant_id=skill.owner_tenant_id,
            actor_user_id=principal.user.id,
            action="skill.updated",
            target_type="skill_definition",
            target_id=skill.id,
            details={"changes": changes},
        )
        db.commit()
        db.refresh(skill)
    return skill


@router.get("/skills/{skill_id}/versions", response_model=list[SkillDefinitionVersionRead])
def list_skill_versions(
    skill_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[SkillDefinitionVersionRead]:
    skill = db.get(SkillDefinition, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    _require_skill_admin(principal, skill)
    versions = list(
        db.scalars(
            select(SkillDefinitionVersion)
            .where(SkillDefinitionVersion.skill_definition_id == skill.id)
            .order_by(SkillDefinitionVersion.version.desc())
        )
    )
    return [_version_read(version) for version in versions]


@router.post("/skills/{skill_id}/versions", response_model=SkillDefinitionVersionRead, status_code=201)
def create_skill_version(
    skill_id: uuid.UUID,
    payload: SkillVersionCreate,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> SkillDefinitionVersionRead:
    skill = db.scalar(select(SkillDefinition).where(SkillDefinition.id == skill_id).with_for_update())
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    _require_skill_admin(principal, skill)
    if skill.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Skill is not active")
    _validate_version_payload(payload)
    skill.current_version += 1
    version = _new_version(
        skill=skill,
        version_number=skill.current_version,
        payload=payload,
        actor_user_id=principal.user.id,
    )
    db.add(version)
    db.flush()
    record_audit(
        db,
        tenant_id=skill.owner_tenant_id,
        actor_user_id=principal.user.id,
        action="skill.version.created",
        target_type="skill_definition",
        target_id=skill.id,
        details={"version": version.version},
    )
    db.commit()
    return _version_read(version)


@router.post("/skills/{skill_id}/versions/{version_number}/publish", response_model=SkillDefinitionVersionRead)
def publish_skill_version(
    skill_id: uuid.UUID,
    version_number: int,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> SkillDefinitionVersionRead:
    skill = db.scalar(select(SkillDefinition).where(SkillDefinition.id == skill_id).with_for_update())
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    _require_skill_admin(principal, skill)
    if skill.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Skill is not active")
    version = db.scalar(
        select(SkillDefinitionVersion).where(
            SkillDefinitionVersion.skill_definition_id == skill.id,
            SkillDefinitionVersion.version == version_number,
        )
    )
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill version not found")
    version.status = "PUBLISHED"
    version.published_at = utcnow()
    skill.published_version = version.version
    record_audit(
        db,
        tenant_id=skill.owner_tenant_id,
        actor_user_id=principal.user.id,
        action="skill.version.published",
        target_type="skill_definition",
        target_id=skill.id,
        details={"version": version.version},
    )
    db.commit()
    return _version_read(version)


def _binding_upsert(
    db: Session,
    principal: Principal,
    payload: WorkflowSkillBindingUpsert,
    *,
    workflow_key: str,
    role_key: str,
    owner_tenant_id: uuid.UUID,
    scope: str,
) -> WorkflowSkillBinding:
    try:
        get_workflow_spec(workflow_key).role(role_key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    version = db.get(SkillDefinitionVersion, payload.skill_definition_version_id)
    skill = db.get(SkillDefinition, version.skill_definition_id) if version is not None else None
    if version is None or skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill version not found")
    if scope == "SYSTEM" and skill.scope != "SYSTEM":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="System bindings require a system skill")
    if scope == "TENANT" and skill.scope != "SYSTEM" and skill.owner_tenant_id != owner_tenant_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Skill is not available to this tenant")
    try:
        validate_skill_version_for_role(skill, version, workflow_key=workflow_key, role_key=role_key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    binding = db.scalar(
        select(WorkflowSkillBinding)
        .where(
            WorkflowSkillBinding.workflow_key == workflow_key,
            WorkflowSkillBinding.role_key == role_key,
            WorkflowSkillBinding.scope == scope,
            WorkflowSkillBinding.owner_tenant_id == owner_tenant_id,
        )
        .with_for_update()
    )
    if binding is None:
        binding = WorkflowSkillBinding(
            workflow_key=workflow_key,
            role_key=role_key,
            scope=scope,
            owner_tenant_id=owner_tenant_id,
            skill_definition_id=skill.id,
            skill_definition_version_id=version.id,
            configuration=payload.configuration,
            status=payload.status,
            created_by_user_id=principal.user.id,
        )
        db.add(binding)
        action = "workflow_skill_binding.created"
    else:
        binding.skill_definition_id = skill.id
        binding.skill_definition_version_id = version.id
        binding.configuration = payload.configuration
        binding.status = payload.status
        action = "workflow_skill_binding.updated"
    db.flush()
    record_audit(
        db,
        tenant_id=owner_tenant_id,
        actor_user_id=principal.user.id,
        action=action,
        target_type="workflow_skill_binding",
        target_id=binding.id,
        details={
            "workflow_key": workflow_key,
            "role_key": role_key,
            "scope": scope,
            "skill_definition_version_id": str(version.id),
            "status": binding.status,
        },
    )
    db.commit()
    return binding


@router.get("/admin/workflow-skill-bindings", response_model=list[WorkflowSkillBindingRead])
def list_system_workflow_skill_bindings(
    principal: Principal = Depends(require_root_admin),
    db: Session = Depends(get_db),
) -> list[WorkflowSkillBinding]:
    return list(
        db.scalars(
            select(WorkflowSkillBinding)
            .where(
                WorkflowSkillBinding.scope == "SYSTEM",
                WorkflowSkillBinding.owner_tenant_id == principal.user.tenant_id,
            )
            .order_by(WorkflowSkillBinding.workflow_key, WorkflowSkillBinding.role_key)
        )
    )


@router.put(
    "/admin/workflow-skill-bindings/{workflow_key}/{role_key}",
    response_model=WorkflowSkillBindingRead,
)
def upsert_system_workflow_skill_binding(
    workflow_key: str,
    role_key: str,
    payload: WorkflowSkillBindingUpsert,
    principal: Principal = Depends(require_root_admin),
    db: Session = Depends(get_db),
) -> WorkflowSkillBinding:
    return _binding_upsert(
        db,
        principal,
        payload,
        workflow_key=workflow_key,
        role_key=role_key,
        owner_tenant_id=principal.user.tenant_id,
        scope="SYSTEM",
    )


@router.get("/tenants/{tenant_id}/workflow-skill-bindings", response_model=list[WorkflowSkillBindingRead])
def list_tenant_workflow_skill_bindings(
    tenant_id: uuid.UUID,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[WorkflowSkillBinding]:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if not can_admin_tenant(principal, tenant.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant ADMIN required")
    return list(
        db.scalars(
            select(WorkflowSkillBinding)
            .where(
                or_(
                    (
                        (WorkflowSkillBinding.scope == "TENANT")
                        & (WorkflowSkillBinding.owner_tenant_id == tenant.id)
                    ),
                    WorkflowSkillBinding.scope == "SYSTEM",
                )
            )
            .order_by(WorkflowSkillBinding.scope, WorkflowSkillBinding.workflow_key, WorkflowSkillBinding.role_key)
        )
    )


@router.put(
    "/tenants/{tenant_id}/workflow-skill-bindings/{workflow_key}/{role_key}",
    response_model=WorkflowSkillBindingRead,
)
def upsert_tenant_workflow_skill_binding(
    tenant_id: uuid.UUID,
    workflow_key: str,
    role_key: str,
    payload: WorkflowSkillBindingUpsert,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> WorkflowSkillBinding:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if tenant.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant is not active")
    if not can_admin_tenant(principal, tenant.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant ADMIN required")
    return _binding_upsert(
        db,
        principal,
        payload,
        workflow_key=workflow_key,
        role_key=role_key,
        owner_tenant_id=tenant.id,
        scope="TENANT",
    )
