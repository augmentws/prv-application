"""Matter-specific entity relationships with centrally registered semantic rules."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import MatterEntity, MatterEntityRelationship, utcnow

RelationshipStatus = Literal["PROPOSED", "CONFIRMED", "REJECTED"]


class EntityRelationshipError(ValueError):
    pass


class EntityRelationshipNotFound(EntityRelationshipError):
    pass


class EntityRelationshipConflict(EntityRelationshipError):
    pass


@dataclass(frozen=True)
class RelationshipDefinition:
    key: str
    forward_label: str
    inverse_label: str
    source_entity_types: frozenset[str]
    source_organization_categories: frozenset[str] | None
    target_entity_types: frozenset[str]


RELATIONSHIP_DEFINITIONS = {
    "OUTSIDE_COUNSEL_FOR": RelationshipDefinition(
        key="OUTSIDE_COUNSEL_FOR",
        forward_label="Outside counsel for",
        inverse_label="Represented by outside counsel",
        source_entity_types=frozenset({"ORGANIZATION"}),
        source_organization_categories=frozenset({"LAW_FIRM"}),
        target_entity_types=frozenset({"PERSON", "ORGANIZATION"}),
    ),
}


def relationship_definition(relationship_type: str) -> RelationshipDefinition:
    definition = RELATIONSHIP_DEFINITIONS.get(relationship_type)
    if definition is None:
        raise EntityRelationshipError(f"Unsupported entity relationship type: {relationship_type}")
    return definition


def _canonical_entity(db: Session, *, matter_id: uuid.UUID, entity_id: uuid.UUID) -> MatterEntity:
    entity = db.scalar(
        select(MatterEntity).where(
            MatterEntity.id == entity_id,
            MatterEntity.matter_id == matter_id,
            MatterEntity.parent_id.is_(None),
        )
    )
    if entity is None:
        raise EntityRelationshipError("Relationship entities must be canonical entities in this matter")
    if entity.status == "REJECTED":
        raise EntityRelationshipConflict("Rejected entities cannot participate in an active relationship")
    return entity


def _validate_parties(
    definition: RelationshipDefinition,
    *,
    source: MatterEntity,
    target: MatterEntity,
    status: RelationshipStatus,
) -> None:
    if source.id == target.id:
        raise EntityRelationshipError("An entity cannot have a relationship to itself")
    if source.entity_type not in definition.source_entity_types:
        raise EntityRelationshipError(f"{definition.forward_label} requires a valid source entity type")
    if (
        definition.source_organization_categories is not None
        and source.organization_category not in definition.source_organization_categories
    ):
        raise EntityRelationshipError(f"{definition.forward_label} requires a confirmed law-firm category")
    if target.entity_type not in definition.target_entity_types:
        raise EntityRelationshipError(f"{definition.forward_label} does not support this target entity type")
    if status == "CONFIRMED" and (source.status != "CONFIRMED" or target.status != "CONFIRMED"):
        raise EntityRelationshipConflict("Confirm both entities before confirming their relationship")


def create_entity_relationship(
    db: Session,
    *,
    matter_id: uuid.UUID,
    source_entity_id: uuid.UUID,
    target_entity_id: uuid.UUID,
    relationship_type: str,
    status: RelationshipStatus,
    actor_user_id: uuid.UUID | None,
    workflow_run_id: uuid.UUID | None = None,
    confidence: float | None = None,
    rationale: str | None = None,
    effective_from: date | None = None,
    effective_to: date | None = None,
) -> MatterEntityRelationship:
    if (actor_user_id is None) == (workflow_run_id is None):
        raise EntityRelationshipError("Exactly one relationship creator is required")
    if status != "PROPOSED" and actor_user_id is None:
        raise EntityRelationshipError("A human decision is required for a terminal relationship status")
    if effective_from and effective_to and effective_to < effective_from:
        raise EntityRelationshipError("Relationship end date cannot precede its start date")
    definition = relationship_definition(relationship_type)
    source = _canonical_entity(db, matter_id=matter_id, entity_id=source_entity_id)
    target = _canonical_entity(db, matter_id=matter_id, entity_id=target_entity_id)
    _validate_parties(definition, source=source, target=target, status=status)
    active = db.scalar(
        select(MatterEntityRelationship).where(
            MatterEntityRelationship.matter_id == matter_id,
            MatterEntityRelationship.source_entity_id == source.id,
            MatterEntityRelationship.target_entity_id == target.id,
            MatterEntityRelationship.relationship_type == relationship_type,
            MatterEntityRelationship.status.in_(("PROPOSED", "CONFIRMED")),
        )
    )
    if active is not None:
        raise EntityRelationshipConflict("This active entity relationship already exists")
    now = utcnow()
    relationship = MatterEntityRelationship(
        matter_id=matter_id,
        source_entity_id=source.id,
        target_entity_id=target.id,
        relationship_type=relationship_type,
        status=status,
        confidence=confidence,
        rationale=" ".join(rationale.split()) if rationale else None,
        effective_from=effective_from,
        effective_to=effective_to,
        created_by_user_id=actor_user_id,
        created_by_workflow_run_id=workflow_run_id,
        decided_by_user_id=actor_user_id if status in {"CONFIRMED", "REJECTED"} else None,
        decided_at=now if status in {"CONFIRMED", "REJECTED"} else None,
    )
    db.add(relationship)
    db.flush()
    return relationship


def get_entity_relationship(
    db: Session,
    *,
    matter_id: uuid.UUID,
    relationship_id: uuid.UUID,
    for_update: bool = False,
) -> MatterEntityRelationship:
    statement = select(MatterEntityRelationship).where(
        MatterEntityRelationship.id == relationship_id,
        MatterEntityRelationship.matter_id == matter_id,
    )
    if for_update:
        statement = statement.with_for_update()
    relationship = db.scalar(statement)
    if relationship is None:
        raise EntityRelationshipNotFound("Entity relationship not found")
    return relationship


def decide_entity_relationship(
    db: Session,
    *,
    relationship: MatterEntityRelationship,
    decision: Literal["CONFIRMED", "REJECTED"],
    lock_version: int,
    actor_user_id: uuid.UUID,
) -> MatterEntityRelationship:
    if relationship.lock_version != lock_version:
        raise EntityRelationshipConflict("Entity relationship changed since it was loaded")
    if relationship.status == decision:
        raise EntityRelationshipConflict(f"Entity relationship is already {decision.lower()}")
    if relationship.status != "PROPOSED":
        raise EntityRelationshipConflict("Only proposed relationships can be reviewed")
    source = _canonical_entity(
        db, matter_id=relationship.matter_id, entity_id=relationship.source_entity_id
    )
    target = _canonical_entity(
        db, matter_id=relationship.matter_id, entity_id=relationship.target_entity_id
    )
    definition = relationship_definition(relationship.relationship_type)
    _validate_parties(definition, source=source, target=target, status=decision)
    relationship.status = decision
    relationship.decided_by_user_id = actor_user_id
    relationship.decided_at = utcnow()
    relationship.lock_version += 1
    return relationship


def list_entity_relationships(
    db: Session,
    *,
    matter_id: uuid.UUID,
    entity_id: uuid.UUID | None = None,
    statuses: tuple[str, ...] = ("PROPOSED", "CONFIRMED"),
) -> list[MatterEntityRelationship]:
    statement = select(MatterEntityRelationship).where(
        MatterEntityRelationship.matter_id == matter_id,
        MatterEntityRelationship.status.in_(statuses),
    )
    if entity_id is not None:
        statement = statement.where(
            or_(
                MatterEntityRelationship.source_entity_id == entity_id,
                MatterEntityRelationship.target_entity_id == entity_id,
            )
        )
    return list(db.scalars(statement.order_by(MatterEntityRelationship.created_at, MatterEntityRelationship.id)))


def reject_relationships_for_entity(
    db: Session,
    *,
    entity_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> list[uuid.UUID]:
    relationships = list(
        db.scalars(
            select(MatterEntityRelationship).where(
                or_(
                    MatterEntityRelationship.source_entity_id == entity_id,
                    MatterEntityRelationship.target_entity_id == entity_id,
                ),
                MatterEntityRelationship.status.in_(("PROPOSED", "CONFIRMED")),
            )
        )
    )
    now = utcnow()
    for relationship in relationships:
        relationship.status = "REJECTED"
        relationship.decided_by_user_id = actor_user_id
        relationship.decided_at = now
        relationship.lock_version += 1
    return [relationship.id for relationship in relationships]


def reconcile_relationships_for_merge(
    db: Session,
    *,
    source_entity_id: uuid.UUID,
    target_entity_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> dict[str, list[uuid.UUID]]:
    relationships = list(
        db.scalars(
            select(MatterEntityRelationship).where(
                or_(
                    MatterEntityRelationship.source_entity_id == source_entity_id,
                    MatterEntityRelationship.target_entity_id == source_entity_id,
                ),
                MatterEntityRelationship.status.in_(("PROPOSED", "CONFIRMED")),
            )
        )
    )
    moved: list[uuid.UUID] = []
    rejected: list[uuid.UUID] = []
    now = utcnow()
    for relationship in relationships:
        new_source_id = (
            target_entity_id if relationship.source_entity_id == source_entity_id else relationship.source_entity_id
        )
        new_target_id = (
            target_entity_id if relationship.target_entity_id == source_entity_id else relationship.target_entity_id
        )
        duplicate = db.scalar(
            select(MatterEntityRelationship.id).where(
                MatterEntityRelationship.id != relationship.id,
                MatterEntityRelationship.matter_id == relationship.matter_id,
                MatterEntityRelationship.source_entity_id == new_source_id,
                MatterEntityRelationship.target_entity_id == new_target_id,
                MatterEntityRelationship.relationship_type == relationship.relationship_type,
                MatterEntityRelationship.status.in_(("PROPOSED", "CONFIRMED")),
            )
        )
        if new_source_id == new_target_id or duplicate is not None:
            relationship.status = "REJECTED"
            relationship.decided_by_user_id = actor_user_id
            relationship.decided_at = now
            rejected.append(relationship.id)
        else:
            relationship.source_entity_id = new_source_id
            relationship.target_entity_id = new_target_id
            moved.append(relationship.id)
        relationship.lock_version += 1
    return {"moved": moved, "rejected": rejected}
