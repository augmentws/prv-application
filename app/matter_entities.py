"""Matter entity store: the people and organizations that own and take part in facts.

Each ``matter_entity`` row is either a canonical entity (``parent_id`` is null) or a variant (a name and/or one
email address the entity was seen under). Merging points one canonical entity and its variants at another;
unmerging detaches a row again. Facts reference the matched row, so merges carry fact links with them. Every
change goes through this module, appends a ``matter_entity_event`` and updates the rows in the same transaction.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Custodian, Matter, MatterEntity, MatterEntityEvent, utcnow

EntityType = Literal["PERSON", "ORGANIZATION"]
ActorType = Literal["HUMAN", "AGENT", "SYSTEM"]
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MatterEntityError(ValueError):
    pass


class MatterEntityNotFound(MatterEntityError):
    pass


class MatterEntityConflict(MatterEntityError):
    pass


def normalize_entity_name(value: str) -> str:
    name = " ".join(value.split())
    if not name:
        raise MatterEntityError("Entity names cannot be blank")
    return name.casefold()


def normalize_entity_email(value: str) -> str:
    email = value.strip().lower()
    if not EMAIL_PATTERN.match(email):
        raise MatterEntityError(f"Invalid email address: {value}")
    return email


def _clean_name(value: str) -> str:
    name = " ".join(value.split())
    if not name:
        raise MatterEntityError("Entity names cannot be blank")
    return name


def _record(
    db: Session,
    entity: MatterEntity,
    event_type: str,
    payload: dict[str, Any],
    *,
    actor_type: ActorType,
    actor_user_id: uuid.UUID | None,
    workflow_run_id: uuid.UUID | None = None,
) -> MatterEntityEvent:
    event = MatterEntityEvent(
        matter_id=entity.matter_id,
        entity_id=entity.id,
        event_type=event_type,
        payload=payload,
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        workflow_run_id=workflow_run_id,
        created_at=utcnow(),
    )
    db.add(event)
    return event


def get_entity(db: Session, *, matter_id: uuid.UUID, entity_id: uuid.UUID, for_update: bool = False) -> MatterEntity:
    statement = select(MatterEntity).where(MatterEntity.id == entity_id, MatterEntity.matter_id == matter_id)
    if for_update:
        statement = statement.with_for_update()
    entity = db.scalar(statement)
    if entity is None:
        raise MatterEntityNotFound("Entity not found")
    return entity


def canonical_entity(db: Session, entity: MatterEntity, *, for_update: bool = False) -> MatterEntity:
    if entity.parent_id is None:
        return entity
    return get_entity(db, matter_id=entity.matter_id, entity_id=entity.parent_id, for_update=for_update)


def canonical_ids(db: Session, entity_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """Map any entity row ID to the ID of its canonical entity."""

    ids = list(set(entity_ids))
    if not ids:
        return {}
    rows = db.execute(select(MatterEntity.id, MatterEntity.parent_id).where(MatterEntity.id.in_(ids))).all()
    return {row_id: parent_id or row_id for row_id, parent_id in rows}


def entity_group_ids(db: Session, canonical: MatterEntity) -> list[uuid.UUID]:
    return [canonical.id, *db.scalars(select(MatterEntity.id).where(MatterEntity.parent_id == canonical.id))]


def _check_version(entity: MatterEntity, lock_version: int) -> None:
    if entity.lock_version != lock_version:
        raise MatterEntityConflict(
            f"Entity changed since it was loaded (version {entity.lock_version}, expected {lock_version})"
        )


def _bump(entity: MatterEntity) -> None:
    entity.lock_version += 1
    entity.updated_at = utcnow()


def _require_email_available(db: Session, *, matter_id: uuid.UUID, normalized_email: str) -> None:
    owner = db.scalar(
        select(MatterEntity).where(
            MatterEntity.matter_id == matter_id,
            MatterEntity.normalized_email == normalized_email,
        )
    )
    if owner is not None:
        raise MatterEntityConflict(f"{normalized_email} already belongs to another entity in this matter")


def _validated_organization(db: Session, *, matter_id: uuid.UUID, organization_id: uuid.UUID | None) -> uuid.UUID | None:
    if organization_id is None:
        return None
    organization = get_entity(db, matter_id=matter_id, entity_id=organization_id)
    if organization.parent_id is not None:
        organization = canonical_entity(db, organization)
    if organization.entity_type != "ORGANIZATION" or organization.status == "REJECTED":
        raise MatterEntityError("A person's organization must be an active organization entity")
    return organization.id


def _validated_custodian(db: Session, *, matter: Matter, custodian_id: uuid.UUID | None) -> uuid.UUID | None:
    if custodian_id is None:
        return None
    custodian = db.get(Custodian, custodian_id)
    if custodian is None or custodian.client_id != matter.client_id:
        raise MatterEntityError("Custodian was not found for this matter's client")
    return custodian.id


def create_entity(
    db: Session,
    *,
    matter: Matter,
    entity_type: EntityType,
    display_name: str,
    email: str | None = None,
    title: str | None = None,
    organization_id: uuid.UUID | None = None,
    custodian_id: uuid.UUID | None = None,
    description: str | None = None,
    status: Literal["PROPOSED", "CONFIRMED"] = "CONFIRMED",
    actor_type: ActorType = "HUMAN",
    actor_user_id: uuid.UUID | None = None,
    workflow_run_id: uuid.UUID | None = None,
) -> MatterEntity:
    if entity_type != "PERSON" and (title is not None or organization_id is not None):
        raise MatterEntityError("Only people have a title or an organization")
    name = _clean_name(display_name)
    normalized_email = normalize_entity_email(email) if email else None
    if normalized_email is not None:
        _require_email_available(db, matter_id=matter.id, normalized_email=normalized_email)
    entity = MatterEntity(
        matter_id=matter.id,
        parent_id=None,
        entity_type=entity_type,
        display_name=name,
        normalized_name=normalize_entity_name(name),
        email=email.strip() if email else None,
        normalized_email=normalized_email,
        title=" ".join(title.split()) or None if title else None,
        organization_id=_validated_organization(db, matter_id=matter.id, organization_id=organization_id),
        custodian_id=_validated_custodian(db, matter=matter, custodian_id=custodian_id),
        description=description,
        status=status,
        created_by_user_id=actor_user_id if workflow_run_id is None else None,
        created_by_workflow_run_id=workflow_run_id,
    )
    db.add(entity)
    db.flush()
    _record(
        db,
        entity,
        "CREATED",
        {
            "entity_type": entity_type,
            "display_name": entity.display_name,
            "email": entity.email,
            "title": entity.title,
            "organization_id": str(entity.organization_id) if entity.organization_id else None,
            "custodian_id": str(entity.custodian_id) if entity.custodian_id else None,
            "status": status,
        },
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        workflow_run_id=workflow_run_id,
    )
    return entity


def add_variant(
    db: Session,
    *,
    canonical: MatterEntity,
    display_name: str | None,
    email: str | None,
    actor_type: ActorType = "HUMAN",
    actor_user_id: uuid.UUID | None = None,
    workflow_run_id: uuid.UUID | None = None,
) -> MatterEntity:
    """Record another name and/or email address the canonical entity appears under."""

    if canonical.parent_id is not None:
        raise MatterEntityError("Variants can only be added to a canonical entity")
    if canonical.status == "REJECTED":
        raise MatterEntityConflict("Rejected entities cannot receive variants")
    if not display_name and not email:
        raise MatterEntityError("A variant needs a name, an email address, or both")
    name = _clean_name(display_name) if display_name else canonical.display_name
    normalized_email = normalize_entity_email(email) if email else None
    if normalized_email is not None:
        _require_email_available(db, matter_id=canonical.matter_id, normalized_email=normalized_email)
    elif normalize_entity_name(name) in {
        row.normalized_name
        for row in db.scalars(
            select(MatterEntity).where(
                (MatterEntity.id == canonical.id) | (MatterEntity.parent_id == canonical.id)
            )
        )
    }:
        raise MatterEntityConflict(f"{name} is already recorded for this entity")
    variant = MatterEntity(
        matter_id=canonical.matter_id,
        parent_id=canonical.id,
        entity_type=canonical.entity_type,
        display_name=name,
        normalized_name=normalize_entity_name(name),
        email=email.strip() if email else None,
        normalized_email=normalized_email,
        status=None,
        created_by_user_id=actor_user_id if workflow_run_id is None else None,
        created_by_workflow_run_id=workflow_run_id,
    )
    db.add(variant)
    db.flush()
    _bump(canonical)
    _record(
        db,
        canonical,
        "VARIANT_ADDED",
        {"variant_id": str(variant.id), "display_name": variant.display_name, "email": variant.email},
        actor_type=actor_type,
        actor_user_id=actor_user_id,
        workflow_run_id=workflow_run_id,
    )
    return variant


def update_entity(
    db: Session,
    *,
    matter: Matter,
    entity: MatterEntity,
    lock_version: int,
    changes: dict[str, Any],
    actor_user_id: uuid.UUID,
) -> MatterEntity:
    """Edit a canonical entity's descriptive fields. ``changes`` holds only the fields the caller set."""

    if entity.parent_id is not None:
        raise MatterEntityError("Edit the canonical entity, not a variant")
    _check_version(entity, lock_version)
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    if "display_name" in changes:
        name = _clean_name(changes["display_name"])
        before["display_name"], after["display_name"] = entity.display_name, name
        entity.display_name = name
        entity.normalized_name = normalize_entity_name(name)
    if "title" in changes:
        if entity.entity_type != "PERSON" and changes["title"]:
            raise MatterEntityError("Only people have a title")
        before["title"], after["title"] = entity.title, changes["title"]
        entity.title = changes["title"]
    if "organization_id" in changes:
        if entity.entity_type != "PERSON" and changes["organization_id"]:
            raise MatterEntityError("Only people have an organization")
        organization_id = _validated_organization(
            db, matter_id=entity.matter_id, organization_id=changes["organization_id"]
        )
        before["organization_id"] = str(entity.organization_id) if entity.organization_id else None
        after["organization_id"] = str(organization_id) if organization_id else None
        entity.organization_id = organization_id
    if "custodian_id" in changes:
        custodian_id = _validated_custodian(db, matter=matter, custodian_id=changes["custodian_id"])
        before["custodian_id"] = str(entity.custodian_id) if entity.custodian_id else None
        after["custodian_id"] = str(custodian_id) if custodian_id else None
        entity.custodian_id = custodian_id
    if "description" in changes:
        before["description"], after["description"] = entity.description, changes["description"]
        entity.description = changes["description"]
    if not after:
        raise MatterEntityError("No entity changes were supplied")
    _bump(entity)
    _record(db, entity, "UPDATED", {"before": before, "after": after}, actor_type="HUMAN", actor_user_id=actor_user_id)
    return entity


