import asyncio
import uuid

from pydantic_ai.models.test import TestModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bootstrap import ensure_standard_agents
from app.document_cleaner import propose_document_cleaner_rule
from app.models import AgentDefinition, AgentDefinitionVersion, AuditRecord, Client, User
from app.schemas import DocumentCleanerProposalRequest


def test_document_cleaner_returns_a_valid_advisory_rule(db: Session, root_admin: User) -> None:
    assert ensure_standard_agents(db, root_admin.tenant, root_admin) is True
    client = Client(tenant_id=root_admin.tenant_id, name="Cleaner Client", status="ACTIVE")
    db.add(client)
    db.commit()
    agent = db.scalar(select(AgentDefinition).where(AgentDefinition.key == "document_cleaner"))
    assert agent is not None
    version = db.scalar(
        select(AgentDefinitionVersion).where(
            AgentDefinitionVersion.agent_definition_id == agent.id,
            AgentDefinitionVersion.version == agent.published_version,
        )
    )
    assert version is not None
    collection_id = uuid.uuid4()
    payload = DocumentCleanerProposalRequest.model_validate(
        {
            "instruction": "Remove the repeated confidentiality footer.",
            "documents": [{
                "item_id": str(uuid.uuid4()),
                "filename": "message.txt",
                "original_text": "Useful text\nCONFIDENTIAL FOOTER",
                "normalized_text": "Useful text\nCONFIDENTIAL FOOTER",
                "changes": [],
            }],
            "current_rules": [],
            "clarification_history": [],
        }
    )
    output = {
        "status": "PROPOSAL",
        "clarifying_question": None,
        "explanation": "Matches only the exact standalone footer line.",
        "rule": {
            "id": "remove-confidential-footer",
            "name": "Remove confidentiality footer",
            "description": "Removes the recurring confidentiality footer.",
            "action": "REMOVE_LINE",
            "pattern": "^CONFIDENTIAL FOOTER$",
            "end_pattern": None,
            "replacement": "",
            "case_sensitive": False,
            "enabled": True,
        },
        "replace_rule_id": None,
    }

    response = asyncio.run(
        propose_document_cleaner_rule(
            db,
            version=version,
            payload=payload,
            tenant_id=root_admin.tenant_id,
            client_id=client.id,
            actor_user_id=root_admin.id,
            collection_id=collection_id,
            model=TestModel(custom_output_args=output),
        )
    )

    assert response.status == "PROPOSAL"
    assert response.rule is not None
    assert response.rule.pattern == "^CONFIDENTIAL FOOTER$"
    audit = db.scalar(
        select(AuditRecord).where(
            AuditRecord.action == "collection.document_cleaner_proposed",
            AuditRecord.target_id == collection_id,
        )
    )
    assert audit is not None
    assert audit.details["selected_document_count"] == 1
