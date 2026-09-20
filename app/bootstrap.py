from sqlalchemy import select

from app.agent_workflows import STANDARD_MATTER_DEFINITION_AGENT_KEY
from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    AgentDefinition,
    AgentDefinitionVersion,
    AgentVersionTool,
    PasswordCredential,
    Tenant,
    User,
    utcnow,
)
from app.schemas import normalize_email
from app.security import hash_password
from app.standard_skills import ensure_standard_assessment_skills

STANDARD_MATTER_DEFINITION_AGENT_PROMPT = """You are the Matter Definition Setup agent. Help matter
administrators turn source reviewer guidance into precise Markdown instructions for both human and agentic
reviewers. Read the current Matter Definition and editable metadata before making recommendations. Identify
referenced coding fields, compare them with existing definitions and enum values, call out missing or ambiguous
configuration, and improve definitions, examples, edge cases, and decision rules. For a missing coding field or
an enum change, explain the exact proposal and use the corresponding approval-gated metadata tool. Ask focused
questions when the guidance is incomplete. Validate the resulting guidance, and only request the approval-gated
draft-edit tool when you are proposing a complete replacement draft. Never publish a revision and never claim a
metadata, enum, or draft change was applied unless its tool succeeded."""
STANDARD_MATTER_DEFINITION_AGENT_TOOLS = (
    "matter_definition.read",
    "matter_metadata.list_editable",
    "matter_metadata.compare",
    "matter_definition.validate",
    "matter_metadata.create_definition",
    "matter_metadata.update_definition",
    "matter_metadata.enum.add",
    "matter_metadata.enum.update",
    "matter_metadata.enum.deactivate",
    "matter_definition.apply_draft_edit",
    "matter_definition.start_assessment",
)


def ensure_standard_agents(db, root: Tenant, actor: User) -> bool:
    existing = db.scalar(
        select(AgentDefinition).where(
            AgentDefinition.owner_tenant_id == root.id,
            AgentDefinition.key == STANDARD_MATTER_DEFINITION_AGENT_KEY,
        )
    )
    if existing is not None:
        return False
    agent = AgentDefinition(
        owner_tenant_id=root.id,
        scope="SYSTEM",
        key=STANDARD_MATTER_DEFINITION_AGENT_KEY,
        name="Matter Definition Setup",
        description="Reconciles reviewer guidance with matter coding fields and proposes clearer instructions.",
        current_version=1,
        published_version=1,
        status="ACTIVE",
        created_by_user_id=actor.id,
    )
    db.add(agent)
    db.flush()
    version = AgentDefinitionVersion(
        agent_definition_id=agent.id,
        version=1,
        system_prompt=STANDARD_MATTER_DEFINITION_AGENT_PROMPT,
        model_key="configured-default",
        model_policy={"temperature": 0},
        output_schema={"type": "string"},
        limits={"max_requests": 30, "max_tool_calls": 20},
        status="PUBLISHED",
        created_by_user_id=actor.id,
        published_at=utcnow(),
    )
    db.add(version)
    db.flush()
    db.add_all(
        AgentVersionTool(agent_definition_version_id=version.id, tool_key=tool_key, configuration={})
        for tool_key in STANDARD_MATTER_DEFINITION_AGENT_TOOLS
    )
    return True


def bootstrap_root() -> tuple[Tenant, User, bool]:
    settings = get_settings()
    if not settings.bootstrap_superuser_email or not settings.bootstrap_superuser_password:
        raise RuntimeError("BOOTSTRAP_SUPERUSER_EMAIL and BOOTSTRAP_SUPERUSER_PASSWORD are required")

    with SessionLocal() as db:
        existing_root = db.scalar(select(Tenant).where(Tenant.is_root.is_(True)))
        if existing_root is not None:
            existing_user = db.scalar(
                select(User).where(
                    User.tenant_id == existing_root.id,
                    User.normalized_email == normalize_email(settings.bootstrap_superuser_email),
                )
            )
            if existing_user is None:
                raise RuntimeError("Root tenant already exists with a different bootstrap administrator")
            if ensure_standard_agents(db, existing_root, existing_user):
                db.flush()
            if ensure_standard_assessment_skills(db, existing_root, existing_user):
                db.flush()
            db.commit()
            return existing_root, existing_user, False

        root = Tenant(
            parent_tenant_id=None,
            slug=settings.bootstrap_root_tenant_slug,
            name=settings.bootstrap_root_tenant_name,
            status="ACTIVE",
            is_root=True,
        )
        db.add(root)
        db.flush()
        user = User(
            tenant_id=root.id,
            email=settings.bootstrap_superuser_email,
            normalized_email=normalize_email(settings.bootstrap_superuser_email),
            display_name=settings.bootstrap_superuser_display_name,
            status="ACTIVE",
            tenant_role="ADMIN",
            is_superuser=True,
        )
        db.add(user)
        db.flush()
        db.add(
            PasswordCredential(
                user_id=user.id,
                password_hash=hash_password(settings.bootstrap_superuser_password),
            )
        )
        ensure_standard_agents(db, root, user)
        ensure_standard_assessment_skills(db, root, user)
        db.commit()
        return root, user, True


def main() -> None:
    root, user, created = bootstrap_root()
    action = "created" if created else "already exists"
    print(f"Root tenant and super administrator {action}: tenant={root.slug} user={user.email}")


if __name__ == "__main__":
    main()