def confirm_entity(db: Session, *, entity: MatterEntity, lock_version: int, actor_user_id: uuid.UUID) -> MatterEntity:
    if entity.parent_id is not None:
        raise MatterEntityError("Confirm the canonical entity, not a variant")
    _check_version(entity, lock_version)
    if entity.status == "CONFIRMED":
        raise MatterEntityConflict("Entity is already confirmed")
    prior = entity.status
    entity.status = "CONFIRMED"
    _bump(entity)
    _record(db, entity, "CONFIRMED", {"prior_status": prior}, actor_type="HUMAN", actor_user_id=actor_user_id)
    return entity


def reject_entity(
    db: Session,
    *,
    entity: MatterEntity,
    lock_version: int,
    actor_user_id: uuid.UUID,
    reason: str | None,
) -> MatterEntity:
    """Reject a canonical entity and remove it (and its variants) from every fact that references it."""

    from app.facts import detach_entities_from_facts

    if entity.parent_id is not None:
        raise MatterEntityError("Reject the canonical entity, not a variant")
    _check_version(entity, lock_version)
    if entity.status == "REJECTED":
        raise MatterEntityConflict("Entity is already rejected")
    if db.scalar(
        select(MatterEntity.id).where(MatterEntity.organization_id == entity.id, MatterEntity.status != "REJECTED")
    ):
        raise MatterEntityConflict("Reassign the people who belong to this organization before rejecting it")
    prior = entity.status
    entity.status = "REJECTED"
    _bump(entity)
    detached = detach_entities_from_facts(
        db,
        matter_id=entity.matter_id,
        entity_ids=entity_group_ids(db, entity),
        actor_user_id=actor_user_id,
        reason=f"Entity {entity.display_name} was rejected",
    )
    _record(
        db,
        entity,
        "REJECTED",
        {"prior_status": prior, "reason": reason, "detached_fact_ids": [str(fact_id) for fact_id in detached]},
        actor_type="HUMAN",
        actor_user_id=actor_user_id,
    )
    return entity


