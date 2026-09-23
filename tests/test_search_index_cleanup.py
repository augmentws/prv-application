import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Client,
    Matter,
    MatterDefinition,
    MatterDefinitionAssessmentRun,
    MatterDefinitionRevision,
    SearchIndexGeneration,
    Tenant,
    User,
    WorkflowRun,
)
from app.search.cleanup import (
    SearchIndexCleanupSafetyError,
    execute_search_index_cleanup,
    plan_search_index_cleanup,
)


class CleanupIndexClient:
    def __init__(self, indexes: set[str], aliases: dict[str, set[str]]) -> None:
        self.indexes = indexes
        self.aliases = aliases
        self.deleted: list[str] = []

    def resolve_indices(self, pattern: str) -> list[str]:
        prefix = pattern.removesuffix("*")
        return sorted(index for index in self.indexes if index.startswith(prefix))

    def alias_indices(self, alias: str) -> list[str]:
        return sorted(self.aliases.get(alias, set()))

    def delete_index(self, name: str) -> None:
        self.deleted.append(name)
        self.indexes.discard(name)


def _matter_with_generations(db: Session) -> tuple[Matter, SearchIndexGeneration, SearchIndexGeneration]:
    tenant = Tenant(slug=f"cleanup-{uuid.uuid4().hex}", name="Cleanup Tenant", status="ACTIVE", is_root=True)
    db.add(tenant)
    db.flush()
    client = Client(tenant_id=tenant.id, name="Cleanup Client", status="ACTIVE")
    db.add(client)
    db.flush()
    matter = Matter(client_id=client.id, name="Cleanup Matter", status="ACTIVE")
    db.add(matter)
    db.flush()
    alias = f"test-matter-{matter.id.hex}-documents"
    stale = SearchIndexGeneration(
        matter_id=matter.id,
        generation=1,
        index_name=f"{alias}-v000001",
        alias_name=alias,
        schema_hash="a" * 64,
        schema_snapshot={},
        status="RETIRED",
        document_count=10,
    )
    active = SearchIndexGeneration(
        matter_id=matter.id,
        generation=2,
        index_name=f"{alias}-v000002",
        alias_name=alias,
        schema_hash="b" * 64,
        schema_snapshot={},
        status="ACTIVE",
        document_count=10,
    )
    db.add_all([stale, active])
    db.commit()
    return matter, stale, active


def test_cleanup_plan_preserves_active_and_finds_orphan(db: Session) -> None:
    matter, stale, active = _matter_with_generations(db)
    orphan = f"{active.alias_name}-v000003"
    client = CleanupIndexClient(
        {stale.index_name, active.index_name, orphan},
        {active.alias_name: {active.index_name}},
    )

    plan = plan_search_index_cleanup(db, client, matter.id)  # type: ignore[arg-type]

    assert plan.active_index == active.index_name
    assert plan.delete_indexes == (stale.index_name, orphan)
    assert plan.orphan_indexes == (orphan,)
    assert plan.missing_physical_indexes == ()


def test_cleanup_refuses_alias_mismatch(db: Session) -> None:
    matter, stale, active = _matter_with_generations(db)
    client = CleanupIndexClient(
        {stale.index_name, active.index_name},
        {active.alias_name: {stale.index_name, active.index_name}},
    )

    with pytest.raises(SearchIndexCleanupSafetyError, match="expected only"):
        plan_search_index_cleanup(db, client, matter.id)  # type: ignore[arg-type]

    assert client.deleted == []


def test_execute_cleanup_deletes_physical_indexes_then_prunes_records(db: Session) -> None:
    matter, stale, active = _matter_with_generations(db)
    orphan = f"{active.alias_name}-v000003"
    client = CleanupIndexClient(
        {stale.index_name, active.index_name, orphan},
        {active.alias_name: {active.index_name}},
    )

    plan = execute_search_index_cleanup(db, client, matter.id)  # type: ignore[arg-type]

    assert client.deleted == [stale.index_name, orphan]
    assert plan.delete_indexes == (stale.index_name, orphan)
    remaining = list(
        db.scalars(select(SearchIndexGeneration).where(SearchIndexGeneration.matter_id == matter.id))
    )
    assert [generation.id for generation in remaining] == [active.id]


def test_cleanup_preserves_generation_pinned_by_active_assessment(db: Session) -> None:
    matter, stale, active = _matter_with_generations(db)
    tenant = db.get(Tenant, matter.client.tenant_id)
    assert tenant is not None
    email = f"cleanup-{uuid.uuid4().hex}@example.com"
    user = User(
        tenant_id=tenant.id,
        email=email,
        normalized_email=email,
        display_name="Cleanup Admin",
        status="ACTIVE",
        tenant_role="ADMIN",
        is_superuser=True,
    )
    db.add(user)
    db.flush()
    definition = MatterDefinition(
        matter_id=matter.id,
        current_revision=1,
        created_by_user_id=user.id,
    )
    db.add(definition)
    db.flush()
    revision = MatterDefinitionRevision(
        matter_definition_id=definition.id,
        revision=1,
        content_markdown="Issue 1",
        source_kind="PASTE",
        created_by_user_id=user.id,
    )
    workflow = WorkflowRun(
        tenant_id=tenant.id,
        client_id=matter.client_id,
        matter_id=matter.id,
        workflow_key="matter_definition_assessment_v1",
        code_version="1",
        dbos_workflow_id=f"cleanup-assessment:{uuid.uuid4()}",
        status="QUEUED",
        input_snapshot={},
        binding_snapshot={},
        configuration_snapshot={},
        progress={},
        initiated_by_user_id=user.id,
    )
    db.add_all([revision, workflow])
    db.flush()
    db.add(
        MatterDefinitionAssessmentRun(
            name="Cleanup assessment",
            matter_id=matter.id,
            matter_definition_revision_id=revision.id,
            definition_content_hash="c" * 64,
            workflow_run_id=workflow.id,
            search_index_generation_id=stale.id,
            requested_document_count=500,
            status="QUEUED",
            initiated_by_user_id=user.id,
        )
    )
    db.commit()
    orphan = f"{active.alias_name}-v000003"
    client = CleanupIndexClient(
        {stale.index_name, active.index_name, orphan},
        {active.alias_name: {active.index_name}},
    )

    plan = execute_search_index_cleanup(db, client, matter.id)  # type: ignore[arg-type]

    assert plan.preserved_assessment_indexes == (stale.index_name,)
    assert client.deleted == [orphan]
    assert db.get(SearchIndexGeneration, stale.id) is not None
