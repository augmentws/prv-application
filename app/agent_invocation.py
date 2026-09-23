from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.artifact_gateway import get_collection_snapshot
from app.dependencies import Principal, can_admin_client
from app.document_cleaner import STANDARD_DOCUMENT_CLEANER_AGENT_KEY, propose_document_cleaner_rule
from app.model_execution import validate_structured_output
from app.models import AgentDefinition, AgentDefinitionVersion, Client
from app.schemas import AgentInvocationScope, DocumentCleanerProposalRequest
from artifact_service.auth import ArtifactPrincipal

AgentHandler = Callable[..., Awaitable[dict[str, Any]]]


async def _invoke_document_cleaner(
    *,
    agent: AgentDefinition,
    version: AgentDefinitionVersion,
    scope: AgentInvocationScope,
    payload: dict[str, Any],
    principal: Principal,
    artifact_principal: ArtifactPrincipal,
    db: Session,
    artifact_db: Session,
) -> dict[str, Any]:
    if scope.type != "COLLECTION":
        raise ValueError("Document Cleaner Agent requires a COLLECTION scope")
    try:
        collection = get_collection_snapshot(scope.id, artifact_principal, artifact_db)
    except PermissionError as exc:
        raise PermissionError(str(exc)) from exc
    if collection is None:
        raise LookupError("Collection not found")
    client = db.get(Client, collection.client_id)
    if client is None or client.tenant_id != collection.tenant_id:
        raise LookupError("Client not found")
    if not can_admin_client(db, principal, client):
        raise PermissionError("Client ADMIN required")
    request = DocumentCleanerProposalRequest.model_validate(payload)
    response = await propose_document_cleaner_rule(
        db,
        version=version,
        payload=request,
        tenant_id=collection.tenant_id,
        client_id=collection.client_id,
        actor_user_id=principal.user.id,
        collection_id=collection.id,
    )
    return response.model_dump(mode="json")


AGENT_HANDLERS: dict[str, AgentHandler] = {
    STANDARD_DOCUMENT_CLEANER_AGENT_KEY: _invoke_document_cleaner,
}


async def invoke_registered_agent(
    *,
    agent: AgentDefinition,
    version: AgentDefinitionVersion,
    scope: AgentInvocationScope,
    payload: dict[str, Any],
    principal: Principal,
    artifact_principal: ArtifactPrincipal,
    db: Session,
    artifact_db: Session,
) -> dict[str, Any]:
    if version.invocation_mode != "STRUCTURED":
        raise ValueError("This agent is not available through structured invocation")
    if scope.type not in version.scope_types:
        raise ValueError(f"Agent does not support {scope.type} scope")
    validate_structured_output(payload, version.input_schema)
    handler = AGENT_HANDLERS.get(agent.key)
    if handler is None:
        raise ValueError("No structured invocation handler is registered for this agent")
    try:
        output = await handler(
            agent=agent,
            version=version,
            scope=scope,
            payload=payload,
            principal=principal,
            artifact_principal=artifact_principal,
            db=db,
            artifact_db=artifact_db,
        )
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    validate_structured_output(output, version.output_schema)
    return output


def package_for(agent: AgentDefinition, version: AgentDefinitionVersion) -> dict[str, Any]:
    return {
        "id": agent.id,
        "key": agent.key,
        "name": agent.name,
        "description": agent.description,
        "scope": agent.scope,
        "version": {
            "id": version.id,
            "version": version.version,
            "invocation_mode": version.invocation_mode,
            "usage_instructions": version.usage_instructions,
            "scope_types": version.scope_types,
            "input_schema": version.input_schema,
            "output_schema": version.output_schema,
        },
    }