def merge_entities(
    db: Session,
    *,
    source: MatterEntity,
    target: MatterEntity,
    source_lock_version: int,
    actor_user_id: uuid.UUID,
) -> MatterEntity:
    """Make ``source`` and its variants variants of ``target``. Facts follow automatically."""

    if source.parent_id is not None or target.parent_id is not None:
        raise MatterEntityError("Only canonical entities can be merged")
    if source.id == target.id:
        raise MatterEntityError("An entity cannot be merged into itself")
    if source.matter_id != target.matter_id:
        raise MatterEntityError("Entities must belong to the same matter")
    if source.entity_type != target.entity_type:
        raise MatterEntityError("A person and an organization cannot be merged")
    if target.status == "REJECTED":
        raise MatterEntityConflict("Entities cannot be merged into a rejected entity")
    _check_version(source, source_lock_version)
    if source.entity_type == "ORGANIZATION":
        db.execute(
            update(MatterEntity).where(MatterEntity.organization_id == source.id).values(organization_id=target.id)
        )
    moved_variant_ids = list(db.scalars(select(MatterEntity.id).where(MatterEntity.parent_id == source.id)))
    db.execute(update(MatterEntity).where(MatterEntity.parent_id == source.id).values(parent_id=target.id))
    prior = {
        "status": source.status,
        "title": source.title,
        "organization_id": str(source.organization_id) if source.organization_id else None,
        "custodian_id": str(source.custodian_id) if source.custodian_id else None,
        "moved_variant_ids": [str(value) for value in moved_variant_ids],
    }
    source.parent_id = target.id
    source.status = None
    source.title = None
    source.organization_id = None
    source.custodian_id = None
    _bump(source)
    _bump(target)
    _record(
        db,
        source,
        "MERGED",
        {"target_entity_id": str(target.id), "prior": prior},
        actor_type="HUMAN",
        actor_user_id=actor_user_id,
    )
    _record(
        db,
        target,
        "ABSORBED",
        {"source_entity_id": str(source.id), "moved_variant_ids": prior["moved_variant_ids"]},
        actor_type="HUMAN",
        actor_user_id=actor_user_id,
    )
    return target


def unmerge_entity(
    db: Session,
    *,
    variant: MatterEntity,
    lock_version: int,
    actor_user_id: uuid.UUID,
) -> MatterEntity:
    """Split a variant off as its own canonical entity; facts linked to it follow it."""

    if variant.parent_id is None:
        raise MatterEntityError("Only a variant can be split off")
    _check_version(variant, lock_version)
    parent = get_entity(db, matter_id=variant.matter_id, entity_id=variant.parent_id, for_update=True)
    variant.parent_id = None
    variant.status = "CONFIRMED"
    _bump(variant)
    _bump(parent)
    _record(
        db,
        variant,
        "UNMERGED",
        {"former_parent_id": str(parent.id)},
        actor_type="HUMAN",
        actor_user_id=actor_user_id,
    )
    _record(db, parent, "VARIANT_REMOVED", {"variant_id": str(variant.id)}, actor_type="HUMAN", actor_user_id=actor_user_id)
    return variant
