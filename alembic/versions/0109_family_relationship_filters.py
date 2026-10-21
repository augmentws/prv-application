"""Backfill filterable Parent ID and Child ID definitions.

Revision ID: 0109_family_relationship_filters
Revises: 0108_email_only_entities
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op

revision: str = "0109_family_relationship_filters"
down_revision: str = "0108_email_only_entities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROFILE_KEY = "edrm-core"
PROFILE_VERSION = 7

matter = sa.table("matter", sa.column("id", sa.Uuid()))
matter_membership = sa.table(
    "matter_membership",
    sa.column("matter_id", sa.Uuid()),
    sa.column("user_id", sa.Uuid()),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
metadata_definition = sa.table(
    "metadata_definition",
    sa.column("id", sa.Uuid()),
    sa.column("matter_id", sa.Uuid()),
    sa.column("key", sa.String()),
    sa.column("display_name", sa.String()),
    sa.column("description", sa.Text()),
    sa.column("type", sa.String()),
    sa.column("cardinality", sa.String()),
    sa.column("allowed_values", sa.JSON()),
    sa.column("value_source", sa.String()),
    sa.column("reference_target", sa.String()),
    sa.column("template_key", sa.String()),
    sa.column("template_version", sa.Integer()),
    sa.column("assertion_policy", sa.String()),
    sa.column("resolution_policy", sa.String()),
    sa.column("searchable", sa.Boolean()),
    sa.column("facetable", sa.Boolean()),
    sa.column("normalize_to_lowercase", sa.Boolean()),
    sa.column("hierarchy_separator", sa.String()),
    sa.column("reviewable", sa.Boolean()),
    sa.column("ai_assignable", sa.Boolean()),
    sa.column("status", sa.String()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
metadata_group = sa.table(
    "metadata_group",
    sa.column("id", sa.Uuid()),
    sa.column("matter_id", sa.Uuid()),
    sa.column("scope", sa.String()),
    sa.column("owner_user_id", sa.Uuid()),
    sa.column("created_by_user_id", sa.Uuid()),
    sa.column("key", sa.String()),
    sa.column("display_name", sa.String()),
    sa.column("description", sa.Text()),
    sa.column("sort_order", sa.Integer()),
    sa.column("default_table_visible", sa.Boolean()),
    sa.column("default_document_visible", sa.Boolean()),
    sa.column("status", sa.String()),
    sa.column("template_key", sa.String()),
    sa.column("template_version", sa.Integer()),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
metadata_group_field = sa.table(
    "metadata_group_field",
    sa.column("metadata_group_id", sa.Uuid()),
    sa.column("metadata_definition_id", sa.Uuid()),
    sa.column("sort_order", sa.Integer()),
)

DEFINITIONS = (
    {
        "key": "parent_id",
        "display_name": "Parent ID",
        "description": "Canonical document identifier of this document's direct parent.",
        "cardinality": "SINGLE",
    },
    {
        "key": "child_id",
        "display_name": "Child ID",
        "description": "Canonical document identifiers of this document's direct children.",
        "cardinality": "MULTIPLE",
    },
)


def _definition_values(matter_id: uuid.UUID, definition: dict[str, str], now: datetime) -> dict[str, object]:
    return {
        "id": uuid.uuid4(),
        "matter_id": matter_id,
        "key": definition["key"],
        "display_name": definition["display_name"],
        "description": definition["description"],
        "type": "TEXT",
        "cardinality": definition["cardinality"],
        "allowed_values": None,
        "value_source": "SYSTEM",
        "reference_target": None,
        "template_key": PROFILE_KEY,
        "template_version": PROFILE_VERSION,
        "assertion_policy": "IMMEDIATE",
        "resolution_policy": "EXPLICIT_ONLY",
        "searchable": True,
        "facetable": True,
        "normalize_to_lowercase": False,
        "hierarchy_separator": None,
        "reviewable": False,
        "ai_assignable": False,
        "status": "ACTIVE",
        "created_at": now,
        "updated_at": now,
    }


def _family_group(bind: sa.Connection, matter_id: uuid.UUID, now: datetime) -> uuid.UUID | None:
    row = bind.execute(
        sa.select(metadata_group.c.id, metadata_group.c.template_key).where(
            metadata_group.c.matter_id == matter_id,
            metadata_group.c.key == "family",
            metadata_group.c.scope.in_(("SYSTEM", "MATTER")),
        )
    ).first()
    if row is not None:
        values: dict[str, object] = {"status": "ACTIVE", "updated_at": now}
        if row.template_key == PROFILE_KEY:
            values["template_version"] = PROFILE_VERSION
        bind.execute(sa.update(metadata_group).where(metadata_group.c.id == row.id).values(**values))
        return row.id

    created_by_user_id = bind.execute(
        sa.select(matter_membership.c.user_id)
        .where(matter_membership.c.matter_id == matter_id)
        .order_by(matter_membership.c.created_at, matter_membership.c.user_id)
        .limit(1)
    ).scalar_one_or_none()
    if created_by_user_id is None:
        return None

    group_id = uuid.uuid4()
    bind.execute(
        sa.insert(metadata_group).values(
            id=group_id,
            matter_id=matter_id,
            scope="SYSTEM",
            owner_user_id=None,
            created_by_user_id=created_by_user_id,
            key="family",
            display_name="Family",
            description="Document family relationship.",
            sort_order=20,
            default_table_visible=True,
            default_document_visible=True,
            status="ACTIVE",
            template_key=PROFILE_KEY,
            template_version=PROFILE_VERSION,
            created_at=now,
            updated_at=now,
        )
    )
    return group_id


def _order_family_group(bind: sa.Connection, group_id: uuid.UUID, definitions_by_key: dict[str, uuid.UUID]) -> None:
    memberships = list(
        bind.execute(
            sa.select(
                metadata_group_field.c.metadata_definition_id,
                metadata_group_field.c.sort_order,
            )
            .where(metadata_group_field.c.metadata_group_id == group_id)
            .order_by(metadata_group_field.c.sort_order, metadata_group_field.c.metadata_definition_id)
        )
    )
    existing_ids = {row.metadata_definition_id for row in memberships}
    core_ids = [
        definitions_by_key[key]
        for key in ("family_id", "parent_id", "child_id")
        if key in definitions_by_key
    ]
    remaining_ids = [row.metadata_definition_id for row in memberships if row.metadata_definition_id not in core_ids]

    for index, row in enumerate(memberships, start=1):
        bind.execute(
            sa.update(metadata_group_field)
            .where(
                metadata_group_field.c.metadata_group_id == group_id,
                metadata_group_field.c.metadata_definition_id == row.metadata_definition_id,
            )
            .values(sort_order=-(index * 10))
        )
    for index, definition_id in enumerate(core_ids, start=1):
        if definition_id not in existing_ids:
            bind.execute(
                sa.insert(metadata_group_field).values(
                    metadata_group_id=group_id,
                    metadata_definition_id=definition_id,
                    sort_order=-(1000 + index * 10),
                )
            )
    for index, definition_id in enumerate([*core_ids, *remaining_ids], start=1):
        bind.execute(
            sa.update(metadata_group_field)
            .where(
                metadata_group_field.c.metadata_group_id == group_id,
                metadata_group_field.c.metadata_definition_id == definition_id,
            )
            .values(sort_order=index * 10)
        )


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    for matter_id in bind.execute(sa.select(matter.c.id)).scalars():
        existing = {
            row.key: row
            for row in bind.execute(
                sa.select(
                    metadata_definition.c.id,
                    metadata_definition.c.key,
                    metadata_definition.c.template_key,
                    metadata_definition.c.template_version,
                ).where(
                    metadata_definition.c.matter_id == matter_id,
                    metadata_definition.c.key.in_(("parent_id", "child_id")),
                )
            )
        }
        for definition in DEFINITIONS:
            row = existing.get(definition["key"])
            if row is None:
                bind.execute(sa.insert(metadata_definition).values(**_definition_values(matter_id, definition, now)))
            elif row.template_key == PROFILE_KEY and row.template_version == 6:
                bind.execute(
                    sa.update(metadata_definition)
                    .where(metadata_definition.c.id == row.id)
                    .values(
                        searchable=True,
                        facetable=True,
                        status="ACTIVE",
                        template_version=PROFILE_VERSION,
                        updated_at=now,
                    )
                )

        compatible_definitions = {
            row.key: row.id
            for row in bind.execute(
                sa.select(
                    metadata_definition.c.id,
                    metadata_definition.c.key,
                ).where(
                    metadata_definition.c.matter_id == matter_id,
                    metadata_definition.c.key.in_(("family_id", "parent_id", "child_id")),
                    metadata_definition.c.template_key == PROFILE_KEY,
                )
            )
        }
        group_id = _family_group(bind, matter_id, now)
        if group_id is not None:
            _order_family_group(bind, group_id, compatible_definitions)


def downgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    bind.execute(
        sa.update(metadata_definition)
        .where(
            metadata_definition.c.key.in_(("parent_id", "child_id")),
            metadata_definition.c.template_key == PROFILE_KEY,
            metadata_definition.c.template_version == PROFILE_VERSION,
        )
        .values(facetable=False, template_version=6, updated_at=now)
    )
    bind.execute(
        sa.update(metadata_group)
        .where(
            metadata_group.c.key == "family",
            metadata_group.c.template_key == PROFILE_KEY,
            metadata_group.c.template_version == PROFILE_VERSION,
        )
        .values(template_version=6, updated_at=now)
    )
