import importlib.util
from pathlib import Path
from types import ModuleType

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.metadata_profiles import instantiate_default_configuration
from app.models import (
    Client,
    Matter,
    MatterMembership,
    MetadataDefinition,
    MetadataGroup,
    MetadataGroupField,
    User,
)


def load_migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0109_family_relationship_filters.py"
    spec = importlib.util.spec_from_file_location("migration_0109_family_relationship_filters", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_backfills_existing_matter_family_filters(
    db: Session,
    root_admin: User,
    monkeypatch,
) -> None:
    client = Client(tenant_id=root_admin.tenant_id, name="Legacy Client", status="ACTIVE")
    db.add(client)
    db.flush()
    matter = Matter(client_id=client.id, name="Legacy Matter", status="ACTIVE")
    db.add(matter)
    db.flush()
    db.add(MatterMembership(matter_id=matter.id, user_id=root_admin.id, role="ADMIN"))

    configuration = instantiate_default_configuration(matter.id, root_admin.id)
    parent = next(definition for definition in configuration.definitions if definition.key == "parent_id")
    child = next(definition for definition in configuration.definitions if definition.key == "child_id")
    parent.facetable = False
    parent.template_version = 6
    family_group = next(group for group in configuration.groups if group.key == "family")
    family_group.template_version = 6
    db.add_all(definition for definition in configuration.definitions if definition.id != child.id)
    db.add_all(configuration.groups)
    db.add_all(
        field
        for field in configuration.group_fields
        if field.metadata_definition_id != child.id
    )
    db.flush()

    migration = load_migration()
    monkeypatch.setattr(migration.op, "get_bind", db.connection)
    migration.upgrade()
    db.expire_all()

    definitions = {
        definition.key: definition
        for definition in db.scalars(
            select(MetadataDefinition).where(
                MetadataDefinition.matter_id == matter.id,
                MetadataDefinition.key.in_(("family_id", "parent_id", "child_id")),
            )
        )
    }
    assert set(definitions) == {"family_id", "parent_id", "child_id"}
    assert definitions["parent_id"].facetable is True
    assert definitions["child_id"].facetable is True
    assert definitions["parent_id"].template_version == 7
    assert definitions["child_id"].template_version == 7

    upgraded_group = db.scalar(
        select(MetadataGroup).where(
            MetadataGroup.matter_id == matter.id,
            MetadataGroup.key == "family",
        )
    )
    assert upgraded_group is not None
    assert upgraded_group.template_version == 7
    ordered_keys = list(
        db.scalars(
            select(MetadataDefinition.key)
            .join(
                MetadataGroupField,
                MetadataGroupField.metadata_definition_id == MetadataDefinition.id,
            )
            .where(MetadataGroupField.metadata_group_id == upgraded_group.id)
            .order_by(MetadataGroupField.sort_order)
        )
    )
    assert ordered_keys == ["family_id", "parent_id", "child_id"]
